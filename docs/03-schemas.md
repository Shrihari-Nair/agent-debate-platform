# Schemas — Every Pydantic Model Explained

All Pydantic models live in a single file: [src/schemas.py](../src/schemas.py). This document explains every model field by field, with annotations.

---

## What Is a Pydantic Model?

A Pydantic model is a Python class that describes the *shape* of data — which fields exist, what types they must have, and what constraints apply (minimum length, maximum value, etc.). When you create an instance of a Pydantic model, it validates the data automatically and raises a `ValidationError` if anything is wrong.

```python
from pydantic import BaseModel, Field

class Person(BaseModel):
    name: str = Field(min_length=1)
    age: int = Field(ge=0, le=150)  # ge = >=, le = <=

# This works:
p = Person(name="Alice", age=30)

# This raises ValidationError:
p = Person(name="", age=-5)
```

In this codebase, Pydantic models serve *two distinct purposes*:

1. **Wire-format validation** — when the browser sends a `POST /debate` request, or when the judge sends a `TurnRequest` over RPC, Pydantic validates the incoming data.
2. **Gemini structured-output schemas** — Gemini's SDK accepts a Pydantic class as `response_schema` and returns a validated instance. This is how the system forces Gemini to return structured JSON instead of free text.

---

## Module Header

```python
"""Pydantic models shared across orchestrator, workers, and RPC payloads.

Two roles for these models:
1. Wire-format validation for HTTP requests (orchestrator) and RPC payloads
   (debate.speak_turn).
2. Gemini structured-output schemas — Pydantic classes are passed directly as
   `response_schema` so the SDK converts them to Gemini's OpenAPI subset.
"""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field

DEFAULT_PHASES: list[str] = ["opening", "rebuttal_1", "rebuttal_2", "closing"]
```

`DEFAULT_PHASES` is not a model — it is a plain Python list used as the default list of debate phases. It appears both in `orchestrator.py` (when creating a `DebateConfig`) and in `judge_agent.py` (as a fallback if the dispatch metadata has no `phases` field).

---

## `Verdict` — The Fact-Check Result Type

```python
Verdict = Literal[
    "SUPPORTED",
    "PARTIALLY_SUPPORTED",
    "UNSUPPORTED",
    "CONTRADICTED",
    "UNVERIFIABLE",
]
```

This is not a model — it is a *type alias*. A `Literal` type means the value must be exactly one of the listed strings, nothing else.

| Value | Meaning |
|---|---|
| `SUPPORTED` | Every specific factual part of the claim is corroborated by evidence |
| `PARTIALLY_SUPPORTED` | Some parts are correct, some are not |
| `UNSUPPORTED` | No relevant evidence found either way |
| `CONTRADICTED` | Evidence directly refutes a specific concrete fact in the claim — this is the hallucination flag |
| `UNVERIFIABLE` | Opinion, prediction, or rhetorical claim — not checkable as fact |

Only `CONTRADICTED` with high confidence triggers disqualification.

---

## `Source` — A Web Citation

```python
class Source(BaseModel):
    title: str = ""
    uri: str
```

Represents one web page that Gemini found via Google Search grounding. Extracted from `grounding_metadata.grounding_chunks` in the Gemini response.

- `title` — the page title (may be empty, defaults to `""`).
- `uri` — the URL of the source (always present when a source is returned by Gemini).

Sources are stored in `TranscriptEntry.citations`, `FactCheck.citations`, and passed to the browser for display as clickable links.

---

## `DebaterSpec` — One Debater's Configuration

```python
class DebaterSpec(BaseModel):
    slug: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        description="Lower-case slug used as identity suffix (debater-<slug>).",
    )
    name: str = Field(min_length=1, max_length=64)
    stance: str = Field(
        min_length=3,
        max_length=500,
        description="One-sentence position this debater will argue for.",
    )
```

- `slug` — a short, URL-safe identifier for this debater. Must start with a lowercase letter or digit and contain only `a-z`, `0-9`, `_`, `-`. Used to form the LiveKit participant identity `debater-<slug>` and to route RPC calls.
- `name` — the debater's display name, spoken by the judge and shown in the browser.
- `stance` — the one-sentence position this debater must argue for. Sent to the debater agent via dispatch metadata. **Must be at least 3 characters** — this constraint caused the bug described in [README.md](../README.md) when `stance="—"` (an em-dash, 1 character) was used as a fallback.

**Important:** This model is used by:
1. The browser → orchestrator HTTP request (`CreateDebateRequest`)
2. The orchestrator → judge dispatch metadata (`DebateConfig`)
3. The judge internally to track who is alive and look up names

---

## `DebateConfig` — The Full Debate Definition

```python
class DebateConfig(BaseModel):
    topic: str = Field(min_length=3, max_length=500)
    debaters: list[DebaterSpec] = Field(min_length=2, max_length=4)
    phases: list[str] = Field(default_factory=lambda: list(DEFAULT_PHASES))
```

