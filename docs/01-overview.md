# Overview — What Is Agent Debate?

Agent Debate is a multi-agent system where AI debaters argue a topic live, over audio, in a real structured debate. It is not a chatbot or a simulation — it is a distributed system with multiple independent AI agents that communicate, coordinate, and compete in real time inside a shared audio room.

---

## What Happens When You Run It

1. You open a browser, type a topic (e.g. *"Should AI agents vote in online communities?"*) and define two to four debaters — each with a name and a one-sentence position (stance).
2. You click **Start debate**.
3. Within seconds, AI agents connect to a live audio room. You hear a judge speak aloud, introducing the topic. Then each debater takes the floor, one at a time, and speaks their argument out loud with a distinct human-like voice.
4. Debaters research the web in real time to build their arguments. In later rounds, each debater also checks one of their opponent's factual claims against web evidence.
5. If a debater makes a claim that is contradicted by real web evidence with high confidence, the judge announces it aloud and disqualifies them.
6. After all rounds, the judge delivers a final spoken verdict, names the winner, and explains why.
7. A complete record of the debate — every argument, every fact-check, every citation, the final verdict — is saved as a JSON file you can read later.

Everything is real-time, voice-first, and grounded in live web evidence.

---

## The Three Kinds of Processes

The system runs as four processes simultaneously. Think of them as three roles:

### The Orchestrator (HTTP API)

A FastAPI web server running at `localhost:8000`. It does not participate in any debate. Its only job is to:
- Accept the browser's "start a debate" request
- Create a LiveKit room (a virtual audio meeting room)
- Tell the judge and debater agents to join that room
- Give the browser a token so it can observe the room

It is stateless. Once it has dispatched the agents, it is done.

### The Workers (AI Agents)

Two long-running Python processes — one for the `judge` role, one for the `debater` role — sit idle waiting for work. When the orchestrator creates a dispatch, LiveKit's server routes it to the right worker. Each dispatch becomes an isolated async task (a "job") inside the worker process.

- The **judge worker** receives one job per debate room. It runs the entire debate from start to finish.
- The **debater worker** receives one job per debater per room. If you have two debaters, that worker handles two jobs simultaneously (each isolated).

### The Browser (Observer)

A static HTML file served at `localhost:5173`. It connects to the LiveKit room as a read-only observer. It subscribes to the audio tracks of all agents (so you hear the debate) and to a data channel (so it can render the live transcript, fact-checks, and verdict in real time).

---

## System Architecture Diagram

```
You (Browser)
  │
  │  1. POST /debate { topic, debaters[] }
  ▼
┌─────────────────────────────────┐
│  Orchestrator  (FastAPI)        │
│  localhost:8000                 │
│                                 │
│  - Creates LiveKit room         │
│  - Dispatches judge job         │
│  - Dispatches debater jobs      │
│  - Returns observer token       │
└──────┬──────────────────────────┘
       │
       │  LiveKit Agent Dispatch API
       │  (server-side, not in the room)
       │
  ┌────┴──────────────────────────────────────────────┐
  │                                                   │
  ▼                                                   ▼
┌─────────────────┐              ┌────────────────────────────┐
│  Judge Agent    │◄── RPC ─────►│  Debater Agent(s)          │
│  identity=judge │  speak_turn  │  identity=debater-{slug}   │
│                 │              │  (one job per debater)      │
└────────┬────────┘              └───────────┬────────────────┘
         │                                   │
         │        LiveKit Room               │
         │  ◄── Audio tracks (WebRTC) ──────►│
         │  ◄── Data packets (debate.event)─►│
         │                                   │
         ▼
You (Browser) — subscribing, reading transcript, hearing audio
```

**Important:** The orchestrator is *outside* the room. It uses LiveKit's server-side API to create rooms and dispatch agents, but it never joins as a participant. Everything inside the room is the judge, debaters, and observers.

---

## Technology Stack

