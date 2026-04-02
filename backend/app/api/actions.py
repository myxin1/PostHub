from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps import get_current_user, get_db
from app.api.schemas import ActionCreate, ActionOut
from app.models import AiAction, User


router = APIRouter(prefix="/api/actions", tags=["actions"])


@router.get("", response_model=list[ActionOut])
def list_actions(user: User = Depends(get_current_user), db=Depends(get_db)):
    rows = list(db.scalars(select(AiAction).where(AiAction.user_id == user.id).order_by(AiAction.created_at.desc())))
    return [
        ActionOut(
            id=a.id,
            name=a.name,
            destination=a.destination,
            prompt_text=a.prompt_text,
            active=a.active,
            created_at=a.created_at,
        )
        for a in rows
    ]


@router.post("", response_model=ActionOut, status_code=status.HTTP_201_CREATED)
def create_action(body: ActionCreate, user: User = Depends(get_current_user), db=Depends(get_db)):
    a = AiAction(user_id=user.id, name=body.name, destination=body.destination, prompt_text=body.prompt_text, active=body.active)
    db.add(a)
    db.commit()
    db.refresh(a)
    return ActionOut(id=a.id, name=a.name, destination=a.destination, prompt_text=a.prompt_text, active=a.active, created_at=a.created_at)


@router.put("/{action_id}", response_model=ActionOut)
def update_action(action_id: str, body: ActionCreate, user: User = Depends(get_current_user), db=Depends(get_db)):
    a = db.scalar(select(AiAction).where(AiAction.id == action_id, AiAction.user_id == user.id))
    if not a:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    a.name = body.name
    a.destination = body.destination
    a.prompt_text = body.prompt_text
    a.active = body.active
    db.add(a)
    db.commit()
    db.refresh(a)
    return ActionOut(id=a.id, name=a.name, destination=a.destination, prompt_text=a.prompt_text, active=a.active, created_at=a.created_at)


@router.delete("/{action_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_action(action_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    a = db.scalar(select(AiAction).where(AiAction.id == action_id, AiAction.user_id == user.id))
    if not a:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    db.delete(a)
    db.commit()