Created by the orchestrator and serialised as JSON into the judge's dispatch metadata. The judge deserialises this to know what topic to debate, who the debaters are, and in what order to run the phases.

- `topic` — the debate topic string (3–500 characters).
- `debaters` — 2 to 4 `DebaterSpec` objects.
- `phases` — ordered list of phase names. Defaults to `["opening", "rebuttal_1", "rebuttal_2", "closing"]`.

---

## `CreateDebateRequest` — Browser → Orchestrator

```python
class CreateDebateRequest(BaseModel):
    topic: str
    debaters: list[DebaterSpec]
```

The body of the `POST /debate` HTTP request from the browser. Intentionally simple — no phase list (the orchestrator supplies defaults). Pydantic validates it on arrival at the FastAPI endpoint.

---

## `CreateDebateResponse` — Orchestrator → Browser

```python
class CreateDebateResponse(BaseModel):
    room: str
    ws_url: str
    observer_token: str
    observer_identity: str
    debate: DebateConfig
```

Returned by `POST /debate`. Contains everything the browser needs to connect to the room:

- `room` — the room name (e.g. `"debate-8def33ea"`)
- `ws_url` — the LiveKit WebSocket URL to connect to
- `observer_token` — a signed JWT the browser uses to join the room as a read-only observer
- `observer_identity` — a unique observer identity string (e.g. `"observer-a3f9c2b1"`)
- `debate` — the full `DebateConfig` (echoed back so the browser knows the canonical config)

---

## `TranscriptEntry` — One Spoken Turn

```python
class TranscriptEntry(BaseModel):
    slug: str
    name: str
    phase: str
    text: str
    key_claims: list[str] = Field(default_factory=list)
    citations: list[Source] = Field(default_factory=list)
```

Represents one debater's complete turn. Appended to the judge's `transcript` list as each turn completes.

- `slug` — which debater spoke
- `name` — debater's display name
- `phase` — which debate phase this was spoken in
- `text` — the full spoken text (including any fact-check callout)
- `key_claims` — list of atomic factual claims the debater made (extracted by Gemini as part of the `Argument` model). These are what other debaters fact-check.
- `citations` — web sources from argument generation

`TranscriptEntry` objects are serialised as data packets (type `"turn_spoken"`) for the browser, and are also sent back to debaters in the `TurnRequest` so they have context for their next turn.

---

## `Argument` — Gemini Output for Argument Generation

```python
class Argument(BaseModel):
    """Gemini structured-output for a debater's argument (phase 2 of two-phase)."""

    text: str = Field(description="Spoken argument, 40-90 seconds when read aloud.")
    key_claims: list[str] = Field(
        description="Atomic factual claims in `text` that a fact-checker should verify.",
        max_length=6,
    )
```

This model is the **Gemini output schema** for argument generation (Phase 2 of the two-phase pipeline). Gemini is asked to produce JSON that matches this structure, and the SDK returns a validated `Argument` instance.

- `text` — the actual spoken argument text, 100–220 words.
- `key_claims` — up to 6 specific, checkable factual claims the debater made. Each should be a single sentence with a concrete number, date, or named entity. These are what opponents will fact-check.

**How it is used:** `_generate_argument_core()` in `argument_generator.py` calls `structured_generate(prompt, schema=Argument)`. The result is an `Argument` instance. The `text` becomes the spoken output; `key_claims` is stored in the `TranscriptEntry`.

---

## `FactCheck` — Gemini Output for Fact-Checking

```python
class FactCheck(BaseModel):
    """Gemini structured-output for a per-claim verdict."""

    claim: str
    verdict: Verdict
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_summary: str
    citations: list[Source] = Field(default_factory=list)
```

The **Gemini output schema** for fact-checking (Phase 2 of fact-checker's two-phase pipeline).

- `claim` — echoes back the claim that was checked (Gemini sometimes omits this, so `fact_checker.py` forces it to the original).
- `verdict` — one of the 5 `Verdict` values.
- `confidence` — how confident Gemini is in the verdict, from 0.0 to 1.0.
- `evidence_summary` — a human-readable summary of the web evidence found. This is included in the debater's spoken callout and in the judge's disqualification ruling.
- `citations` — web sources found during fact-checking.

**Disqualification threshold:** The judge compares `confidence` against `settings.factcheck_hallucination_threshold` (default 0.8). Only `CONTRADICTED` verdicts above the threshold cause disqualification.

---

## `TurnRequest` — Judge → Debater RPC Payload

```python
class TurnRequest(BaseModel):
    """Judge -> debater RPC payload for debate.speak_turn."""

    phase: str
    topic: str
    my_slug: str
    my_stance: str
    transcript: list[TranscriptEntry] = Field(default_factory=list)
    last_opponent_text: str | None = None
    allow_fact_check: bool = True
    time_limit_s: int = 60
```

Serialised as JSON and sent as the payload of the `debate.speak_turn` RPC call from the judge to each debater.

- `phase` — the current phase (e.g. `"rebuttal_1"`).
- `topic` — the debate topic.
- `my_slug` — the debater's own slug (so the debater agent doesn't need to remember it between RPC calls).
- `my_stance` — the debater's stance string.
- `transcript` — a windowed, citation-stripped copy of the conversation so far (last 6 entries). Used for context.
- `last_opponent_text` — the text of the most recent opposing turn. Used in the research prompt so Gemini knows what to rebut.
- `allow_fact_check` — `False` for the opening phase (no opponent has spoken yet). `True` for all subsequent phases.
- `time_limit_s` — advisory time limit, currently 75 seconds in the judge's code.

