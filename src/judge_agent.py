"""Judge worker process.

Orchestrates the debate:
  1. Wait for every expected `debater-<slug>` identity to connect.
  2. Announce the debate, then run each phase (opening → rebuttals → closing).
  3. For each debater's turn: RPC `debate.speak_turn`, collect the `TurnReply`,
     concurrently fact-check every `key_claim`.
  4. If any claim comes back CONTRADICTED with confidence ≥ threshold, announce
     the hallucination call, remove the debater via Room Service, and continue
     with the survivors.
  5. After the final phase (or when only one debater is left), render a
     `FinalVerdict` via a schema-constrained Gemini call and read it aloud.

Published data packets mirror every transcript entry + fact-check so the web
observer can render a live transcript panel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
from datetime import datetime, timezone

from dotenv import load_dotenv
from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobRequest,
    WorkerOptions,
    cli,
)
from livekit.plugins import cartesia

from .config import settings
from .gemini_client import structured_generate
from .personas import JUDGE_VOICE
from .prompts import final_verdict_prompt
from .schemas import (
    DEFAULT_PHASES,
    DebaterSpec,
    FactCheck,
    FinalVerdict,
    ScoreEntry,
    TranscriptEntry,
    TurnReply,
    TurnRequest,
)

load_dotenv()

logger = logging.getLogger("judge")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


DEBATER_CONNECT_TIMEOUT_S = 90.0
PER_TURN_RPC_TIMEOUT_S = 240.0
# The opening phase has no opponent turn to fact-check yet, so we disable
# peer fact-checks there. Every other phase allows them.
NO_FACTCHECK_PHASES: set[str] = {"opening"}


def _parse_metadata(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        logger.warning("judge: dispatch metadata was not valid JSON: %r", raw)
        return {}


async def on_request(req: JobRequest) -> None:
    await req.accept(
        identity="judge",
        name="Judge",
        attributes={"role": "judge"},
    )


async def _wait_for_debater(ctx: JobContext, identity: str, timeout_s: float) -> None:
    """Wait until a participant with the given identity is in the room."""
    for p in ctx.room.remote_participants.values():
        if p.identity == identity:
            return

    done = asyncio.Event()

    def on_connected(participant: rtc.RemoteParticipant) -> None:
        if participant.identity == identity:
            done.set()

    ctx.room.on("participant_connected", on_connected)
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout_s)
    finally:
        ctx.room.off("participant_connected", on_connected)


async def _publish_event(room: rtc.Room, event: dict) -> None:
    """Fire-and-forget data packet for the web observer's live transcript."""
    try:
        await room.local_participant.publish_data(
            json.dumps(event).encode("utf-8"),
            reliable=True,
            topic="debate.event",
        )
    except Exception as exc:  # pragma: no cover - observer is best-effort
        logger.debug("publish_data failed: %s", exc)


def _worst_verdict_against(
    phase_checks: list[dict], target_slug: str, threshold: float
) -> dict | None:
    """Return the highest-confidence CONTRADICTED verdict against `target_slug`
    in this phase's collected fact-checks, if it meets the threshold.

    `phase_checks[i]` is expected to contain `by_slug`, `target_slug`, and
    FactCheck fields (`claim`, `verdict`, `confidence`, `evidence_summary`).
    """
    best: dict | None = None
    for c in phase_checks:
        if c.get("target_slug") != target_slug:
            continue
        if c.get("verdict") != "CONTRADICTED":
            continue
        if c.get("confidence", 0.0) < threshold:
            continue
        if best is None or c["confidence"] > best["confidence"]:
            best = c
    return best


def _render_transcript(transcript: list[TranscriptEntry]) -> str:
    if not transcript:
        return "(no turns)"
    lines = [
        f"[{e.phase}] {e.name} ({e.slug}): {e.text}" for e in transcript
    ]
    return "\n\n".join(lines)


# LiveKit RPC has a payload size limit (~15 KB). The transcript grows with each
# turn, so we strip citations and window to the most recent turns before
# including it in the RPC payload.
_RPC_TRANSCRIPT_WINDOW = 6


def _slim_transcript(transcript: list[TranscriptEntry]) -> list[TranscriptEntry]:
    """Return a windowed, citation-stripped copy of the transcript for RPC."""
    recent = transcript[-_RPC_TRANSCRIPT_WINDOW:]
    return [
        TranscriptEntry(
            slug=e.slug,
            name=e.name,
            phase=e.phase,
            text=e.text,
            key_claims=e.key_claims,
            citations=[],
        )
        for e in recent
    ]


async def _remove_participant(room_name: str, identity: str) -> None:
    try:
        async with api.LiveKitAPI() as lkapi:
            await lkapi.room.remove_participant(
                api.RoomParticipantIdentity(room=room_name, identity=identity)
            )
    except Exception as exc:  # pragma: no cover - Room Service edge cases
        logger.warning("remove_participant(%s) failed: %s", identity, exc)


