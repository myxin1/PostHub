from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps import get_current_user, get_db
from app.api.schemas import ProfileCreate, ProfileOut
from app.models import AutomationProfile, User
from app.queue import JOB_COLLECT, enqueue_job


router = APIRouter(prefix="/api/profiles", tags=["profiles"])


@router.get("", response_model=list[ProfileOut])
def list_profiles(user: User = Depends(get_current_user), db=Depends(get_db)):
    rows = list(db.scalars(select(AutomationProfile).where(AutomationProfile.user_id == user.id).order_by(AutomationProfile.created_at.desc())))
    return [
        ProfileOut(
            id=p.id,
            name=p.name,
            active=p.active,
            schedule_config=p.schedule_config_json,
            anti_block_config=p.anti_block_config_json,
            publish_config=p.publish_config_json,
            created_at=p.created_at,
        )
        for p in rows
    ]


@router.post("", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
def create_profile(body: ProfileCreate, user: User = Depends(get_current_user), db=Depends(get_db)):
    p = AutomationProfile(
        user_id=user.id,
        name=body.name,
        active=body.active,
        schedule_config_json=body.schedule_config,
        anti_block_config_json=body.anti_block_config,
        publish_config_json=body.publish_config,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return ProfileOut(
        id=p.id,
        name=p.name,
        active=p.active,
        schedule_config=p.schedule_config_json,
        anti_block_config=p.anti_block_config_json,
        publish_config=p.publish_config_json,
        created_at=p.created_at,
    )


@router.put("/{profile_id}", response_model=ProfileOut)
def update_profile(profile_id: str, body: ProfileCreate, user: User = Depends(get_current_user), db=Depends(get_db)):
    p = db.scalar(select(AutomationProfile).where(AutomationProfile.id == profile_id, AutomationProfile.user_id == user.id))
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    p.name = body.name
    p.active = body.active
    p.schedule_config_json = body.schedule_config
    p.anti_block_config_json = body.anti_block_config
    p.publish_config_json = body.publish_config
    db.add(p)
    db.commit()
    db.refresh(p)
    return ProfileOut(
        id=p.id,
        name=p.name,
        active=p.active,
        schedule_config=p.schedule_config_json,
        anti_block_config=p.anti_block_config_json,
        publish_config=p.publish_config_json,
        created_at=p.created_at,
    )


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile(profile_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    p = db.scalar(select(AutomationProfile).where(AutomationProfile.id == profile_id, AutomationProfile.user_id == user.id))
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    db.delete(p)
    db.commit()


@router.post("/{profile_id}/run", status_code=status.HTTP_202_ACCEPTED)
def run_profile(profile_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    p = db.scalar(select(AutomationProfile).where(AutomationProfile.id == profile_id, AutomationProfile.user_id == user.id))
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    enqueue_job(db, user_id=user.id, profile_id=p.id, job_type=JOB_COLLECT, payload={})
    db.commit()
    return {"status": "queued"}