| Layer | Technology | Why |
|---|---|---|
| Room transport | [LiveKit](https://livekit.io) | WebRTC-based room: audio tracks + reliable data packets |
| Agent framework | [LiveKit Agents](https://docs.livekit.io/agents/) | Worker model: dispatch, RPC, AgentSession |
| LLM & grounding | [Gemini 2.5 Flash](https://ai.google.dev/) | Supports live Google Search grounding |
| Text-to-speech | [Cartesia sonic-2](https://cartesia.ai/) | Low-latency, high-quality TTS |
| HTTP API | [FastAPI](https://fastapi.tiangolo.com/) | Async Python web framework |
| Data validation | [Pydantic v2](https://docs.pydantic.dev/) | Schema validation + Gemini structured output |
| Config | [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | `.env` file loading |
| Process manager | [Honcho](https://honcho.readthedocs.io/) | Run all four processes from one command |

---

## Key Concepts to Understand First

Before reading the component docs, these four ideas are worth internalising:

### 1. LiveKit Workers and Jobs

A LiveKit *Worker* is a long-running process that registers itself with a name (e.g. `"judge"` or `"debater"`). When a server-side dispatch is created for that name, LiveKit routes it to a worker and calls its `on_request` function, then its `entrypoint` function. These are the entry points you will see in `judge_agent.py` and `debater_agent.py`. Think of it like a task queue: the orchestrator enqueues work, the worker picks it up.

### 2. LiveKit RPC

LiveKit RPC lets any participant call a method on another participant and get a response. In this system, the judge calls `debate.speak_turn` on each debater. The debater registers that method, runs its argument generation, speaks, and returns the result. The entire round-trip — including TTS audio playback — happens inside one RPC call. This is the main coordination mechanism between agents.

### 3. Two-Phase Gemini Pipeline

Google Gemini's SDK has a hard constraint: you cannot use live web search (GoogleSearch grounding) and structured JSON output (`response_schema`) in the same API call. Every AI operation in this system therefore runs as two sequential calls:

- **Phase 1:** Call with Google Search enabled → get free-text evidence and web citations
- **Phase 2:** Call with `response_schema` → get a validated Pydantic object back

This applies to both argument generation and fact-checking.

### 4. Agents That Only Speak

In typical voice AI applications, agents listen *and* speak. Here, all agents have speech-to-text and voice-activity-detection completely disabled. They only ever output audio via TTS. Turn coordination is done by RPC, not by listening. This is a deliberate design choice that eliminates any audio feedback loop between agents.

---

## File Index

```
agent-debate/
├── Procfile                    — honcho process definitions
├── pyproject.toml              — Python package + dependencies
├── scripts/run_all.sh          — single command to start everything
├── src/
│   ├── config.py               — reads .env into a settings object
│   ├── schemas.py              — ALL Pydantic models (wire format + Gemini schemas)
│   ├── gemini_client.py        — the two-phase Gemini pipeline helpers
│   ├── prompts.py              — every prompt template
│   ├── personas.py             — voice assignment per debater slug
│   ├── fact_checker.py         — fact_check_claim(): two-phase claim verification
│   ├── argument_generator.py   — build_turn(): concurrent argument + fact-check
│   ├── orchestrator.py         — FastAPI app: POST /debate
│   ├── judge_agent.py          — the judge worker
│   └── debater_agent.py        — the debater worker
├── web/index.html              — browser observer UI
└── runs/                       — completed debate JSON records
```

---

## What to Read Next

If you are new to the codebase, suggested reading order:

1. [02-setup.md](./02-setup.md) — get it running first
2. [03-schemas.md](./03-schemas.md) — understand every data structure before reading code
3. [13-debate-flow.md](./13-debate-flow.md) — the end-to-end flow as a sequence diagram
4. [04-gemini-client.md](./04-gemini-client.md) — the two-phase pipeline
5. [06-orchestrator.md](./06-orchestrator.md) — how a debate is created
6. [07-judge-agent.md](./07-judge-agent.md) — the conductor
7. [08-debater-agent.md](./08-debater-agent.md) — each debater
8. [09-argument-generator.md](./09-argument-generator.md) — how an argument is built
9. [10-fact-checker.md](./10-fact-checker.md) — how claims are verified
10. [05-prompts.md](./05-prompts.md) — every prompt in detail
11. [11-personas.md](./11-personas.md) — voice system
12. [12-web-ui.md](./12-web-ui.md) — browser UI
13. [14-data-packets.md](./14-data-packets.md) — the event bus
