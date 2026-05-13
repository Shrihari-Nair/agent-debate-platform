"""FastAPI orchestrator: creates rooms, dispatches agents, mints observer tokens.

Endpoints:
  POST /debate                    -> create room + dispatch judge + N debaters, return observer token
  GET  /debate/{room}/token       -> issue a fresh observer token for rejoins
  GET  /healthz                   -> liveness

The orchestrator itself does not join any rooms — it only talks to LiveKit's
server-side APIs (`api.LiveKitAPI`) and to the browser observer via HTTP.
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from livekit import api
from pydantic import BaseModel

from .config import settings
from .schemas import (
    DEFAULT_PHASES,
    CreateDebateRequest,
    CreateDebateResponse,
    DebateConfig,
)

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


class TokenResponse(BaseModel):
    room: str
    ws_url: str
    observer_token: str
    observer_identity: str


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


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/debate", response_model=CreateDebateResponse)
async def create_debate(req: CreateDebateRequest) -> CreateDebateResponse:
    _require_livekit_env()

    auto_mode = not req.debaters

    if not auto_mode:
        if len(req.debaters) < 2:
            raise HTTPException(400, "Need at least 2 debaters")
        if len(req.debaters) > 4:
            raise HTTPException(400, "At most 4 debaters supported")
        slugs = [d.slug for d in req.debaters]
        if len(set(slugs)) != len(slugs):
            raise HTTPException(400, "Debater slugs must be unique")

    room_name = f"debate-{uuid.uuid4().hex[:8]}"

    if auto_mode:
        # Judge metadata contains only topic + phases; debaters are decided by the judge.
        judge_meta = {"topic": req.topic, "phases": list(DEFAULT_PHASES)}
        response_debate = None
        logger.info("creating debate (auto-mode): room=%s topic=%r", room_name, req.topic)
    else:
        config = DebateConfig(
            topic=req.topic, debaters=req.debaters, phases=list(DEFAULT_PHASES)
        )
        judge_meta = config.model_dump()
        response_debate = config
        logger.info(
            "creating debate (manual): room=%s topic=%r debaters=%s",
            room_name,
            req.topic,
            [d.slug for d in req.debaters],
        )

    async with api.LiveKitAPI(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    ) as lkapi:
        await lkapi.room.create_room(
            api.CreateRoomRequest(
                name=room_name,
                empty_timeout=10 * 60,
                # judge + up to 4 debaters + 10 observers
                max_participants=15,
                metadata=json.dumps({"topic": req.topic}),
            )
        )

        # Dispatch the judge. In auto-mode the judge's metadata has no "debaters"
        # key, which triggers the auto-setup path in judge_agent.entrypoint().
        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name="judge",
                room=room_name,
                metadata=json.dumps(judge_meta),
            )
        )

        # Dispatch debater workers only in manual mode.
        # In auto-mode the judge dispatches them after deciding positions.
        if not auto_mode:
            for spec in req.debaters:
                await lkapi.agent_dispatch.create_dispatch(
                    api.CreateAgentDispatchRequest(
                        agent_name="debater",
                        room=room_name,
                        metadata=json.dumps(
                            {
                                "slug": spec.slug,
                                "name": spec.name,
                                "stance": spec.stance,
                                "topic": req.topic,
                            }
                        ),
                    )
                )

    identity, token = _mint_observer_token(room_name)
    return CreateDebateResponse(
        room=room_name,
        ws_url=settings.livekit_url,
        observer_token=token,
        observer_identity=identity,
        debate=response_debate,
    )


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
