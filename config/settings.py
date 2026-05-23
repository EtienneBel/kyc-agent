from pydantic_settings import BaseSettings
from pydantic import computed_field
from typing import Literal


class Settings(BaseSettings):
    # ── Environment ────────────────────────────────────────────
    APP_ENV: Literal["local", "prod"] = "local"

    # ── LLM ────────────────────────────────────────────────────
    GEMINI_API_KEY: str = ""
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "gemma2:2b"

    # ── Database ───────────────────────────────────────────────
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "kyc_agent"
    DB_USER: str = "kyc_user"
    DB_PASSWORD: str = "kyc_secret"

    # ── KYC thresholds ─────────────────────────────────────────
    KYC_AUTO_APPROVE_THRESHOLD: int = 95
    KYC_AUTO_REJECT_THRESHOLD: int = 70
    FACE_MATCH_MIN_CONFIDENCE: float = 85.0

    # ── SMS ────────────────────────────────────────────────────
    SMS_PROVIDER: Literal["mock", "africastalking"] = "mock"
    AFRICASTALKING_USERNAME: str = ""
    AFRICASTALKING_API_KEY: str = ""
    AFRICASTALKING_SENDER_ID: str = "KYC-AGENT"

    # ── A2A ────────────────────────────────────────────────────
    HUMAN_REVIEW_AGENT_URL: str = "http://localhost:8001"

    @computed_field
    @property
    def DATABASE_URL(self) -> str:
        return (
            f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @computed_field
    @property
    def active_model(self) -> str:
        """
        local  → Gemma 2B via Ollama  (on-premise, data never leaves your infra)
        prod   → Gemini 1.5 Flash     (Google Cloud, scalable)
        """
        if self.APP_ENV == "local":
            return f"ollama/{self.OLLAMA_MODEL}"
        return "gemini/gemini-1.5-flash"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
