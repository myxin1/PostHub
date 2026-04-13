from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _build_engine(raw: str):
    """Use pg8000 (pure Python) for PostgreSQL so Vercel Lambda has no binary deps.
    pg8000 doesn't accept ?sslmode=require — SSL is passed via connect_args instead."""
    import re
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
            return create_engine(url, future=True, connect_args={"ssl_context": ctx})
        return create_engine(url, future=True)
    return create_engine(raw, future=True)


engine = _build_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)


def db_session() -> Session:
    return SessionLocal()

