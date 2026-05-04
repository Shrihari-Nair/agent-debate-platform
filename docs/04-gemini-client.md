# Gemini Client — The Two-Phase Pipeline

All AI calls in this project go through one file: [src/gemini_client.py](../src/gemini_client.py). This document explains why the two-phase pattern exists, what each function does, and how to reason about it.

---

## The Core Problem: An SDK Limitation

Google Gemini's SDK (as of Gemini 2.5 Flash) has a hard constraint:

> **You cannot use Google Search grounding AND structured JSON output (`response_schema`) in the same API call.**

These are two of the most useful Gemini features:
- **Google Search grounding** — Gemini automatically searches the web, finds relevant pages, and references them in its answer. You get live, cited evidence.
- **Structured output** (`response_schema`) — Gemini returns valid JSON that matches a schema you specify. Instead of free text, you get a validated Python object.

But you cannot ask for both at once. This is tracked as [python-genai#58](https://github.com/google-gemini/python-sdk/issues/58).

### Solution: Two-Phase Pipeline

Every AI operation in this system runs as **two consecutive API calls**:

```
Phase 1 — Grounded call
  Input:  a prompt asking for evidence
  Config: GoogleSearch tool enabled
  Output: free-text evidence + list of web citations

                    ↓

Phase 2 — Structured call
  Input:  a new prompt that includes the Phase 1 evidence
  Config: response_schema=<Pydantic class>
  Output: a validated Pydantic instance
```

Phase 1 does the research. Phase 2 does the reasoning and returns structured data. This pattern appears in both argument generation and fact-checking.

---

## File Structure

```python
# src/gemini_client.py

from __future__ import annotations
import logging
from functools import lru_cache
from google import genai
from google.genai import types
from .config import settings
from .schemas import Source
```

The file imports:
- `genai` — the Google Gemini Python SDK
- `types` — Gemini SDK configuration types (tools, config objects)
- `settings` — the loaded `.env` config (API key, model name)
- `Source` — the Pydantic model for a web citation

---

## Singleton Client: `get_client()`

```python
@lru_cache(maxsize=1)
def get_client() -> genai.Client:
    if not settings.gemini_api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Get one from https://aistudio.google.com/apikey."
        )
    return genai.Client(api_key=settings.gemini_api_key)
```

### What is `@lru_cache(maxsize=1)`?

`lru_cache` is a Python standard library decorator that memoises a function's return value. With `maxsize=1`, it caches exactly one result — the first call creates a `genai.Client` and stores it; every subsequent call returns the same instance without creating a new one.

### Why a singleton?

Creating a `genai.Client` initialises an HTTP session. If you created a new client on every API call:
- You'd pay the connection setup cost for every call
- You'd leak file descriptors if clients aren't properly closed
- You'd lose any connection pooling benefits

The singleton pattern means all agents in the process share one client.

### Why raise, not silently return `None`?

Missing API keys fail immediately with a clear error message instead of silently making calls that fail with cryptic auth errors.

---

## The Google Search Tool Helper

```python
def _google_search_tool() -> types.Tool:
    return types.Tool(google_search=types.GoogleSearch())
```

This creates the Gemini tool object that enables web search. It is constructed fresh each call (it is a lightweight config object, not a connection) and passed to `grounded_generate`. The underscore prefix (`_`) indicates it is a private helper not intended for import outside this module.

---

## Citation Extraction: `extract_sources()`

```python
def extract_sources(response: types.GenerateContentResponse) -> list[Source]:
    """Pull `grounding_metadata.grounding_chunks` out of a grounded response."""
    if not response.candidates:
        return []
    meta = response.candidates[0].grounding_metadata
    if not meta or not meta.grounding_chunks:
        return []
    sources: list[Source] = []
    for chunk in meta.grounding_chunks:
        web = getattr(chunk, "web", None)
        if web and web.uri:
            sources.append(Source(title=web.title or "", uri=web.uri))
    return sources
```

When Gemini uses Google Search, the response includes metadata about which pages it accessed. This metadata is in `response.candidates[0].grounding_metadata.grounding_chunks`. Each chunk may have a `.web` attribute with `.title` and `.uri`.

This function walks that structure and converts each chunk into a `Source` Pydantic object. The defensive checks (`if not response.candidates`, `if not meta`) handle the case where no search was performed or the response format changes.

---

## Formatting Citations: `format_sources_block()`

```python
def format_sources_block(sources: list[Source]) -> str:
    if not sources:
        return "(no sources retrieved)"
    return "\n".join(
        f"[{i + 1}] {s.title or '(untitled)'} — {s.uri}" for i, s in enumerate(sources)
    )
```

Converts a list of `Source` objects into a numbered text block for inclusion in prompts:

```
[1] Some Article Title — https://example.com/article
[2] Another Source — https://other.com/page
```

This is what Phase 2 prompts reference when they say "cite by number in your reasoning".

---

## Phase 1: `grounded_generate()`

```python
async def grounded_generate(prompt: str, temperature: float = 0.2) -> tuple[str, list[Source]]:
    """Phase 1: grounded free-text generation with Google Search.

    Returns (evidence_text, citations). Never mixes with function tools or
    `response_schema` — that's the whole reason this helper exists.
    """
    client = get_client()
    response = await client.aio.models.generate_content(
        model=settings.debate_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[_google_search_tool()],
            temperature=temperature,
        ),
    )
    text = response.text or ""
    sources = extract_sources(response)
    return text, sources
```

### Key points

- **`client.aio`** — this is the async interface to the Gemini SDK. All calls in this codebase are `async`. Without `.aio`, calls would block the event loop and freeze all other agents running in the same process.
- **`temperature=0.2`** — a low but non-zero temperature. Slightly creative output produces more varied research notes than temperature=0 (which would always give the same answer for the same prompt).
- **Return type** — returns `(text, sources)` where `text` is the free-text evidence and `sources` is the extracted list of `Source` objects.
- **No `response_schema`** — intentionally. Adding it here would break the Google Search grounding.

---

## Phase 2: `structured_generate()`

```python
async def structured_generate(
    prompt: str,
    schema: type,
    temperature: float = 0.0,
):
    """Phase 2: ungrounded, schema-constrained generation.

    `schema` is a Pydantic class; the SDK auto-converts it to Gemini's OpenAPI
    subset and returns an auto-validated instance on `response.parsed`.
    """
    client = get_client()
    response = await client.aio.models.generate_content(
        model=settings.debate_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=temperature,
        ),
    )
    parsed = response.parsed
    if parsed is None:
        logger.warning(
            "structured_generate: response.parsed was None, falling back to JSON text"
        )
        return schema.model_validate_json(response.text or "{}")
    return parsed
```

### Key points

- **`response_mime_type="application/json"`** — forces Gemini to output JSON.
- **`response_schema=schema`** — the Pydantic class (e.g. `Argument`, `FactCheck`, `FinalVerdict`). The SDK automatically converts the Pydantic model to an OpenAPI-subset JSON Schema and sends it to Gemini. Gemini generates JSON that matches it.
- **`response.parsed`** — the SDK's auto-parser reads the JSON and creates a validated Python instance of the Pydantic class. This is what you get back — not a string, but an actual `Argument(...)` or `FactCheck(...)` instance.
- **Fallback** — if `response.parsed` is `None` (rare, happens when SDK versions differ), we fall back to parsing `response.text` as JSON manually with `schema.model_validate_json()`.
- **`temperature=0.0`** — zero temperature for deterministic, structured output. When you have a strict schema, creativity is not wanted.
- **No `tools=`** — no Google Search. Adding it would break structured output.

---

## How the Two Phases Work Together

Here is the full flow for argument generation as an example:

```python
# Called in argument_generator.py

# Phase 1 — research
evidence_text, sources = await grounded_generate(
    ground_topic_prompt(topic, stance, phase, last_opponent_text),
    temperature=0.2,
)
# evidence_text is like:
#   "According to Nature (2024), AI systems have achieved X% accuracy on Y..."
# sources is like:
#   [Source(title="Nature Article", uri="https://nature.com/..."), ...]

# Phase 2 — compose argument using the evidence
argument: Argument = await structured_generate(
    compose_argument_prompt(
        topic=topic,
        evidence=evidence_text,
        sources_block=format_sources_block(sources),
        ...
    ),
    schema=Argument,
    temperature=0.6,
)
# argument is an Argument instance:
#   argument.text = "Today I argue that..."
#   argument.key_claims = ["AI accuracy reached X% in 2024 per Nature", ...]
```

The Phase 2 prompt includes the Phase 1 evidence in the prompt body (not via the `tools` mechanism). Gemini does not need to search again — it composes the structured output using the pre-fetched evidence.

---

## Temperature Guide

| Function | Call type | Temperature | Reason |
|---|---|---|---|
| `grounded_generate` (argument research) | Phase 1 | 0.2 | Slightly creative research notes |
| `grounded_generate` (claim research) | Phase 1 | 0.1 | More conservative fact-finding |
| `structured_generate` (argument composition) | Phase 2 | 0.6 | Creative, varied spoken text |
| `structured_generate` (fact-check judgment) | Phase 2 | 0.0 | Strictly deterministic verdict |
| `structured_generate` (final verdict) | Phase 2 | 0.2 | Slight variation in rationale phrasing |

---

## What Happens on API Errors

`grounded_generate` and `structured_generate` do not catch exceptions — they let them propagate. The callers (`fact_checker.py`, `argument_generator.py`) are responsible for handling errors:

- `fact_checker.py` catches all exceptions and returns `FactCheck(verdict="UNVERIFIABLE", confidence=0.0, ...)` — a safe fallback.
- `argument_generator.py` lets exceptions propagate, which causes the debater's RPC handler to return an `RpcError(500)`, which the judge catches and announces as a forfeit.

This means a Gemini API error during argument generation = the debater forfeits the turn. A Gemini API error during fact-checking = the check is silently skipped (verdict is `UNVERIFIABLE`).
