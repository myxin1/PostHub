from __future__ import annotations

import asyncio
import os
import socket

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.api.actions import router as actions_router
from app.api.admin_users import router as admin_router
from app.api.auth import router as auth_router
from app.api.integrations import router as integrations_router
from app.api.logs import router as logs_router
from app.api.oauth_google import router as oauth_router
from app.api.posts import router as posts_router
from app.api.profiles import router as profiles_router
from app.api.sources import router as sources_router
from app.config import settings
from app.db import Base, engine
from app.models import UserRole
from app.security import hash_password
from app.worker import run_worker_tick
from app.web import router as web_router


def create_app() -> FastAPI:
    app = FastAPI(title="PostHub")
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        # Rotas web /app/* não autenticadas → redirect para login
        if exc.status_code == 401 and request.url.path.startswith("/app"):
            return RedirectResponse(url="/app/login", status_code=302)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
    brand_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "Logo"))
    if os.path.isdir(brand_dir):
        app.mount("/brand", StaticFiles(directory=brand_dir), name="brand")
    sounds_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "SONS"))
    if os.path.isdir(sounds_dir):
        app.mount("/sons", StaticFiles(directory=sounds_dir), name="sons")

    _startup_error: list[str] = []

    @app.on_event("startup")
    async def _startup():
        try:
            Base.metadata.create_all(bind=engine)
            _auto_migrate_schema()
            _seed_admin_user()
        except Exception as exc:  # noqa: BLE001
            import traceback
            _startup_error.append(traceback.format_exc())
        if os.getenv("POSTHUB_INLINE_WORKER", "1") != "0":
            worker_id = f"inline:{socket.gethostname()}:{os.getpid()}"

            async def loop():
                while True:
                    try:
                        did_work = await asyncio.to_thread(run_worker_tick, worker_id=worker_id)
                    except Exception:
                        did_work = False
                    await asyncio.sleep(0.2 if did_work else 1.0)

            asyncio.create_task(loop())

    @app.get("/api/worker/tick", include_in_schema=False)
    async def _worker_tick(request: Request):
        """Endpoint chamado pelo Vercel Cron a cada minuto."""
        secret = os.getenv("CRON_SECRET", "")
        if secret and request.headers.get("authorization") != f"Bearer {secret}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        import time, traceback as _tb
        worker_id = f"cron:{socket.gethostname()}"
        ticks, deadline = 0, time.monotonic() + 50
        last_error: str | None = None
        while time.monotonic() < deadline:
            try:
                did_work = await asyncio.to_thread(run_worker_tick, worker_id=worker_id)
            except Exception as _exc:
                last_error = _tb.format_exc()[-400:]
                break
            if not did_work:
                break
            ticks += 1
        return JSONResponse({"ok": True, "ticks": ticks, "last_error": last_error})

    @app.get("/api/debug/jobs", include_in_schema=False)
    def _debug_jobs(request: Request):
        """Diagnóstico completo: jobs + posts — protegido por CRON_SECRET."""
        from fastapi.responses import PlainTextResponse
        from app.db import db_session
        from app.models import Job, Post, PostStatus
        from sqlalchemy import select, desc, func as _func
        secret = os.getenv("CRON_SECRET", "")
        if secret and request.headers.get("authorization") != f"Bearer {secret}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        lines = []
        with db_session() as db:
            # Contagem de posts por status
            for st in PostStatus:
                cnt = db.scalar(select(_func.count()).select_from(Post).where(Post.status == st)) or 0
                lines.append(f"[posts] {st.value}: {cnt}")
            lines.append("")
            # Jobs por tipo+status
            from sqlalchemy import text as _text
            rows = db.execute(_text(
                "SELECT type, status, COUNT(*) as n FROM jobs GROUP BY type, status ORDER BY type, status"
            )).fetchall()
            for r in rows:
                lines.append(f"[jobs] {r[0]:<22} | {r[1]:<10} | n={r[2]}")
            lines.append("")
            # Últimos 20 jobs com erro ou não-succeeded
            for j in db.scalars(select(Job).where(Job.status != 'succeeded').order_by(desc(Job.updated_at)).limit(20)).all():
                err = str(j.last_error or "").strip()[:120]
                lines.append(f"{str(j.updated_at)[:19]} | {j.type:<20} | {j.status.value:<10} | att={j.attempts}/{j.max_attempts} | {err}")
        return PlainTextResponse("\n".join(lines) or "no data")

    @app.get("/api/setup", include_in_schema=False)
    def _setup():
        """Force DB setup — call once after deploy to create tables and seed admin."""
        from fastapi.responses import PlainTextResponse
        log = []
        try:
            Base.metadata.create_all(bind=engine)
            log.append("OK: create_all")
        except Exception as exc:
            log.append(f"ERR create_all: {exc}")
        try:
            _auto_migrate_schema()
            log.append("OK: migrate")
        except Exception as exc:
            log.append(f"ERR migrate: {exc}")
        try:
            _seed_admin_user()
            log.append("OK: seed_admin")
        except Exception as exc:
            log.append(f"ERR seed_admin: {exc}")
        if _startup_error:
            log.append(f"Startup error: {_startup_error[0]}")
        return PlainTextResponse("\n".join(log))

    app.include_router(auth_router)
    app.include_router(profiles_router)
    app.include_router(sources_router)
    app.include_router(actions_router)
    app.include_router(integrations_router)
    app.include_router(posts_router)
    app.include_router(logs_router)
    app.include_router(admin_router)
    app.include_router(oauth_router)
    app.include_router(web_router)

    return app


