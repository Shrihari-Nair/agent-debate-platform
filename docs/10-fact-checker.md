# Fact Checker — Verifying Claims Against the Web

The fact checker lives in [src/fact_checker.py](../src/fact_checker.py). It is a standalone module with one public function: `fact_check_claim(claim: str) -> FactCheck`. Given a single claim string, it searches the web for evidence and returns a structured verdict.

---

## What Is Fact-Checking in This System?

In human debates, fact-checking typically happens after the debate by third-party journalists. In this system, fact-checking happens **during the debate**, by the opposing debaters themselves.

Every debater (except during the opening phase) picks one of their opponent's `key_claims` and runs it through the two-phase fact-check pipeline. The result — a `FactCheck` object with a verdict and confidence — is returned to the judge via the `TurnReply`. The judge accumulates these at end-of-phase and disqualifies debaters whose claims were found to be `CONTRADICTED` with high confidence.

This creates a dynamic where debaters are incentivised to make claims that are actually true, because false claims can lead to disqualification.

---

## The Full Function

```python
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
    except Exception as exc:
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
    except Exception as exc:
        logger.warning("fact-check phase-2 failed: %s", exc)
        return FactCheck(
            claim=claim,
            verdict="UNVERIFIABLE",
            confidence=0.0,
            evidence_summary=f"Judgment unavailable: {exc}",
            citations=sources,
        )

    result.claim = claim
    if not result.citations:
        result.citations = sources
    return result
```

---

## Step-by-Step Walkthrough

### Step 1: Phase 1 — Web Research (temperature 0.1)

```python
evidence, sources = await grounded_generate(
    ground_claim_prompt(claim), temperature=0.1
)
```

`ground_claim_prompt(claim)` generates a neutral research prompt:

```
You are a research assistant gathering evidence to fact-check a single claim.
CLAIM: <claim>

Search the web for current, authoritative evidence that either supports or
contradicts this specific claim. Report what the evidence says — quote or
paraphrase with specifics (dates, numbers, named entities). Do not pass
judgment yet. If you cannot find relevant evidence, say so.
```

The `temperature=0.1` is lower than for argument research (0.2) because we want more conservative, factual output — less creative interpretation of the evidence.

The output `evidence` is free text like:
```
According to WHO data from 2023, global vaccination rates reached 68%...
However, a Nature Medicine study from 2022 found that the claimed 90% efficacy
rate was actually measured in a specific controlled population, not globally...
```

### Step 2: Phase 2 — Judgment (temperature 0.0)

```python
result = await structured_generate(
    judge_claim_prompt(claim, evidence or "(no evidence retrieved)",
                       format_sources_block(sources)),
    schema=FactCheck,
    temperature=0.0,
)
```

`judge_claim_prompt(claim, evidence, sources_block)` generates the classification prompt:

```
You are a strict fact-checking judge.

CLAIM: <claim>

Web evidence (already retrieved):
<<<
<evidence>
>>>

Sources:
[1] Article Title — https://...
[2] ...

Classify the claim strictly per the schema:
- SUPPORTED: every specific factual part is corroborated.
- CONTRADICTED: evidence directly refutes the claim (this is a HALLUCINATION).
- PARTIALLY_SUPPORTED: mix of correct and incorrect specifics.
- UNSUPPORTED: no relevant evidence either way.
- UNVERIFIABLE: opinion, prediction, or normative claim.

Only treat specific numbers/dates/named events/named people as factual
assertions. Rhetorical framing is UNVERIFIABLE, not CONTRADICTED.
Be conservative — do NOT flag CONTRADICTED unless the evidence clearly
refutes a specific, concrete fact.
```

`temperature=0.0` — zero temperature means the classification is as deterministic as possible. For a binary-ish judgment task (5 categories), you do not want variation.

The Gemini SDK returns a `FactCheck` instance:
```python
FactCheck(
    claim="The global vaccination rate reached 90% by 2023",
    verdict="CONTRADICTED",
    confidence=0.92,
    evidence_summary="WHO data shows 68%, not 90%, global vaccination rate in 2023.",
    citations=[Source(title="WHO Vaccination Data", uri="https://...")]
)
```

### Step 3: Post-Processing Fixes

```python
result.claim = claim
if not result.citations:
    result.citations = sources
```

**Why overwrite `result.claim`?**

Gemini sometimes omits the claim field or paraphrases it in the output JSON. Forcing it back to the original `claim` argument ensures the returned `FactCheck` always has the exact claim text that was passed in. This matters because the judge and the browser both display this claim text in their outputs.

