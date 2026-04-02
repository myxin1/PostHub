from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from app.api.deps import get_current_user, get_db
from app.api.schemas import LoginRequest, MeResponse, RegisterRequest, SetPasswordRequest, TokenResponse
from app.models import PasswordToken, PasswordTokenType, User, UserRole
from app.security import create_access_token, hash_password, verify_password


router = APIRouter(prefix="/api/auth", tags=["auth"])

def _validate_password(pw: str) -> None:
    if pw is None or len(pw) < 6:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="weak_password")


def _hash_token(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()



@router.post("/register", response_model=MeResponse)
def register(body: RegisterRequest, db=Depends(get_db)):
    existing = db.scalar(select(User).where(User.email == body.email))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email_already_exists")
    _validate_password(body.password)
    total_users = db.scalar(select(func.count()).select_from(User))
    role = UserRole.ADMIN if (total_users or 0) == 0 else UserRole.USER
    user = User(email=body.email, password_hash=hash_password(body.password), role=role, must_set_password=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return MeResponse(
        id=user.id,
        email=user.email,
        access_id=getattr(user, "access_id", None),
        role=user.role,
        timezone=user.timezone,
        created_at=user.created_at,
    )


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db=Depends(get_db)):
    login_value = (body.login or "").strip().lower()
    if "@" in login_value:
        user = db.scalar(select(User).where(User.email == login_value))
    else:
        user = db.scalar(select(User).where(User.access_id == login_value))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")
    if getattr(user, "must_set_password", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="password_not_set")
    token = create_access_token(subject=user.id, role=user.role.value)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user)):
    return MeResponse(
        id=user.id,
        email=user.email,
        access_id=getattr(user, "access_id", None),
        role=user.role,
        timezone=user.timezone,
        created_at=user.created_at,
    )


@router.post("/set-password")
def set_password(body: SetPasswordRequest, db=Depends(get_db)):
    token_hash = _hash_token(body.token)
    t = db.scalar(select(PasswordToken).where(PasswordToken.token_hash == token_hash))
    if not t:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_token")
    now = datetime.utcnow()
    if t.used_at is not None or t.expires_at < now:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_token")
    _validate_password(body.password)
    user = db.scalar(select(User).where(User.id == t.user_id))
    if not user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_token")
    user.password_hash = hash_password(body.password)
    user.must_set_password = False
    db.add(user)
    t.used_at = now
    db.add(t)
    db.commit()
    return {"ok": True}


def create_password_token(db, *, user_id: str, token_type: PasswordTokenType, ttl_hours: int = 48) -> tuple[str, PasswordToken]:
    token = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    t = PasswordToken(
        user_id=user_id,
        type=token_type,
        token_hash=_hash_token(token),
        expires_at=now + timedelta(hours=ttl_hours),
        used_at=None,
    )
    db.add(t)
    db.flush()
    return token, t
