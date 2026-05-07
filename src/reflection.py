"""Self-Critique Reflection Loop — Feature 1.

Implements the SELF-REFINE pattern using a LangGraph StateGraph:

    generate (done upstream)
         │
         ▼
      critique  ──── should_revise=False ──► END
         ▲                │
         │         should_revise=True
         │                ▼
         └────────── revise (max 2 cycles)

The critique node uses ChatGoogleGenerativeAI with `with_structured_output`
(no Google Search grounding — pure reasoning).  The revise node is a targeted
rewrite that incorporates the critique's `weaknesses` list.

Google Search grounding is intentionally NOT used here: grounding was already
applied in Phase 1 of `argument_generator._generate_argument_core()`.  The
revision only polishes structure, stance, and factual density.
"""

from __future__ import annotations

import logging
from typing import Annotated

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from .config import settings
from .schemas import Argument, ArgumentCritique

logger = logging.getLogger(__name__)

# Maximum number of critique → revise cycles before we accept what we have.
_MAX_REVISION_CYCLES = 2

# Minimum average score (across all three dimensions) below which we always
# trigger a revision pass.
_REVISION_SCORE_THRESHOLD = 0.65


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------


class ReflectionState(TypedDict):
    """State threaded through the LangGraph critique/revise graph."""

    # Inputs (set before running the graph)
    phase: str
    stance: str
    last_opponent_text: str | None
    evidence: str  # Phase-1 grounding evidence — used to score factual_density accurately

    # Mutable across iterations
    argument_text: str
    key_claims: list[str]

    # Populated by the critique node; consumed by the revise node
    critique: ArgumentCritique | None

    # Safety counter — prevents infinite revision loops
    revision_count: int


# ---------------------------------------------------------------------------
# LLM helpers — lazy-initialised to avoid importing before API key is loaded
# ---------------------------------------------------------------------------


def _critique_llm() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model=settings.debate_model,
        google_api_key=settings.gemini_api_key,
        temperature=0.0,  # deterministic critique scoring
    )


def _revise_llm() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model=settings.debate_model,
        google_api_key=settings.gemini_api_key,
        temperature=0.4,  # slight creativity for the rewrite
    )


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def _critique_prompt(state: ReflectionState) -> str:
    opponent_block = (
        f"Opponent's last turn:\n<<<\n{state['last_opponent_text']}\n>>>"
        if state["last_opponent_text"]
        else "This is the opening phase — no opponent turn yet."
    )
    evidence_block = (
        f"Phase-1 web evidence (ground truth for factual claims):\n<<<\n{state['evidence']}\n>>>"
        if state.get("evidence")
        else "(no evidence available for this turn)"
    )
    return (
        f"You are a debate coach evaluating an argument draft.\n\n"
        f"Debater's assigned stance: {state['stance']}\n"
        f"Current phase: {state['phase']}\n"
        f"{opponent_block}\n\n"
        f"{evidence_block}\n\n"
        f"ARGUMENT DRAFT:\n<<<\n{state['argument_text']}\n>>>\n\n"
        f"Score the argument on three dimensions (0.0–1.0) and list specific "
        f"weaknesses.  Be strict: a score below 0.65 on any dimension should "
        f"trigger a revision.\n"
        f"For `factual_density`: cross-check every specific number/date/statistic "
        f"in the argument against the evidence block above — penalise anything not "
        f"explicitly present in the evidence.\n"
        f"Do NOT rewrite — only evaluate."
    )


def _revise_prompt(state: ReflectionState) -> str:
    critique = state["critique"]
    weaknesses_block = (
        "\n".join(f"- {w}" for w in critique.weaknesses)
        if critique and critique.weaknesses
        else "(no specific weaknesses listed)"
    )
    opponent_block = (
        f"Opponent's last turn:\n<<<\n{state['last_opponent_text']}\n>>>"
        if state["last_opponent_text"]
        else "This is the opening phase — no opponent turn yet."
    )
    scores = (
        f"stance_adherence={critique.stance_adherence:.2f}, "
        f"factual_density={critique.factual_density:.2f}, "
        f"rebuttal_strength={critique.rebuttal_strength:.2f}"
        if critique
        else "N/A"
    )
    return (
        f"You are revising a debate argument based on a coach's critique.\n\n"
        f"Debater's assigned stance: {state['stance']}\n"
        f"Current phase: {state['phase']}\n"
        f"{opponent_block}\n\n"
        f"ORIGINAL ARGUMENT:\n<<<\n{state['argument_text']}\n>>>\n\n"
        f"CRITIQUE SCORES: {scores}\n"
        f"WEAKNESSES TO FIX:\n{weaknesses_block}\n\n"
        f"Rewrite the argument fixing the weaknesses above.  "
        f"Keep the same length (100–220 words) and retain all factual evidence. "
        f"NEVER switch to the opponent's side. "
        f"Output ONLY the revised spoken argument text — no meta-commentary."
    )


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


