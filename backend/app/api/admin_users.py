from __future__ import annotations

import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps import get_db, require_admin
from app.api.schemas import InviteUserRequest, InviteUserResponse
from app.api.auth import create_password_token
from app.models import EmailOutbox, PasswordTokenType, User
from app.security import hash_password


router = APIRouter(prefix="/api/admin/users", tags=["admin"])


def _generate_access_id() -> str:
    n1 = secrets.randbelow(10000)
    n2 = secrets.randbelow(10000)
    return f"ph-{n1:04d}-{n2:04d}"


def _normalize_access_id(v: str) -> str:
    x = (v or "").strip().lower()
    if not x:
        return x
    for ch in x:
        ok = ch.isalnum() or ch in ("-", "_")
        if not ok:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_access_id")
    if len(x) < 3 or len(x) > 32:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_access_id")
    return x


@router.post("/invite", response_model=InviteUserResponse)
def invite_user(body: InviteUserRequest, _admin: User = Depends(require_admin), db=Depends(get_db)):
    existing = db.scalar(select(User).where(User.email == body.email))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email_already_exists")
    access_id = _normalize_access_id(body.access_id or "")
    if access_id:
        if db.scalar(select(User.id).where(User.access_id == access_id)) is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="access_id_already_exists")
    else:
        for _ in range(10):
            candidate = _generate_access_id()
            if db.scalar(select(User.id).where(User.access_id == candidate)) is None:
                access_id = candidate
                break
        if not access_id:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="cannot_generate_access_id")
    temp_pw = secrets.token_urlsafe(24)
    user = User(email=body.email, access_id=access_id, password_hash=hash_password(temp_pw), role=body.role, must_set_password=True)
    db.add(user)
    db.flush()

    token, token_row = create_password_token(db, user_id=user.id, token_type=PasswordTokenType.invite, ttl_hours=72)
    invite_url = f"/app/set-password?token={token}"
    out = EmailOutbox(
        to_email=user.email,
        subject="Convite PostHub - crie sua senha",
        body="Você foi convidado(a) para o PostHub. Use o link para criar sua senha.",
        meta_json={"invite_url": invite_url, "type": "invite", "token_id": token_row.id, "access_id": access_id},
        created_at=datetime.utcnow(),
    )
    db.add(out)
    db.commit()
    return InviteUserResponse(user_id=user.id, outbox_id=out.id)


@router.get("/outbox")
def list_outbox(_admin: User = Depends(require_admin), db=Depends(get_db)):
    rows = list(db.scalars(select(EmailOutbox).order_by(EmailOutbox.created_at.desc()).limit(200)))
    return [
        {
            "id": o.id,
            "to_email": o.to_email,
            "subject": o.subject,
            "status": o.status,
            "created_at": o.created_at,
            "sent_at": o.sent_at,
            "meta": o.meta_json,
        }
        for o in rows
    ]
