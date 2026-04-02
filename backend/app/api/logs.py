from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import get_current_user, get_db
from app.api.schemas import JobLogOut
from app.models import JobLog, User, UserRole


router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("", response_model=list[JobLogOut])
def list_logs(user: User = Depends(get_current_user), db=Depends(get_db), user_id: str | None = None):
    q = select(JobLog).order_by(JobLog.created_at.desc()).limit(500)
    if user.role == UserRole.ADMIN:
        if user_id:
            q = q.where(JobLog.user_id == user_id)
    else:
        q = q.where(JobLog.user_id == user.id)
    rows = list(db.scalars(q))
    return [
        JobLogOut(
            id=l.id,
            stage=l.stage,
            status=l.status,
            message=l.message,
            meta_json=l.meta_json,
            created_at=l.created_at,
        )
        for l in rows
    ]
