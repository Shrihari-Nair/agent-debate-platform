# Prompts — Every LLM Prompt Explained

All prompt templates live in [src/prompts.py](../src/prompts.py). This file is the single source of truth for what the AI is asked to do. There are no prompts scattered across other files — any change to LLM behaviour goes here.

---

## Design Philosophy

The prompts follow two important principles that are documented at the top of the file:

```
Design notes:
- Phase 1 (grounded) prompts ask for *evidence*, not arguments. Keep them
  descriptive and neutral so the search queries Gemini emits are good.
- Phase 2 prompts consume the phase-1 evidence plus debate state and emit
  schema-constrained output (Argument or FactCheck).
- We never ask the model to invent citations; citations come exclusively from
  grounding_metadata.grounding_chunks of the phase-1 call.
```

### Evidence Before Argument

The Phase 1 prompts deliberately say "Do NOT write the argument yet" and "this is research notes only". This is important because:

1. Gemini's Google Search query generation is better when the prompt is a research question, not a debate prompt.
2. If you ask Gemini to argue AND search at the same time, it may write the argument first and then add irrelevant citations to justify it.
3. Separating research from composition gives Phase 2 clean evidence to work with.

### Citations From Grounding Only

The prompts explicitly do not ask Gemini to invent citation URLs. All sources come from the `grounding_metadata.grounding_chunks` structure in the Phase 1 response. If you ask a model to invent citations, you get hallucinated URLs. This system avoids that entirely.

---

## `PHASE_INSTRUCTIONS` — Per-Phase Spoken Instructions

```python
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
```

This dictionary maps phase names to rhetorical instructions. It is embedded into both the Phase 1 and Phase 2 prompts so that each phase has different rhetorical goals:

- **Opening** — introduce your position, no attacks yet.
- **Rebuttal 1** — attack the opponent's specific claim, reinforce your own point.
- **Rebuttal 2** — expose inconsistencies, escalate, add new evidence.
- **Closing** — summarise your strongest evidence, dismiss opponent's case, memorable final line.

---

## `ground_topic_prompt()` — Phase 1 Research for Argument

```python
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
```

### Parameters

- `topic` — the debate topic
- `stance` — this debater's one-sentence position
- `phase` — current phase name (used to look up `PHASE_INSTRUCTIONS`)
- `last_opponent` — the text of the most recent opposing turn, or `None` for the opening

### What This Produces (example output from Gemini with this prompt)

```
According to MIT Technology Review (January 2024), studies show remote workers 
complete 13% more work per day than office workers [source 1]. Stanford research 
from 2023 found that remote work reduces commute-related stress by 35% [source 2]. 
The World Economic Forum's 2024 Future of Work report found 82% of managers 
reported equivalent or higher productivity from remote teams [source 3].
```

This free-text evidence block (plus the `sources` list extracted from `grounding_metadata`) is then passed to Phase 2.

---

## `compose_argument_prompt()` — Phase 2 Argument Composition

```python
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
```

### The CRITICAL Constraint — Hard Stance Commitment

The most important line is the last one. Without this constraint, Gemini models tend to be excessively agreeable — they may concede points, say "my opponent raises a fair argument", or gradually drift toward the opponent's position. In a debate system, this is catastrophically bad. The hard stance commitment forces the model to always argue FOR its assigned side.

The one exception (`"acknowledge a narrow factual point only to immediately reframe it"`) is intentional — it allows the debater to seem rational (not dogmatic) while still maintaining its core stance.

### What the Output Schema (`Argument`) Looks Like

The Phase 2 call uses `schema=Argument`, so the output must be:
```json
{
  "text": "Remote work has proven itself. A 2024 MIT study found remote workers complete 13% more work daily than their office counterparts...",
  "key_claims": [
    "A 2024 MIT study found remote workers complete 13% more work daily.",
    "Stanford research from 2023 found remote work reduces commute stress by 35%."
  ]
}
```

---

## `ground_claim_prompt()` — Phase 1 Research for Fact-Checking

```python
def ground_claim_prompt(claim: str) -> str:
    return (
        "You are a research assistant gathering evidence to fact-check a single claim.\n"
        f"CLAIM: {claim}\n\n"
        "Search the web for current, authoritative evidence that either supports or "
        "contradicts this specific claim. Report what the evidence says — quote or "
        "paraphrase with specifics (dates, numbers, named entities). Do not pass "
        "judgment yet. If you cannot find relevant evidence, say so."
    )
```

