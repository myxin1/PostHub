"""Vercel Python entry point — routes all requests to the FastAPI app."""
from __future__ import annotations

import os
import sys

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.normpath(os.path.join(_HERE, "..", "backend"))
sys.path.insert(0, _BACKEND)

# ── SQLite fallback for local/serverless without DATABASE_URL ─────────────────
# On Vercel /tmp is the only writable directory — data won't persist between
# cold-starts. Set DATABASE_URL in Vercel env vars (Neon PostgreSQL) for
# a persistent deployment.
if not os.environ.get("DATABASE_URL"):
    os.environ["DATABASE_URL"] = "sqlite:////tmp/posthub.db"

# ── Enable inline background worker ──────────────────────────────────────────
os.environ.setdefault("POSTHUB_INLINE_WORKER", "1")

# ── Import the FastAPI app ────────────────────────────────────────────────────
from app.main import app  # noqa: E402  (must come after sys.path setup)

__all__ = ["app"]
