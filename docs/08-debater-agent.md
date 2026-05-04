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
    caller = data.caller_identity
    logger.info("debater[%s] speak_turn invoked by %s", slug, caller)

    async with rpc_lock:
        try:
            req = TurnRequest.model_validate_json(data.payload)
        except Exception as exc:
            raise rtc.RpcError(
                code=400, message=f"invalid TurnRequest payload: {exc}"
            ) from exc

        try:
            spoken, argument, citations, fact_check, target_slug = await build_turn(
                topic=req.topic or topic,
                stance=req.my_stance or stance,
                phase=req.phase,
                my_slug=req.my_slug or slug,
                transcript=req.transcript,
                last_opponent_text=req.last_opponent_text,
                allow_fact_check=req.allow_fact_check,
            )
        except Exception as exc:
            logger.exception("debater[%s] argument generation failed", slug)
            raise rtc.RpcError(code=500, message=f"generation failed: {exc}") from exc

        handle = await session.say(spoken, allow_interruptions=False)
        try:
            await handle.wait_for_playout()
        except Exception as exc:
            logger.warning("debater[%s] wait_for_playout error: %s", slug, exc)

        reply = TurnReply(
            text=spoken,
            key_claims=argument.key_claims,
            citations=citations,
            fact_check=fact_check,
            target_slug=target_slug,
        )
        return reply.model_dump_json()
```

This is the most important function in the debater agent. Let's trace through it step by step.

### `@ctx.room.local_participant.register_rpc_method("debate.speak_turn")`

This decorator registers `speak_turn` as the handler for incoming RPC calls with the method name `"debate.speak_turn"`. When the judge calls `perform_rpc(method="debate.speak_turn", ...)`, this function runs.

### `rpc_lock = asyncio.Lock()`

An `asyncio.Lock` ensures this function runs one call at a time. Although the judge should not call a debater twice simultaneously, this lock prevents any edge case where concurrent RPC calls could overlap.

What is an `asyncio.Lock`? It is a mutual exclusion mechanism for async code. `async with rpc_lock:` acquires the lock when entering the block and releases it when exiting. If a second caller tries to acquire the lock while the first is inside, it waits.

### `TurnRequest.model_validate_json(data.payload)`

Deserialises the JSON string from the RPC payload into a `TurnRequest` Pydantic object. If the JSON is invalid or missing required fields, raises an `rtc.RpcError(code=400)` — the judge sees this as an RPC failure and forfeits the turn.

### `await build_turn(...)`

Delegates all the actual intelligence to `argument_generator.build_turn()`. See [09-argument-generator.md](./09-argument-generator.md) for the full breakdown. Returns 5 values:
- `spoken` — the full text to say (argument + optional fact-check callout)
- `argument` — the `Argument` Pydantic object
- `citations` — web sources
- `fact_check` — the `FactCheck` result (or `None`)
- `target_slug` — which debater was fact-checked (or `None`)

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