A deliberately **neutral** prompt. It says "either supports or contradicts" to avoid confirmation bias — we want the real evidence, not evidence cherry-picked to support the expected answer. It explicitly says "do not pass judgment yet" to keep Phase 1 clean.

---

## `judge_claim_prompt()` — Phase 2 Fact-Check Judgment

```python
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
```

### The Conservative Instruction

The most important design choice here is: **"Be conservative — do NOT flag CONTRADICTED unless the evidence clearly refutes a specific, concrete fact."**

Why conservative? Because a false `CONTRADICTED` verdict disqualifies a debater. If Gemini is too aggressive (flags rhetorical statements as CONTRADICTED), you get unfair disqualifications. Better to under-flag than over-flag.

The distinction this prompt enforces:
- `"AI has improved"` → `UNVERIFIABLE` (rhetorical/normative)
- `"AI accuracy reached 90% in 2023"` → checkable as factual, can be `SUPPORTED` or `CONTRADICTED`
- `"AI is better than humans at chess"` → `SUPPORTED` (specific, verifiable)
- `"GPT-4 was released in 2020"` → `CONTRADICTED` (it was 2023)

### The `<<<...>>>` Delimiter Convention

All multi-line injected content in these prompts is wrapped in `<<<` and `>>>` delimiters. This is a convention that helps Gemini clearly distinguish the prompt instructions from the injected data (evidence, transcript, etc.). It reduces the risk of the model treating injected content as instructions.

---

## `final_verdict_prompt()` — Judge's Final Ruling

```python
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
```

### Key Design Choices

- **"PURELY on the quality of their argumentation"** — the judge was explicitly told not to re-do fact-checking. Fact-checking already happened during the debate. This keeps the final verdict focused on rhetoric and reasoning quality.
- **"READ ALOUD"** — the rationale text goes directly to TTS. The instruction "no markdown, no stage directions" prevents Gemini from outputting things like `**Winner:** pro` or `[The judge pauses dramatically]`.
- `debater_names` only includes surviving debaters (`alive` list) — disqualified debaters are not scored.

---

## `FACT_CHECK_CALLOUT` — Template Strings for Spoken Verdicts

```python
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
```

These are **not LLM calls** — they are plain Python string templates. After fact-checking, the debater's spoken text includes a spoken callout about the fact-check verdict. Using a template instead of another LLM call:
- Has zero latency (no API call)
- Is deterministic (no variation in phrasing)
- Stays within budget (no tokens consumed)
- Still sounds natural — the templates are written to sound conversational

The `{claim}` and `{evidence}` placeholders are filled in by `format_fact_check_callout()`.

---

## `format_fact_check_callout()` — Applies the Template

```python
def format_fact_check_callout(fc) -> str:
    template = FACT_CHECK_CALLOUT.get(fc.verdict, FACT_CHECK_CALLOUT["UNVERIFIABLE"])
    evidence = (fc.evidence_summary or "").strip()
    if len(evidence) > 240:
        evidence = evidence[:237].rsplit(" ", 1)[0] + "..."
    return template.format(claim=fc.claim, evidence=evidence)
```

- Looks up the template by verdict.
- Truncates `evidence_summary` at 240 characters (with a clean word boundary) to prevent the spoken callout from becoming too long.
- `rsplit(" ", 1)[0]` — splits on the last space before the 237-char mark, ensuring the truncation doesn't cut a word in half.
- Falls back to `UNVERIFIABLE` template if the verdict isn't in the dict (defensive programming).

---

## How All Prompts Are Used — Summary

```
argument_generator.build_turn()
  ├── Phase 1: ground_topic_prompt()         → grounded_generate()
  └── Phase 2: compose_argument_prompt()     → structured_generate(schema=Argument)
      └── (concurrent) fact_checker.fact_check_claim()
            ├── Phase 1: ground_claim_prompt()  → grounded_generate()
            └── Phase 2: judge_claim_prompt()   → structured_generate(schema=FactCheck)

          then: format_fact_check_callout()  → template string (no LLM)

judge_agent.entrypoint()
  └── (end of debate): final_verdict_prompt() → structured_generate(schema=FinalVerdict)
```
