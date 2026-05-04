# Argument Generator — Building a Turn

The argument generator lives in [src/argument_generator.py](../src/argument_generator.py). It is called once per debater per turn and produces everything a debater needs to speak: the argument text, the extracted factual claims, the web citations, and an optional fact-check of an opponent's claim.

---

## What a "Turn" Produces

A single call to `build_turn()` produces 5 things:

```
spoken_text    — the full utterance sent to TTS
               = argument text + (optional) fact-check callout

argument       — the structured Argument object (text + key_claims)

citations      — web sources from argument research

fact_check     — the FactCheck verdict on an opponent's claim (or None)

target_slug    — which opponent was fact-checked (or None)
```

---

## The Concurrency Pattern

The most important design in this file is that argument generation and fact-checking run **concurrently** (at the same time):

```python
argument_task = asyncio.create_task(_generate_argument_core(...))

check_task = None
if target is not None:
    check_task = asyncio.create_task(fact_check_claim(claim_text))

argument, arg_sources = await argument_task
# ... then collect fact_check from check_task
```

### What is `asyncio.create_task()`?

In Python's async model, a coroutine only runs when you `await` it. If you called them sequentially:

```python
argument = await _generate_argument_core(...)   # blocks ~30s
fact_check = await fact_check_claim(...)        # blocks ~25s
# total: ~55 seconds
```

By using `asyncio.create_task()`, both coroutines are submitted to the event loop immediately and run concurrently:

```python
task1 = asyncio.create_task(_generate_argument_core(...))   # starts immediately
task2 = asyncio.create_task(fact_check_claim(...))          # starts immediately

result1 = await task1   # wait for task1; task2 is running in background
result2 = await task2   # task2 may already be done
# total: ~max(30s, 25s) ≈ 30 seconds instead of 55
```

This is safe here because the two operations are completely independent — fact-checking uses a different Gemini call on a different piece of text.

---

## `_pick_opponent_claim()` — What to Fact-Check

```python
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
```

### What This Does

Scans the transcript from newest to oldest, looking at entries from other debaters (not `my_slug`). Returns the first non-empty `key_claim` it finds, along with the slug of who made it.

### Why Newest-First?

Fact-checking a recent claim is more rhetorically relevant than fact-checking an old one. If you check a claim from the opening statement in round 3, it feels disconnected from the current debate. Newest-first means you're always rebutting something your opponent said recently.

### Why Only One Claim?

Each debater checks exactly one claim per turn. This keeps the debate structured and prevents a "fact-check bombing" strategy where a debater spends their entire turn listing fact-checks instead of making arguments.

### Returns `None` When?

- Opening phase — `allow_fact_check=False` is set in `TurnRequest`, so `_pick_opponent_claim` is never called.
- No opponent has a `key_claims` list (can happen if Gemini returned an empty list, or if the transcript is empty).

---

## `_generate_argument_core()` — The Two-Phase Argument Pipeline

```python
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
```

### Phase 1: Research

```python
evidence, sources = await grounded_generate(
    ground_topic_prompt(topic, stance, phase, last_opponent_text),
    temperature=0.2,
)
```

- `ground_topic_prompt(...)` constructs the research prompt (see [05-prompts.md](./05-prompts.md)).
- `grounded_generate()` calls Gemini with Google Search enabled.
- Returns `evidence` (free text describing what was found) and `sources` (extracted `Source` objects with titles and URLs).

### Phase 2: Compose

```python
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
```

- `compose_argument_prompt(...)` builds the composition prompt, embedding the Phase 1 evidence.
- `structured_generate(schema=Argument)` calls Gemini with `response_schema=Argument`.
- Returns an `Argument` instance: `argument.text` (the spoken argument, 100–220 words) and `argument.key_claims` (up to 6 checkable facts).

### `_render_transcript()` — Formatting Context

```python
def _render_transcript(transcript: list[TranscriptEntry]) -> str:
    if not transcript:
        return "(no turns yet)"
    lines = []
    for entry in transcript:
        lines.append(f"[{entry.phase}] {entry.name} ({entry.slug}): {entry.text}")
    return "\n\n".join(lines)
```

Formats the transcript list into a readable string for the Phase 2 prompt. The format is `[phase] Name (slug): text` — including the phase label helps the LLM understand temporal context.

---

## `build_turn()` — The Full Turn Builder

```python
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
```

The `*` in the function signature means all arguments must be passed as keyword arguments (e.g. `build_turn(topic=..., stance=..., ...)`). This prevents subtle bugs from positional argument mismatches.

### Step 1: Pick a claim to fact-check

```python
target: tuple[str, str] | None = None
if allow_fact_check:
    target = _pick_opponent_claim(transcript, my_slug)
```

Only done if `allow_fact_check` is `True`. Returns `(claim_text, target_slug)` or `None`.

### Step 2: Launch concurrent tasks

```python
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
```

Both tasks are started. If there is no claim to check, `check_task` remains `None`.

### Step 3: Collect results

```python
argument, arg_sources = await argument_task
fact_check: FactCheck | None = None
target_slug: str | None = None
if check_task is not None and target is not None:
    try:
        fact_check = await check_task
        target_slug = target[1]
    except Exception as exc:
        logger.warning("fact_check side-task failed: %s", exc)
```

`await argument_task` gets the `Argument` and sources. Then `await check_task` (if it exists) gets the `FactCheck`. The `try/except` around `check_task` ensures that if fact-checking fails for any reason, the debate still gets the argument — the turn is not lost.

### Step 4: Compose spoken text

```python
spoken = argument.text.strip()
if fact_check is not None:
    spoken = spoken + "\n\n" + format_fact_check_callout(fact_check)
```

The spoken text is the argument text, optionally followed by the fact-check callout. The `\n\n` separator gives TTS a natural pause between the argument and the callout.

`format_fact_check_callout()` is a pure template function (no LLM) that converts the `FactCheck` into a spoken callout phrase. See [05-prompts.md](./05-prompts.md) for the templates.

### Step 5: Log and return

```python
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
```

The log line is diagnostic gold — you can see at a glance how many claims were extracted, what verdict the fact-check returned, and how long the spoken text is.

---

## Time Budget

A typical turn takes approximately:

| Step | Duration | Notes |
|---|---|---|
| Phase 1 argument research | ~15–30s | Google Search + Gemini generation |
| Phase 1 fact-check research | ~10–25s | Runs concurrently with above |
| Phase 2 argument composition | ~5–15s | Schema-constrained, no search |
| Phase 2 fact-check judgment | ~3–8s | Temperature=0, fast |
| Total concurrent | ~25–45s | Max of parallel paths + Phase 2s |

Then the debater agent adds:
- TTS synthesis: ~3–10s
- TTS audio playback: ~20–60s (100–220 word argument at ~130 WPM)

Total round-trip per turn: **~50–120 seconds**. The `PER_TURN_RPC_TIMEOUT_S = 240` gives a 2× safety margin.
