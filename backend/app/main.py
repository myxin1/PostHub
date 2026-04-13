from __future__ import annotations

import asyncio
import os
import socket

from fastapi import FastAPI
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
    # Trust proxy headers (X-Forwarded-Proto, X-Forwarded-For) from Vercel/reverse proxies
    # so that request.url uses https:// instead of http://
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
    brand_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "Logo"))
    if os.path.isdir(brand_dir):
        app.mount("/brand", StaticFiles(directory=brand_dir), name="brand")

    _startup_error: list[str] = []

    @app.on_event("startup")
    async def _startup():
        try:
            Base.metadata.create_all(bind=engine)
            _sqlite_auto_migrate()
            _seed_admin_user()
        except Exception as exc:  # noqa: BLE001
            import traceback
            _startup_error.append(traceback.format_exc())

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
            _sqlite_auto_migrate()
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
        if os.getenv("POSTHUB_INLINE_WORKER", "0") == "1":
            worker_id = f"inline:{socket.gethostname()}:{os.getpid()}"

            async def loop():
                while True:
                    try:
                        did_work = await asyncio.to_thread(run_worker_tick, worker_id=worker_id)
                    except Exception:
                        did_work = False
                    await asyncio.sleep(0.2 if did_work else 1.0)

            asyncio.create_task(loop())

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


def _seed_admin_user() -> None:
    login_id = (os.getenv("POSTHUB_ADMIN_LOGIN") or "adm").strip().lower()
    email = (os.getenv("POSTHUB_ADMIN_EMAIL") or "admin@posthub.local").strip().lower()
    password = os.getenv("POSTHUB_ADMIN_PASSWORD") or "000000"
    if not login_id:
        login_id = "adm"
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT id, role, access_id FROM users WHERE email = :email LIMIT 1"),
            {"email": email},
        ).fetchone()
        if row:
            if row[1] != UserRole.ADMIN.value:
                conn.execute(text("UPDATE users SET role = :role WHERE email = :email"), {"role": UserRole.ADMIN.value, "email": email})
            if not row[2]:
                conn.execute(text("UPDATE users SET access_id = :access_id WHERE email = :email"), {"access_id": login_id, "email": email})
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
                "password_hash": hash_password(password),
                "role": UserRole.ADMIN.value,
                "created_at": __import__("datetime").datetime.utcnow(),
            },
        )
