from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _build_url(raw: str) -> str:
    """Use pg8000 (pure Python) for PostgreSQL so Vercel Lambda has no binary deps."""
    if raw.startswith("postgresql://") or raw.startswith("postgres://"):
        return raw.replace("postgresql://", "postgresql+pg8000://", 1).replace("postgres://", "postgresql+pg8000://", 1)
    return raw


engine = create_engine(_build_url(settings.database_url), future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)


def db_session() -> Session:
    return SessionLocal()

