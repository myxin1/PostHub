"""Vercel Python entry point."""
from __future__ import annotations

import os
import sys
import traceback

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.normpath(os.path.join(_HERE, "..", "backend"))
sys.path.insert(0, _BACKEND)

# ── Env defaults ──────────────────────────────────────────────────────────────
if not os.environ.get("DATABASE_URL"):
    os.environ["DATABASE_URL"] = "sqlite:////tmp/posthub.db"
os.environ.setdefault("POSTHUB_INLINE_WORKER", "0")

# ── Import the app — show any error clearly ───────────────────────────────────
_app = None
_import_tb: str | None = None

try:
    from app.main import app as _app  # noqa: E402
except Exception:
    _import_tb = traceback.format_exc()

if _app is not None:
    app = _app
else:
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse

    app = FastAPI()

    _tb = _import_tb or "Unknown import error"

    @app.get("/{path:path}")
    async def _import_error(path: str = ""):
        return PlainTextResponse(
            f"IMPORT ERROR\n\nPython path: {sys.path}\n\nBackend dir: {_BACKEND}\n"
            f"Dir exists: {os.path.isdir(_BACKEND)}\n"
            f"Files: {os.listdir(_BACKEND) if os.path.isdir(_BACKEND) else 'N/A'}\n\n"
            f"Traceback:\n{_tb}",
            status_code=500,
        )