**Note:** The transcript is stripped of citations before sending to stay under LiveKit's ~15KB RPC payload limit.

---

## `TurnReply` — Debater → Judge RPC Response

```python
class TurnReply(BaseModel):
    """Debater -> judge RPC response."""

    text: str
    key_claims: list[str] = Field(default_factory=list)
    citations: list[Source] = Field(default_factory=list)
    fact_check: FactCheck | None = None
    target_slug: str | None = None
```

Serialised as JSON and returned from the debater's `speak_turn` RPC handler.

- `text` — the full spoken text (argument + optional fact-check callout).
- `key_claims` — factual claims from the `Argument` model.
- `citations` — web sources from argument generation.
- `fact_check` — the debater's `FactCheck` result on an opponent's claim. `None` if no fact-check was performed (opening phase, or no suitable claim found).
- `target_slug` — which debater's claim was checked. `None` if no fact-check.

The judge reads this, appends a `TranscriptEntry`, and accumulates the `fact_check` for end-of-phase adjudication.

---

## `ScoreEntry` — One Debater's Final Score

```python
class ScoreEntry(BaseModel):
    slug: str
    score: float = Field(ge=0.0, le=1.0)
```

Used as part of `FinalVerdict`. Holds one debater's final argument quality score from 0.0 to 1.0.

---

## `FinalVerdict` — Gemini Output for the Judge's Ruling

```python
class FinalVerdict(BaseModel):
    """Gemini structured-output for the judge's final ruling."""

    winner_slug: str
    scores: list[ScoreEntry]
    rationale: str = Field(
        description="Plain-spoken verdict, 2-4 sentences, will be read aloud."
    )
```

The **Gemini output schema** for the final verdict. The judge calls `structured_generate(final_verdict_prompt(...), schema=FinalVerdict)`.

- `winner_slug` — the slug of the winning debater.
- `scores` — a `ScoreEntry` for each surviving debater.
- `rationale` — 2–4 sentences, written conversationally for TTS, explaining why this debater won. No markdown, no stage directions.

---

## How Models Chain Together

```
Browser POST /debate
    ↓ CreateDebateRequest (validated by Pydantic on entry)
    ↓ CreateDebateResponse (returned to browser)
    ↓
Orchestrator → Judge dispatch metadata
    ↓ DebateConfig (serialised as JSON into metadata)
    ↓
Judge parses metadata
    ↓ DebaterSpec[] (re-validated from dict)
    ↓
Judge → Debater RPC (each turn)
    ↓ TurnRequest (serialised to JSON string)
    ↓ TurnReply (deserialised from JSON string returned by debater)
    ↓
Debater → Gemini (argument generation)
    ↓ Argument (response_schema, returned by Gemini SDK as validated instance)
    ↓
Debater → Gemini (fact-checking, concurrent)
    ↓ FactCheck (response_schema, returned by Gemini SDK as validated instance)
    ↓
Judge → Gemini (final verdict)
    ↓ FinalVerdict (response_schema, returned by Gemini SDK as validated instance)
    ↓
Judge → runs/{room}.json
    ↓ All models serialised via .model_dump()
```

---

## Quick Reference

| Model | Source | Destination | Purpose |
|---|---|---|---|
| `CreateDebateRequest` | Browser | Orchestrator HTTP | Start a debate |
| `DebaterSpec` | Browser/Orchestrator | Judge metadata | One debater's config |
| `DebateConfig` | Orchestrator | Judge metadata | Full debate setup |
| `CreateDebateResponse` | Orchestrator | Browser | Room + token |
| `TurnRequest` | Judge | Debater (RPC) | "Take your turn" |
| `TurnReply` | Debater | Judge (RPC) | Spoken text + fact-check |
| `TranscriptEntry` | Judge | Transcript list, browser | Record of one turn |
| `Argument` | Gemini → Debater | TurnReply | Structured argument output |
| `FactCheck` | Gemini → Debater | TurnReply, adjudication | Claim verification result |
| `FinalVerdict` | Gemini → Judge | Announcement + persist | Winner + rationale |
| `ScoreEntry` | Part of FinalVerdict | — | One debater's score |
| `Source` | Gemini grounding | Everywhere | Web citation |
