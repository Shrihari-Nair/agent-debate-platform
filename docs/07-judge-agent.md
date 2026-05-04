# Judge Agent — The Debate Conductor

The judge agent lives in [src/judge_agent.py](../src/judge_agent.py). It is the most complex component in the system. Its job is to run the entire debate lifecycle from start to finish — wait for everyone to connect, run the rounds, adjudicate, and deliver a verdict.

---

## What Is a LiveKit Worker?

Before reading the code, understand the LiveKit Worker model.

A Worker is a long-running Python process that:
1. Registers itself with LiveKit under a name (e.g. `"judge"`).
2. Waits for *dispatches* — server-side messages that say "please handle this job".
3. When a dispatch arrives, calls `on_request` (to accept/reject and set identity), then `entrypoint` (the actual job logic).

Each dispatch runs as an isolated async task. One worker process can handle multiple jobs at once (but the judge worker only ever gets one job per room, and one debate happens in one room at a time — so in practice it is one job at a time unless you start multiple simultaneous debates).

---

## Constants and Configuration

```python
DEBATER_CONNECT_TIMEOUT_S = 90.0
PER_TURN_RPC_TIMEOUT_S = 240.0
NO_FACTCHECK_PHASES: set[str] = {"opening"}
_RPC_TRANSCRIPT_WINDOW = 6
```

