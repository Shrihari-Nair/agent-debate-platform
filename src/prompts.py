"""Prompt templates for the two-phase Gemini pipeline.

Design notes:
- Phase 1 (grounded) prompts ask for *evidence*, not arguments. Keep them
  descriptive and neutral so the search queries Gemini emits are good.
- Phase 2 prompts consume the phase-1 evidence plus debate state and emit
  schema-constrained output (Argument or FactCheck).
- We never ask the model to invent citations; citations come exclusively from
  `grounding_metadata.grounding_chunks` of the phase-1 call.
"""

from __future__ import annotations

PHASE_INSTRUCTIONS: dict[str, str] = {
    "opening": (
        "Deliver your OPENING STATEMENT. Clearly state your position and the 2-3 "
        "strongest reasons you will defend. Do not attack opponents yet."
    ),
    "rebuttal_1": (
        "Deliver your FIRST REBUTTAL. Directly attack the weakest specific claim "
        "your opponent just made (quote it), offer a counter-argument with evidence, "
        "and reinforce one of your own points."
    ),
    "rebuttal_2": (
        "Deliver your SECOND REBUTTAL. Escalate: expose any inconsistency between "
        "the opponent's opening and their first rebuttal, and introduce one new "
        "piece of evidence that strengthens your side."
    ),
    "closing": (
        "Deliver your CLOSING STATEMENT. Summarize the two strongest pieces of "
        "evidence for YOUR side, remind the judge why the opponent's core argument "
        "failed, and end with a memorable one-sentence call to the judge."
    ),
}


def ground_topic_prompt(topic: str, stance: str, phase: str, last_opponent: str | None) -> str:
    phase_hint = PHASE_INSTRUCTIONS.get(phase, "Deliver your next turn.")
    last = (
        f"Your opponent just said:\n<<<\n{last_opponent}\n>>>\n"
        if last_opponent
        else "No opponent turn yet.\n"
    )
    return (
        "You are researching live web evidence to support an upcoming debate argument.\n"
        f"Debate topic: {topic}\n"
        f"Your side: {stance}\n"
        f"Upcoming phase: {phase} — {phase_hint}\n\n"
        f"{last}"
        "Search the web for CURRENT, authoritative evidence that supports your side "
        "OR refutes the opponent's last claim. For each finding, state the specific "
        "fact (dates, numbers, named entities) and which source it came from. Do NOT "
        "write the argument yet — this is research notes only."
    )


def compose_argument_prompt(
    topic: str,
    stance: str,
    phase: str,
    transcript_text: str,
    last_opponent: str | None,
    evidence: str,
    sources_block: str,
) -> str:
    phase_hint = PHASE_INSTRUCTIONS.get(phase, "Deliver your next turn.")
    last = (
        f"Opponent's last turn:\n<<<\n{last_opponent}\n>>>\n"
        if last_opponent
        else "You are the first speaker.\n"
    )
    return (
        f"Debate topic: {topic}\n"
        f"Your side: {stance}\n"
        f"Current phase: {phase}. {phase_hint}\n\n"
        f"Transcript so far (may be empty):\n<<<\n{transcript_text}\n>>>\n\n"
        f"{last}\n"
        "Pre-researched web evidence (already verified, use freely):\n"
        f"<<<\n{evidence}\n>>>\n\n"
        f"Sources available (cite by number in your reasoning, but speak naturally):\n{sources_block}\n\n"
        "Write the argument you will SPEAK ALOUD.\n"
        "Constraints:\n"
        "- 40 to 90 seconds of spoken text (roughly 100-220 words).\n"
        "- Conversational, confident, no stage directions, no markdown.\n"
        "- Weave in 1-3 specific facts from the evidence (numbers, dates, names).\n"
        "- Extract the atomic factual claims you made into `key_claims`; each should "
        "  be a single checkable sentence with a specific number/date/named entity.\n"
        "- CRITICAL: You are COMMITTED to the position stated in 'Your side' above. "
        "  You must NEVER agree with, support, or switch to the opponent's position. "
        "  Do not concede your core stance under any circumstances. You may "
        "  acknowledge a narrow factual point only to immediately reframe it in "
        "  favour of YOUR side."
    )


