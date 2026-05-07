"""Plan-and-Execute — Feature 3.

Implements the LangGraph plan-and-execute pattern at the turn level.

Before the two-phase Gemini grounding pipeline runs, the planner:
  1. `observe` — builds a compact structured view of the debate state
  2. `plan`    — produces a `DebateStrategy` via ChatGoogleGenerativeAI
                 with `with_structured_output(DebateStrategy)`

The `DebateStrategy` is then injected into:
  • `ground_topic_prompt()` — adds `evidence_keywords` to sharpen the
    Google Search queries (grounding is preserved and improved)
  • `compose_argument_prompt()` — adds `rhetorical_angle` and
    `claims_to_make` as composition constraints

Google Search grounding is NOT used in the planner itself — the planner
reasons over the transcript state to decide *what* to search for.  The
actual search happens in Phase 1 of `_generate_argument_core()` as before.

Graph shape:
    START → observe → plan → END
"""

from __future__ import annotations

import logging

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from .config import settings
from .schemas import DebateStrategy, TranscriptEntry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------


class PlanState(TypedDict):
    """State for the observe → plan graph."""

    topic: str
    stance: str
    phase: str
    my_slug: str
    transcript: list[TranscriptEntry]
    last_opponent_text: str | None
    memory_context: list[str]  # retrieved episodes from DebateMemory

    # Output: populated by the plan node
    strategy: DebateStrategy | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_planner_llm() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model=settings.debate_model,
        google_api_key=settings.gemini_api_key,
        temperature=0.3,  # slight creativity for strategic thinking
    )


def _render_recent_transcript(
    transcript: list[TranscriptEntry], my_slug: str, max_turns: int = 4
) -> str:
    if not transcript:
        return "(no turns yet)"
    recent = transcript[-max_turns:]
    lines = []
    for e in recent:
        tag = "ME" if e.slug == my_slug else "OPPONENT"
        lines.append(f"[{e.phase}] {tag} ({e.slug}): {e.text[:300]}")
    return "\n".join(lines)


def _render_memory(memory_context: list[str]) -> str:
    if not memory_context:
        return "(no relevant past episodes)"
    return "\n---\n".join(memory_context[:3])  # cap at 3 to keep prompt size down


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


def observe_node(state: PlanState) -> dict:
    """Summarise the current debate state into a structured observation.

    This node doesn't call an LLM — it just formats state for the plan node.
    Returns the state unchanged (observation is implicit in the state fields).
    """
    # Log what the planner will work with so developers can trace it
    transcript_snippet = _render_recent_transcript(
        state["transcript"], state["my_slug"]
    )
    logger.debug(
        "planner[%s]: observe — phase=%s opponent_len=%s recent_turns=%d mem=%d",
        state["my_slug"],
        state["phase"],
        len(state["last_opponent_text"] or ""),
        len(state["transcript"]),
        len(state["memory_context"]),
    )
    logger.debug("planner[%s]: recent transcript:\n%s", state["my_slug"], transcript_snippet)
    return {}  # no state mutations — plan node reads raw state


