from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import certifi
import httpx

from app.config import settings


class FacebookError(Exception):
    pass


@dataclass(frozen=True)
class FacebookPostResult:
    post_id: str
    permalink_url: str | None = None


def _graph_url(path: str) -> str:
    version = "v19.0"
    p = path.lstrip("/")
    return f"https://graph.facebook.com/{version}/{p}"


def publish_page_post(*, page_id: str, page_access_token: str, message: str, link: str | None = None) -> FacebookPostResult:
    url = _graph_url(f"{page_id}/feed")
    verify = False if settings.http_insecure_skip_verify else certifi.where()
    data: dict[str, Any] = {"access_token": page_access_token, "message": message}
    if link:
        data["link"] = link
    with httpx.Client(timeout=30, follow_redirects=True, verify=verify) as client:
        resp = client.post(url, data=data)
    if resp.status_code >= 400:
        detail = (resp.text or "").strip().replace("\n", " ")
        raise FacebookError(f"facebook_post_failed:{resp.status_code}:{detail[:240]}")
    j = resp.json()
    post_id = str(j.get("id") or "").strip()
    if not post_id:
        raise FacebookError("facebook_post_missing_id")
    return FacebookPostResult(post_id=post_id, permalink_url=None)


def comment_on_post(*, post_id: str, page_access_token: str, message: str) -> str:
    url = _graph_url(f"{post_id}/comments")
    verify = False if settings.http_insecure_skip_verify else certifi.where()
    data: dict[str, Any] = {"access_token": page_access_token, "message": message}
    with httpx.Client(timeout=30, follow_redirects=True, verify=verify) as client:
        resp = client.post(url, data=data)
    if resp.status_code >= 400:
        detail = (resp.text or "").strip().replace("\n", " ")
        raise FacebookError(f"facebook_comment_failed:{resp.status_code}:{detail[:240]}")
    j = resp.json()
    cid = str(j.get("id") or "").strip()
    if not cid:
        raise FacebookError("facebook_comment_missing_id")
    return cid