def ground_claim_prompt(claim: str) -> str:
    return (
        "You are a research assistant gathering evidence to fact-check a single claim.\n"
        f"CLAIM: {claim}\n\n"
        "Search the web for current, authoritative evidence that either supports or "
        "contradicts this specific claim. Report what the evidence says — quote or "
        "paraphrase with specifics (dates, numbers, named entities). Do not pass "
        "judgment yet. If you cannot find relevant evidence, say so."
    )


def judge_claim_prompt(claim: str, evidence: str, sources_block: str) -> str:
    return (
        "You are a strict fact-checking judge.\n\n"
        f"CLAIM: {claim}\n\n"
        "Web evidence (already retrieved):\n"
        f"<<<\n{evidence}\n>>>\n\n"
        f"Sources:\n{sources_block}\n\n"
        "Classify the claim strictly per the schema:\n"
        "- SUPPORTED: every specific factual part is corroborated.\n"
        "- CONTRADICTED: evidence directly refutes the claim (this is a HALLUCINATION).\n"
        "- PARTIALLY_SUPPORTED: mix of correct and incorrect specifics.\n"
        "- UNSUPPORTED: no relevant evidence either way.\n"
        "- UNVERIFIABLE: opinion, prediction, or normative claim.\n\n"
        "Only treat specific numbers/dates/named events/named people as factual "
        "assertions. Rhetorical framing is UNVERIFIABLE, not CONTRADICTED. "
        "Be conservative — do NOT flag CONTRADICTED unless the evidence clearly "
        "refutes a specific, concrete fact."
    )


def final_verdict_prompt(topic: str, transcript_text: str, debater_names: dict[str, str]) -> str:
    names_block = "\n".join(f"- {slug}: {name}" for slug, name in debater_names.items())
    return (
        f"You are the judge of a debate on: {topic}\n\n"
        f"Debaters:\n{names_block}\n\n"
        f"Full transcript:\n<<<\n{transcript_text}\n>>>\n\n"
        "Score each debater from 0.0 to 1.0 based PURELY on the quality of their "
        "argumentation: clarity of reasoning, persuasive structure, rebuttal "
        "effectiveness, and rhetorical strength. You are NOT doing fact-checking "
        "here — the debaters have already fact-checked one another during the "
        "debate, and any hallucinators have been disqualified. Judge the "
        "remaining arguments on their merits. Pick a single winner_slug from "
        "the debaters listed above. Write a 2-4 sentence rationale that will "
        "be READ ALOUD — conversational, authoritative, no markdown, no stage "
        "directions."
    )


# Templates used by a debater to turn their own FactCheck verdict into a short
# spoken callout during their turn. We compose these by template (no LLM) to
# keep latency bounded — the underlying FactCheck is already LLM-validated.
FACT_CHECK_CALLOUT: dict[str, str] = {
    "SUPPORTED": (
        "On a point of verification: I checked my opponent's claim that \"{claim}\" "
        "and the evidence does support it — I'll concede that one."
    ),
    "PARTIALLY_SUPPORTED": (
        "A partial correction: my opponent claimed \"{claim}\". The evidence "
        "supports some of that, but not all of it. {evidence}"
    ),
    "UNSUPPORTED": (
        "I'd also like to flag that my opponent's claim — \"{claim}\" — is "
        "unsupported. I searched for corroborating evidence and found none. "
        "I ask the judge to weigh that carefully."
    ),
    "CONTRADICTED": (
        "And I must call out a hallucination. My opponent claimed \"{claim}\". "
        "The evidence directly contradicts this. {evidence} "
        "I submit to the judge that this is a fabricated claim."
    ),
    "UNVERIFIABLE": (
        "I'll note that my opponent's claim — \"{claim}\" — is a matter of "
        "opinion rather than fact, so I won't press it."
    ),
}


def format_fact_check_callout(fc) -> str:
    template = FACT_CHECK_CALLOUT.get(fc.verdict, FACT_CHECK_CALLOUT["UNVERIFIABLE"])
    evidence = (fc.evidence_summary or "").strip()
    if len(evidence) > 240:
        evidence = evidence[:237].rsplit(" ", 1)[0] + "..."
    return template.format(claim=fc.claim, evidence=evidence)
