"""Runtime configuration loaded from environment variables.

Every process (judge worker, debater worker, orchestrator) imports `settings`
from this module; LiveKit Agents' CLI also reads LIVEKIT_* from env directly,
so keep the names aligned with LiveKit's conventions.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    livekit_url: str = Field(default="", alias="LIVEKIT_URL")
    livekit_api_key: str = Field(default="", alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(default="", alias="LIVEKIT_API_SECRET")

    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    cartesia_api_key: str = Field(default="", alias="CARTESIA_API_KEY")

    debate_model: str = Field(default="gemini-2.5-flash", alias="DEBATE_MODEL")
    factcheck_hallucination_threshold: float = Field(
        default=0.8, alias="FACTCHECK_HALLUCINATION_THRESHOLD"
    )

    orchestrator_host: str = Field(default="0.0.0.0", alias="ORCHESTRATOR_HOST")
    orchestrator_port: int = Field(default=8000, alias="ORCHESTRATOR_PORT")
    web_origin: str = Field(default="http://localhost:5173", alias="WEB_ORIGIN")


settings = Settings()
