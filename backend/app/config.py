from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_APP_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _APP_DIR.parent
_REPO_DIR = _BACKEND_DIR.parent


def _env_files() -> tuple[str, ...]:
    candidates = (
        _REPO_DIR / ".env",
        _BACKEND_DIR / ".env",
        _REPO_DIR / ".env.local",
    )
    return tuple(str(path) for path in candidates if path.exists())


def _default_data_dir(raw: str = "") -> Path:
    explicit = (raw or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    railway_mount = (os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    if railway_mount:
        return Path(railway_mount).expanduser().resolve()
    return _BACKEND_DIR.resolve()


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def _normalize_database_url(raw: str, *, data_dir: Path) -> str:
    value = (raw or "").strip()
    if not value:
        return _sqlite_url(data_dir / "posthub.db")
    if not value.startswith("sqlite:"):
        return value
    if ":memory:" in value:
        return value
    if value.startswith("sqlite:////"):
        return value
    if re.match(r"^sqlite:///[A-Za-z]:", value):
        return value.replace("\\", "/")
    rel = value[len("sqlite:///"):].replace("\\", "/")
    rel = rel[2:] if rel.startswith("./") else rel
    return _sqlite_url(data_dir / rel)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_env_files(), env_ignore_empty=True, extra="ignore")

    database_url: str = ""
    posthub_data_dir: str = ""
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

    @model_validator(mode="after")
    def _normalize_runtime_paths(self) -> "Settings":
        data_dir = _default_data_dir(self.posthub_data_dir)
        self.posthub_data_dir = str(data_dir)
        self.database_url = _normalize_database_url(self.database_url, data_dir=data_dir)
        return self


settings = Settings()
