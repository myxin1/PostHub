from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
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


def discover_feed_urls(*, site_url: str, raw_html: str | None = None) -> list[str]:
    base = (site_url or "").strip()
    if not base:
        return []
    parsed = urlparse(base)
    root = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else base.rstrip("/")
    candidates: list[str] = []
    if raw_html:
        try:
            soup = BeautifulSoup(raw_html, "html.parser")
            for link in soup.find_all("link"):
                href = str(link.get("href") or "").strip()
                if not href:
                    continue
                rel = " ".join(str(x).lower() for x in (link.get("rel") or []))
                typ = str(link.get("type") or "").lower()
                if "alternate" not in rel and "rss" not in href.lower() and "feed" not in href.lower():
                    continue
                if "rss" not in typ and "atom" not in typ and "feed" not in href.lower() and "rss" not in href.lower():
                    continue
                candidates.append(urljoin(base, href))
        except Exception:
            pass
    for suffix in ("/feed", "/feed/", "/rss", "/rss/", "/feed.xml", "/rss.xml", "/atom.xml"):
        if root:
            candidates.append(urljoin(root.rstrip("/") + "/", suffix.lstrip("/")))
    seen: set[str] = set()
    out: list[str] = []
    for item in candidates:
        clean = item.strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def fetch_site_feed_items(*, site_url: str, raw_html: str | None = None, limit: int = 20) -> tuple[str | None, list[RssItem]]:
    for feed_url in discover_feed_urls(site_url=site_url, raw_html=raw_html):
        try:
            items = fetch_rss_items(feed_url, limit=limit)
        except Exception:
            continue
        if items:
            return feed_url, items
    return None, []


def keyword_to_google_news_rss(keyword: str) -> str:
    from urllib.parse import quote_plus

    q = quote_plus((keyword or "").strip())
    return f"https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
