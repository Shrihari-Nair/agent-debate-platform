"""Turn builder used by debater agents.

A debater's turn now runs a 5-step pipeline per turn:

  1. Memory retrieval   — fetch semantically relevant past episodes from
                          `DebateMemory` to enrich prompts (Feature 2).
  2. Planner            — LangGraph observe→plan graph produces a
                          `DebateStrategy` (Feature 3).
  3. Core argument +    — two-phase Gemini (ground → schema) *with strategy-
     fact-check           enhanced prompts*, and opponent claim fact-check,
                          run concurrently (existing pipeline, preserved).
  4. Reflection         — LangGraph SELF-REFINE loop optionally revises the
                          argument up to 2 times (Feature 1).
  5. Memory store       — persist this turn (and opponent turn) in
                          `DebateMemory` (Feature 2).

All new features degrade gracefully: any exception causes the feature to be
skipped and the original argument/pipeline behaviour is preserved.

The judge never fact-checks. It only aggregates the `FactCheck` objects that
debaters bring back, then at end-of-phase disqualifies any debater whose
opponent's check returned CONTRADICTED with high confidence.
"""

from __future__ import annotations

import asyncio
import logging

from .fact_checker import fact_check_claim
from .gemini_client import format_sources_block, grounded_generate, structured_generate
from .memory import DebateMemory
from .planner import run_planner
from .prompts import (
    compose_argument_prompt,
    format_fact_check_callout,
    ground_topic_prompt,
    verify_claims_prompt,
)
from .reflection import reflect_on_argument
from .schemas import Argument, DebateStrategy, FactCheck, Source, TranscriptEntry, VerifiedClaims

logger = logging.getLogger(__name__)


def _render_transcript(transcript: list[TranscriptEntry]) -> str:
    if not transcript:
        return "(no turns yet)"
    lines = []
    for entry in transcript:
        lines.append(f"[{entry.phase}] {entry.name} ({entry.slug}): {entry.text}")
    return "\n\n".join(lines)


def _pick_opponent_claim(
    transcript: list[TranscriptEntry],
    my_slug: str,
) -> tuple[str, str] | None:
    """Pick ONE opponent claim to fact-check this turn.

    Strategy: scan the transcript from most recent to oldest; for each entry
    from a different slug, return its first available key_claim (fresher >
    older). Returns `(claim_text, target_slug)` or None if nothing suitable.
    """
    for entry in reversed(transcript):
        if entry.slug == my_slug:
            continue
        for c in entry.key_claims:
            if c and c.strip():
                return c.strip(), entry.slug
    return None


def _build_grounding_prompt(
    topic: str,
    stance: str,
    phase: str,
    last_opponent_text: str | None,
    strategy: DebateStrategy | None,
) -> str:
    """Build Phase-1 search prompt, optionally enriched with planner keywords."""
    prompt = ground_topic_prompt(topic, stance, phase, last_opponent_text)
    if strategy and strategy.evidence_keywords:
        kw = ", ".join(strategy.evidence_keywords)
        prompt += (
            f"\n\nFocus your web search on these strategy-derived keywords: {kw}. "
            f"Prioritise sources that address these terms directly."
        )
    return prompt


def _build_composition_prompt(
    topic: str,
    stance: str,
    phase: str,
    transcript: list[TranscriptEntry],
    last_opponent_text: str | None,
    evidence: str,
    sources: list[Source],
    strategy: DebateStrategy | None,
) -> str:
    """Build Phase-2 argument composition prompt, optionally guided by strategy."""
    prompt = compose_argument_prompt(
        topic=topic,
        stance=stance,
        phase=phase,
        transcript_text=_render_transcript(transcript),
        last_opponent=last_opponent_text,
        evidence=evidence or "(no evidence retrieved)",
        sources_block=format_sources_block(sources),
    )
    if strategy:
        if strategy.primary_attack:
            prompt += (
                f"\n\nPrimary target: attack THIS specific opponent claim — "
                f"{strategy.primary_attack}"
            )
        if strategy.claims_to_make:
            claims = "; ".join(strategy.claims_to_make)
            prompt += f"\n\nClaims to establish this turn: {claims}"
        prompt += (
            f"\n\nRhetorical angle for this turn: {strategy.rhetorical_angle}. "
            f"Let this shape your tone and structure."
        )
    return prompt


async def _verify_claims_against_evidence(
    key_claims: list[str],
    evidence: str,
    argument_text: str,
) -> tuple[list[str], list[str]]:
    """Post-Phase-2 grounding check: filter out claims not in the evidence.

    Returns (verified_claims, removed_claims).  Any claim whose specific
    numbers / dates / entities cannot be traced to the Phase-1 evidence text
    is moved to `removed` and never handed to the judge.
    """
    if not key_claims:
        return [], []
    if not evidence or evidence.strip() == "(no evidence retrieved)":
        logger.warning("verify_claims: no evidence to check against, returning claims as-is")
        return key_claims, []

    logger.info(
        "verify_claims: auditing %d claims against evidence (%d chars)",
        len(key_claims), len(evidence),
    )
    try:
        result: VerifiedClaims = await structured_generate(
            verify_claims_prompt(key_claims, evidence, argument_text),
            schema=VerifiedClaims,
            temperature=0.0,  # deterministic auditing
        )
        if result.removed:
            logger.warning(
                "verify_claims: REMOVED %d hallucinated/ungrounded claim(s): %s",
                len(result.removed),
                result.removed,
            )
        logger.info(
            "verify_claims: kept %d/%d claims after grounding audit",
            len(result.verified), len(key_claims),
        )
        return result.verified, result.removed
    except Exception as exc:
        logger.warning("verify_claims: audit call failed, returning all claims unfiltered: %s", exc)
        return key_claims, []


