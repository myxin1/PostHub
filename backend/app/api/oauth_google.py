from __future__ import annotations

import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from authlib.integrations.starlette_client import OAuth

from app.api.deps import get_db
from app.config import settings
from app.models import User, UserRole
from app.security import create_access_token


router = APIRouter(tags=["oauth"])


def _oauth() -> OAuth:
    oauth = OAuth()
    verify = not settings.http_insecure_skip_verify
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile", "verify": verify},
    )
    return oauth


def _callback_uri(request: Request) -> str:
    """Build the OAuth callback URI, forcing https in production."""
    import os
    base = os.getenv("BASE_URL", "").rstrip("/")
    if base:
        return f"{base}/app/auth/google/callback"
    uri = str(request.url_for("google_callback"))
    # Reverse proxies often terminate SSL at the edge; ensure https
    if uri.startswith("http://"):
        uri = "https://" + uri[len("http://"):]
    return uri


@router.get("/app/login/google", include_in_schema=False)
async def google_login(request: Request):
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="google_oauth_not_configured")
    oauth = _oauth()
    redirect_uri = _callback_uri(request)
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/app/auth/google/callback", include_in_schema=False, name="google_callback")
async def google_callback(request: Request, db=Depends(get_db)):
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="google_oauth_not_configured")
    oauth = _oauth()
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as exc:
        return RedirectResponse(
            f"/app/login?msg=Erro+OAuth:+{str(exc)[:120]}",
            status_code=status.HTTP_302_FOUND,
        )
    userinfo = token.get("userinfo") or {}
    email = str(userinfo.get("email") or "").strip().lower()
    if not email:
        return RedirectResponse("/app/login?msg=Falha+no+login+Google", status_code=status.HTTP_302_FOUND)
    user = db.scalar(select(User).where(User.email == email))
    if not user:
        return RedirectResponse("/app/login?msg=Acesso+negado.+Solicite+ao+administrador.", status_code=status.HTTP_302_FOUND)
    access_token = create_access_token(subject=user.id, role=user.role.value)
    resp = RedirectResponse("/app", status_code=status.HTTP_302_FOUND)
    resp.set_cookie("access_token", access_token, httponly=True, samesite="lax")
    return resp

