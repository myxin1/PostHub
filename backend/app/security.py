from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from app.config import settings


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_access_token(*, subject: str, role: str) -> str:
    now = datetime.now(tz=timezone.utc)
    exp = now + timedelta(seconds=settings.access_token_ttl_seconds)
    payload = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": subject,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )


class AuthError(Exception):
    pass


def get_subject_from_token(token: str) -> tuple[str, str]:
    try:
        payload = decode_access_token(token)
    except JWTError as e:
        raise AuthError("invalid_token") from e
    sub = payload.get("sub")
    role = payload.get("role")
    if not sub or not role:
        raise AuthError("invalid_token")
    return str(sub), str(role)
