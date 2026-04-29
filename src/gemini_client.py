"""Shared async `google.genai` client + helpers.

Why this exists: the LiveKit Google plugin's `GoogleSearch` tool cannot be
combined with a `response_schema` or custom function tools on Gemini 2.5 Flash
in a single call (`python-genai#58`). The entire debate system is therefore
built on a two-phase pattern: a grounded call for evidence, then an ungrounded
schema-constrained call for structured output. Both phases go through the
`google.genai.Client.aio` async API we expose here.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from google import genai
from google.genai import types

from .config import settings
from .schemas import Source

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_client() -> genai.Client:
    if not settings.gemini_api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Get one from https://aistudio.google.com/apikey."
        )
    return genai.Client(api_key=settings.gemini_api_key)


def _google_search_tool() -> types.Tool:
    return types.Tool(google_search=types.GoogleSearch())


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


def format_sources_block(sources: list[Source]) -> str:
    if not sources:
        return "(no sources retrieved)"
    return "\n".join(
        f"[{i + 1}] {s.title or '(untitled)'} — {s.uri}" for i, s in enumerate(sources)
    )


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
        # Fall back to re-parsing the raw text; covers the rare case where the
        # SDK's auto-parser skips (e.g., schema changes between versions).
        logger.warning(
            "structured_generate: response.parsed was None, falling back to JSON text"
        )
        return schema.model_validate_json(response.text or "{}")
    return parsed