**Why copy citations if empty?**

The Phase 2 structured call (no Google Search) might not include citations in its output. The Phase 1 call found sources but the Phase 2 model might not echo them. To ensure the fact-check always has source attribution, we fall back to the Phase 1 sources.

---

## The Error Handling Strategy

The fact checker has a specific and deliberate error handling philosophy, documented in its docstring:

> *"On API error (rate limit, etc.) we return an UNVERIFIABLE verdict with confidence 0.0 rather than raising — a judge crash shouldn't take down the whole debate over a transient Gemini hiccup."*

Each phase has its own `try/except`:

```python
# Phase 1 failure
try:
    evidence, sources = await grounded_generate(...)
except Exception as exc:
    return FactCheck(
        claim=claim,
        verdict="UNVERIFIABLE",
        confidence=0.0,
        evidence_summary=f"Fact-check unavailable: {exc}",
        citations=[],
    )

# Phase 2 failure (Phase 1 succeeded)
try:
    result = await structured_generate(...)
except Exception as exc:
    return FactCheck(
        claim=claim,
        verdict="UNVERIFIABLE",
        confidence=0.0,
        evidence_summary=f"Judgment unavailable: {exc}",
        citations=sources,  # Phase 1 sources still available
    )
```

### Why is `UNVERIFIABLE` the right fallback?

The 5 verdicts have different consequences:
- `CONTRADICTED` with high confidence → disqualification
- `SUPPORTED` → judge sees the evidence
- `PARTIALLY_SUPPORTED`, `UNSUPPORTED` → neutral
- `UNVERIFIABLE` → treated as non-checkable, no negative consequence

A failed fact-check (API error) should have no negative consequence on the debate. If we returned `CONTRADICTED` on failure, we'd be disqualifying debaters for Gemini API errors, not for hallucinations. `UNVERIFIABLE` with `confidence=0.0` means:
- The `confidence` is far below the disqualification threshold (0.8).
- Even if checked by `_worst_verdict_against()`, it won't trigger disqualification.
- The debater is not penalised for our API failure.

---

## The 5 Verdicts Explained with Examples

### `SUPPORTED`

The claim is backed by evidence.

```
Claim: "OpenAI released GPT-4 in March 2023."
Evidence: Multiple sources confirm GPT-4 was announced on March 14, 2023.
Verdict: SUPPORTED, confidence: 0.98
```

### `CONTRADICTED`

The evidence clearly and specifically refutes the claim. This is the hallucination flag.

```
Claim: "ChatGPT was released in 2020."
Evidence: ChatGPT launched on November 30, 2022, according to OpenAI's own blog.
Verdict: CONTRADICTED, confidence: 0.99
```

### `PARTIALLY_SUPPORTED`

Some parts are correct, some are not.

```
Claim: "Apple has 2 billion users and is worth $4 trillion."
Evidence: Apple has ~1.8 billion active devices; market cap was ~$3.1T in 2024.
Verdict: PARTIALLY_SUPPORTED, confidence: 0.85
```

### `UNSUPPORTED`

No relevant evidence found either way.

```
Claim: "Most AI researchers believe AGI will arrive by 2027."
Evidence: No authoritative survey found supporting or denying this consensus.
Verdict: UNSUPPORTED, confidence: 0.70
```

### `UNVERIFIABLE`

The claim is an opinion, prediction, or normative statement — not a checkable fact.

```
Claim: "Remote work is fundamentally better for human wellbeing."
Verdict: UNVERIFIABLE, confidence: 0.90
(This is a normative/value claim, not a factual assertion)
```

---

## Where `fact_check_claim` Is Called

1. **In `argument_generator.build_turn()`** — as a concurrent task alongside argument generation. The debater calls this to fact-check one opponent claim during their turn.

2. **(Potentially in the future)** — The judge could run additional fact-checks on any claim. Currently, the judge only aggregates what debaters return.

---

## Confidence and Disqualification Threshold

The `confidence` field from Gemini is a probability estimate. Gemini is calibrated to be conservative about `CONTRADICTED` verdicts (as instructed in the prompt). In practice:
- Clearly false specific facts → confidence 0.85–0.99
- Ambiguous or partially contradicted claims → confidence 0.4–0.7
- The claim prompt instructs Gemini to be "conservative"

The disqualification threshold (default `FACTCHECK_HALLUCINATION_THRESHOLD=0.8`) means only claims with ≥80% confidence `CONTRADICTED` verdicts trigger disqualification. This eliminates most edge cases while catching clear hallucinations.
