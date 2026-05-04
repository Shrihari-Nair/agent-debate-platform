# End-to-End Debate Flow

This document traces the complete lifecycle of a debate — from the moment you click "Start debate" in the browser to the final verdict being spoken and the room closing. Read this to understand how all components fit together.

---

## System Participants

```
Browser (Observer)
Orchestrator (FastAPI HTTP server)
LiveKit Cloud (room infrastructure)
Judge Agent (Python worker)
Debater-Pro Agent (Python worker job)
Debater-Con Agent (Python worker job)
Gemini API (Google AI)
Cartesia API (TTS)
```

---

## Phase 0: Pre-Debate Setup (Before Any Click)

The four processes must already be running before a debate can start:

```
┌─────────────────────────────────────────────────────────┐
│  honcho start (reads Procfile)                          │
│                                                         │
│  orch.1    → uvicorn serving FastAPI on :8000           │
│  judge.1   → judge worker registered "judge" on LiveKit │
│  debater.1 → debater worker registered "debater"        │
│  web.1     → static HTTP server on :5173                │
└─────────────────────────────────────────────────────────┘
```

The judge and debater workers are idle — they are connected to LiveKit's server waiting for dispatches. No rooms exist yet.

---

## Phase 1: Creating the Debate

```
Browser                         Orchestrator
  │                                  │
  │  POST /debate                    │
  │  { topic, debaters: [pro, con] } │
  │─────────────────────────────────►│
  │                                  │
  │                                  │──► LiveKit: create_room("debate-8def33ea")
  │                                  │
  │                                  │──► LiveKit: dispatch("judge", metadata=DebateConfig JSON)
  │                                  │
  │                                  │──► LiveKit: dispatch("debater", metadata={slug:pro,...})
  │                                  │
  │                                  │──► LiveKit: dispatch("debater", metadata={slug:con,...})
  │                                  │
  │                                  │──► mint_observer_token(room="debate-8def33ea")
  │                                  │
  │  200 OK                          │
  │  { room, ws_url, token, ... }    │
  │◄─────────────────────────────────│
  │                                  │
```

The orchestrator's work is now **done**. It made 5 API calls to LiveKit's server-side API and returned credentials to the browser. It will not be involved in anything that happens next.

---

## Phase 2: Agents Connect to the Room

The three dispatches trigger three simultaneous agent startups:

```
LiveKit Cloud
  │
  ├──► judge worker: on_request() → accept(identity="judge") → entrypoint()
  │
  ├──► debater worker: on_request() → accept(identity="debater-pro") → entrypoint()
  │
  └──► debater worker: on_request() → accept(identity="debater-con") → entrypoint()
```

Each agent:
1. Parses its metadata
2. Calls `ctx.connect()` → joins the LiveKit room as a participant
3. Creates an `AgentSession` (TTS-only)
4. Starts its session

The browser also connects (using the `observer_token`) at roughly the same time.

---

## Phase 3: Judge Waits for Debaters

```
Judge                          Debater-Pro       Debater-Con
  │                                │                  │
  │── asyncio.gather([            │                  │
  │     _wait_for_debater("pro"), │                  │
  │     _wait_for_debater("con")  │                  │
  │   ])                          │                  │
  │                               │ connects to room  │
  │◄── participant_connected ─────│                  │
  │                               │                  │ connects to room
  │◄── participant_connected ─────────────────────────│
  │                               │                  │
  │ (both events set; gather completes)               │
  │                               │                  │
```

The judge polls `remote_participants` first (in case they connected before the judge registered the event listener), then falls back to `participant_connected` event listeners. When both debaters are seen, `asyncio.gather()` returns.

---

## Phase 4: Debate Introduction

```
Judge                                          Room (all hear)
  │                                                │
  │  session.say("Welcome. Today's debate...")      │
  │────────────────────────────────────────────────►│
  │         (TTS → audio → WebRTC stream)          │
  │                                                │
  │  publish_data({"type": "debate_started", ...}) │
  │────────────────────────────────────────────────►│
  │         (data packet → browser reads)          │
```

