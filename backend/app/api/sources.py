from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps import get_current_user, get_db
from app.api.schemas import SourceCreate, SourceOut
from app.models import AutomationProfile, Source, User


router = APIRouter(prefix="/api/profiles/{profile_id}/sources", tags=["sources"])


@router.get("", response_model=list[SourceOut])
def list_sources(profile_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    profile = db.scalar(select(AutomationProfile).where(AutomationProfile.id == profile_id, AutomationProfile.user_id == user.id))
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    rows = list(db.scalars(select(Source).where(Source.profile_id == profile_id).order_by(Source.created_at.desc())))
    return [
        SourceOut(id=s.id, profile_id=s.profile_id, type=s.type, value=s.value, active=s.active, created_at=s.created_at)
        for s in rows
    ]


@router.post("", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
def create_source(profile_id: str, body: SourceCreate, user: User = Depends(get_current_user), db=Depends(get_db)):
    profile = db.scalar(select(AutomationProfile).where(AutomationProfile.id == profile_id, AutomationProfile.user_id == user.id))
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    s = Source(profile_id=profile_id, type=body.type, value=body.value, active=body.active)
    db.add(s)
    db.commit()
    db.refresh(s)
    return SourceOut(id=s.id, profile_id=s.profile_id, type=s.type, value=s.value, active=s.active, created_at=s.created_at)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(profile_id: str, source_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    profile = db.scalar(select(AutomationProfile).where(AutomationProfile.id == profile_id, AutomationProfile.user_id == user.id))
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    s = db.scalar(select(Source).where(Source.id == source_id, Source.profile_id == profile_id))
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    db.delete(s)
    db.commit()

