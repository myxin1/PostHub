from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, Field, field_validator

from app.models import ActionDestination, IntegrationStatus, IntegrationType, PostStatus, SourceType, UserRole


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=6, max_length=200)
    password_confirm: str = Field(min_length=6, max_length=200)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        e = (v or "").strip().lower()
        if "@" not in e or "." not in e.split("@", 1)[-1]:
            raise ValueError("invalid_email")
        return e

    @field_validator("password_confirm")
    @classmethod
    def _confirm_password(cls, v: str, info):
        pw = info.data.get("password")
        if pw is not None and v != pw:
            raise ValueError("password_mismatch")
        return v


class LoginRequest(BaseModel):
    login: str = Field(validation_alias=AliasChoices("login", "email", "access_id"))
    password: str

    @field_validator("login")
    @classmethod
    def _normalize_login(cls, v: str) -> str:
        return (v or "").strip().lower()


class MeResponse(BaseModel):
    id: str
    email: str
    access_id: str | None = None
    role: UserRole
    timezone: str
    created_at: datetime


class InviteUserRequest(BaseModel):
    email: str
    role: UserRole = UserRole.USER
    access_id: str | None = None

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        e = (v or "").strip().lower()
        if "@" not in e or "." not in e.split("@", 1)[-1]:
            raise ValueError("invalid_email")
        return e


class InviteUserResponse(BaseModel):
    user_id: str
    outbox_id: str


class SetPasswordRequest(BaseModel):
    token: str = Field(min_length=20, max_length=500)
    password: str = Field(min_length=6, max_length=200)
    password_confirm: str = Field(min_length=6, max_length=200)

    @field_validator("password_confirm")
    @classmethod
    def _confirm_password(cls, v: str, info):
        pw = info.data.get("password")
        if pw is not None and v != pw:
            raise ValueError("password_mismatch")
        return v


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    active: bool = True
    schedule_config: dict[str, Any] = Field(default_factory=dict)
    anti_block_config: dict[str, Any] = Field(default_factory=dict)
    publish_config: dict[str, Any] = Field(default_factory=dict)


class ProfileOut(BaseModel):
    id: str
    name: str
    active: bool
    schedule_config: dict[str, Any]
    anti_block_config: dict[str, Any]
    publish_config: dict[str, Any]
    created_at: datetime


class SourceCreate(BaseModel):
    type: SourceType
    value: str = Field(min_length=3)
    active: bool = True


class SourceOut(BaseModel):
    id: str
    profile_id: str
    type: SourceType
    value: str
    active: bool
    created_at: datetime


class ActionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    destination: ActionDestination
    prompt_text: str = Field(min_length=1)
    active: bool = True


class ActionOut(BaseModel):
    id: str
    name: str
    destination: ActionDestination
    prompt_text: str
    active: bool
    created_at: datetime


class IntegrationCreate(BaseModel):
    type: IntegrationType
    name: str = Field(min_length=1, max_length=128)
    credentials: dict[str, Any] = Field(default_factory=dict)


class IntegrationOut(BaseModel):
    id: str
    type: IntegrationType
    name: str
    status: IntegrationStatus
    last_checked_at: datetime | None
    created_at: datetime


class PostOut(BaseModel):
    id: str
    status: PostStatus
    scheduled_for: datetime | None
    published_at: datetime | None
    wp_post_id: int | None
    wp_url: str | None
    created_at: datetime
    updated_at: datetime


class JobLogOut(BaseModel):
    id: str
    stage: str
    status: str
    message: str
    meta_json: dict[str, Any]
    created_at: datetime