async def _generate_argument_core(
    *,
    topic: str,
    stance: str,
    phase: str,
    transcript: list[TranscriptEntry],
    last_opponent_text: str | None,
    strategy: DebateStrategy | None = None,
) -> tuple[Argument, list[Source], str]:
    """Returns (argument, sources, evidence_text).

    evidence_text is kept for the reflection critique and claim verification.
    """
    logger.info(
        "core[%s/%s]: → phase-1 grounding  keywords=%s",
        phase, stance[:30],
        strategy.evidence_keywords if strategy else "(no strategy)",
    )
    evidence, sources = await grounded_generate(
        _build_grounding_prompt(topic, stance, phase, last_opponent_text, strategy),
        temperature=0.2,
    )
    logger.info(
        "core[%s/%s]: → phase-2 composition  angle=%s  attack=%r",
        phase, stance[:30],
        strategy.rhetorical_angle if strategy else "none",
        (strategy.primary_attack or "none")[:60] if strategy else "none",
    )
    argument = await structured_generate(
        _build_composition_prompt(
            topic=topic,
            stance=stance,
            phase=phase,
            transcript=transcript,
            last_opponent_text=last_opponent_text,
            evidence=evidence,
            sources=sources,
            strategy=strategy,
        ),
        schema=Argument,
        temperature=0.6,
    )
    logger.info(
        "core[%s/%s]: ✓ argument composed  chars=%d  claims=%d (pre-verification)",
        phase, stance[:30], len(argument.text), len(argument.key_claims),
    )

    # ── Grounding verification: remove claims not traceable to the evidence ──
    verified_claims, removed_claims = await _verify_claims_against_evidence(
        argument.key_claims, evidence, argument.text
    )
    if removed_claims:
        # Rebuild Argument with only verified claims
        argument = Argument(text=argument.text, key_claims=verified_claims)
        logger.info(
            "core[%s/%s]: ✓ verification done  claims=%d→%d  removed=%d",
            phase, stance[:30],
            len(verified_claims) + len(removed_claims),
            len(verified_claims),
            len(removed_claims),
        )
    else:
        logger.info(
            "core[%s/%s]: ✓ verification done  all %d claims grounded",
            phase, stance[:30], len(argument.key_claims),
        )

    return argument, sources, evidence


