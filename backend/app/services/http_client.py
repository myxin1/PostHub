from __future__ import annotations

import certifi
import httpx

from app.config import settings


def get_client() -> httpx.Client:
    verify = False if settings.http_insecure_skip_verify else certifi.where()
    return httpx.Client(timeout=settings.http_timeout_seconds, follow_redirects=True, verify=verify)
