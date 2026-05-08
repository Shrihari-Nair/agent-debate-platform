# Debater Agent — The Speaker

The debater agent lives in [src/debater_agent.py](../src/debater_agent.py). It is responsible for one thing: when the judge calls `debate.speak_turn`, generate an argument, speak it aloud via TTS, wait for the audio to finish, and return the result.

One worker process handles **all** debater dispatches. If you have 3 debaters, the same worker process runs 3 concurrent jobs.

---

## Key Design Choices

```python
"""Debater worker process.

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
```

### Why No STT or VAD?

If debater agents had speech-to-text enabled, they would hear each other (and the judge) through the shared room. This would create an audio feedback loop where debater A hears debater B and tries to respond in real time, making the turn structure impossible to control. By disabling STT entirely, debaters are "deaf" — they can only speak, and only when explicitly told to via RPC.

### Why `agent_name="debater"` Not Per-Slug Names?

All debaters register under one name so a single worker can handle any number of debater dispatches. The differentiation between individual debaters is done via `metadata` in the dispatch, not the agent name.

---

## Entry Points

### `on_request()` — Setting Identity

```python
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
```

The identity `f"debater-{slug}"` is critical. The judge uses this exact string to route RPC calls: `destination_identity=f"debater-{slug}"`. If the identity does not match, the RPC call fails.

`attributes` are key-value pairs visible to all other participants in the room. The browser reads `role`, `slug`, and `stance` from participant attributes to populate the participant cards.

`stance[:200]` — LiveKit participant attribute values have a size limit. Truncating to 200 characters avoids exceeding it.

---

## `entrypoint()` — The Main Job Logic

### Step 1: Parse metadata

```python
meta = _parse_metadata(ctx.job.metadata)
slug: str = meta.get("slug", "unknown")
name: str = meta.get("name", slug.title())
stance: str = meta.get("stance", "")
topic: str = meta.get("topic", "")
```

The orchestrator sets these four values in the dispatch metadata for each debater. They are the minimum info the debater needs: who it is, what it argues, and what the topic is. The full transcript and per-turn context is sent via RPC with each `TurnRequest`.

### Step 2: Assign voice

```python
voice = voice_for_slug(slug)
logger.info("debater starting: slug=%s name=%s voice=%s topic=%r", ...)
```

`voice_for_slug()` returns a deterministic Cartesia voice ID for this slug. See [11-personas.md](./11-personas.md) for the full voice assignment logic.

### Step 3: Connect and start TTS session

```python
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
```

Identical pattern to the judge's session setup. The key difference is the voice — each debater gets a different `voice.id` from the Cartesia pool.

`allow_interruptions=False` — the debater will not stop speaking if another participant starts audio. This prevents one debater's speech from being cut off by the judge or another debater.

---

## The RPC Handler: `speak_turn`

```python
rpc_lock = asyncio.Lock()

@ctx.room.local_participant.register_rpc_method("debate.speak_turn")
async def speak_turn(data: rtc.RpcInvocationData) -> str:
```

This is the most important function in the debater agent. Here is the full execution order:

1. Deserialise `TurnRequest` from the RPC payload
2. **Publish `turn_text_start`** — creates the UI card immediately, before any work starts
3. Call `build_turn(...)` with `on_status` and `on_research_chunk` callbacks
4. Publish `research_done` — attaches sources to the research panel
5. Stream text sentence-by-sentence with 50ms pacing
6. Publish `turn_text_end` — removes the typing cursor
7. Call `session.say()` to start TTS audio
8. `await handle.wait_for_playout()` — blocks until audio finishes
9. Return `TurnReply` JSON to the judge

---

### Why `turn_text_start` Comes First

All streaming events — `turn_status` (pipeline step badges) and `research_chunk` (live evidence tokens) — fire **during** `build_turn`. The frontend looks up an existing card by `slug:phase` key to attach these events. If the card does not exist yet, the events are silently discarded.

Publishing `turn_text_start` before `build_turn` ensures the card exists from the very first moment the debater starts thinking, so every subsequent event lands visibly.

---

### Streaming Callbacks

```python
async def _on_status(label: str) -> None:
    await _publish_to_room(ctx.room, {
        "type": "turn_status",
        "slug": slug, "name": name,
        "phase": current_phase, "status": label,
    })

async def _on_research_chunk(text: str) -> None:
    await _publish_to_room(ctx.room, {
        "type": "research_chunk",
        "slug": slug, "name": name,
        "phase": current_phase, "text": text,
    })
```

Both are closures over `ctx.room`, `slug`, `name`, and `current_phase` — values captured at call time. They are passed to `build_turn` which fires them at the right moments.

---

### Text Streaming

```python
sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(spoken) if s.strip()]
for sentence in sentences:
    await _publish_to_room(ctx.room, {
        "type": "turn_text_chunk", ..., "text": sentence + " ",
    })
    await asyncio.sleep(0.05)  # 50 ms typewriter pacing
await _publish_to_room(ctx.room, {"type": "turn_text_end", ...})
```