def plan_node(state: PlanState) -> dict:
    """Produce a `DebateStrategy` for the current turn."""
    is_opening = state["phase"] == "opening"

    opponent_block = (
        f"Opponent's last turn:\n<<<\n{state['last_opponent_text']}\n>>>"
        if state["last_opponent_text"]
        else "This is the OPENING phase — no opponent turn yet."
    )

    memory_block = _render_memory(state["memory_context"])
    transcript_block = _render_recent_transcript(state["transcript"], state["my_slug"])

    prompt = (
        f"You are a debate strategist planning ONE turn for a competitive debate.\n\n"
        f"Topic: {state['topic']}\n"
        f"My stance: {state['stance']}\n"
        f"Current phase: {state['phase']}\n\n"
        f"{opponent_block}\n\n"
        f"Recent transcript:\n<<<\n{transcript_block}\n>>>\n\n"
        f"Relevant past episodes (from debater's own memory):\n"
        f"<<<\n{memory_block}\n>>>\n\n"
        f"Produce a tactical plan for this turn:\n"
        f"{'- primary_attack: null (opening phase)' if is_opening else '- primary_attack: identify the single weakest specific claim in the opponent last turn'}\n"
        f"- evidence_keywords: 3–6 specific search keywords that will find the "
        f"  best evidence for this turn (be precise — include dates, proper nouns, "
        f"  field names)\n"
        f"- rhetorical_angle: choose one that fits: statistical / narrative / "
        f"  authority / reductio / balanced\n"
        f"- claims_to_make: 2–3 specific claims you should try to establish this "
        f"  turn, grounded in evidence you expect to find\n"
        f"- opponent_weak_points: patterns in the opponent's reasoning you can "
        f"  keep exploiting in future turns"
    )

    llm = _make_planner_llm().with_structured_output(DebateStrategy)
    logger.info(
        "planner[%s]: plan node calling LLM  phase=%s  opponent_len=%d  mem_episodes=%d",
        state["my_slug"], state["phase"],
        len(state["last_opponent_text"] or ""),
        len(state["memory_context"]),
    )
    try:
        strategy: DebateStrategy = llm.invoke(prompt)
    except Exception as exc:
        logger.warning("planner[%s]: plan node failed: %s", state["my_slug"], exc)
        strategy = DebateStrategy(
            primary_attack=None,
            evidence_keywords=[state["topic"]],
            rhetorical_angle="balanced",
            claims_to_make=[],
            opponent_weak_points=[],
        )

    logger.info(
        "planner[%s]: strategy — phase=%s angle=%s attack=%r keywords=%s",
        state["my_slug"],
        state["phase"],
        strategy.rhetorical_angle,
        (strategy.primary_attack or "none")[:60],
        strategy.evidence_keywords,
    )
    return {"strategy": strategy}


# ---------------------------------------------------------------------------
# Graph construction (lazy singleton)
# ---------------------------------------------------------------------------


def _build_graph() -> object:
    builder: StateGraph = StateGraph(PlanState)
    builder.add_node("observe", observe_node)
    builder.add_node("plan", plan_node)
    builder.add_edge(START, "observe")
    builder.add_edge("observe", "plan")
    builder.add_edge("plan", END)
    return builder.compile()


_graph = None


def _get_graph():
    global _graph
    if _graph is None:
        _graph = _build_graph()
    return _graph


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_planner(
    *,
    topic: str,
    stance: str,
    phase: str,
    my_slug: str,
    transcript: list[TranscriptEntry],
    last_opponent_text: str | None,
    memory_context: list[str],
) -> DebateStrategy:
    """Run the observe → plan graph and return a `DebateStrategy`.

    Always returns a valid `DebateStrategy` — falls back to a minimal default
    on any graph execution error so that `build_turn()` is never blocked.
    """
    logger.info(
        "planner[%s]: starting observe→plan  phase=%s  transcript_turns=%d",
        my_slug, phase, len(transcript),
    )
    graph = _get_graph()
    initial_state: PlanState = {
        "topic": topic,
        "stance": stance,
        "phase": phase,
        "my_slug": my_slug,
        "transcript": transcript,
        "last_opponent_text": last_opponent_text,
        "memory_context": memory_context,
        "strategy": None,
    }

    try:
        final_state: PlanState = await graph.ainvoke(initial_state)
        strategy = final_state.get("strategy")
        if strategy is None:
            raise ValueError("planner returned None strategy")
        logger.info(
            "planner[%s]: ✓ strategy ready  angle=%s  keywords=%s  claims=%d",
            my_slug, strategy.rhetorical_angle, strategy.evidence_keywords,
            len(strategy.claims_to_make),
        )
        return strategy
    except Exception as exc:
        logger.warning("planner[%s]: graph execution failed: %s", my_slug, exc)
        return DebateStrategy(
            primary_attack=None,
            evidence_keywords=[topic],
            rhetorical_angle="balanced",
            claims_to_make=[],
            opponent_weak_points=[],
        )