def _persist_run(room_name: str, payload: dict) -> None:
    runs_dir = pathlib.Path("runs")
    runs_dir.mkdir(exist_ok=True)
    out = runs_dir / f"{room_name}.json"
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    logger.info("judge: wrote transcript to %s", out)


async def entrypoint(ctx: JobContext) -> None:
    meta = _parse_metadata(ctx.job.metadata)
    topic: str = meta.get("topic") or "(unspecified topic)"
    phases: list[str] = meta.get("phases") or list(DEFAULT_PHASES)
    raw_debaters = meta.get("debaters") or []
    debaters: list[DebaterSpec] = [DebaterSpec(**d) for d in raw_debaters]
    if len(debaters) < 2:
        logger.error("judge: need at least 2 debaters in metadata, got %d", len(debaters))
        return

    logger.info(
        "judge starting: topic=%r debaters=%s phases=%s",
        topic,
        [d.slug for d in debaters],
        phases,
    )

    await ctx.connect()

    session = AgentSession(
        llm=None,
        stt=None,
        vad=None,
        turn_detection="manual",
        tts=cartesia.TTS(model="sonic-2", voice=JUDGE_VOICE.id),
        allow_interruptions=False,
    )
    agent = Agent(
        instructions=(
            "You are the judge of a structured debate. You open the debate, "
            "call each speaker in turn, and at the end of each round you "
            "evaluate the fact-checks the debaters raised against one another. "
            "You disqualify only clearly hallucinated claims, and at the end "
            "of the debate you pick a winner based on argument quality alone."
        )
    )
    await session.start(room=ctx.room, agent=agent)

    # 1. Wait for every debater to connect.
    logger.info("judge: waiting for debaters to join...")
    try:
        await asyncio.gather(
            *[
                _wait_for_debater(ctx, f"debater-{d.slug}", DEBATER_CONNECT_TIMEOUT_S)
                for d in debaters
            ]
        )
    except asyncio.TimeoutError:
        logger.error("judge: timed out waiting for debaters")
        await session.say(
            "I'm sorry — not all debaters were able to join in time. Ending the debate."
        )
        return
    logger.info("judge: all debaters connected")

    debater_by_slug: dict[str, DebaterSpec] = {d.slug: d for d in debaters}
    alive: list[str] = [d.slug for d in debaters]
    transcript: list[TranscriptEntry] = []
    factchecks: list[dict] = []  # every peer fact-check, across all phases
    threshold = settings.factcheck_hallucination_threshold
    room_name = ctx.room.name

    intro_names = ", ".join(d.name for d in debaters)
    await session.say(
        f"Welcome. Today's debate topic is: {topic}. "
        f"Our debaters are {intro_names}. "
        "Each round, every debater will speak, and in the rebuttal rounds "
        "they will also fact-check one of their opponent's claims. "
        "I will not interrupt during a round. At the end of each round I will "
        "review the fact-checks and disqualify anyone whose claims were "
        "clearly contradicted by evidence. At the end, I will pick a winner "
        "based purely on the quality of their argumentation. Let's begin."
    )
    await _publish_event(
        ctx.room,
        {"type": "debate_started", "topic": topic, "debaters": [d.model_dump() for d in debaters]},
    )

    # 2. Main phase loop. Each phase is one round; the judge collects every
    # debater's peer fact-check and evaluates them ALL at end of phase.
    for phase in phases:
        if len(alive) <= 1:
            break
        allow_fact_check = phase not in NO_FACTCHECK_PHASES
        await session.say(f"We now move to the {phase.replace('_', ' ')}.")
        await _publish_event(ctx.room, {"type": "phase_started", "phase": phase})

        phase_factchecks: list[dict] = []
        round_slugs = list(alive)  # snapshot: nobody disqualified mid-round

        for slug in round_slugs:
            spec = debater_by_slug[slug]
            last_opponent = transcript[-1].text if transcript else None
            payload = TurnRequest(
                phase=phase,
                topic=topic,
                my_slug=slug,
                my_stance=spec.stance,
                transcript=_slim_transcript(transcript),
                last_opponent_text=last_opponent,
                allow_fact_check=allow_fact_check,
                time_limit_s=75,
            ).model_dump_json()

            await session.say(f"{spec.name}, you have the floor.")
            logger.info("judge: calling debater-%s for phase=%s", slug, phase)
            try:
                reply_json = await ctx.room.local_participant.perform_rpc(
                    destination_identity=f"debater-{slug}",
                    method="debate.speak_turn",
                    payload=payload,
                    response_timeout=PER_TURN_RPC_TIMEOUT_S,
                )
            except Exception as exc:
                logger.warning("judge: RPC to debater-%s failed: %s", slug, exc)
                await session.say(
                    f"{spec.name} failed to respond. They forfeit this turn."
                )
                continue

            try:
                reply = TurnReply.model_validate_json(reply_json)
            except Exception as exc:
                logger.warning("judge: bad TurnReply from debater-%s: %s", slug, exc)
                continue

            entry = TranscriptEntry(
                slug=slug,
                name=spec.name,
                phase=phase,
                text=reply.text,
                key_claims=reply.key_claims,
                citations=reply.citations,
            )
            transcript.append(entry)
            await _publish_event(
                ctx.room,
                {"type": "turn_spoken", "entry": entry.model_dump()},
            )

            # Accumulate the peer fact-check for end-of-round evaluation.
            if reply.fact_check is not None and reply.target_slug:
                record = {
                    "phase": phase,
                    "by_slug": slug,
                    "target_slug": reply.target_slug,
                    **reply.fact_check.model_dump(),
                }
                phase_factchecks.append(record)
                factchecks.append(record)
                await _publish_event(
                    ctx.room,
                    {"type": "fact_check", **record},
                )
                logger.info(
                    "judge: peer fact-check recorded: by=%s target=%s verdict=%s conf=%.2f",
                    slug,
                    reply.target_slug,
                    reply.fact_check.verdict,
                    reply.fact_check.confidence,
                )

        # 3. End-of-round adjudication. Only disqualify on CONTRADICTED with
        # confidence >= threshold. Never mid-phase.
        if phase_factchecks:
            await session.say(
                "The round is complete. Let me review the fact-checks raised."
            )
        # Evaluate one target at a time so the ruling speech stays coherent.
        already_removed_this_phase: set[str] = set()
        for target in round_slugs:
            if target in already_removed_this_phase:
                continue
            hit = _worst_verdict_against(phase_factchecks, target, threshold)
            if hit is None:
                continue
            target_name = debater_by_slug[target].name
            accuser_name = debater_by_slug.get(hit["by_slug"], DebaterSpec(slug=hit["by_slug"], name=hit["by_slug"], stance="unknown")).name
            ruling = (
                f"I'm calling a hallucination against {target_name}, based on "
                f"the fact-check raised by {accuser_name}. The claim in question — "
                f"\"{hit['claim']}\" — is contradicted by the evidence. "
                f"{hit.get('evidence_summary', '').strip()} "
                f"{target_name} is disqualified from this debate."
            )
            logger.info("judge: disqualifying debater-%s for %r", target, hit["claim"])
            await session.say(ruling)
            if target in alive:
                alive.remove(target)
            already_removed_this_phase.add(target)
            await _publish_event(
                ctx.room,
                {
                    "type": "debater_removed",
                    "slug": target,
                    "reason": "hallucination",
                    "by_slug": hit["by_slug"],
                    "claim": hit["claim"],
                    "evidence": hit.get("evidence_summary", ""),
                },
            )
            await _remove_participant(room_name, f"debater-{target}")
            if len(alive) <= 1:
                break

    # 4. Final verdict.
    if len(alive) == 1:
        last_slug = alive[0]
        last_name = debater_by_slug[last_slug].name
        verdict_text = (
            f"With only {last_name} remaining after disqualifications, "
            f"{last_name} wins by default. The debate is concluded."
        )
        final = FinalVerdict(
            winner_slug=last_slug,
            scores=[ScoreEntry(slug=last_slug, score=1.0)] + [ScoreEntry(slug=s, score=0.0) for s in debater_by_slug if s != last_slug],
            rationale=verdict_text,
        )
    else:
        try:
            final = await structured_generate(
                final_verdict_prompt(
                    topic=topic,
                    transcript_text=_render_transcript(transcript),
                    debater_names={d.slug: d.name for d in debaters if d.slug in alive},
                ),
                schema=FinalVerdict,
                temperature=0.2,
            )
        except Exception as exc:
            logger.exception("judge: final verdict generation failed")
            final = FinalVerdict(
                winner_slug=alive[0] if alive else debaters[0].slug,
                scores=[ScoreEntry(slug=s, score=0.5) for s in alive],
                rationale=(
                    "I was unable to render a conclusive verdict due to a "
                    "technical issue. Thank you to all debaters."
                ),
            )

    winner_name = debater_by_slug.get(final.winner_slug).name if final.winner_slug in debater_by_slug else final.winner_slug
    await session.say(
        f"My verdict: the winner is {winner_name}. {final.rationale}"
    )
    await _publish_event(
        ctx.room,
        {"type": "verdict", **final.model_dump(), "winner_name": winner_name},
    )

    # 5. Persist run and shut down.
    _persist_run(
        room_name,
        {
            "room": room_name,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "topic": topic,
            "debaters": [d.model_dump() for d in debaters],
            "phases": phases,
            "transcript": [e.model_dump() for e in transcript],
            "fact_checks": factchecks,
            "final_verdict": final.model_dump(),
            "winner_name": winner_name,
        },
    )

    # Give listeners a moment before the judge's job ends.
    await asyncio.sleep(3)
    logger.info("judge: debate complete, disconnecting")
    ctx.shutdown(reason="debate_complete")


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            request_fnc=on_request,
            agent_name="judge",
            # Health server port — must differ from the debater's (8081) since
            # we run both workers on the same host.
            port=int(os.environ.get("JUDGE_HEALTH_PORT", "8082")),
        )
    )
