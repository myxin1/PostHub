from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.utcnow()


class UserRole(str, enum.Enum):
    ADMIN = "ADMIN"
    USER = "USER"


class IntegrationType(str, enum.Enum):
    WORDPRESS = "WORDPRESS"
    FACEBOOK = "FACEBOOK"
    INSTAGRAM = "INSTAGRAM"
    GEMINI = "GEMINI"


class IntegrationStatus(str, enum.Enum):
    CONNECTED = "CONNECTED"
    EXPIRED = "EXPIRED"
    ERROR = "ERROR"


class SourceType(str, enum.Enum):
    URL = "URL"
    RSS = "RSS"
    KEYWORD = "KEYWORD"


class ActionDestination(str, enum.Enum):
    WORDPRESS = "WORDPRESS"
    FACEBOOK = "FACEBOOK"
    INSTAGRAM = "INSTAGRAM"


class PostStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    access_id: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    must_set_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), nullable=False, default=UserRole.USER)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    profiles: Mapped[list["AutomationProfile"]] = relationship(back_populates="user")
    actions: Mapped[list["AiAction"]] = relationship(back_populates="user")
    integrations: Mapped[list["Integration"]] = relationship(back_populates="user")


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True)
    plan: Mapped[str] = mapped_column(String(64), nullable=False, default="free")
    limits_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    user: Mapped[User] = relationship()


class Integration(Base):
    __tablename__ = "integrations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("automation_profiles.id"), nullable=True, index=True
    )
    type: Mapped[IntegrationType] = mapped_column(Enum(IntegrationType), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    credentials_encrypted: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[IntegrationStatus] = mapped_column(
        Enum(IntegrationStatus), nullable=False, default=IntegrationStatus.CONNECTED
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    user: Mapped[User] = relationship(back_populates="integrations")
    profile: Mapped[Optional["AutomationProfile"]] = relationship(back_populates="integrations")


class AutomationProfile(Base):
    __tablename__ = "automation_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    schedule_config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    anti_block_config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    publish_config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    user: Mapped[User] = relationship(back_populates="profiles")
    sources: Mapped[list["Source"]] = relationship(back_populates="profile", cascade="all, delete-orphan")
    actions: Mapped[list["AiAction"]] = relationship(back_populates="profile", cascade="all, delete-orphan")
    integrations: Mapped[list["Integration"]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
        overlaps="profile",
    )


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(String(36), ForeignKey("automation_profiles.id"), nullable=False, index=True)
    type: Mapped[SourceType] = mapped_column(Enum(SourceType), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    profile: Mapped[AutomationProfile] = relationship(back_populates="sources")


class AiAction(Base):
    __tablename__ = "ai_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("automation_profiles.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    destination: Mapped[ActionDestination] = mapped_column(Enum(ActionDestination), nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    user: Mapped[User] = relationship(back_populates="actions")
    profile: Mapped[AutomationProfile | None] = relationship(back_populates="actions")


class CollectedContent(Base):
    __tablename__ = "collected_contents"
    __table_args__ = (UniqueConstraint("user_id", "fingerprint", name="uq_content_fingerprint"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    profile_id: Mapped[str] = mapped_column(String(36), ForeignKey("automation_profiles.id"), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(36), ForeignKey("sources.id"), nullable=False, index=True)

    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    lead_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    profile_id: Mapped[str] = mapped_column(String(36), ForeignKey("automation_profiles.id"), nullable=False, index=True)
    collected_content_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("collected_contents.id"), nullable=False, index=True
    )

    status: Mapped[PostStatus] = mapped_column(Enum(PostStatus), nullable=False, default=PostStatus.pending, index=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    wp_post_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wp_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    outputs_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    tags_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    categories_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    profile_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("automation_profiles.id"), nullable=True)
    post_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("posts.id"), nullable=True, index=True)

    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), nullable=False, default=JobStatus.queued, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow, index=True)

    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    profile_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("automation_profiles.id"), nullable=True)
    post_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("posts.id"), nullable=True, index=True)

    stage: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    meta_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class PasswordTokenType(str, enum.Enum):
    invite = "invite"
    reset = "reset"


class PasswordToken(Base):
    __tablename__ = "password_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    type: Mapped[PasswordTokenType] = mapped_column(Enum(PasswordTokenType), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class EmailOutboxStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


class EmailOutbox(Base):
    __tablename__ = "email_outbox"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    to_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    meta_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[EmailOutboxStatus] = mapped_column(Enum(EmailOutboxStatus), nullable=False, default=EmailOutboxStatus.pending, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
