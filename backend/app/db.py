from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _build_engine(raw: str):
    """Use pg8000 (pure Python) for PostgreSQL so Vercel Lambda has no binary deps.
    pg8000 doesn't accept ?sslmode=require — SSL is passed via connect_args instead."""
    import re
    _ensure_sqlite_parent(raw)
    if raw.startswith("postgresql://") or raw.startswith("postgres://"):
        url = re.sub(r"^postgres(ql)?://", "postgresql+pg8000://", raw)
        # Strip sslmode query param (not supported by pg8000)
        needs_ssl = "sslmode=require" in url or "sslmode=prefer" in url
        url = re.sub(r"[?&]sslmode=[^&]*", "", url).rstrip("?").rstrip("&")
        if needs_ssl:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return create_engine(url, future=True, pool_pre_ping=True, connect_args={"ssl_context": ctx})
        return create_engine(url, future=True, pool_pre_ping=True)
    if raw.startswith("sqlite:"):
        return create_engine(
            raw,
            future=True,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
    return create_engine(raw, future=True, pool_pre_ping=True)


def _ensure_sqlite_parent(raw: str) -> None:
    if not raw.startswith("sqlite:") or ":memory:" in raw:
        return
    path_str: str | None = None
    if raw.startswith("sqlite:////"):
        path_str = "/" + raw[len("sqlite:////"):]
    elif re.match(r"^sqlite:///[A-Za-z]:", raw):
        path_str = raw[len("sqlite:///"):]
    elif raw.startswith("sqlite:///"):
        path_str = raw[len("sqlite:///"):]
    if not path_str:
        return
    Path(path_str).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


engine = _build_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)


def db_session() -> Session:
    return SessionLocal()

