from __future__ import annotations

from dataclasses import dataclass

import feedparser

from app.services.http_client import get_client


@dataclass(frozen=True)
class RssItem:
    url: str
    title: str | None


def fetch_rss_items(feed_url: str, *, limit: int = 20) -> list[RssItem]:
    with get_client() as client:
        resp = client.get(feed_url, headers={"user-agent": "PostHubBot/1.0"})
        resp.raise_for_status()
        parsed = feedparser.parse(resp.text)
    items: list[RssItem] = []
    for entry in parsed.entries[:limit]:
        url = getattr(entry, "link", None) or getattr(entry, "id", None)
        if not url:
            continue
        title = getattr(entry, "title", None)
        items.append(RssItem(url=str(url), title=str(title) if title else None))
    return items


def keyword_to_google_news_rss(keyword: str) -> str:
    from urllib.parse import quote_plus

    q = quote_plus((keyword or "").strip())
    return f"https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
