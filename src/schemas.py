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

Verdict = Literal[
    "SUPPORTED",
    "PARTIALLY_SUPPORTED",
    "UNSUPPORTED",
    "CONTRADICTED",
    "UNVERIFIABLE",
]


class Source(BaseModel):
    title: str = ""
    uri: str


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


class DebateConfig(BaseModel):
    topic: str = Field(min_length=3, max_length=500)
    debaters: list[DebaterSpec] = Field(min_length=2, max_length=4)
    phases: list[str] = Field(default_factory=lambda: list(DEFAULT_PHASES))


class CreateDebateRequest(BaseModel):
    topic: str
    debaters: list[DebaterSpec]


class CreateDebateResponse(BaseModel):
    room: str
    ws_url: str
    observer_token: str
    observer_identity: str
    debate: DebateConfig


class TranscriptEntry(BaseModel):
    slug: str
    name: str
    phase: str
    text: str
    key_claims: list[str] = Field(default_factory=list)
    citations: list[Source] = Field(default_factory=list)


class Argument(BaseModel):
    """Gemini structured-output for a debater's argument (phase 2 of two-phase)."""

    text: str = Field(description="Spoken argument, 40-90 seconds when read aloud.")
    key_claims: list[str] = Field(
        description="Atomic factual claims in `text` that a fact-checker should verify.",
        max_length=6,
    )


class FactCheck(BaseModel):
    """Gemini structured-output for a per-claim verdict.

    Produced by a debater against an opponent's claim during their turn; the
    judge aggregates these at end-of-phase for disqualification decisions.
    """

    claim: str
    verdict: Verdict
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_summary: str
    citations: list[Source] = Field(default_factory=list)


class TurnRequest(BaseModel):
    """Judge -> debater RPC payload for debate.speak_turn."""

    phase: str
    topic: str
    my_slug: str
    my_stance: str
    transcript: list[TranscriptEntry] = Field(default_factory=list)
    last_opponent_text: str | None = None
    # Opening phase: no peer to fact-check yet. Non-opening phases: debaters
    # are expected to fact-check one opponent claim as part of their turn.
    allow_fact_check: bool = True
    time_limit_s: int = 60


class TurnReply(BaseModel):
    """Debater -> judge RPC response.

    `fact_check` + `target_slug` carry the debater's own verdict on an
    opponent's recent claim. The judge accumulates these across the phase and
    evaluates them at the end of the round (never mid-phase).
    """

    text: str
    key_claims: list[str] = Field(default_factory=list)
    citations: list[Source] = Field(default_factory=list)
    fact_check: FactCheck | None = None
    target_slug: str | None = None


class ScoreEntry(BaseModel):
    slug: str
    score: float = Field(ge=0.0, le=1.0)


class FinalVerdict(BaseModel):
    """Gemini structured-output for the judge's final ruling."""

    winner_slug: str
    scores: list[ScoreEntry]
    rationale: str = Field(
        description="Plain-spoken verdict, 2-4 sentences, will be read aloud."
    )


# ---------------------------------------------------------------------------
# New schemas for Reflection Loop, Episodic Memory, and Plan-and-Execute
# ---------------------------------------------------------------------------


class ArgumentCritique(BaseModel):
    """LangChain/LangGraph structured output for the self-critique node.

    The reflection graph's critique node uses this as its `with_structured_output`
    target.  Scores are 0.0–1.0.  `should_revise` drives the conditional edge.
    """

    stance_adherence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "How strongly the argument commits to the debater's assigned stance. "
            "1.0 = never concedes; 0.0 = flip-flopped."
        ),
    )
    factual_density: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of the argument backed by concrete, specific evidence "
            "(numbers, dates, named entities). 1.0 = every claim grounded."
        ),
    )
    rebuttal_strength: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "How effectively the argument attacks or neutralises the opponent's "
            "last turn. 1.0 = devastating rebuttal; 0.0 = ignored the opponent."
        ),
    )
    weaknesses: list[str] = Field(
        default_factory=list,
        max_length=4,
        description=(
            "Short, specific improvement notes (e.g. 'statistic on line 3 needs "
            "a date', 'opening sentence concedes the opponent's frame'). "
            "Empty if no revision needed."
        ),
    )
    should_revise: bool = Field(
        description=(
            "True if ANY score is below 0.65 or if there are important weaknesses "
            "that a single revision pass would likely fix."
        ),
    )


class DebateStrategy(BaseModel):
    """LangGraph plan-and-execute output — one turn's tactical plan.

    Produced by `src/planner.py` before argument generation and injected into
    both the Phase-1 research prompt (via `evidence_keywords`) and the Phase-2
    composition prompt (via `rhetorical_angle` + `claims_to_make`).
    """

    primary_attack: str | None = Field(
        default=None,
        description=(
            "The single most vulnerable claim in the opponent's last turn to target. "
            "None for the opening phase (no opponent yet)."
        ),
    )
    evidence_keywords: list[str] = Field(
        default_factory=list,
        max_length=6,
        description=(
            "Search keywords that will maximise the quality of the Phase-1 "
            "Google Search grounding call for this specific turn."
        ),
    )
    rhetorical_angle: str = Field(
        default="balanced",
        description=(
            "One of: 'statistical' (lead with numbers/data), "
            "'narrative' (use a concrete real-world example), "
            "'authority' (cite expert consensus), "
            "'reductio' (show the opponent's position leads to an absurd conclusion), "
            "'balanced' (no specific angle)."
        ),
    )
    claims_to_make: list[str] = Field(
        default_factory=list,
        max_length=3,
        description=(
            "2–3 specific claims the debater should aim to make this turn, "
            "derived from the opponent's weaknesses and own past strong points."
        ),
    )
    opponent_weak_points: list[str] = Field(
        default_factory=list,
        max_length=3,
        description=(
            "Patterns observed in the opponent's arguments that can be exploited "
            "across this and future turns."
        ),
    )


class VerifiedClaims(BaseModel):
    """Output of the post-Phase-2 claim grounding verification pass.

    Each key_claim produced by Phase 2 is checked against the actual Phase-1
    evidence text.  Claims that are directly supported (possibly with minor
    number/date corrections to match the evidence exactly) land in `verified`.
    Any claim that is extrapolated, invented, or contradicted by the evidence
    lands in `removed` and is never passed to the judge.
    """

    verified: list[str] = Field(
        default_factory=list,
        description=(
            "Claims that are directly and explicitly supported by the evidence. "
            "Minor corrections (e.g. exact number from the source) are applied."
        ),
    )
    removed: list[str] = Field(
        default_factory=list,
        description=(
            "Claims that were not directly found in the evidence — hallucinated, "
            "extrapolated, or contradicted.  Logged for debugging."
        ),
    )
