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