| Constant | Value | Meaning |
|---|---|---|
| `DEBATER_CONNECT_TIMEOUT_S` | 90s | How long the judge waits for each debater to connect before giving up |
| `PER_TURN_RPC_TIMEOUT_S` | 240s | How long the judge waits for a debater's RPC to return (argument gen + TTS playback) |
| `NO_FACTCHECK_PHASES` | `{"opening"}` | Which phases disallow peer fact-checking (opening: no opponent text yet) |
| `_RPC_TRANSCRIPT_WINDOW` | 6 | Maximum transcript entries sent in RPC payload (to avoid hitting LiveKit's ~15KB limit) |

---

## Entry Points

### `on_request()` — Accepting the Dispatch

```python
async def on_request(req: JobRequest) -> None:
    await req.accept(
        identity="judge",
        name="Judge",
        attributes={"role": "judge"},
    )
```

This runs before `entrypoint`. It accepts the dispatch and sets the participant identity to `"judge"`. This identity is how the debaters and the browser refer to the judge. The `attributes` dictionary is visible to other participants in the room.

### `entrypoint()` — The Main Job Logic

`entrypoint(ctx: JobContext)` is called after `on_request` returns. `ctx` is the job context — it gives access to:
- `ctx.job.metadata` — the JSON string the orchestrator passed in the dispatch
- `ctx.room` — the LiveKit room object (once connected)
- `ctx.room.local_participant` — the judge's own participant object
- `ctx.room.remote_participants` — all other participants currently in the room
- `ctx.shutdown(reason=...)` — gracefully ends the job

---

## Phase 1: Parsing Metadata and Setup

```python
meta = _parse_metadata(ctx.job.metadata)
topic: str = meta.get("topic") or "(unspecified topic)"
phases: list[str] = meta.get("phases") or list(DEFAULT_PHASES)
raw_debaters = meta.get("debaters") or []
debaters: list[DebaterSpec] = [DebaterSpec(**d) for d in raw_debaters]
if len(debaters) < 2:
    logger.error("judge: need at least 2 debaters in metadata, got %d", len(debaters))
    return
```

The metadata is the JSON string the orchestrator encoded when creating the dispatch. It is a `DebateConfig` serialised with `.model_dump()`. The judge parses it and reconstructs `DebaterSpec` objects by passing each dict to the Pydantic model constructor.

The guard against fewer than 2 debaters is defensive — the orchestrator already validates this, but if metadata is somehow corrupted or malformed, the judge exits cleanly instead of crashing later.

### `_parse_metadata()` — Safe JSON Parsing

```python
def _parse_metadata(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        logger.warning("judge: dispatch metadata was not valid JSON: %r", raw)
        return {}
```

Returns an empty dict on any error. This means any downstream `.get()` calls will return `None`, which the code handles with `or` fallbacks.

---

## Phase 2: Connecting and Starting TTS

```python
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
    instructions=("You are the judge of a structured debate...")
)
await session.start(room=ctx.room, agent=agent)
```

### `AgentSession` — TTS-Only Mode

The judge only ever *speaks* — it never listens. So:
- `llm=None` — no language model for real-time conversation
- `stt=None` — no speech-to-text
- `vad=None` — no voice-activity detection
- `turn_detection="manual"` — turns are not detected by audio; they are controlled by the judge's own code
- `tts=cartesia.TTS(...)` — text-to-speech engine with the judge's designated voice

This "TTS-only" session is used purely as an audio output channel. The judge generates speech text in its own code and calls `session.say()`.

---

## Phase 3: Waiting for All Debaters

```python
try:
    await asyncio.gather(
        *[
            _wait_for_debater(ctx, f"debater-{d.slug}", DEBATER_CONNECT_TIMEOUT_S)
            for d in debaters
        ]
    )
except asyncio.TimeoutError:
    logger.error("judge: timed out waiting for debaters")
    await session.say("I'm sorry — not all debaters were able to join in time. Ending the debate.")
    return
```

`asyncio.gather()` waits for ALL `_wait_for_debater()` coroutines concurrently. The timeout is applied per-debater (inside the function), but since they run concurrently, the total wait is still at most `DEBATER_CONNECT_TIMEOUT_S`.

### `_wait_for_debater()` — Connection Event Logic

```python
async def _wait_for_debater(ctx: JobContext, identity: str, timeout_s: float) -> None:
    """Wait until a participant with the given identity is in the room."""
    for p in ctx.room.remote_participants.values():
        if p.identity == identity:
            return  # already connected

    done = asyncio.Event()

    def on_connected(participant: rtc.RemoteParticipant) -> None:
        if participant.identity == identity:
            done.set()

    ctx.room.on("participant_connected", on_connected)
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout_s)
    finally:
        ctx.room.off("participant_connected", on_connected)
```

**Two-step check — why?**

There is a race condition between the judge connecting and the debaters connecting. If the judge checks `remote_participants` first and the debater has already connected, it would see it immediately. If the debater hasn't connected yet, the judge registers a `participant_connected` event listener and waits.

The `finally` block ensures the event listener is always removed, even if an exception or timeout occurs. Failing to remove listeners is a common source of memory leaks in event-driven code.

`asyncio.Event` is a synchronisation primitive — `done.wait()` blocks until `done.set()` is called. `asyncio.wait_for` adds the timeout.

---

## Phase 4: The Phase Loop — Core Debate Logic

### State Variables

```python
debater_by_slug: dict[str, DebaterSpec] = {d.slug: d for d in debaters}
alive: list[str] = [d.slug for d in debaters]   # slugs still in the debate
transcript: list[TranscriptEntry] = []            # growing debate record
factchecks: list[dict] = []                       # all fact-checks across all phases
threshold = settings.factcheck_hallucination_threshold  # default 0.8
room_name = ctx.room.name
```

`alive` is the critical list. Debaters are removed from it when disqualified. The phase loop checks `len(alive)` before each phase — if only one remains, the debate ends.

### Opening Announcement

```python
intro_names = ", ".join(d.name for d in debaters)
await session.say(
    f"Welcome. Today's debate topic is: {topic}. "
    f"Our debaters are {intro_names}. ..."
)
await _publish_event(ctx.room, {"type": "debate_started", "topic": topic, ...})
```

`session.say()` converts text to speech and publishes the audio to the room. All participants (debaters and browser observers) hear it.

`_publish_event()` sends a data packet — not audio — on the `"debate.event"` topic. The browser reads this to render the transcript panel.

### Phase Loop

```python
for phase in phases:
    if len(alive) <= 1:
        break
    allow_fact_check = phase not in NO_FACTCHECK_PHASES
    await session.say(f"We now move to the {phase.replace('_', ' ')}.")
    await _publish_event(ctx.room, {"type": "phase_started", "phase": phase})

    phase_factchecks: list[dict] = []
    round_slugs = list(alive)   # snapshot — no disqualifications mid-round
```

**`round_slugs = list(alive)` — the snapshot pattern**

This creates a copy of `alive` at the start of the phase. Even if a debater gets disqualified during the adjudication loop later, `round_slugs` still has all the original debaters — they all get to speak. Disqualifications only take effect at the *end* of a phase, never mid-phase. This is a deliberate fairness rule.

### Calling Each Debater

```python
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
    try:
        reply_json = await ctx.room.local_participant.perform_rpc(
            destination_identity=f"debater-{slug}",
            method="debate.speak_turn",
            payload=payload,
            response_timeout=PER_TURN_RPC_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warning("judge: RPC to debater-%s failed: %s", slug, exc)
        await session.say(f"{spec.name} failed to respond. They forfeit this turn.")
        continue
```

`perform_rpc` is a LiveKit API call that:
1. Sends the `payload` to the participant with `destination_identity=f"debater-{slug}"`
2. Waits up to `response_timeout` for the participant to call its registered RPC handler and return a response
3. Returns the response string

**Why is `response_timeout=240` seconds?**

A turn involves: argument generation (two Gemini calls, ~10–30s each) + fact-check (two more Gemini calls, concurrent, ~10–30s) + TTS synthesis (~3–10s) + TTS playback (~20–60s for 100–220 word argument). Total: up to ~4 minutes. 240 seconds is a conservative upper bound.

### Processing the Reply

```python
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
await _publish_event(ctx.room, {"type": "turn_spoken", "entry": entry.model_dump()})

if reply.fact_check is not None and reply.target_slug:
    record = {
        "phase": phase,
        "by_slug": slug,
        "target_slug": reply.target_slug,
        **reply.fact_check.model_dump(),
    }
    phase_factchecks.append(record)
    factchecks.append(record)
    await _publish_event(ctx.room, {"type": "fact_check", **record})
```

The `TranscriptEntry` is appended to the growing `transcript` list. This will be sent (windowed) to the next debater as context.

If the reply includes a `fact_check`, it is stored in `phase_factchecks` (for end-of-phase adjudication) and `factchecks` (for the final persisted record). A data packet is also published so the browser can render it.

---

## Phase 5: End-of-Phase Adjudication

```python
if phase_factchecks:
    await session.say("The round is complete. Let me review the fact-checks raised.")

already_removed_this_phase: set[str] = set()
for target in round_slugs:
    if target in already_removed_this_phase:
        continue
    hit = _worst_verdict_against(phase_factchecks, target, threshold)
    if hit is None:
        continue
    # ... disqualify target
```

### `_worst_verdict_against()`

```python
def _worst_verdict_against(
    phase_checks: list[dict], target_slug: str, threshold: float
) -> dict | None:
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
```

Finds the highest-confidence `CONTRADICTED` verdict against a specific target debater in the current phase's checks. Only returns a result if confidence is at or above the threshold (default 0.8).

**Why the highest confidence?** If a debater made three hallucinated claims, we report the one we're most sure about, making the ruling more defensible.

### Disqualification

```python
target_name = debater_by_slug[target].name
accuser_name = debater_by_slug.get(hit["by_slug"], DebaterSpec(
    slug=hit["by_slug"], name=hit["by_slug"], stance="unknown"
)).name
ruling = (
    f"I'm calling a hallucination against {target_name}, based on "
    f"the fact-check raised by {accuser_name}. The claim in question — "
    f"\"{hit['claim']}\" — is contradicted by the evidence. "
    f"{hit.get('evidence_summary', '').strip()} "
    f"{target_name} is disqualified from this debate."
)
await session.say(ruling)
if target in alive:
    alive.remove(target)
already_removed_this_phase.add(target)
await _publish_event(ctx.room, {"type": "debater_removed", ...})
await _remove_participant(room_name, f"debater-{target}")
```

The disqualification sequence:
1. Speak the ruling aloud (the audience hears it).
2. Remove the target from `alive`.
3. Add to `already_removed_this_phase` (prevents double-processing).
4. Publish `"debater_removed"` data packet (browser marks the card as disqualified).
5. Call `_remove_participant()` to physically evict the debater from the room.

### `_remove_participant()`

```python
async def _remove_participant(room_name: str, identity: str) -> None:
    try:
        async with api.LiveKitAPI() as lkapi:
            await lkapi.room.remove_participant(
                api.RoomParticipantIdentity(room=room_name, identity=identity)
            )
    except Exception as exc:
        logger.warning("remove_participant(%s) failed: %s", identity, exc)
```

Uses LiveKit's server-side Room Service API to forcibly remove a participant from the room. This terminates their WebRTC connection. When the debater agent is evicted, its `await asyncio.Future()` (the keepalive) gets cancelled, and the job ends.

---

## `_slim_transcript()` — Fitting Within RPC Limits

```python
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
            citations=[],   # stripped — main source of size growth
        )
        for e in recent
    ]
```

LiveKit RPC has an undocumented payload limit around 15KB. As the transcript grows, the serialised `TurnRequest` would eventually exceed this limit.

Two strategies combined:
1. **Window** — only send the last 6 turns (not the entire history). More than 6 turns of context makes little difference for argument quality.
2. **Strip citations** — citations are the largest part of a `TranscriptEntry` (each source has a full URL). They are not useful context for the debater anyway.

---

## Phase 6: Final Verdict

```python
if len(alive) == 1:
    # Default win by elimination
    final = FinalVerdict(
        winner_slug=alive[0],
        scores=[ScoreEntry(slug=alive[0], score=1.0)] + [...],
        rationale=f"{last_name} wins by default...",
    )
else:
    final = await structured_generate(
        final_verdict_prompt(topic, _render_transcript(transcript), ...),
        schema=FinalVerdict,
        temperature=0.2,
    )
```

**Two paths:**

1. **Elimination path**: If all but one debater was disqualified, the winner is automatic. No LLM call needed.
2. **Judgment path**: If multiple debaters survive, Gemini scores them on argument quality and picks a winner.

```python
winner_name = debater_by_slug.get(final.winner_slug).name ...
await session.say(f"My verdict: the winner is {winner_name}. {final.rationale}")
await _publish_event(ctx.room, {"type": "verdict", ...})
```

---

## Phase 7: Run Persistence

```python
def _persist_run(room_name: str, payload: dict) -> None:
    runs_dir = pathlib.Path("runs")
    runs_dir.mkdir(exist_ok=True)
    out = runs_dir / f"{room_name}.json"
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    logger.info("judge: wrote transcript to %s", out)
```

Writes the complete debate record to `runs/{room_name}.json`. The `default=str` argument tells `json.dumps` to convert any non-serialisable value (like `datetime`) to a string instead of crashing.

The persisted payload includes:
- `room`, `ended_at`, `topic`, `debaters`, `phases`
- `transcript` — every `TranscriptEntry`
- `fact_checks` — every `FactCheck` from every phase
- `final_verdict` — the `FinalVerdict`
- `winner_name` — the winner's display name

---

## Phase 8: Shutdown

```python
await asyncio.sleep(3)
logger.info("judge: debate complete, disconnecting")
ctx.shutdown(reason="debate_complete")
```

A 3-second pause before shutdown gives:
- Any in-flight data packets time to be delivered to the browser
- The browser time to receive and render the verdict before the room closes
- The TTS audio time to finish playing through the speaker

`ctx.shutdown()` cleanly ends the job. The worker process continues running, ready for the next dispatch.

---

## Worker Registration

```python
if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            request_fnc=on_request,
            agent_name="judge",
            port=int(os.environ.get("JUDGE_HEALTH_PORT", "8082")),
        )
    )
```

`cli.run_app()` starts the LiveKit worker. The `WorkerOptions` binds:
- `entrypoint_fnc` — the main job function
- `request_fnc` — the accept/reject hook
- `agent_name="judge"` — the name dispatches must target
- `port` — health probe port (must differ from the debater worker's 8081)

Run with: `python -m src.judge_agent start`