The judge speaks the opening announcement via TTS. The audio streams to all room participants (debaters can "hear" it, but they have no STT so they don't process it). The browser receives the `"debate_started"` data packet and pre-seeds the participant cards with debater names and stances.

---

## Phase 5: A Single Turn (Expanded Detail)

This is the core loop, repeated for each debater in each phase. Here it is for "debater-pro" in the "opening" phase:

```
Judge                    Debater-Pro              Gemini API           Cartesia
  │                           │                       │                    │
  │  session.say("Alex, you   │                       │                    │
  │  have the floor.")        │                       │                    │
  │                           │                       │                    │
  │  perform_rpc(             │                       │                    │
  │    "debate.speak_turn",   │                       │                    │
  │    payload=TurnRequest    │                       │                    │
  │  )                        │                       │                    │
  │──────────────────────────►│                       │                    │
  │                           │                       │                    │
  │                           │─ Phase 1 research ───►│                    │
  │                           │  (GoogleSearch)       │                    │
  │                           │◄─ evidence + sources ─│                    │
  │                           │                       │                    │
  │                           │─ Phase 2 compose ────►│                    │
  │                           │  (schema=Argument)    │                    │
  │                           │◄─ Argument instance ──│                    │
  │                           │                       │                    │
  │                           │  [no fact-check in opening]                │
  │                           │                       │                    │
  │                           │─ session.say(spoken) ─────────────────────►│
  │                           │  (TTS request)        │                    │
  │                           │◄─ audio stream ──────────────────────────── │
  │                           │  (audio → room)       │                    │
  │                           │                       │                    │
  │                           │  handle.wait_for_playout()  ←───────────────│
  │                           │  (blocks until audio done) │               │
  │                           │                       │                    │
  │◄── TurnReply JSON ────────│                       │                    │
  │  (RPC returns)            │                       │                    │
  │                           │                       │                    │
  │  transcript.append(entry) │                       │                    │
  │  publish_data("turn_spoken")                       │                    │
```

**Key timing insight:** The RPC call to the debater includes the entire TTS playback time. The judge doesn't get control back until after the audio has finished playing to the room. This is what serialises turns — the `response_timeout=240s` must account for this full time.

---

## Phase 6: A Turn With Fact-Checking (Rebuttal Phase)

In rebuttal phases, argument generation and fact-checking run concurrently inside the debater:

```
Debater-Con (in rebuttal_1)
  │
  ├── asyncio.create_task(_generate_argument_core(...))   ← starts immediately
  │
  ├── target = _pick_opponent_claim(transcript, "con")
  │     → returns ("pro's claim text", "pro")
  │
  ├── asyncio.create_task(fact_check_claim("pro's claim"))  ← starts immediately
  │
  │          Both tasks running concurrently...
  │
  ├── await argument_task → (Argument, sources)
  │                            (takes ~25–40s)
  │
  ├── await check_task → FactCheck
  │                            (takes ~20–35s, likely already done)
  │
  ├── spoken = argument.text + "\n\n" + format_fact_check_callout(fact_check)
  │
  └── session.say(spoken) → wait_for_playout()
```

The fact-check callout is appended directly to the spoken text. The debater speaks the argument and the verdict in one uninterrupted utterance.

---

## Phase 7: End-of-Phase Adjudication

After all debaters in a phase have spoken, the judge evaluates any fact-checks:

```
Judge
  │
  │ for each debater in round_slugs:
  │   hit = _worst_verdict_against(phase_factchecks, target, threshold=0.8)
  │   if hit (CONTRADICTED, confidence ≥ 0.8):
  │     session.say("I'm calling a hallucination against {name}...")
  │     alive.remove(target)
  │     publish_data("debater_removed")
  │     _remove_participant(room, "debater-{target}")
```

If no CONTRADICTED verdicts at or above threshold exist, this step is silent and no one is removed.

---

## Phase 8: End of All Phases → Final Verdict

After all 4 phases complete (or once only 1 debater remains):

```
Judge                                    Gemini
  │                                         │
  │  structured_generate(                   │
  │    final_verdict_prompt(transcript),    │
  │    schema=FinalVerdict                  │
  │  )──────────────────────────────────────►│
  │◄─────────────── FinalVerdict instance ──│
  │                                         │
  │  session.say("My verdict: the winner is...")
  │  publish_data("verdict")
  │
  │  _persist_run(room, {...})  → runs/debate-8def33ea.json
  │
  │  await asyncio.sleep(3)
  │
  │  ctx.shutdown("debate_complete")  → job ends, judge disconnects
```

---

## Phase 9: Room Cleanup

When the judge disconnects, the debater agents are still alive (waiting on `await asyncio.Future()`). But:
- The judge called `_remove_participant()` for any disqualified debaters during the debate.
- Surviving debaters are still connected but no longer receiving RPC calls.
- The room's `empty_timeout=600` (10 minutes) eventually auto-closes the room.
- When the room closes, the debater jobs are cancelled, their `asyncio.CancelledError` is raised, and they shut down cleanly.

---

## Complete Sequence Diagram (2-Debater Debate, All 4 Phases, No Disqualifications)

```
Browser → POST /debate
         Orchestrator: create room, dispatch judge, dispatch pro, dispatch con
                       → return observer_token to browser

Browser → room.connect(ws_url, observer_token)

judge → ctx.connect() → join room
debater-pro → ctx.connect() → join room
debater-con → ctx.connect() → join room

judge waits for debater-pro and debater-con (parallel asyncio.gather)
judge speaks: "Welcome. Today's debate..."
judge publish: {type: "debate_started"}

=== OPENING PHASE ===
judge speaks: "We now move to the opening."
judge publish: {type: "phase_started", phase: "opening"}

judge speaks: "Alex, you have the floor."
judge → RPC("debate.speak_turn") → debater-pro
  debater-pro: research → compose argument (no fact-check)
  debater-pro: say() → wait_for_playout()
  debater-pro → TurnReply
judge: append transcript entry
judge publish: {type: "turn_spoken", entry: ...}

judge speaks: "Morgan, you have the floor."
judge → RPC("debate.speak_turn") → debater-con
  [same process]
judge publish: {type: "turn_spoken", entry: ...}

[no fact-checks in opening → no adjudication]

=== REBUTTAL 1 PHASE ===
[each debater speaks + fact-checks one opponent claim]
[if fact-check CONTRADICTED ≥ 0.8 → disqualification announcement]

=== REBUTTAL 2 PHASE ===
[same]

=== CLOSING PHASE ===
[each debater speaks final statement]

=== VERDICT ===
judge → structured_generate(final_verdict_prompt) → FinalVerdict
judge speaks: "My verdict: the winner is..."
judge publish: {type: "verdict", ...}
judge: write runs/debate-8def33ea.json
judge: sleep 3s
judge: ctx.shutdown()

Room auto-closes after 10min empty_timeout
```

---

## Timing Summary (Typical 2-Debater Debate)

| Phase | Per-Debater Time | Phase Total |
|---|---|---|
| Wait for connect | 0–30s | — |
| Opening turn | ~60–90s | ~2–3 min |
| Rebuttal 1 turn | ~70–110s | ~2.5–4 min |
| Rebuttal 2 turn | ~70–110s | ~2.5–4 min |
| Closing turn | ~60–90s | ~2–3 min |
| Final verdict | ~15–30s | — |
| **Total** | | **~10–15 min** |

---

## What the Browser Sees (Live Transcript Sequence)

```
[debate_started]     → debater cards appear with names and stances
[phase_started]      → "— opening —" separator appears in transcript
[turn_spoken]        → Alex's argument card appears
[turn_spoken]        → Morgan's argument card appears
[phase_started]      → "— rebuttal 1 —" separator appears
[turn_spoken]        → Alex's rebuttal appears (includes fact-check callout text)
[fact_check]         → Red CONTRADICTED badge card appears: "Alex checked Morgan's claim"
[turn_spoken]        → Morgan's rebuttal appears
[fact_check]         → Green SUPPORTED badge card: "Morgan checked Alex's claim"
...
[debater_removed]    → (if disqualified) red left-border card + participant card greys out
...
[verdict]            → Green verdict card with winner and rationale
```