async def build_turn(
    *,
    topic: str,
    stance: str,
    phase: str,
    my_slug: str,
    transcript: list[TranscriptEntry],
    last_opponent_text: str | None,
    allow_fact_check: bool,
    memory: DebateMemory | None = None,
) -> tuple[str, Argument, list[Source], FactCheck | None, str | None]:
    """Compose one debater's turn end to end.

    Pipeline (all new features degrade gracefully on error):
      1. Memory retrieval  → context injected into planner
      2. Planner           → DebateStrategy for enriched prompts
      3. Core argument + fact-check (concurrent, existing pipeline)
      4. Reflection        → SELF-REFINE loop (skipped for opening)
      5. Memory store      → persist this turn + opponent turn

    Returns:
      spoken_text    — the full utterance to send to TTS (argument + optional callout)
      argument       — the structured Argument (text + key_claims)
      citations      — grounding sources for the argument
      fact_check     — the debater's verdict on an opponent claim (or None)
      target_slug    — which opponent's claim was checked (or None)
    """
    # ── Step 1: Memory retrieval ─────────────────────────────────────────────
    logger.info("[1/5] build_turn[%s/%s]: starting — memory retrieval", my_slug, phase)
    memory_context: list[str] = []
    if memory is not None:
        try:
            query = f"{phase} {last_opponent_text or ''}"
            memory_context = memory.retrieve(query)
            logger.info(
                "[1/5] build_turn[%s/%s]: memory retrieved %d episodes",
                my_slug, phase, len(memory_context),
            )
        except Exception as exc:
            logger.warning("[1/5] build_turn[%s/%s]: memory retrieval failed: %s", my_slug, phase, exc)
    else:
        logger.debug("[1/5] build_turn[%s/%s]: no memory instance — skipping retrieval", my_slug, phase)

    # ── Step 2: Planner — produce DebateStrategy ─────────────────────────────
    logger.info("[2/5] build_turn[%s/%s]: running planner", my_slug, phase)
    strategy: DebateStrategy | None = None
    try:
        strategy = await run_planner(
            topic=topic,
            stance=stance,
            phase=phase,
            my_slug=my_slug,
            transcript=transcript,
            last_opponent_text=last_opponent_text,
            memory_context=memory_context,
        )
        logger.info(
            "[2/5] build_turn[%s/%s]: planner ✓ angle=%s  keywords=%s  attack=%r",
            my_slug, phase,
            strategy.rhetorical_angle,
            strategy.evidence_keywords,
            (strategy.primary_attack or "none")[:60],
        )
    except Exception as exc:
        logger.warning("[2/5] build_turn[%s/%s]: planner failed, no strategy: %s", my_slug, phase, exc)

    # ── Step 3: Core argument + optional fact-check (concurrent) ────────────
    logger.info("[3/5] build_turn[%s/%s]: launching core generation (concurrent)", my_slug, phase)
    target: tuple[str, str] | None = None
    if allow_fact_check:
        target = _pick_opponent_claim(transcript, my_slug)
        if target:
            logger.info(
                "[3/5] build_turn[%s/%s]: fact-check target claim=%r from slug=%s",
                my_slug, phase, target[0][:80], target[1],
            )
        else:
            logger.info("[3/5] build_turn[%s/%s]: no opponent claim found to fact-check", my_slug, phase)

    argument_task = asyncio.create_task(
        _generate_argument_core(
            topic=topic,
            stance=stance,
            phase=phase,
            transcript=transcript,
            last_opponent_text=last_opponent_text,
            strategy=strategy,
        )
    )
    check_task: asyncio.Task[FactCheck] | None = None
    if target is not None:
        claim_text, _target_slug = target
        check_task = asyncio.create_task(fact_check_claim(claim_text))

    argument, arg_sources, evidence = await argument_task
    logger.info(
        "[3/5] build_turn[%s/%s]: core argument ✓  chars=%d  claims=%d  sources=%d",
        my_slug, phase, len(argument.text), len(argument.key_claims), len(arg_sources),
    )
    fact_check: FactCheck | None = None
    target_slug: str | None = None
    if check_task is not None and target is not None:
        try:
            fact_check = await check_task
            target_slug = target[1]
            logger.info(
                "[3/5] build_turn[%s/%s]: fact-check ✓  verdict=%s  confidence=%.2f  target=%s",
                my_slug, phase, fact_check.verdict, fact_check.confidence, target_slug,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[3/5] build_turn[%s/%s]: fact_check side-task failed: %s", my_slug, phase, exc)

    # ── Step 4: Reflection — SELF-REFINE loop ───────────────────────────────
    logger.info("[4/5] build_turn[%s/%s]: running reflection loop", my_slug, phase)
    try:
        argument_before = argument.text
        argument = await reflect_on_argument(
            argument,
            phase=phase,
            stance=stance,
            last_opponent_text=last_opponent_text,
            evidence=evidence,
        )
        if argument.text != argument_before:
            logger.info(
                "[4/5] build_turn[%s/%s]: reflection ✓ argument was revised  chars=%d→%d",
                my_slug, phase, len(argument_before), len(argument.text),
            )
        else:
            logger.info("[4/5] build_turn[%s/%s]: reflection ✓ no revision needed", my_slug, phase)
    except Exception as exc:
        logger.warning("[4/5] build_turn[%s/%s]: reflection failed, using un-revised argument: %s", my_slug, phase, exc)

    # ── Step 5: Memory store ─────────────────────────────────────────────────
    logger.info("[5/5] build_turn[%s/%s]: storing turn to memory", my_slug, phase)
    if memory is not None:
        try:
            # Store this turn
            memory.store(
                TranscriptEntry(
                    slug=my_slug,
                    name=my_slug,
                    phase=phase,
                    text=argument.text,
                    key_claims=argument.key_claims,
                )
            )
            logger.info("[5/5] build_turn[%s/%s]: stored MY turn in memory", my_slug, phase)
            # Store opponent turn so it's retrievable in future turns
            if last_opponent_text:
                memory.store(
                    TranscriptEntry(
                        slug="opponent",
                        name="opponent",
                        phase=phase,
                        text=last_opponent_text,
                        key_claims=[],
                    )
                )
                logger.info("[5/5] build_turn[%s/%s]: stored OPPONENT turn in memory", my_slug, phase)
            # Compress phases that are old enough
            memory.compress_older_phases(phase)
        except Exception as exc:
            logger.warning("[5/5] build_turn[%s/%s]: memory store failed: %s", my_slug, phase, exc)
    else:
        logger.debug("[5/5] build_turn[%s/%s]: no memory instance — skipping store", my_slug, phase)

    # ── Compose spoken output ────────────────────────────────────────────────
    spoken = argument.text.strip()
    if fact_check is not None:
        spoken = spoken + "\n\n" + format_fact_check_callout(fact_check)

    logger.info(
        "built turn: phase=%s slug=%s claims=%d fact_check=%s target=%s chars=%d "
        "strategy_angle=%s reflection=%s",
        phase,
        my_slug,
        len(argument.key_claims),
        fact_check.verdict if fact_check else "NONE",
        target_slug,
        len(spoken),
        strategy.rhetorical_angle if strategy else "none",
        "yes" if phase != "opening" else "skipped",
    )
    return spoken, argument, arg_sources, fact_check, target_slug
