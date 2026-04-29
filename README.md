# Agent Debate Room

A multi-agent system where AI debaters argue a topic live — over audio — in a structured, judged debate. Built on [LiveKit Agents](https://docs.livekit.io/agents/), [Google Gemini 2.5 Flash](https://ai.google.dev/), and [Cartesia TTS](https://cartesia.ai/).

Each debater is a fully autonomous agent: it searches the web for live evidence, composes a grounded argument, fact-checks its opponent's claims, and speaks the result aloud — all in real time. A separate judge agent orchestrates every phase, adjudicates hallucination accusations, and delivers a final spoken verdict. A single-page browser UI lets you watch and hear the debate unfold live, with a scrolling transcript, colour-coded fact-check verdicts, and clickable source citations.

---

## Table of Contents

1. [High-Level Architecture](#high-level-architecture)
2. [Process Map](#process-map)
3. [Full Debate Flow — Step by Step](#full-debate-flow--step-by-step)
4. [Component Deep Dives](#component-deep-dives)
5. [Data Flow Diagrams](#data-flow-diagrams)
6. [Web Observer UI](#web-observer-ui)
7. [Debate Phases](#debate-phases)
8. [Disqualification Logic](#disqualification-logic)
9. [Run Persistence](#run-persistence)
10. [Project Layout](#project-layout)
11. [Configuration Reference](#configuration-reference)
12. [Setup & Running](#setup--running)
13. [Cost & Latency Notes](#cost--latency-notes)
14. [Key Design Decisions](#key-design-decisions)
15. [Roadmap & Ideas](#roadmap--ideas)

---

## High-Level Architecture

```
Browser (index.html)
      │
      │  POST /debate  { topic, debaters[] }
      ▼
┌────────────────────────┐
│   Orchestrator         │  FastAPI — creates LiveKit room, dispatches all agents,
│   (src/orchestrator.py)│  mints observer tokens. Never joins any room itself.
│   http://localhost:8000│
└──────┬─────────────────┘
       │  LiveKit Agent Dispatch API (server-side)
       │
       ├─────────────────────────────────────────────────────────┐
       │                                                         │
       ▼                                                         ▼
┌──────────────────┐                              ┌──────────────────────────┐
│  Judge Agent     │  ◄──── LiveKit RPC ────────► │  Debater Agent(s)        │
│  (src/judge_     │        debate.speak_turn      │  (src/debater_agent.py)  │
│   agent.py)      │                              │  one job per debater     │
│  identity=judge  │                              │  identity=debater-{slug} │
└──────┬───────────┘                              └──────────┬───────────────┘
       │                                                     │
       │            LiveKit Room (WebRTC)                    │
       │  ◄──── audio tracks (Cartesia TTS) ────────────────►│
       │  ◄──── data packets  (debate.event) ───────────────►│
       ▼
Browser (subscribes read-only: audio + data, renders live transcript)
```

**Key point:** All participants — judge, each debater, and the browser observer — share a single LiveKit room. Audio flows over WebRTC. Structured events (transcript turns, fact-checks, verdicts) flow as reliable data packets on the `debate.event` topic. The orchestrator is entirely outside the room.

---

## Process Map

Four processes run concurrently, defined in `Procfile` and started together via `honcho`:

| Label | Command | Role |
|---|---|---|
| `orch` | `uvicorn src.orchestrator:app --host 0.0.0.0 --port 8000` | HTTP API server |
| `judge` | `python -m src.judge_agent start` | LiveKit Worker — one job per debate room |
| `debater` | `python -m src.debater_agent start` | LiveKit Worker — one job per debater per room |
| `web` | `python -m http.server 5173 --directory web` | Static file server for the browser UI |

The `judge` and `debater` entries are LiveKit **Workers** — long-running processes that register with the LiveKit server under a named `agent_name` and receive dispatch requests. When the orchestrator creates a dispatch, LiveKit routes it to the correct worker process, which spins up an isolated async job task.

---

## Full Debate Flow — Step by Step

### Step 1: Debate Creation

The browser sends:

```json
POST http://localhost:8000/debate
{
  "topic": "Should AI agents be allowed to vote in online communities?",
  "debaters": [
    { "slug": "pro", "name": "Alex (Pro)", "stance": "Yes, AI agents that contribute meaningfully to a community should earn limited voting rights." },
    { "slug": "con", "name": "Morgan (Con)", "stance": "No, voting rights must remain exclusive to verified human members of any community." }
  ]
}
```

The orchestrator:
1. Validates `topic` and `debaters` (2–4 unique slugs required).
2. Calls `lkapi.room.create_room(name="debate-{8-char uuid}", empty_timeout=600, max_participants=1+N+10)`.
3. Dispatches one **judge** job with the full `DebateConfig` JSON as job metadata.
4. Dispatches one **debater** job per position, each with its own `{slug, name, stance, topic}` metadata.
5. Mints a subscribe-only JWT observer token.
6. Returns `{ room, ws_url, observer_token, observer_identity, debate }`.

### Step 2: Worker Acceptance

Each worker's `on_request(req: JobRequest)` handler runs first:

- **Judge:** `await req.accept(identity="judge", name="Judge", attributes={"role":"judge"})`
- **Debater:** `await req.accept(identity="debater-{slug}", name="{name}", attributes={"role":"debater","slug":slug,"stance":stance[:200]})`

The deterministic identity (`debater-{slug}`) is essential — the judge addresses debaters by this identity over RPC.

### Step 3: Room Connection

Each agent calls `await ctx.connect()`, which joins the LiveKit room and makes the agent's participant visible to all other room members.

### Step 4: Judge Waits for All Debaters

```python
await asyncio.gather(*[
    _wait_for_debater(ctx, f"debater-{d.slug}", timeout_s=90)
    for d in debaters
])
```

`_wait_for_debater` polls `ctx.room.remote_participants` immediately, then registers a `participant_connected` listener and waits up to 90 seconds. If any debater fails to connect in time, the judge announces the timeout and returns.

### Step 5: Opening Announcement

The judge speaks a welcoming statement aloud via `session.say(...)`, introducing the topic and debaters. It also publishes a `debate_started` data packet so the browser pre-seeds the participant panel with stances.

### Step 6: Phase Loop

For each phase in `["opening", "rebuttal_1", "rebuttal_2", "closing"]`:

1. Judge checks `len(alive)` — if ≤ 1 debater remains, skip to final verdict.
2. Judge speaks the phase announcement and publishes `phase_started`.
3. For each debater slug in `alive` (snapshot at start of phase):
   - Judge says `"{name}, you have the floor."`
   - Judge builds a `TurnRequest` with phase, topic, stance, windowed transcript, and last opponent text.
   - Judge calls `perform_rpc(destination="debater-{slug}", method="debate.speak_turn", payload=TurnRequest JSON, timeout=240s)`.
   - The debater processes its turn (argument generation + fact-check, ~30–90s), speaks aloud, waits for TTS playout, then returns a `TurnReply`.
   - Judge validates the reply, appends a `TranscriptEntry`, publishes `turn_spoken`.
   - If the reply includes a `fact_check`, it is recorded in `phase_factchecks` and published as `fact_check`.
4. After all debaters in the phase have spoken, the judge evaluates `phase_factchecks` for disqualifications.

### Step 7: Final Verdict

After all phases (or `len(alive) <= 1`):

- **Only one debater left:** they win by default; a `FinalVerdict` is constructed programmatically.
- **Multiple survivors:** the judge calls `structured_generate(final_verdict_prompt(...), schema=FinalVerdict)` with the full topic and rendered transcript. Gemini returns `{ winner_slug, scores[], rationale }`.

The judge reads the verdict aloud and publishes a `verdict` data packet.

### Step 8: Shutdown

The judge writes the complete run record to `runs/{room}.json`, waits 3 seconds for audio to finish, then calls `ctx.shutdown(reason="debate_complete")`.

---

## Component Deep Dives

### Orchestrator

**File:** `src/orchestrator.py`

A stateless FastAPI app. It never joins or subscribes to any room — all LiveKit interaction goes through the server-side API (`livekit.api.LiveKitAPI`).

**Endpoints:**

| Method | Path | Description |
|---|---|---|
| `POST` | `/debate` | Create room + dispatch agents + return observer token |
| `GET` | `/debate/{room}/token` | Mint a fresh observer token for re-joining a running debate |
| `GET` | `/healthz` | Liveness probe |

**Observer token grants:**

```python
api.VideoGrants(
    room_join=True, room=room,
    can_subscribe=True,
    can_publish=False,
    can_publish_data=False,
    can_update_own_metadata=False,
)
```

Observers can hear audio and receive data packets, but cannot contribute anything. Tokens have a 2-hour TTL.

---

### Judge Agent

**File:** `src/judge_agent.py`

The conductor. Registered as a LiveKit Worker with `agent_name="judge"`. Receives exactly one dispatch per debate room.

**Session setup — TTS only:**

```python
AgentSession(
    llm=None, stt=None, vad=None,
    turn_detection="manual",
    tts=cartesia.TTS(model="sonic-2", voice=JUDGE_VOICE.id),
    allow_interruptions=False,
)
```

**State managed throughout the debate:**

| Variable | Type | Purpose |
|---|---|---|
| `alive` | `list[str]` | Slugs of non-disqualified debaters |
| `transcript` | `list[TranscriptEntry]` | Full debate transcript, all phases |
| `factchecks` | `list[dict]` | All fact-checks across all phases (for persistence) |
| `phase_factchecks` | `list[dict]` | Fact-checks for the current phase only (for disqualification) |
| `debater_by_slug` | `dict[str, DebaterSpec]` | Name/stance lookup |

**RPC payload compression:**

LiveKit RPC has a ~15KB payload limit. The transcript is truncated to the last 6 turns and citations are stripped before being sent to debaters:

```python
_RPC_TRANSCRIPT_WINDOW = 6

def _slim_transcript(transcript):
    recent = transcript[-6:]
    return [TranscriptEntry(..., citations=[]) for e in recent]
```

**Participant eviction:** When a debater is disqualified, the judge calls `lkapi.room.remove_participant()` via the server-side API to physically remove them from the room.

---

### Debater Agent

**File:** `src/debater_agent.py`

A LiveKit Worker with `agent_name="debater"`. One worker process handles all debater dispatches (each gets its own isolated async task).

**Audio only — no listening:**

```python
AgentSession(
    llm=None, stt=None, vad=None,
    turn_detection="manual",
    tts=cartesia.TTS(model="sonic-2", voice=voice_for_slug(slug).id),
    allow_interruptions=False,
)
```

**RPC handler (core logic):**

```python
@ctx.room.local_participant.register_rpc_method("debate.speak_turn")
async def speak_turn(data: RpcInvocationData) -> str:
    async with rpc_lock:
        req = TurnRequest.model_validate_json(data.payload)
        spoken, argument, citations, fact_check, target_slug = await build_turn(...)
        handle = await session.say(spoken, allow_interruptions=False)
        await handle.wait_for_playout()  # blocks until audio finishes
        return TurnReply(...).model_dump_json()
```

`wait_for_playout()` makes the RPC round-trip include the full TTS playout duration. The judge's 240s timeout therefore naturally covers generation + speech time with no separate signalling needed.

---

### Argument Generator

**File:** `src/argument_generator.py`

The brain of each debater turn. `build_turn()` launches argument generation and fact-checking **concurrently** using `asyncio.create_task`:

```
build_turn()
  │
  ├─ create_task(_generate_argument_core())   ← always runs
  │     Phase 1: grounded_generate(ground_topic_prompt)
  │         → Google Search for supporting evidence
  │     Phase 2: structured_generate(compose_argument_prompt)
  │         → Argument { text, key_claims[] }
  │
  └─ create_task(fact_check_claim(claim))     ← if allow_fact_check=True
        picks ONE opponent claim from transcript (newest first)
        → two-phase check → FactCheck
  │
  ├─ await both tasks (concurrent I/O)
  │
  └─ spoken = argument.text + "\n\n" + format_fact_check_callout(fact_check)
     return (spoken, argument, arg_sources, fact_check, target_slug)
```

**Opponent claim picking (`_pick_opponent_claim`):**
Scans transcript from newest to oldest. For each entry from a *different* slug, returns the first non-empty `key_claim`. Always targets the most recently made verifiable claim.

---

### Fact Checker

**File:** `src/fact_checker.py`

`fact_check_claim(claim: str) -> FactCheck` — a standalone two-phase check on a single atomic claim.

```
Phase 1: grounded_generate(ground_claim_prompt(claim))
  → Google Search for evidence about the claim
  → returns (evidence_text, sources)

Phase 2: structured_generate(judge_claim_prompt(claim, evidence, sources))
  → FactCheck { claim, verdict, confidence, evidence_summary, citations }
```

**Verdicts:**

| Verdict | Meaning | Can disqualify? |
|---|---|---|
| `SUPPORTED` | Every factual part corroborated | No |
| `CONTRADICTED` | Evidence directly refutes the claim | **Yes** (if confidence ≥ threshold) |
| `PARTIALLY_SUPPORTED` | Mix of correct and incorrect | No |
| `UNSUPPORTED` | No relevant evidence found | No |
| `UNVERIFIABLE` | Opinion, prediction, normative | No |

Any API error returns `verdict="UNVERIFIABLE", confidence=0.0` — a fact-check failure never crashes the debate.

---

### Gemini Client & Two-Phase Pipeline

**File:** `src/gemini_client.py`

The core design constraint: **the Gemini SDK cannot combine `GoogleSearch` grounding with `response_schema` structured output in the same call.** Every AI operation therefore splits into two sequential calls:

```
Phase 1 — grounded_generate()
  tools=[Tool(google_search=GoogleSearch())]
  temperature=0.1–0.2
  output: free-form text + citations from grounding_metadata.grounding_chunks

      ↓  evidence_text, sources[]

Phase 2 — structured_generate()
  response_mime_type="application/json"
  response_schema=<Pydantic class>
  temperature=0.0–0.6
  input: prompt containing Phase 1 evidence
  output: response.parsed → validated Pydantic instance
```

The client is a process-level singleton (`@lru_cache(maxsize=1)`) using `google.genai.Client` with the `client.aio` async interface. Citations are extracted from `grounding_metadata` — the model is never asked to invent URLs.

**Temperature guide:**

| Use case | Temp | Reason |
|---|---|---|
| Grounded evidence retrieval | 0.1–0.2 | Accurate factual retrieval |
| Fact-check judgment | 0.0 | Fully deterministic verdicts |
| Argument composition | 0.6 | Some variation in phrasing |
| Final verdict | 0.2 | Mostly deterministic |

---

### Prompt Templates

**File:** `src/prompts.py`

All prompt-building functions in one place.

| Function | Phase | Purpose |
|---|---|---|
| `ground_topic_prompt` | Phase 1 (argument) | Research for web evidence supporting the debater's stance |
| `compose_argument_prompt` | Phase 2 (argument) | Argument composition with strict stance-commitment constraint |
| `ground_claim_prompt` | Phase 1 (fact-check) | Research to gather evidence about a specific claim |
| `judge_claim_prompt` | Phase 2 (fact-check) | Strict classification into one of the 5 verdicts |

**Per-phase argument instructions:**

| Phase | Instruction |
|---|---|
| `opening` | State position + 2–3 strongest reasons. Do not attack opponents yet. |
| `rebuttal_1` | Attack the opponent's weakest specific claim (quote it), counter with evidence, reinforce one own point. |
| `rebuttal_2` | Escalate: expose inconsistency between opponent's opening and first rebuttal; introduce one new piece of evidence. |
| `closing` | Summarize two strongest evidence points; explain why opponent's core argument failed; memorable closing sentence. |

**Hard stance commitment:** `compose_argument_prompt` contains an explicit constraint that the debater must *never* agree with or switch to the opponent's position. Narrow factual concessions are allowed only if immediately reframed in favour of the debater's own side. This prevents the model from being diplomatically submissive.

---

### Personas & Voices

**File:** `src/personas.py`

Each participant gets a distinct Cartesia voice so listeners can tell speakers apart by ear.

**Assignment:**

```python
NAMED_VOICES = {
    "pro":      VoiceProfile("a167e0f3...", "Blake (energetic US male)"),
    "con":      VoiceProfile("9626c31c...", "Jacqueline (confident US female)"),
    "optimist": VoiceProfile("421b3369...", "Newscaster (neutral US male)"),
    "skeptic":  VoiceProfile("248be419...", "Elizabeth (British female)"),
}

# Unknown slugs: stable hash → index into VOICE_POOL (deterministic across restarts)
def voice_for_slug(slug: str) -> VoiceProfile:
    if slug in NAMED_VOICES:
        return NAMED_VOICES[slug]
    return VOICE_POOL[abs(hash(slug)) % len(VOICE_POOL)]
```

`JUDGE_VOICE` is fixed: Katie (authoritative US female), never part of the rotation.

---

### Schemas

**File:** `src/schemas.py`

Pydantic models serving two purposes simultaneously:
1. **Wire-format validation** — HTTP request/response bodies and RPC JSON payloads.
2. **Gemini structured-output schemas** — passed as `response_schema` to `structured_generate`; the SDK auto-converts to Gemini's OpenAPI subset.

```
DebaterSpec         slug (1-32, lowercase), name (1-64), stance (3-500)
DebateConfig        topic, debaters (2-4), phases
TranscriptEntry     slug, name, phase, text, key_claims[], citations[]
Argument            text, key_claims[]                       ← Gemini output
FactCheck           claim, verdict, confidence, evidence_summary, citations[]  ← Gemini output
TurnRequest         phase, topic, my_slug, my_stance, transcript, allow_fact_check, time_limit_s
TurnReply           text, key_claims[], citations[], fact_check?, target_slug?
ScoreEntry          slug, score (0.0–1.0)
FinalVerdict        winner_slug, scores[], rationale          ← Gemini output
```

---

## Data Flow Diagrams

### Per-Turn RPC Flow

```
Judge process                                     Debater process
     │                                                 │
     │─── perform_rpc("debate.speak_turn",             │
     │        TurnRequest JSON, timeout=240s) ────────►│
     │                                                 │
     │                                     rpc_lock.acquire()
     │                                                 │
     │                             ┌───────────────────┴──────────────────┐
     │                             │             build_turn()             │
     │                             │                                      │
     │                             │  create_task(_generate_argument_core)│ ← concurrent
     │                             │  create_task(fact_check_claim)       │ ← concurrent
     │                             │                                      │
     │                             │  await both tasks                    │
     │                             │  spoken = arg.text + fc_callout      │
     │                             └───────────────────┬──────────────────┘
     │                                                 │
     │                                     session.say(spoken)
     │                                     handle.wait_for_playout()
     │                                                 │
     │◄─── TurnReply JSON ─────────────────────────────│
     │
     │─── publish_data("turn_spoken") ──────────────────────────────────► Browser
     │─── publish_data("fact_check") ───────────────────────────────────► Browser
```

### Two-Phase Gemini Pipeline

```
Prompt (topic, stance, phase, last_opponent)
  │
  ▼
grounded_generate()
  Gemini 2.5 Flash + GoogleSearch
  temperature=0.2
  │
  ▼
evidence_text + sources[]
  │
  ▼
structured_generate(compose_argument_prompt(... evidence=evidence_text ...))
  Gemini 2.5 Flash, response_schema=Argument, temperature=0.6
  │
  ▼
Argument { text: str, key_claims: list[str] }
```

The same two-step pattern applies to fact-checking, with `ground_claim_prompt` → `judge_claim_prompt` and `response_schema=FactCheck`.

### End-of-Phase Disqualification Flow

```
Judge (all debaters have spoken this phase)
  │
  ├─ for target in round_slugs:
  │     _worst_verdict_against(phase_factchecks, target, threshold=0.8)
  │
  ├─ if CONTRADICTED hit found with confidence ≥ 0.8:
  │     session.say(disqualification ruling)    ← judge speaks aloud
  │     alive.remove(target)
  │     publish_data("debater_removed")         ← browser grays out card
  │     remove_participant(room, identity)      ← LiveKit evicts debater
  │
  └─ next phase (or final verdict if len(alive) ≤ 1)
```

---

## Web Observer UI

**File:** `web/index.html`

A zero-dependency single-page app — vanilla JS + `livekit-client@2.9.0` imported from ESM CDN. No build step.

**Layout:**

```
┌──────────────────────────────────────────────────────┐
│ Header: title │ connection status │ room name        │
├──────────────────┬───────────────────────────────────┤
│  Left panel      │  Right panel: Live Transcript     │
│                  │                                   │
│  Topic input     │  ── opening ──                    │
│  Debater cards   │  [Alex (Pro) · opening]            │
│  (slug/name/     │   argument text...                │
│   stance)        │   [1] source — url                │
│  Start button    │                                   │
│  Status          │  CONTRADICTED 1.00 · con→pro      │
│                  │   Claim: "..."                    │
│  Participants    │   Evidence: "..."                 │
│  ┌────┐ ┌────┐  │                                   │
│  │Alex│ │Morg│  │  ── rebuttal 1 ──                 │
│  │LIVE│ │    │  │  ...                              │
│  └────┘ └────┘  │                                   │
│                  │  VERDICT · winner: Morgan (Con)   │
└──────────────────┴───────────────────────────────────┘
```

**LiveKit events handled:**

| Event | Action |
|---|---|
| `ParticipantConnected` | Add card to participants panel |
| `ParticipantDisconnected` | Mark card as removed (grayed out) |
| `TrackSubscribed` (audio) | Attach `<audio autoplay>` for the agent's audio |
| `ActiveSpeakersChanged` | Blue glow on the currently-speaking card |
| `DataReceived` (topic=`debate.event`) | Route to `handleEvent()` → update transcript |

**Data packet types rendered:**

| `type` | Visual output |
|---|---|
| `debate_started` | Seed debater cards with stances from the authoritative config |
| `phase_started` | Horizontal divider with phase name |
| `turn_spoken` | Turn bubble: name, phase, full text, clickable citation links |
| `fact_check` | Colour-coded verdict card (green/amber/red) with confidence, claim, evidence |
| `debater_removed` | Red-bordered disqualification notice; participant card grayed out |
| `verdict` | Green-bordered verdict card with winner and rationale |

---

## Debate Phases

Default sequence (configurable per debate):

```python
DEFAULT_PHASES = ["opening", "rebuttal_1", "rebuttal_2", "closing"]
```

| Phase | Fact-Check? | Argument Style |
|---|---|---|
| `opening` | **No** | Establish position; present 2–3 strongest reasons; no attacks |
| `rebuttal_1` | Yes | Attack opponent's weakest claim; counter with evidence; reinforce one own point |
| `rebuttal_2` | Yes | Escalate; expose opponent inconsistency; one new piece of evidence |
| `closing` | Yes | Summarize two strongest evidence points; memorable appeal to judge |

Opening disables fact-checking (`allow_fact_check=False`) because there are no prior claims to check against.

---

## Disqualification Logic

A debater is disqualified **at the end of a phase** (never mid-phase) when an opponent's fact-check returned `CONTRADICTED` with `confidence >= FACTCHECK_HALLUCINATION_THRESHOLD` (default `0.8`).

The judge picks the *worst* (highest-confidence) contradiction per target:

```python
def _worst_verdict_against(phase_checks, target_slug, threshold):
    best = None
    for c in phase_checks:
        if c["target_slug"] != target_slug: continue
        if c["verdict"] != "CONTRADICTED": continue
        if c["confidence"] < threshold: continue
        if best is None or c["confidence"] > best["confidence"]:
            best = c
    return best
```

The ruling is spoken aloud before eviction:
> *"I'm calling a hallucination against {target_name}, based on the fact-check raised by {accuser_name}. The claim in question — "{claim}" — is contradicted by the evidence. {evidence_summary} {target_name} is disqualified from this debate."*

If disqualification reduces `alive` to ≤ 1 debater, the phase loop ends and the remaining debater wins by default.

---

## Run Persistence

After every debate, the judge writes a complete record to `runs/{room_name}.json`:

```json
{
  "room": "debate-dad33dd7",
  "ended_at": "2026-04-24T11:23:08.318442+00:00",
  "topic": "Is the current delimitation bill a good thing?",
  "debaters": [
    { "slug": "pro", "name": "Alex (Pro)", "stance": "Yes, it is" },
    { "slug": "con", "name": "Morgan (Con)", "stance": "No, don't think so" }
  ],
  "phases": ["opening", "rebuttal_1", "rebuttal_2", "closing"],
  "transcript": [
    {
      "slug": "pro", "name": "Alex (Pro)", "phase": "opening",
      "text": "Good morning, everyone...",
      "key_claims": ["The delimitation bill aligns electoral seats with current population levels"],
      "citations": [{ "title": "sundayguardianlive.com", "uri": "https://..." }]
    }
  ],
  "fact_checks": [
    {
      "phase": "rebuttal_1", "by_slug": "con", "target_slug": "pro",
      "claim": "...", "verdict": "CONTRADICTED", "confidence": 1.0,
      "evidence_summary": "..."
    }
  ],
  "final_verdict": {
    "winner_slug": "con",
    "scores": [{ "slug": "pro", "score": 0.3 }, { "slug": "con", "score": 0.7 }],
    "rationale": "Morgan presented stronger evidence..."
  },
  "winner_name": "Morgan (Con)"
}
```

The file is written atomically with `pathlib.Path.write_text`. The `runs/` directory is created if it does not exist.

---

## Project Layout

```
agent-debate/
├── Procfile                    # honcho process definitions
├── pyproject.toml              # dependencies + build config
├── scripts/
│   └── run_all.sh              # validates .env + honcho start
├── src/
│   ├── __init__.py
│   ├── config.py               # pydantic-settings — reads .env
│   ├── personas.py             # Cartesia voice pool + slug→voice mapping
│   ├── prompts.py              # all prompt templates (four functions)
│   ├── schemas.py              # Pydantic models: wire format + Gemini schemas
│   ├── gemini_client.py        # grounded_generate + structured_generate helpers
│   ├── argument_generator.py   # build_turn(): concurrent argument + fact-check
│   ├── fact_checker.py         # fact_check_claim(): two-phase claim verification
│   ├── debater_agent.py        # LiveKit Worker agent_name="debater"
│   ├── judge_agent.py          # LiveKit Worker agent_name="judge"
│   └── orchestrator.py         # FastAPI: POST /debate, GET /debate/{room}/token
├── web/
│   └── index.html              # Single-page observer UI (no build step)
└── runs/
    ├── debate-8def33ea.json
    ├── debate-dad33dd7.json
    └── debate-ea14b6b3.json
```

---

## Configuration Reference

All settings are read from `.env` via `pydantic-settings`. Copy `.env.example` to `.env` to get started.

| Variable | Required | Default | Description |
|---|---|---|---|
| `LIVEKIT_URL` | **Yes** | — | WebSocket URL, e.g. `wss://my-project.livekit.cloud` |
| `LIVEKIT_API_KEY` | **Yes** | — | LiveKit project API key |
| `LIVEKIT_API_SECRET` | **Yes** | — | LiveKit project API secret |
| `GEMINI_API_KEY` | **Yes** | — | Google AI Studio API key |
| `CARTESIA_API_KEY` | **Yes** | — | Cartesia API key |
| `DEBATE_MODEL` | No | `gemini-2.5-flash` | Gemini model — must support GoogleSearch grounding + JSON output |
| `FACTCHECK_HALLUCINATION_THRESHOLD` | No | `0.8` | Min CONTRADICTED confidence to disqualify |
| `ORCHESTRATOR_HOST` | No | `0.0.0.0` | Bind address for FastAPI |
| `ORCHESTRATOR_PORT` | No | `8000` | Port for FastAPI |
| `WEB_ORIGIN` | No | `http://localhost:5173` | Added to CORS allowlist |
| `LOG_LEVEL` | No | `INFO` | Python logging level |
| `JUDGE_HEALTH_PORT` | No | `8082` | LiveKit health probe port for the judge worker |

---

## Setup & Running

### Prerequisites

- Python 3.11–3.13
- A [LiveKit Cloud](https://cloud.livekit.io/) project (or self-hosted LiveKit server)
- A Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey)
- A Cartesia API key from [play.cartesia.ai](https://play.cartesia.ai/)

### Install

```bash
git clone <repo> agent-debate && cd agent-debate
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Or with [uv](https://github.com/astral-sh/uv):

```bash
uv sync && uv pip install -e ".[dev]"
```

### Configure

```bash
cp .env.example .env
# Edit .env: fill in LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET,
#            GEMINI_API_KEY, CARTESIA_API_KEY
```

### Start Everything

```bash
./scripts/run_all.sh
# or: honcho start
```

Open `http://localhost:5173` in your browser.

### Start Processes Individually

```bash
uvicorn src.orchestrator:app --reload --port 8000   # orchestrator
python -m src.judge_agent start                     # judge worker
python -m src.debater_agent start                   # debater worker
python -m http.server 5173 --directory web          # static UI
```

### Create a Debate via curl

```bash
curl -s -X POST http://localhost:8000/debate \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "Is remote work better for productivity than in-office work?",
    "debaters": [
      { "slug": "pro", "name": "Alex", "stance": "Remote work significantly improves productivity for knowledge workers." },
      { "slug": "con", "name": "Morgan", "stance": "Office work produces better outcomes through collaboration and structure." }
    ]
  }' | jq .
```

---

## Cost & Latency Notes

### Gemini

- **Free tier:** 500 grounded requests/day on Gemini 2.5 Flash (~15 full 2-debater debates/day).
- **Paid tier:** ~$35 per 1,000 grounded prompts on Gemini 2.5 Flash.
- **Per debate (2 debaters × 4 phases):** ~33 Gemini calls total → ~$1–2 at paid rates.

### Cartesia

- `sonic-2`: ~$0.001 per 100 characters. A full 4-phase debate → ~$0.06–0.10.

### Per-Turn Latency

| Stage | Time |
|---|---|
| Gemini grounded call (phase 1) | 5–15s |
| Gemini schema call (phase 2) | 3–8s |
| Argument + fact-check (concurrent) | ~15–35s total |
| Cartesia TTS playout | 5–20s |
| **Total per turn** | **~20–55s** |

The judge's RPC timeout (`PER_TURN_RPC_TIMEOUT_S = 240`) comfortably covers slow days.

---

## Key Design Decisions

**1. Mandatory two-phase Gemini pipeline**
Grounding and structured output are mutually exclusive in a single Gemini call. Phase 1 retrieves evidence; Phase 2 reasons over it. This also improves output quality — the model is never asked to search and compose simultaneously.

**2. No STT or VAD in any agent**
All agents use `stt=None, vad=None, turn_detection="manual"`. `AgentSession` is a TTS output rail only. This eliminates agent-to-agent audio feedback loops and STT costs entirely. Turn coordination is handled by RPC.

**3. RPC-based turn coordination**
The judge drives all turn-taking via LiveKit RPC. `wait_for_playout()` inside the debater's handler means the round-trip naturally includes TTS playout time — the RPC reply *is* the "done speaking" signal.

**4. End-of-phase adjudication only**
Disqualifications happen after all debaters in a phase have spoken, never mid-round, avoiding confusing mid-phase evictions.

**5. Transcript windowing**
`_slim_transcript()` strips citations and limits to the last 6 turns before including the transcript in each `TurnRequest`, keeping RPC payloads well under LiveKit's ~15KB limit.

**6. Deterministic voices**
`voice_for_slug()` uses `abs(hash(slug)) % len(VOICE_POOL)` as a fallback. The same slug always gets the same voice across restarts.

**7. Subscribe-only observer tokens**
The browser JWT has `can_publish=False, can_publish_data=False` — observers cannot accidentally interfere with the room.

**8. Defensive fallbacks on every AI boundary**
`fact_check_claim` and `build_turn` catch all exceptions and degrade gracefully. A failing fact-check returns `UNVERIFIABLE` at confidence `0.0`. A failing debater RPC causes the judge to announce a forfeit and continue. The debate always reaches a verdict.

---

## Roadmap & Ideas

- **Summarizer agent** — maintain a running 200-word summary of the transcript to replace the sliding window, fixing the RPC payload size problem structurally.
- **Parallel fact-checking** — fact-check all key claims from the opponent's last turn concurrently and submit the most damaging one.
- **Cross-examination phase** — judge poses a directed question from one debater to the other with a shorter time limit.
- **Dynamic phases** — judge adds an extra rebuttal round if scores are very close after each phase.
- **Per-turn scoring** — judge silently scores each turn (logic, evidence quality, directness) so the final verdict is a weighted aggregation, not a holistic one-shot call.
- **Preparation phase** — before the debate, each debater runs a research sprint to build a mini knowledge base it carries into all turns.
- **Fallacy detector** — a silent observer agent that checks each turn for logical fallacies and annotates the transcript.
- **Multi-judge panel** — three judge instances scoring with different lenses (empirical, rhetorical, audience appeal); a meta-judge aggregates.
- **Human-in-the-loop** — an endpoint that lets a human observer submit a question for the judge to pose at the next phase boundary.
- **Debate replay / podcast export** — post-process `runs/*.json` + Cartesia TTS to produce a single MP3 with chapter markers per phase.
- **Auth on the orchestrator** — protect `POST /debate` for public deployments.

---

## Key References

- [LiveKit Agents documentation](https://docs.livekit.io/agents/)
- [LiveKit Agent Dispatch](https://docs.livekit.io/agents/worker/agent-dispatch)
- [LiveKit RPC](https://docs.livekit.io/transport/data/rpc/)
- [Gemini grounding with Google Search](https://ai.google.dev/gemini-api/docs/google-search)
- [Gemini structured output](https://ai.google.dev/gemini-api/docs/structured-output)
- [Cartesia voice library](https://play.cartesia.ai/voices)
- [google-genai Python SDK](https://github.com/googleapis/python-genai)
