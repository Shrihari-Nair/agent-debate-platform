# Setup & Running

This document walks you through everything needed to go from zero to a running debate in your browser.

---

## Prerequisites

You need four things before you can run a single debate:

### 1. Python 3.11–3.13

Check your version:
```bash
python --version
# should print Python 3.11.x, 3.12.x, or 3.13.x
```

### 2. A LiveKit Project

LiveKit is the WebRTC platform that hosts the audio rooms and routes the RPC calls between agents.

1. Go to [cloud.livekit.io](https://cloud.livekit.io/) and create a free account.
2. Create a new project.
3. From the project settings, copy three values:
   - `LIVEKIT_URL` — looks like `wss://your-project.livekit.cloud`
   - `LIVEKIT_API_KEY` — looks like `APIxxxxxxxxxxxxxxxx`
   - `LIVEKIT_API_SECRET` — a long secret string

### 3. A Gemini API Key

Gemini does the argument generation and fact-checking.

1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
2. Click "Create API key".
3. Copy the key — it starts with `AIzaSy...`

**Free tier note:** The free tier allows 500 grounded (Google Search) requests per day. A full 2-debater × 4-phase debate uses roughly 30–40 grounded calls. You get about 12–15 free debates per day.

### 4. A Cartesia API Key

Cartesia converts the generated text to speech.

1. Go to [play.cartesia.ai](https://play.cartesia.ai/) and sign up.
2. Go to API Keys and create one.
3. Copy the key.

---

## Installation

### Step 1: Clone the repository

```bash
git clone <repo-url> agent-debate
cd agent-debate
```

### Step 2: Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate     # Mac/Linux
# or on Windows:
.venv\Scripts\activate
```

### Step 3: Install dependencies

```bash
pip install -e ".[dev]"
```

This installs:
- `livekit-agents` — the LiveKit agent framework (includes Cartesia plugin, Silero VAD)
- `livekit-api` — the server-side LiveKit API client
- `google-genai` — the Gemini SDK
- `fastapi` + `uvicorn` — the HTTP API server
- `pydantic` + `pydantic-settings` — data validation and config loading
- `python-dotenv` — `.env` file support
- `honcho` — process manager (dev dependency)
- `ruff` — linter (dev dependency)

**Alternative with uv** (faster):
```bash
pip install uv
uv sync
uv pip install -e ".[dev]"
```

---

## Configuration

### Step 1: Create your `.env` file

```bash
cp .env.example .env
```

### Step 2: Fill in the required values

Open `.env` and fill in:

```bash
# Required — from LiveKit Cloud project settings
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=APIxxxxxxxxxxxxxxxx
LIVEKIT_API_SECRET=your_secret_here

# Required — from Google AI Studio
GEMINI_API_KEY=AIzaSyxxxxxxxxxxxxxxxx

# Required — from Cartesia
CARTESIA_API_KEY=your_cartesia_key_here
```

### Full list of configuration options

| Variable | Required | Default | What it does |
|---|---|---|---|
| `LIVEKIT_URL` | **Yes** | — | WebSocket URL of your LiveKit server |
| `LIVEKIT_API_KEY` | **Yes** | — | LiveKit API key for server-side operations |
| `LIVEKIT_API_SECRET` | **Yes** | — | LiveKit API secret |
| `GEMINI_API_KEY` | **Yes** | — | Used for all Gemini calls (argument gen + fact-check) |
| `CARTESIA_API_KEY` | **Yes** | — | Used for TTS by both judge and debaters |
| `DEBATE_MODEL` | No | `gemini-2.5-flash` | Which Gemini model to use. Must support Google Search grounding and JSON output |
| `FACTCHECK_HALLUCINATION_THRESHOLD` | No | `0.8` | Confidence level (0–1) above which a `CONTRADICTED` fact-check triggers disqualification |
| `ORCHESTRATOR_HOST` | No | `0.0.0.0` | Bind address for the FastAPI server |
| `ORCHESTRATOR_PORT` | No | `8000` | Port for the FastAPI server |
| `WEB_ORIGIN` | No | `http://localhost:5173` | Added to CORS allowlist so the browser UI can call the API |
| `LOG_LEVEL` | No | `INFO` | Python logging level. Set to `DEBUG` to see full Gemini prompts |
| `JUDGE_HEALTH_PORT` | No | `8082` | LiveKit health probe port for the judge worker |

---

## Running the System

### Option A: All processes at once (recommended)

```bash
./scripts/run_all.sh
```

This script checks that `.env` exists, then runs `honcho start`, which reads the `Procfile`:

```
debater: python -m src.debater_agent start
judge:   python -m src.judge_agent start
orch:    uvicorn src.orchestrator:app --host 0.0.0.0 --port 8000
web:     python -m http.server 5173 --directory web
```

All four processes run together. Logs are prefixed with the process name:

```
orch.1    | INFO:     Application startup complete.
judge.1   | INFO:livekit.agents: agent worker started
debater.1 | INFO:livekit.agents: agent worker started
web.1     | Serving HTTP on :: port 5173 (http://[::]:5173/) ...
```

### Option B: Individual terminals (useful for debugging)

Open four separate terminal windows:

```bash
# Terminal 1 — Orchestrator (HTTP API)
uvicorn src.orchestrator:app --reload --port 8000

# Terminal 2 — Judge worker
python -m src.judge_agent start

# Terminal 3 — Debater worker
python -m src.debater_agent start

# Terminal 4 — Static web server
python -m http.server 5173 --directory web
```

---

## Starting Your First Debate

### Using the Browser UI

1. Open [http://localhost:5173](http://localhost:5173)
2. You'll see the "Agent Debate Room" page with a topic field and two pre-filled debater cards.
3. Change the topic or debater stances if you want.
4. Click **Start debate**.
5. The UI connects to the LiveKit room, and within a few seconds you will hear the judge begin speaking.

### Using curl (no browser)

```bash
curl -s -X POST http://localhost:8000/debate \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "Is remote work better for productivity than in-office work?",
    "debaters": [
      {
        "slug": "pro",
        "name": "Alex",
        "stance": "Remote work significantly improves productivity for knowledge workers."
      },
      {
        "slug": "con",
        "name": "Morgan",
        "stance": "Office work produces better outcomes through collaboration and structure."
      }
    ]
  }'
```

Response:
```json
{
  "room": "debate-8def33ea",
  "ws_url": "wss://your-project.livekit.cloud",
  "observer_token": "eyJhbGciOiJIUzI1...",
  "observer_identity": "observer-a3f9c2b1",
  "debate": {
    "topic": "Is remote work better for productivity than in-office work?",
    "debaters": [...],
    "phases": ["opening", "rebuttal_1", "rebuttal_2", "closing"]
  }
}
```

You can use the `observer_token` and `ws_url` to connect any LiveKit client to the room.

### Adding more debaters (up to 4)

```json
{
  "topic": "What is the best programming language for AI?",
  "debaters": [
    { "slug": "python", "name": "Python Advocate", "stance": "Python is the best language for AI due to its ecosystem and readability." },
    { "slug": "rust",   "name": "Rust Advocate",   "stance": "Rust is the best language for AI due to performance and safety guarantees." },
    { "slug": "julia",  "name": "Julia Advocate",  "stance": "Julia is the best language for AI due to its mathematical expressiveness and speed." }
  ]
}
```

---

## Watching the Logs

The most useful log lines to watch during a debate:

```
judge.1   | INFO:judge:judge starting: topic=... debaters=['pro', 'con']
judge.1   | INFO:judge:judge: all debaters connected
judge.1   | INFO:judge:judge: calling debater-pro for phase=opening
debater.1 | INFO:debater:debater[pro] speak_turn invoked by judge
debater.1 | INFO:src.argument_generator:built turn: phase=opening slug=pro claims=4 fact_check=NONE target=None chars=1200
debater.1 | INFO:debater:debater[pro] speaking 1200 chars (claims=4 fact_check=none target=None)
judge.1   | INFO:judge:judge: peer fact-check recorded: by=con target=pro verdict=CONTRADICTED conf=0.95
judge.1   | INFO:judge:judge: disqualifying debater-pro for "..."
judge.1   | INFO:judge:judge: wrote transcript to runs/debate-8def33ea.json
```

---

## Viewing Completed Debates

After a debate finishes, its record is saved in `runs/`:

```bash
ls runs/
# debate-8def33ea.json  debate-dad33dd7.json

cat runs/debate-8def33ea.json | python -m json.tool | head -80
```

The file contains the full transcript, every fact-check, all citations, and the final verdict. See [13-debate-flow.md](./13-debate-flow.md) for the exact structure.

---

## Common Issues

### "Not all debaters were able to join in time"

The judge waits up to 90 seconds for all debaters to connect. This usually means the debater worker is not running. Check that `python -m src.debater_agent start` is running without errors.

### "Response payload too large"

LiveKit RPC has a ~15KB payload limit. The judge window-trims the transcript before sending it. If this appears very early in a debate, check that `_RPC_TRANSCRIPT_WINDOW = 6` in `judge_agent.py` is not set too high.

### Gemini 429 rate limit errors

You've exceeded the free tier (500 grounded requests/day). Wait until midnight Pacific time for the counter to reset, or enable billing on your Google Cloud project.

### TTS not playing in the browser

Browsers require a user gesture to start audio playback. Make sure you clicked the **Start debate** button (not just opened the page) — that click is the gesture that allows autoplay.

### Judge crashes with `ValidationError`

A Pydantic validation error means a schema mismatch between what the judge expects and what the debater returns (or between what the orchestrator sends and what the judge receives as metadata). Check the traceback — it will name the field and model. See [03-schemas.md](./03-schemas.md) for all model definitions.
