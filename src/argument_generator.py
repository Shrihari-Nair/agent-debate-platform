"""Turn builder used by debater agents.

A debater's turn produces three things in parallel:
1. A composed argument for the current phase (two-phase Gemini: ground -> schema).
2. (Non-opening phases only) A fact-check of one opponent's recent claim
   (two-phase Gemini via `fact_checker.fact_check_claim`).
3. A final spoken utterance = argument text + (optional) fact-check callout.

The judge never fact-checks. It only aggregates the `FactCheck` objects that
debaters bring back, then at end-of-phase disqualifies any debater whose
opponent's check returned CONTRADICTED with high confidence.
"""

from __future__ import annotations

import asyncio
import logging

from .fact_checker import fact_check_claim
from .gemini_client import format_sources_block, grounded_generate, structured_generate
from .prompts import (
    compose_argument_prompt,
    format_fact_check_callout,
    ground_topic_prompt,
)
from .schemas import Argument, FactCheck, Source, TranscriptEntry

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


async def _generate_argument_core(
    *,
    topic: str,
    stance: str,
    phase: str,
    transcript: list[TranscriptEntry],
    last_opponent_text: str | None,
) -> tuple[Argument, list[Source]]:
    evidence, sources = await grounded_generate(
        ground_topic_prompt(topic, stance, phase, last_opponent_text),
        temperature=0.2,
    )
    argument = await structured_generate(
        compose_argument_prompt(
            topic=topic,
            stance=stance,
            phase=phase,
            transcript_text=_render_transcript(transcript),
            last_opponent=last_opponent_text,
            evidence=evidence or "(no evidence retrieved)",
            sources_block=format_sources_block(sources),
        ),
        schema=Argument,
        temperature=0.6,
    )
    return argument, sources


async def build_turn(
    *,
    topic: str,
    stance: str,
    phase: str,
    my_slug: str,
    transcript: list[TranscriptEntry],
    last_opponent_text: str | None,
    allow_fact_check: bool,
) -> tuple[str, Argument, list[Source], FactCheck | None, str | None]:
    """Compose one debater's turn end to end.

    Returns:
      spoken_text    — the full utterance to send to TTS (argument + optional callout)
      argument       — the structured Argument (text + key_claims)
      citations      — grounding sources for the argument
      fact_check     — the debater's verdict on an opponent claim (or None)
      target_slug    — which opponent's claim was checked (or None)
    """
    target: tuple[str, str] | None = None
    if allow_fact_check:
        target = _pick_opponent_claim(transcript, my_slug)

    argument_task = asyncio.create_task(
        _generate_argument_core(
            topic=topic,
            stance=stance,
            phase=phase,
            transcript=transcript,
            last_opponent_text=last_opponent_text,
        )
    )
    check_task: asyncio.Task[FactCheck] | None = None
    if target is not None:
        claim_text, _target_slug = target
        check_task = asyncio.create_task(fact_check_claim(claim_text))

    argument, arg_sources = await argument_task
    fact_check: FactCheck | None = None
    target_slug: str | None = None
    if check_task is not None and target is not None:
        try:
            fact_check = await check_task
            target_slug = target[1]
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("fact_check side-task failed: %s", exc)

    spoken = argument.text.strip()
    if fact_check is not None:
        spoken = spoken + "\n\n" + format_fact_check_callout(fact_check)

    logger.info(
        "built turn: phase=%s slug=%s claims=%d fact_check=%s target=%s chars=%d",
        phase,
        my_slug,
        len(argument.key_claims),
        fact_check.verdict if fact_check else "NONE",
        target_slug,
        len(spoken),
    )
    return spoken, argument, arg_sources, fact_check, target_slug