def critique_node(state: ReflectionState) -> dict:
    """Score the current argument_text and decide whether to revise."""
    logger.info(
        "reflection[critique]: cycle %d/%d  chars=%d  phase=%s",
        state["revision_count"], _MAX_REVISION_CYCLES,
        len(state["argument_text"]), state["phase"],
    )
    llm = _critique_llm().with_structured_output(ArgumentCritique)
    try:
        critique: ArgumentCritique = llm.invoke(_critique_prompt(state))
    except Exception as exc:
        logger.warning("reflection: critique node failed: %s", exc)
        # Safe fallback — no revision
        critique = ArgumentCritique(
            stance_adherence=1.0,
            factual_density=1.0,
            rebuttal_strength=1.0,
            weaknesses=[],
            should_revise=False,
        )
    avg_score = (
        critique.stance_adherence + critique.factual_density + critique.rebuttal_strength
    ) / 3
    logger.info(
        "reflection: critique scores stance=%.2f factual=%.2f rebuttal=%.2f avg=%.2f "
        "should_revise=%s (cycle %d/%d)",
        critique.stance_adherence,
        critique.factual_density,
        critique.rebuttal_strength,
        avg_score,
        critique.should_revise,
        state["revision_count"],
        _MAX_REVISION_CYCLES,
    )
    return {"critique": critique}


def revise_node(state: ReflectionState) -> dict:
    """Produce a revised argument text given the critique weaknesses."""
    logger.info(
        "reflection[revise]: starting cycle %d  weaknesses=%d",
        state["revision_count"] + 1,
        len(state["critique"].weaknesses) if state.get("critique") else 0,
    )
    llm = _revise_llm()
    try:
        result = llm.invoke(_revise_prompt(state))
        revised_text: str = result.content.strip()
    except Exception as exc:
        logger.warning("reflection: revise node failed: %s", exc)
        revised_text = state["argument_text"]  # keep original on failure

    logger.info(
        "reflection: revised argument (cycle %d) — %d chars",
        state["revision_count"] + 1,
        len(revised_text),
    )
    return {
        "argument_text": revised_text,
        "revision_count": state["revision_count"] + 1,
    }


# ---------------------------------------------------------------------------
# Conditional edge
# ---------------------------------------------------------------------------


def _should_revise(state: ReflectionState) -> str:
    """Return 'revise' or END based on critique outcome and cycle budget."""
    if state["revision_count"] >= _MAX_REVISION_CYCLES:
        logger.info(
            "reflection[edge]: max revision cycles reached (%d), accepting argument",
            _MAX_REVISION_CYCLES,
        )
        return END

    critique = state.get("critique")
    if critique is None:
        logger.info("reflection[edge]: no critique available, accepting argument")
        return END

    if critique.should_revise:
        logger.info(
            "reflection[edge]: should_revise=True — queuing revision (cycle %d/%d)",
            state["revision_count"] + 1, _MAX_REVISION_CYCLES,
        )
        return "revise"
    logger.info(
        "reflection[edge]: should_revise=False — accepting argument (cycle %d/%d)",
        state["revision_count"], _MAX_REVISION_CYCLES,
    )
    return END


# ---------------------------------------------------------------------------
# Graph construction (compiled once at module level)
# ---------------------------------------------------------------------------


def _build_graph() -> object:
    builder: StateGraph = StateGraph(ReflectionState)
    builder.add_node("critique", critique_node)
    builder.add_node("revise", revise_node)
    builder.add_edge(START, "critique")
    builder.add_conditional_edges(
        "critique",
        _should_revise,
        {"revise": "revise", END: END},
    )
    builder.add_edge("revise", "critique")
    return builder.compile()


_graph = None  # lazy — built on first call to avoid import-time side effects


def _get_graph():
    global _graph
    if _graph is None:
        _graph = _build_graph()
    return _graph


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def reflect_on_argument(
    argument: Argument,
    *,
    phase: str,
    stance: str,
    last_opponent_text: str | None,
    evidence: str = "",
) -> Argument:
    """Run the self-critique loop on a generated argument.

    `evidence` is the Phase-1 grounding text, threaded into the critique prompt
    so `factual_density` is scored against real sources rather than blindly.

    Returns a (possibly revised) `Argument` with improved text.
    `key_claims` are preserved from the original — revisions focus on
    rhetoric and stance, not claim extraction.

    Skips reflection for the opening phase (no opponent text, no rebuttal
    dimension to score) to keep latency low.
    """
    if phase == "opening":
        logger.info("reflection[skip]: opening phase — no revision needed, returning as-is")
        return argument

    logger.info(
        "reflection[start]: phase=%s  stance=%s  chars=%d  max_cycles=%d",
        phase, stance[:40], len(argument.text), _MAX_REVISION_CYCLES,
    )
    graph = _get_graph()
    initial_state: ReflectionState = {
        "phase": phase,
        "stance": stance,
        "last_opponent_text": last_opponent_text,
        "evidence": evidence,
        "argument_text": argument.text.strip(),
        "key_claims": argument.key_claims,
        "critique": None,
        "revision_count": 0,
    }

    try:
        # LangGraph's synchronous invoke is fine here — called from within
        # an asyncio task but the graph itself uses sync LLM calls.
        # Use ainvoke if you want full async chain.
        final_state: ReflectionState = await graph.ainvoke(initial_state)
    except Exception as exc:
        logger.warning("reflection: graph invocation failed: %s", exc)
        return argument

    revised_text = final_state.get("argument_text", argument.text)
    cycles_done = final_state.get("revision_count", 0)
    if revised_text != argument.text:
        logger.info(
            "reflection[done]: argument revised  cycles=%d  chars=%d→%d",
            cycles_done, len(argument.text), len(revised_text),
        )
        return Argument(text=revised_text, key_claims=argument.key_claims)

    logger.info(
        "reflection[done]: no revision applied  cycles=%d  chars=%d",
        cycles_done, len(argument.text),
    )
    return argument
