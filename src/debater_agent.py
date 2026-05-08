"""Debater worker process.

One long-running worker that handles all debater dispatches. Each dispatch
becomes a fresh subprocess/job; dispatch metadata decides the debater's
stance, display name, and voice.

Key design choices (see plan for rationale):
- `agent_name="debater"` forces explicit dispatch (no auto-attach to rooms).
- `request_fnc` sets a deterministic identity (`debater-<slug>`) so the judge
  can address us over RPC.
- `AgentSession` is used only as a TTS/audio-output vehicle: `stt=None`,
  `vad=None`, `turn_detection="manual"`. We never listen to audio, which
  completely avoids the agent-to-agent feedback loop.
- Argument composition is done via the two-phase Gemini pipeline
  (`argument_generator.generate_argument`). The `Agent.llm` field is unused.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobRequest,
    WorkerOptions,
    cli,
)
from livekit.plugins import cartesia

from .argument_generator import build_turn
from .memory import DebateMemory
from .personas import voice_for_slug
from .schemas import TurnReply, TurnRequest

load_dotenv()

logger = logging.getLogger("debater")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')


async def _publish_to_room(room: rtc.Room, event: dict) -> None:
    """Publish a live-update data packet from the debater directly to the room.

    Uses the same channel / topic as the judge so the frontend can handle all
    events in one place.  Failures are logged but never crash the pipeline.
    """
    try:
        await room.local_participant.publish_data(
            json.dumps(event).encode("utf-8"),
            reliable=True,
            topic="debate.event",
        )
    except Exception as exc:
        logger.warning("debater: publish_to_room failed (event=%s): %s", event.get("type"), exc)


def _parse_metadata(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        logger.warning("debater: dispatch metadata was not valid JSON: %r", raw)
        return {}


async def on_request(req: JobRequest) -> None:
    """Accept the job with a deterministic identity derived from metadata."""
    meta = _parse_metadata(req.job.metadata)
    slug = meta.get("slug", "unknown")
    name = meta.get("name", slug.title())
    stance = meta.get("stance", "")
    await req.accept(
        identity=f"debater-{slug}",
        name=name,
        attributes={
            "role": "debater",
            "slug": slug,
            "stance": stance[:200],
        },
    )


async def entrypoint(ctx: JobContext) -> None:
    meta = _parse_metadata(ctx.job.metadata)
    slug: str = meta.get("slug", "unknown")
    name: str = meta.get("name", slug.title())
    stance: str = meta.get("stance", "")
    topic: str = meta.get("topic", "")

    voice = voice_for_slug(slug)
    logger.info(
        "debater starting: slug=%s name=%s voice=%s topic=%r",
        slug,
        name,
        voice.label,
        topic[:80],
    )

    await ctx.connect()

    session = AgentSession(
        llm=None,
        stt=None,
        vad=None,
        turn_detection="manual",
        tts=cartesia.TTS(model="sonic-2", voice=voice.id),
        allow_interruptions=False,
    )
    agent = Agent(
        instructions=(
            f"You are {name}, a debater arguing for: {stance}. "
            f"Debate topic: {topic}. You speak only when prompted via RPC."
        )
    )
    await session.start(room=ctx.room, agent=agent)

    # One DebateMemory instance per job — accumulates episodic context
    # across all turns in this debate and compresses older phases automatically.
    debate_memory = DebateMemory(my_slug=slug)

    rpc_lock = asyncio.Lock()

    @ctx.room.local_participant.register_rpc_method("debate.speak_turn")
    async def speak_turn(data: rtc.RpcInvocationData) -> str:
        caller = data.caller_identity
        logger.info("debater[%s] speak_turn invoked by %s", slug, caller)

        # Serialize turns; the judge should never call twice concurrently but
        # defend anyway.
        async with rpc_lock:
            try:
                req = TurnRequest.model_validate_json(data.payload)
            except Exception as exc:
                raise rtc.RpcError(
                    code=400, message=f"invalid TurnRequest payload: {exc}"
                ) from exc

            current_phase = req.phase

            # ── Create the UI card BEFORE build_turn ────────────────────────
            # This ensures research_chunk and turn_status events (which fire
            # during build_turn) always have a live card in activeTurns to
            # update.  The card starts empty; text is filled in afterward.
            await _publish_to_room(ctx.room, {
                "type": "turn_text_start",
                "slug": slug,
                "name": name,
                "phase": current_phase,
            })

            # ── Streaming callbacks ──────────────────────────────────────────

            async def _on_status(label: str) -> None:
                await _publish_to_room(ctx.room, {
                    "type": "turn_status",
                    "slug": slug,
                    "name": name,
                    "phase": current_phase,
                    "status": label,
                })

            async def _on_research_chunk(text: str) -> None:
                await _publish_to_room(ctx.room, {
                    "type": "research_chunk",
                    "slug": slug,
                    "name": name,
                    "phase": current_phase,
                    "text": text,
                })

            try:
                spoken, argument, citations, fact_check, target_slug = await build_turn(
                    topic=req.topic or topic,
                    stance=req.my_stance or stance,
                    phase=req.phase,
                    my_slug=req.my_slug or slug,
                    transcript=req.transcript,
                    last_opponent_text=req.last_opponent_text,
                    allow_fact_check=req.allow_fact_check,
                    memory=debate_memory,
                    on_status=_on_status,
                    on_research_chunk=_on_research_chunk,
                )
            except Exception as exc:
                logger.exception("debater[%s] argument generation failed", slug)
                # Clean up the pending card so the frontend doesn't show a
                # stuck spinner.
                await _publish_to_room(ctx.room, {
                    "type": "turn_text_end",
                    "slug": slug,
                    "name": name,
                    "phase": current_phase,
                })
                raise rtc.RpcError(code=500, message=f"generation failed: {exc}") from exc

            # Finalize the research panel with source links.
            await _publish_to_room(ctx.room, {
                "type": "research_done",
                "slug": slug,
                "name": name,
                "phase": current_phase,
                "sources": [s.model_dump() for s in citations[:5]],
            })

            logger.info(
                "debater[%s] speaking %d chars (claims=%d fact_check=%s target=%s)",
                slug,
                len(spoken),
                len(argument.key_claims),
                fact_check.verdict if fact_check else "none",
                target_slug,
            )

            # ── Stream text sentence-by-sentence BEFORE TTS starts ──────────
            # Text is fully visible in the UI within ~500 ms, before audio
            # begins.  The 50 ms inter-sentence pause gives the browser time
            # to render each line incrementally (typewriter effect).
            sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(spoken) if s.strip()]
            if not sentences:
                sentences = [spoken]

            for sentence in sentences:
                await _publish_to_room(ctx.room, {
                    "type": "turn_text_chunk",
                    "slug": slug,
                    "name": name,
                    "phase": current_phase,
                    "text": sentence + " ",
                })
                await asyncio.sleep(0.05)  # 50 ms — typewriter pacing

            await _publish_to_room(ctx.room, {
                "type": "turn_text_end",
                "slug": slug,
                "name": name,
                "phase": current_phase,
            })

            # ── Speak ────────────────────────────────────────────────────────
            handle = await session.say(spoken, allow_interruptions=False)
            try:
                await handle.wait_for_playout()
            except Exception as exc:  # pragma: no cover - TTS edge cases
                logger.warning("debater[%s] wait_for_playout error: %s", slug, exc)

            reply = TurnReply(
                text=spoken,
                key_claims=argument.key_claims,
                citations=citations,
                fact_check=fact_check,
                target_slug=target_slug,
            )
            return reply.model_dump_json()

    # Keep the job alive. The judge removes the participant (or the room closes)
    # to end the session.
    logger.info("debater[%s] ready, waiting for RPC calls", slug)
    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        logger.info("debater[%s] shutting down", slug)
        raise


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            request_fnc=on_request,
            agent_name="debater",
            # Health server port — must differ from the judge's (8082) since we
            # run both workers on the same host.
            port=int(os.environ.get("DEBATER_HEALTH_PORT", "8081")),
        )
    )
