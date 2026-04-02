from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True)

    database_url: str = "sqlite:///./posthub.db"
    jwt_secret: str = "dev-secret-change-me"
    jwt_issuer: str = "posthub"
    jwt_audience: str = "posthub"
    access_token_ttl_seconds: int = 60 * 60 * 12

    encryption_key_b64: str = ""

    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-flash-latest"

    wordpress_timeout_seconds: int = 30
    http_timeout_seconds: int = 30
    http_insecure_skip_verify: bool = True

    session_secret: str = "dev-session-secret-change-me"
    google_client_id: str = ""
    google_client_secret: str = ""


settings = Settings()
