"""Two-phase Gemini fact-checker used by the judge agent.

Takes a single atomic claim, does a fresh grounded web search to gather
evidence, then runs a deterministic schema-constrained judgment call.

`judge_claim` is safe to `asyncio.gather` over a whole turn's claims — they
share the same Gemini client and respect free-tier rate limits only via API
error propagation; callers should throttle upstream if running many in
parallel (judge_agent applies a Semaphore).
"""

from __future__ import annotations

import logging

from .gemini_client import format_sources_block, grounded_generate, structured_generate
from .prompts import ground_claim_prompt, judge_claim_prompt
from .schemas import FactCheck

logger = logging.getLogger(__name__)


async def fact_check_claim(claim: str) -> FactCheck:
    """Return a schema-validated `FactCheck` for a single claim.

    On API error (rate limit, etc.) we return an UNVERIFIABLE verdict with
    confidence 0.0 rather than raising — a judge crash shouldn't take down
    the whole debate over a transient Gemini hiccup.
    """
    try:
        evidence, sources = await grounded_generate(
            ground_claim_prompt(claim), temperature=0.1
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("fact-check phase-1 failed: %s", exc)
        return FactCheck(
            claim=claim,
            verdict="UNVERIFIABLE",
            confidence=0.0,
            evidence_summary=f"Fact-check unavailable: {exc}",
            citations=[],
        )

    try:
        result = await structured_generate(
            judge_claim_prompt(claim, evidence or "(no evidence retrieved)",
                               format_sources_block(sources)),
            schema=FactCheck,
            temperature=0.0,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("fact-check phase-2 failed: %s", exc)
        return FactCheck(
            claim=claim,
            verdict="UNVERIFIABLE",
            confidence=0.0,
            evidence_summary=f"Judgment unavailable: {exc}",
            citations=sources,
        )

    # The model sometimes omits the claim echo; force it to the original.
    result.claim = claim
    if not result.citations:
        result.citations = sources
    return result
