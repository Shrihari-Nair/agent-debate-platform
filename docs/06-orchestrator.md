# Orchestrator — The HTTP API

The orchestrator is a stateless FastAPI web server in [src/orchestrator.py](../src/orchestrator.py). It is the entry point for every debate — it accepts the browser's request, creates the room, dispatches the agents, and returns the observer credentials.

**The orchestrator never joins a room.** It never participates in a debate. Its entire job is to provision the resources, dispatch the agents, and hand control back to the browser.

---

## What Is FastAPI?

FastAPI is a modern Python web framework. You define endpoints with Python functions and decorators:

```python
@app.post("/debate")
async def create_debate(req: CreateDebateRequest) -> CreateDebateResponse:
    ...
```

- `@app.post("/debate")` — registers this function as the handler for `POST /debate`.
- `req: CreateDebateRequest` — FastAPI reads the request body, parses it as JSON, and validates it against `CreateDebateRequest` (a Pydantic model). If the JSON is invalid, FastAPI automatically returns a 422 error with details.
- `-> CreateDebateResponse` — FastAPI serialises the return value as JSON.

All endpoint functions are `async` because the server uses `asyncio`. This means it can handle multiple requests concurrently without threads.

---

## App Setup

```python
load_dotenv()

logging.basicConfig(level="INFO")
logger = logging.getLogger("orchestrator")

app = FastAPI(title="Agent Debate Orchestrator", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.web_origin, "http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
```

### CORS

CORS (Cross-Origin Resource Sharing) is a browser security feature. When your browser loads `http://localhost:5173` (the UI) and it tries to make a fetch to `http://localhost:8000` (the API), the browser first sends a CORS preflight request (`OPTIONS`) to check if the API allows it.

The `CORSMiddleware` configuration allows:
- The configured `web_origin` from `.env`
- `localhost:5173` and `127.0.0.1:5173` (hardcoded fallbacks for development)
- `GET`, `POST`, and `OPTIONS` methods

`allow_credentials=False` is correct here because observer tokens are passed via the response body, not via cookies.

---

## Endpoint: `GET /healthz`

```python
@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}
```

A liveness probe. Returns `{"ok": true}` immediately. Used by process monitors or load balancers to check if the server is alive.

---

## Endpoint: `POST /debate` — The Main Endpoint

### Full Function

```python
@app.post("/debate", response_model=CreateDebateResponse)
async def create_debate(req: CreateDebateRequest) -> CreateDebateResponse:
    _require_livekit_env()

    if len(req.debaters) < 2:
        raise HTTPException(400, "Need at least 2 debaters")
    if len(req.debaters) > 4:
        raise HTTPException(400, "At most 4 debaters supported")

    slugs = [d.slug for d in req.debaters]
    if len(set(slugs)) != len(slugs):
        raise HTTPException(400, "Debater slugs must be unique")

    room_name = f"debate-{uuid.uuid4().hex[:8]}"
    config = DebateConfig(topic=req.topic, debaters=req.debaters, phases=list(DEFAULT_PHASES))

    logger.info(
        "creating debate: room=%s topic=%r debaters=%s",
        room_name,
        req.topic,
        slugs,
    )

    async with api.LiveKitAPI(...) as lkapi:
        await lkapi.room.create_room(
            api.CreateRoomRequest(
                name=room_name,
                empty_timeout=10 * 60,
                max_participants=1 + len(req.debaters) + 10,
                metadata=json.dumps({"topic": req.topic}),
            )
        )
        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name="judge",
                room=room_name,
                metadata=json.dumps(config.model_dump()),
            )
        )
        for spec in req.debaters:
            await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name="debater",
                    room=room_name,
                    metadata=json.dumps({
                        "slug": spec.slug,
                        "name": spec.name,
                        "stance": spec.stance,
                        "topic": req.topic,
                    }),
                )
            )

    identity, token = _mint_observer_token(room_name)
    return CreateDebateResponse(
        room=room_name,
        ws_url=settings.livekit_url,
        observer_token=token,
        observer_identity=identity,
        debate=config,
    )
```

### Step-by-Step Walkthrough

**Step 1: Validate environment**
```python
_require_livekit_env()
```
Checks that `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET` are all set. If any are missing, returns HTTP 500 immediately. This catches the "forgot to fill in .env" case.

