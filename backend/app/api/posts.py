from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import get_current_user, get_db
from app.api.schemas import PostOut
from app.models import Post, User, UserRole


router = APIRouter(prefix="/api/posts", tags=["posts"])


@router.get("", response_model=list[PostOut])
def list_posts(user: User = Depends(get_current_user), db=Depends(get_db), user_id: str | None = None):
    q = select(Post).order_by(Post.created_at.desc()).limit(200)
    if user.role == UserRole.ADMIN:
        if user_id:
            q = q.where(Post.user_id == user_id)
    else:
        q = q.where(Post.user_id == user.id)
    rows = list(db.scalars(q))
    return [
        PostOut(
            id=p.id,
            status=p.status,
            scheduled_for=p.scheduled_for,
            published_at=p.published_at,
            wp_post_id=p.wp_post_id,
            wp_url=p.wp_url,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )
        for p in rows
    ]
