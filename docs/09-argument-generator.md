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

## Callback Type Aliases

```python
StatusCallback = Callable[[str], Awaitable[None]] | None
ChunkCallback  = Callable[[str], Awaitable[None]] | None
```

These are module-level type aliases used in `build_turn` and `_generate_argument_core` signatures. They represent optional async callbacks:

- **`StatusCallback`** — called with a short label string at each pipeline step boundary (e.g. `"researching"`, `"reflecting"`). Used by the debater agent to publish `turn_status` events to the frontend.
- **`ChunkCallback`** — called with batches of raw Phase-1 token text as they arrive from the Gemini streaming API. Used to publish `research_chunk` events to the frontend in real time.

All callbacks degrade gracefully: exceptions inside them are caught and logged so a broken observer never kills the pipeline.

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
    strategy: DebateStrategy | None = None,
    on_research_chunk: ChunkCallback = None,
) -> tuple[Argument, list[Source], str]:  # returns (argument, sources, evidence_text)
```

The third return value `evidence_text` is now threaded back to the caller. It is used by the reflection critique to score `factual_density` accurately, and by the `_verify_claims_against_evidence()` post-audit pass.

### Phase 1: Research (with optional streaming)

```python
if on_research_chunk is not None:
    evidence, sources = await grounded_generate_stream(
        grounding_prompt,
        temperature=0.2,
        on_chunk=on_research_chunk,
    )
else:
    evidence, sources = await grounded_generate(grounding_prompt, temperature=0.2)
```

When `on_research_chunk` is provided, Phase 1 uses `grounded_generate_stream()` so the browser receives research tokens in real time. When it is `None`, it falls back to the standard non-streaming call — identical behaviour, no performance difference.

### Phase 2: Compose

Unchanged — `structured_generate(schema=Argument)` uses the Phase 1 evidence as context. Phase 2 cannot stream (JSON must be complete before parsing).

### Claim Verification

After Phase 2, `_verify_claims_against_evidence()` audits the key claims against the evidence text using a `VerifiedClaims` schema call. Any claim not traceable word-for-word to the evidence is removed and logged as a WARNING. This prevents hallucinated statistics from reaching the TTS output.

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
    memory: DebateMemory | None = None,
    on_status: StatusCallback = None,
    on_research_chunk: ChunkCallback = None,
) -> tuple[str, Argument, list[Source], FactCheck | None, str | None]:
```

`build_turn` is a 5-step pipeline. All advanced features degrade gracefully: if any step raises an exception it is caught, logged at WARNING, and execution continues with the previous step's output.

The two optional callback parameters enable real-time frontend streaming:
- `on_status` — called at each step boundary with a label string
- `on_research_chunk` — called with Phase-1 token batches as they stream from Gemini

---

### Step 1: Memory Retrieval

```python
if on_status: await on_status("retrieving_memory")
memory_context = memory.retrieve(f"{phase} {last_opponent_text or ''}")
```

Retrieves up to 4 semantically similar episodes from FAISS episodic memory. These are injected into the planner as context. Skipped if `memory` is `None`.

---

### Step 2: Planner

```python
if on_status: await on_status("planning_strategy")
strategy = await run_planner(topic, stance, phase, my_slug, transcript,
                             last_opponent_text, memory_context)
```

Calls the LangGraph Plan-and-Execute graph to produce a `DebateStrategy`: `primary_attack`, `evidence_keywords`, `rhetorical_angle`, `claims_to_make`, `opponent_weak_points`. The `evidence_keywords` are injected into the Phase 1 research prompt so Gemini searches for the most relevant evidence. See [planner.py](../src/planner.py).

---

### Step 3: Core Argument + Fact-Check (concurrent)

```python
if on_status: await on_status("researching")
argument_task = asyncio.create_task(
    _generate_argument_core(..., on_research_chunk=on_research_chunk)
)
if target is not None:
    check_task = asyncio.create_task(fact_check_claim(claim_text))

argument, arg_sources, evidence = await argument_task
if on_status: await on_status("composing_argument")
```

Argument generation and fact-checking run concurrently (see concurrency pattern above). The third return value `evidence` is kept for the reflection and verification steps. `on_research_chunk` is passed through so Phase-1 tokens stream to the browser in real time.

---

### Step 4: Reflection

```python
if on_status: await on_status("reflecting")
argument = await reflect_on_argument(
    argument, phase=phase, stance=stance,
    last_opponent_text=last_opponent_text, evidence=evidence,
)
```

Runs the LangGraph SELF-REFINE loop (up to 2 revision cycles). Skipped automatically for the opening phase. The `evidence` string is passed so the critique can accurately score `factual_density`. See [reflection.py](../src/reflection.py).

---

### Step 5: Memory Store

```python
if on_status: await on_status("speaking")
memory.store(TranscriptEntry(slug=my_slug, ...))
if last_opponent_text:
    memory.store(TranscriptEntry(slug="opponent", ...))
memory.compress_older_phases(phase)
```

Persists both the debater's own turn and the opponent's last turn into FAISS episodic memory. Older phases are compressed to a single summary to keep the index manageable. See [memory.py](../src/memory.py).

---

### Spoken Output

```python
spoken = argument.text.strip()
if fact_check is not None:
    spoken = spoken + "\n\n" + format_fact_check_callout(fact_check)
```

The spoken text is the argument text, optionally followed by the fact-check callout. The `\n\n` separator gives TTS a natural pause between the argument and the callout.

`format_fact_check_callout()` is a pure template function (no LLM) that converts the `FactCheck` into a spoken callout phrase. See [05-prompts.md](./05-prompts.md) for the templates.

---

## Time Budget

A typical turn takes approximately:

| Step | Duration | Notes |
|---|---|---|
| Memory retrieval (Step 1) | ~0.5–2s | FAISS similarity search |
| Planner (Step 2) | ~3–8s | LangGraph + ChatGoogleGenerativeAI |
| Phase 1 argument research (Step 3) | ~15–30s | Google Search + Gemini streaming |
| Phase 1 fact-check research (Step 3) | ~10–25s | Runs concurrently with above |
| Phase 2 argument composition (Step 3) | ~5–15s | Schema-constrained, no search |
| Phase 2 fact-check judgment (Step 3) | ~3–8s | Temperature=0, fast |
| Reflection (Step 4) | ~5–15s | 0–2 critique+revise cycles |
| Total concurrent | ~35–60s | Steps 3–4 dominate |

Then the debater agent adds:
- Text streaming to browser: ~0.5s (50ms per sentence, done before TTS)
- TTS synthesis: ~3–10s
- TTS audio playback: ~20–60s (100–220 word argument at ~130 WPM)

Total round-trip per turn: **~60–130 seconds**. The `PER_TURN_RPC_TIMEOUT_S = 240` gives a comfortable safety margin.