After `build_turn` returns, the full spoken text is split on sentence boundaries and published 50ms per sentence. The entire text appears in the browser within ~500ms (a 10-sentence argument takes ~0.5s). TTS audio starts immediately after — so the user reads the text, then hears it spoken.

This is intentionally sequential (not concurrent with TTS). Trying to pace text in sync with live audio proved unreliable because `wait_for_playout()` does not cooperate with the asyncio event loop consistently enough for accurate interleaving.

---

### Error Handling

If `build_turn` raises:

```python
except Exception as exc:
    # Clean up the pending card
    await _publish_to_room(ctx.room, {"type": "turn_text_end", ...})
    raise rtc.RpcError(code=500, message=f"generation failed: {exc}") from exc
```

A `turn_text_end` is published to remove the typing cursor from the empty card, preventing a stuck spinner in the UI. The judge's RPC caller sees `RpcError(500)` and announces the forfeit.

### `session.say()` and `wait_for_playout()`

```python
handle = await session.say(spoken, allow_interruptions=False)
try:
    await handle.wait_for_playout()
except Exception as exc:
    logger.warning("debater[%s] wait_for_playout error: %s", slug, exc)
```

`session.say()` enqueues the text for TTS synthesis and streams the audio to the room. It returns immediately with a *handle* object — the audio has not finished playing yet.

`handle.wait_for_playout()` blocks until the audio has fully played through the room's speakers. This is the mechanism that gives the judge clean turn boundaries — the RPC call does not return until the debater has finished speaking.

**Why block until playout?** If the judge called the next debater before the first one finished speaking, both would speak simultaneously. The sequential `wait_for_playout()` ensures one debater speaks at a time.

### Building and Returning `TurnReply`

```python
reply = TurnReply(
    text=spoken,
    key_claims=argument.key_claims,
    citations=citations,
    fact_check=fact_check,
    target_slug=target_slug,
)
return reply.model_dump_json()
```

`model_dump_json()` serialises the `TurnReply` to a JSON string. RPC responses must be strings. The judge deserialises it with `TurnReply.model_validate_json(reply_json)`.

---

## `_publish_to_room()` — The Debater's Event Publisher

```python
async def _publish_to_room(room: rtc.Room, event: dict) -> None:
    try:
        await room.local_participant.publish_data(
            json.dumps(event).encode("utf-8"),
            reliable=True,
            topic="debate.event",
        )
    except Exception as exc:
        logger.warning("debater: publish_to_room failed (event=%s): %s",
                       event.get("type"), exc)
```

Debater agents publish their own streaming events directly to the room using the same `"debate.event"` topic as the judge. This means the browser's `DataReceived` handler sees both debater and judge events through a single `handleEvent()` dispatcher.

Failures are logged at WARNING (not DEBUG) and swallowed — a broken publish never kills the turn pipeline.

---

## `DebateMemory` Per Job

```python
debate_memory = DebateMemory(my_slug=slug)
```

Created once in `entrypoint()` before RPC registration. The same instance is passed to every `build_turn()` call throughout the job, accumulating episodic context across all turns. Each debater job has its own isolated `DebateMemory` — they do not share a vector index. See [memory.py](../src/memory.py).

---

## The Keepalive Loop

```python
logger.info("debater[%s] ready, waiting for RPC calls", slug)
try:
    await asyncio.Future()
except asyncio.CancelledError:
    logger.info("debater[%s] shutting down", slug)
    raise
```

`await asyncio.Future()` suspends the `entrypoint` coroutine indefinitely. It never completes unless:
1. The coroutine is cancelled externally (e.g. the job is cancelled because the worker is shutting down)
2. The LiveKit room evicts the participant (the judge calls `_remove_participant()`)

When the judge evicts the debater, LiveKit cancels the job's coroutine. The `except asyncio.CancelledError: raise` pattern is the Python convention for *not* swallowing cancellation — the coroutine properly terminates instead of silently ignoring the cancel.

**Why `await asyncio.Future()` instead of a loop?**

A common pattern is `while True: await asyncio.sleep(1)` — but this wastes CPU checking every second and adds unnecessary complexity. `asyncio.Future()` that is never resolved suspends cleanly with zero overhead until cancelled.

---

## Worker Registration

```python
if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            request_fnc=on_request,
            agent_name="debater",
            port=int(os.environ.get("DEBATER_HEALTH_PORT", "8081")),
        )
    )
```

The debater worker port is 8081, distinct from the judge's 8082, because both run on the same host and the health probe HTTP server would conflict on the same port.

Run with: `python -m src.debater_agent start`

---

## Concurrency: Multiple Jobs in One Process

If three debaters are dispatched simultaneously, the debater worker runs three concurrent `entrypoint` coroutines, each with its own:
- `session` (separate TTS channel, separate audio track in the room)
- `rpc_lock` (per-job, not shared)
- `slug`, `name`, `stance`, `topic` (local variables, not shared)

They share one `genai.Client` singleton (via `get_client()` with `@lru_cache`), which is safe because `aio` (async) API calls are coroutine-safe.

The three debaters each have their own audio track in the room. Listeners can hear whichever is speaking. Since the judge calls them sequentially and waits for `wait_for_playout()` before calling the next, they effectively speak in serial order.
