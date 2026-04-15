from __future__ import annotations

import base64
from urllib.parse import urljoin

import certifi
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps import get_current_user, get_db
from app.api.schemas import IntegrationCreate, IntegrationOut
from app.config import settings
from app.crypto import CryptoError, decrypt_json, encrypt_json
from app.models import Integration, IntegrationStatus, IntegrationType, User


router = APIRouter(prefix="/api/integrations", tags=["integrations"])


@router.get("", response_model=list[IntegrationOut])
def list_integrations(user: User = Depends(get_current_user), db=Depends(get_db)):
    rows = list(db.scalars(select(Integration).where(Integration.user_id == user.id).order_by(Integration.created_at.desc())))
    return [
        IntegrationOut(id=i.id, type=i.type, name=i.name, status=i.status, last_checked_at=i.last_checked_at, created_at=i.created_at)
        for i in rows
    ]


@router.post("", response_model=IntegrationOut, status_code=status.HTTP_201_CREATED)
def create_integration(body: IntegrationCreate, user: User = Depends(get_current_user), db=Depends(get_db)):
    try:
        encrypted = encrypt_json(body.credentials)
    except CryptoError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    integ = Integration(user_id=user.id, type=body.type, name=body.name, credentials_encrypted=encrypted)
    db.add(integ)
    db.commit()
    db.refresh(integ)
    return IntegrationOut(
        id=integ.id,
        type=integ.type,
        name=integ.name,
        status=integ.status,
        last_checked_at=integ.last_checked_at,
        created_at=integ.created_at,
    )


@router.delete("/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_integration(integration_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    integ = db.scalar(select(Integration).where(Integration.id == integration_id, Integration.user_id == user.id))
    if not integ:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    db.delete(integ)
    db.commit()


@router.post("/{integration_id}/test")
def test_integration(integration_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    integ = db.scalar(select(Integration).where(Integration.id == integration_id, Integration.user_id == user.id))
    if not integ:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    try:
        creds = decrypt_json(integ.credentials_encrypted)
    except CryptoError as e:
        integ.status = IntegrationStatus.ERROR
        db.add(integ)
        db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if integ.type == IntegrationType.WORDPRESS:
        base_url = str(creds.get("base_url") or "")
        if "users" in creds:
            users = creds["users"] or []
            active_username = str(creds.get("active_username") or "")
            active_user = next((u for u in users if u.get("username") == active_username), users[0] if users else {})
            username = str(active_user.get("username") or "")
            app_password = str(active_user.get("app_password") or "")
        else:
            username = str(creds.get("username") or "")
            app_password = str(creds.get("app_password") or "")
        if not base_url or not username or not app_password:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_credentials")
        url = urljoin(base_url.rstrip("/") + "/", "wp-json/wp/v2/users/me?context=edit")
        auth = base64.b64encode(f"{username}:{app_password}".encode("utf-8")).decode("ascii")
        verify = False if settings.http_insecure_skip_verify else certifi.where()
        with httpx.Client(timeout=settings.wordpress_timeout_seconds, follow_redirects=True, verify=verify) as client:
            resp = client.get(url, headers={"Authorization": f"Basic {auth}"})
        ok = resp.status_code < 400
        integ.status = IntegrationStatus.CONNECTED if ok else IntegrationStatus.ERROR
        db.add(integ)
        db.commit()
        return {"ok": ok, "status_code": resp.status_code}
    if integ.type == IntegrationType.OPENAI:
        api_key = str(creds.get("api_key") or "").strip()
        if not api_key:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_credentials")
        try:
            from app.services.openai_service import OpenAIError, generate_text as openai_generate
            openai_generate(prompt="Respond with OK", content="OK", api_key=api_key, model=str(creds.get("model") or "").strip() or "gpt-4o-mini")
            integ.status = IntegrationStatus.CONNECTED
        except OpenAIError as e:
            integ.status = IntegrationStatus.ERROR
            db.add(integ)
            db.commit()
            return {"ok": False, "error": str(e)}
        db.add(integ)
        db.commit()
        return {"ok": True}
    integ.status = IntegrationStatus.CONNECTED
    db.add(integ)
    db.commit()
    return {"ok": True}
