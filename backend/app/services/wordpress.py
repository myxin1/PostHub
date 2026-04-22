from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import certifi
import httpx

from app.config import settings

WORDPRESS_USER_AGENT = "PostHUB/1.0 (+https://posthub-three.vercel.app)"


class WordPressError(Exception):
    pass


@dataclass(frozen=True)
class WordPressPostResult:
    post_id: int
    link: str | None


def _client(username: str, app_password: str, verify: bool | str) -> httpx.Client:
    """Return an httpx.Client with BasicAuth so credentials survive redirects."""
    return httpx.Client(
        auth=httpx.BasicAuth(username, app_password),
        timeout=settings.wordpress_timeout_seconds,
        follow_redirects=True,
        trust_env=False,
        verify=verify,
        headers={
            "Accept": "application/json",
            "User-Agent": WORDPRESS_USER_AGENT,
        },
    )


def upload_media(
    *,
    base_url: str,
    username: str,
    app_password: str,
    filename: str,
    content_type: str,
    data: bytes,
) -> int:
    url = urljoin(base_url.rstrip("/") + "/", "wp-json/wp/v2/media")
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": content_type,
    }
    verify = False if settings.http_insecure_skip_verify else certifi.where()
    with _client(username, app_password, verify) as client:
        resp = client.post(url, headers=headers, content=data)
    if resp.status_code >= 400:
        detail = (resp.text or "").strip().replace("\n", " ")
        raise WordPressError(f"media_upload_failed:{resp.status_code}:{detail[:200]}")
    media = resp.json()
    media_id = media.get("id")
    if not media_id:
        raise WordPressError("media_upload_missing_id")
    return int(media_id)


def create_post(
    *,
    base_url: str,
    username: str,
    app_password: str,
    title: str,
    content_html: str,
    status: str = "publish",
    featured_media_id: int | None = None,
    tags: list[int] | None = None,
    categories: list[int] | None = None,
) -> WordPressPostResult:
    url = urljoin(base_url.rstrip("/") + "/", "wp-json/wp/v2/posts")
    payload: dict[str, Any] = {"title": title, "content": content_html, "status": status}
    if featured_media_id:
        payload["featured_media"] = featured_media_id
    if tags:
        payload["tags"] = tags
    if categories:
        payload["categories"] = categories
    verify = False if settings.http_insecure_skip_verify else certifi.where()
    with _client(username, app_password, verify) as client:
        resp = client.post(url, json=payload)
    if resp.status_code >= 400:
        detail = (resp.text or "").strip().replace("\n", " ")
        raise WordPressError(f"post_create_failed:{resp.status_code}:{detail[:200]}")
    post = resp.json()
    post_id = post.get("id")
    if not post_id:
        raise WordPressError("post_create_missing_id")
    return WordPressPostResult(post_id=int(post_id), link=post.get("link"))


def update_post(
    *,
    base_url: str,
    username: str,
    app_password: str,
    post_id: int,
    title: str,
    content_html: str,
    status: str = "publish",
    featured_media_id: int | None = None,
    tags: list[int] | None = None,
    categories: list[int] | None = None,
) -> WordPressPostResult:
    url = urljoin(base_url.rstrip("/") + "/", f"wp-json/wp/v2/posts/{int(post_id)}")
    payload: dict[str, Any] = {"title": title, "content": content_html, "status": status}
    if featured_media_id:
        payload["featured_media"] = featured_media_id
    if tags is not None:
        payload["tags"] = tags
    if categories is not None:
        payload["categories"] = categories
    verify = False if settings.http_insecure_skip_verify else certifi.where()
    with _client(username, app_password, verify) as client:
        resp = client.post(url, json=payload)
    if resp.status_code >= 400:
        detail = (resp.text or "").strip().replace("\n", " ")
        raise WordPressError(f"post_update_failed:{resp.status_code}:{detail[:200]}")
    post = resp.json()
    updated_id = post.get("id") or post_id
    return WordPressPostResult(post_id=int(updated_id), link=post.get("link"))


def delete_post(*, base_url: str, username: str, app_password: str, post_id: int, force: bool = True) -> None:
    url = urljoin(base_url.rstrip("/") + "/", f"wp-json/wp/v2/posts/{int(post_id)}")
    verify = False if settings.http_insecure_skip_verify else certifi.where()
    params: dict[str, Any] = {}
    if force:
        params["force"] = "true"
    with _client(username, app_password, verify) as client:
        resp = client.delete(url, params=params)
    if resp.status_code >= 400:
        detail = (resp.text or "").strip().replace("\n", " ")
        raise WordPressError(f"post_delete_failed:{resp.status_code}:{detail[:200]}")


def list_categories(*, base_url: str, username: str, app_password: str) -> list[dict[str, Any]]:
    url = urljoin(base_url.rstrip("/") + "/", "wp-json/wp/v2/categories")
    verify = False if settings.http_insecure_skip_verify else certifi.where()
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        with _client(username, app_password, verify) as client:
            resp = client.get(url, params={"per_page": 100, "page": page})
        if resp.status_code == 400 and "rest_invalid_param" in resp.text:
            break
        if resp.status_code == 400 and "rest_post_invalid_page_number" in resp.text:
            break
        if resp.status_code >= 400:
            detail = (resp.text or "").strip().replace("\n", " ")
            raise WordPressError(f"categories_list_failed:{resp.status_code}:{detail[:200]}")
        items = resp.json() or []
        if not items:
            break
        for c in items:
            if isinstance(c, dict) and c.get("id") and c.get("name"):
                out.append({"id": int(c["id"]), "name": str(c["name"])})
        page += 1
        if page > 50:
            break
    return out


def get_or_create_tag_id(*, base_url: str, username: str, app_password: str, tag_name: str) -> int:
    name = (tag_name or "").strip()
    if not name:
        raise WordPressError("tag_name_empty")
    verify = False if settings.http_insecure_skip_verify else certifi.where()
    search_url = urljoin(base_url.rstrip("/") + "/", "wp-json/wp/v2/tags")
    with _client(username, app_password, verify) as client:
        resp = client.get(search_url, params={"search": name, "per_page": 100})
    if resp.status_code >= 400:
        detail = (resp.text or "").strip().replace("\n", " ")
        raise WordPressError(f"tag_search_failed:{resp.status_code}:{detail[:200]}")
    items = resp.json() or []
    for t in items:
        if isinstance(t, dict) and str(t.get("name", "")).strip().lower() == name.lower() and t.get("id"):
            return int(t["id"])
    with _client(username, app_password, verify) as client:
        resp2 = client.post(search_url, json={"name": name})
    if resp2.status_code >= 400:
        try:
            data = resp2.json()
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("code") == "term_exists":
            term_id = (data.get("data") or {}).get("term_id")
            if term_id:
                return int(term_id)
        detail = (resp2.text or "").strip().replace("\n", " ")
        raise WordPressError(f"tag_create_failed:{resp2.status_code}:{detail[:200]}")
    created = resp2.json()
    if not isinstance(created, dict) or not created.get("id"):
        raise WordPressError("tag_create_missing_id")
    return int(created["id"])
