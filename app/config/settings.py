"""Application settings and environment configuration."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly typed application configuration loaded from environment or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    APP_NAME: str = "AI Job Intelligence"
    APP_VERSION: str = "0.1.0"
    APP_ENV: Literal["development", "testing", "staging", "production"] = "development"
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"

    # API Server (Bound strictly to localhost for private local development safety)
    API_HOST: str = "127.0.0.1"
    API_PORT: int = 8000

    # Persistence (PostgreSQL)
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/job_intelligence_dev"
    TEST_DATABASE_URL: str | None = None

    # Scheduling
    SCHEDULER_ENABLED: bool = False

    # Email Notifications (V1)
    EMAIL_NOTIFICATIONS_ENABLED: bool = False
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    SMTP_TIMEOUT_SECONDS: float = 10.0
    NOTIFICATION_RECIPIENT_EMAIL: str = ""
    NOTIFICATION_SENDER_EMAIL: str = "notifications@ai-job-intelligence.local"
    DASHBOARD_BASE_URL: str = "http://127.0.0.1:8000"
    ADDITIONAL_TRUSTED_ORIGINS: list[str] = []
    ADDITIONAL_ALLOWED_HOSTS: list[str] = []

    # LLM Gateway Placeholders
    GEMINI_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    OPENROUTER_API_KEY: str = ""
    OLLAMA_BASE_URL: str = "http://localhost:11434"


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton instance of application settings."""
    return Settings()