app = create_app()


def _auto_migrate_schema() -> None:
    url = str(engine.url)
    if url.startswith("sqlite"):
        _sqlite_auto_migrate()
    elif url.startswith("postgresql"):
        _postgres_auto_migrate()


def _sqlite_auto_migrate() -> None:
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()]
        if "must_set_password" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN must_set_password BOOLEAN NOT NULL DEFAULT 0"))
        if "access_id" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN access_id VARCHAR(32)"))
        indexes = [row[1] for row in conn.execute(text("PRAGMA index_list(users)")).fetchall()]
        if "ux_users_access_id" not in indexes:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_users_access_id ON users(access_id)"))

        ai_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(ai_actions)")).fetchall()]
        if "profile_id" not in ai_cols:
            conn.execute(text("ALTER TABLE ai_actions ADD COLUMN profile_id VARCHAR(36)"))
        ai_indexes = [row[1] for row in conn.execute(text("PRAGMA index_list(ai_actions)")).fetchall()]
        if "ix_ai_actions_profile_id" not in ai_indexes:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ai_actions_profile_id ON ai_actions(profile_id)"))

        integ_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(integrations)")).fetchall()]
        if "profile_id" not in integ_cols:
            conn.execute(text("ALTER TABLE integrations ADD COLUMN profile_id VARCHAR(36)"))
        integ_indexes = [row[1] for row in conn.execute(text("PRAGMA index_list(integrations)")).fetchall()]
        if "ix_integrations_profile_id" not in integ_indexes:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_integrations_profile_id ON integrations(profile_id)"))

        prof_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(automation_profiles)")).fetchall()]
        if "publish_config_json" not in prof_cols:
            conn.execute(text("ALTER TABLE automation_profiles ADD COLUMN publish_config_json TEXT NOT NULL DEFAULT '{}'"))


def _postgres_auto_migrate() -> None:
    """Best-effort schema fixes for older Postgres deployments."""
    with engine.begin() as conn:
        conn.execute(text("ALTER TYPE integrationtype ADD VALUE IF NOT EXISTS 'OPENAI'"))


def _seed_admin_user() -> None:
    login_id = (os.getenv("POSTHUB_ADMIN_LOGIN") or "adm").strip().lower()
    email = (os.getenv("POSTHUB_ADMIN_EMAIL") or "admin@posthub.local").strip().lower()
    password = os.getenv("POSTHUB_ADMIN_PASSWORD") or "000000"
    if not login_id:
        login_id = "adm"
    pw_hash = hash_password(password)
    with engine.begin() as conn:
        # Find by email first, then fall back to any existing admin (handles email changes)
        row = conn.execute(
            text("SELECT id FROM users WHERE email = :email OR (role = :role AND access_id = :aid) LIMIT 1"),
            {"email": email, "role": UserRole.ADMIN.value, "aid": login_id},
        ).fetchone()
        if row:
            conn.execute(
                text("UPDATE users SET email = :email, role = :role, access_id = :aid, password_hash = :pw WHERE id = :id"),
                {"email": email, "role": UserRole.ADMIN.value, "aid": login_id, "pw": pw_hash, "id": row[0]},
            )
            return
        conn.execute(
            text(
                "INSERT INTO users (id, email, access_id, password_hash, must_set_password, role, timezone, created_at) "
                "VALUES (:id, :email, :access_id, :password_hash, false, :role, 'UTC', :created_at)"
            ),
            {
                "id": str(__import__("uuid").uuid4()),
                "email": email,
                "access_id": login_id,
                "password_hash": pw_hash,
                "role": UserRole.ADMIN.value,
                "created_at": __import__("datetime").datetime.utcnow(),
            },
        )