**Step 2: Validate request**
```python
if len(req.debaters) < 2: raise HTTPException(400, ...)
if len(req.debaters) > 4: raise HTTPException(400, ...)
if len(set(slugs)) != len(slugs): raise HTTPException(400, ...)
```
Business rules enforced at the HTTP boundary:
- Minimum 2 debaters (can't debate alone).
- Maximum 4 debaters (more than 4 makes the debate too long and TTS costs too high).
- Unique slugs required because slugs become LiveKit participant identities (`debater-<slug>`). Duplicate identities in one room would cause collisions.

**Step 3: Generate a room name**
```python
room_name = f"debate-{uuid.uuid4().hex[:8]}"
```
A UUID4 (randomly generated UUID) is truncated to 8 hex characters to get a short but sufficiently unique room name like `debate-8def33ea`. The probability of a collision with 8 hex chars (32 bits) is 1 in 4 billion.

**Step 4: Create the LiveKit room**
```python
await lkapi.room.create_room(
    api.CreateRoomRequest(
        name=room_name,
        empty_timeout=10 * 60,    # auto-delete room after 10 min with no participants
        max_participants=1 + len(req.debaters) + 10,  # judge + debaters + observers
        metadata=json.dumps({"topic": req.topic}),
    )
)
```
Creates the room via LiveKit's server-side API. The room exists on LiveKit's infrastructure — participants can join it by its name.

- `empty_timeout=600` — after the debate ends and all agents disconnect, the room auto-deletes after 10 minutes.
- `max_participants` — set to allow the exact number of agents plus 10 observer slots.

**Step 5: Dispatch the judge**
```python
await lkapi.agent_dispatch.create_dispatch(
    api.CreateAgentDispatchRequest(
        agent_name="judge",
        room=room_name,
        metadata=json.dumps(config.model_dump()),
    )
)
```
Creates a dispatch for the `"judge"` agent. LiveKit's agent infrastructure sees this dispatch, finds a running worker registered as `agent_name="judge"`, and calls its `on_request` function, then `entrypoint`. The full `DebateConfig` (serialised as JSON) is passed as `metadata` so the judge knows everything about the debate without needing any other communication.

**Step 6: Dispatch each debater**
```python
for spec in req.debaters:
    await lkapi.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(
            agent_name="debater",
            room=room_name,
            metadata=json.dumps({
                "slug": spec.slug,
                "name": spec.name,
                "stance": spec.stance,
                "topic": req.topic,
            }),
        )
    )
```
One dispatch per debater, all to the same `agent_name="debater"` worker. Each dispatch becomes an independent job in the debater worker. The debater needs only its `slug`, `name`, `stance`, and `topic` — it does not need the full config.

**Step 7: Mint observer token and return**
```python
identity, token = _mint_observer_token(room_name)
return CreateDebateResponse(...)
```
See `_mint_observer_token()` below.

---

## Helper: `_require_livekit_env()`

```python
def _require_livekit_env() -> None:
    missing = [
        k
        for k, v in (
            ("LIVEKIT_URL", settings.livekit_url),
            ("LIVEKIT_API_KEY", settings.livekit_api_key),
            ("LIVEKIT_API_SECRET", settings.livekit_api_secret),
        )
        if not v
    ]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Missing required LiveKit env: {', '.join(missing)}",
        )
```

Validates the three required LiveKit config values at request time (not at startup). Raises HTTP 500 with a clear error message if any are missing. This design means the server starts fine without env vars — you only get an error when you actually try to use the feature.

---

## Helper: `_mint_observer_token()`

```python
def _mint_observer_token(room: str) -> tuple[str, str]:
    identity = f"observer-{uuid.uuid4().hex[:8]}"
    token = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name("Observer")
        .with_ttl(datetime.timedelta(hours=2))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room,
                can_subscribe=True,
                can_publish=False,
                can_publish_data=False,
                can_update_own_metadata=False,
            )
        )
        .to_jwt()
    )
    return identity, token
```

### What Is a LiveKit Token?

LiveKit uses JWT (JSON Web Token) for authentication. Every participant that joins a room must present a signed JWT. The JWT is signed with the `LIVEKIT_API_SECRET`, so only someone with the secret can mint valid tokens.

### The Grants — Read-Only Observer

The grants define what the token holder is allowed to do in the room:

| Grant | Value | Meaning |
|---|---|---|
| `room_join` | `True` | Can connect to the room |
| `room` | `room_name` | Can only join this specific room |
| `can_subscribe` | `True` | Can receive audio/video tracks (hear the debate) |
| `can_publish` | `False` | Cannot publish audio/video (cannot speak) |
| `can_publish_data` | `False` | Cannot send data packets |
| `can_update_own_metadata` | `False` | Cannot modify own participant attributes |

This is a strict read-only observer. The browser cannot speak, cannot send data, cannot modify anything. It can only listen and read data packets published by agents.

### TTL (Time-To-Live)

Tokens expire after 2 hours. For long debates, the browser can request a fresh token from `GET /debate/{room}/token` before the old one expires.

---

## Endpoint: `GET /debate/{room}/token` — Fresh Observer Token

```python
@app.get("/debate/{room}/token", response_model=TokenResponse)
async def rejoin_observer(room: str) -> TokenResponse:
    _require_livekit_env()
    identity, token = _mint_observer_token(room)
    return TokenResponse(
        room=room,
        ws_url=settings.livekit_url,
        observer_token=token,
        observer_identity=identity,
    )
```

Issues a fresh observer token for any room name. Used when:
- The original token expires (2-hour TTL)
- A second browser tab wants to observe the same debate
- You reconnected after a network drop

The room must already exist on LiveKit (the `create_room` call in `POST /debate` must have happened first). If the room doesn't exist, joining will fail at the LiveKit WebSocket level, not at this endpoint.

---

## The `async with api.LiveKitAPI(...)` Pattern

```python
async with api.LiveKitAPI(
    url=settings.livekit_url,
    api_key=settings.livekit_api_key,
    api_secret=settings.livekit_api_secret,
) as lkapi:
    # ... calls to lkapi.room, lkapi.agent_dispatch
```

`api.LiveKitAPI` is a context manager that:
1. Creates an authenticated gRPC/HTTP client to LiveKit's server-side API.
2. Executes the body of the `async with` block.
3. Closes the connection when the block exits (even on exception).

This is the standard resource management pattern in Python. The `async with` ensures the connection is always closed properly, preventing connection leaks.

---

## What Happens to the Orchestrator After Dispatch

Once `POST /debate` returns, the orchestrator is completely uninvolved. It holds no state about the debate. The judge and debaters communicate directly inside the LiveKit room via RPC and audio tracks. The browser watches via the observer token.

If you restart the orchestrator mid-debate, the debate continues uninterrupted — the orchestrator had nothing to do with it.
