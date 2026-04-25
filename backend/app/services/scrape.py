from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.http_client import get_client


@dataclass(frozen=True)
class ScrapedContent:
    canonical_url: str
    title: str | None
    raw_html: str
    extracted_text: str | None
    lead_image_url: str | None


_TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
}


def _normalize_url(url: str) -> str:
    u = urlparse(url)
    if not u.scheme or not u.netloc:
        return url
    q = u.query
    if q:
        kept: list[str] = []
        for part in q.split("&"):
            if not part:
                continue
            key = part.split("=", 1)[0]
            if key in _TRACKING_QUERY_KEYS:
                continue
            kept.append(part)
        q = "&".join(kept)
    path = u.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return u._replace(path=path, query=q, fragment="").geturl()


def extract_candidate_links(*, raw_html: str, base_url: str, max_links: int = 80) -> list[str]:
    soup = BeautifulSoup(raw_html or "", "html.parser")
    base = urlparse(base_url)
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a"):
        href = a.get("href")
        if not href:
            continue
        h = str(href).strip()
        if not h or h.startswith("#"):
            continue
        if h.startswith("javascript:") or h.startswith("mailto:") or h.startswith("tel:"):
            continue
        abs_url = urljoin(base_url, h)
        u = urlparse(abs_url)
        if u.scheme not in ("http", "https"):
            continue
        if base.netloc and u.netloc and u.netloc != base.netloc:
            continue
        clean = _normalize_url(u.geturl())
        if clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
        if len(out) >= max_links:
            break
    return out


def is_probably_homepage(*, url: str) -> bool:
    u = urlparse((url or "").strip())
    path = (u.path or "/").strip()
    if not path or path == "/" or path.lower() in ("/index.html", "/home", "/inicio"):
        return True
    segs = [p for p in path.split("/") if p]
    return len(segs) == 0


def _site_recipe_url_score(url: str) -> int:
    u = urlparse(url)
    host = (u.netloc or "").lower()
    path = (u.path or "").lower()
    if "tudogostoso.com.br" in host:
        if re.search(r"^/receita/\d+[-\w]*\.html$", path):
            return 200
        if path.startswith("/receita/"):
            return 120
        if path.startswith("/receitas/"):
            return 20
    if "receiteria.com.br" in host:
        if re.search(r"^/receita/[^/]+/?$", path):
            return 200
        if path.startswith("/receita/"):
            return 120
        if path.startswith("/receitas/"):
            return 25
    if re.search(r"/receita(s)?/", path):
        return 80
    if "/receita" in path:
        return 60
    return 0


def _is_listing_url(url: str) -> bool:
    u = urlparse(url)
    path = (u.path or "").lower()
    if "/receitas/" in path and "/receita/" not in path:
        return True
    return any(
        part in path
        for part in (
            "/categoria/",
            "/categorias/",
            "/tag/",
            "/tags/",
            "/busca",
            "/pesquisa",
            "/sitemap",
            "/politica",
            "/privacidade",
            "/termos",
            "/contato",
            "/sobre",
            "/login",
            "/cadastro",
        )
    )


def discover_deep_start_links(*, raw_html: str, base_url: str, max_links: int = 30) -> list[str]:
    """Return category/pagination/archive URLs found on a page to use as deeper starting points.

    Calling code should shuffle the result and pick one randomly, then scrape it for recipe links
    instead of always scraping the same homepage.
    """
    soup = BeautifulSoup(raw_html or "", "html.parser")
    base = urlparse(base_url)
    out: list[str] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        if not href or href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:"):
            continue
        abs_url = urljoin(base_url, href)
        u = urlparse(abs_url)
        if u.scheme not in ("http", "https"):
            continue
        if base.netloc and u.netloc and u.netloc != base.netloc:
            continue
        clean = _normalize_url(u.geturl())
        if clean in seen:
            continue
        if is_probably_homepage(url=clean):
            continue
        path = (u.path or "").lower()
        query = (u.query or "").lower()
        # Pagination patterns: /page/2/, /p/3/, ?page=2, ?paged=2
        is_paginated = bool(
            re.search(r"/page/\d+/?$", path)
            or re.search(r"/p/\d+/?$", path)
            or re.search(r"[?&]paged?=\d+", query)
        )
        # Category/tag/listing pages that contain multiple recipes
        is_category = bool(
            re.search(r"/(categoria|categorias|category|tag|tags|arquivo|archive|cardapio)/", path)
            or re.search(r"/(receitas|recipes)/[^/]+/?$", path)
        )
        if is_paginated or is_category:
            seen.add(clean)
            out.append(clean)
            if len(out) >= max_links:
                break

    return out


def _iter_jsonld_objects(raw_html: str) -> list[object]:
    soup = BeautifulSoup(raw_html or "", "html.parser")
    out: list[object] = []
    for s in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        txt = (s.string or "").strip()
        if not txt:
            continue
        try:
            out.append(json.loads(txt))
        except Exception:
            continue
    return out


def _jsonld_contains_recipe(obj: object) -> bool:
    if obj is None:
        return False
    if isinstance(obj, dict):
        t = obj.get("@type") or obj.get("type")
        if isinstance(t, str) and t.lower() == "recipe":
            return True
        if isinstance(t, list) and any(isinstance(x, str) and x.lower() == "recipe" for x in t):
            return True
        if "@graph" in obj and isinstance(obj["@graph"], list):
            return any(_jsonld_contains_recipe(x) for x in obj["@graph"])
        return any(_jsonld_contains_recipe(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_jsonld_contains_recipe(x) for x in obj)
    return False


def _text_recipe_signal_score(text: str) -> int:
    t = (text or "").lower()
    if not t:
        return 0
    score = 0
    if "ingrediente" in t or "ingredientes" in t:
        score += 2
    if "modo de preparo" in t or "modo de fazer" in t or "preparo" in t or "instru" in t:
        score += 2
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    ingredient_like = 0
    unit_re = re.compile(r"\b(\d+|meia|meio|uma|duas|tres|três)\b.*\b(g|kg|ml|l|x[ií]cara|colher|pitada|unidade|dente)\b")
    for ln in lines[:400]:
        if unit_re.search(ln):
            ingredient_like += 1
            if ingredient_like >= 3:
                break
    if ingredient_like >= 3:
        score += 2
    if len(t) > 900:
        score += 1
    return score


def looks_like_recipe_page(*, url: str, extracted_text: str | None, raw_html: str | None = None) -> bool:
    norm = _normalize_url(url or "")
    if is_probably_homepage(url=norm):
        return False
    if _is_listing_url(norm):
        return False
    url_score = _site_recipe_url_score(norm)
    if url_score >= 180:
        return True
    if raw_html:
        if any(_jsonld_contains_recipe(o) for o in _iter_jsonld_objects(raw_html)):
            return True
    text_score = _text_recipe_signal_score(extracted_text or "")
    if url_score >= 80 and text_score >= 2:
        return True
    return text_score >= 5


def discover_recipe_links(*, raw_html: str, base_url: str, max_links: int = 120) -> list[str]:
    links = extract_candidate_links(raw_html=raw_html, base_url=base_url, max_links=max_links)
    scored: list[tuple[int, str]] = []
    for u in links:
        nu = _normalize_url(u)
        if is_probably_homepage(url=nu):
            continue
        if _is_listing_url(nu):
            continue
        s = _site_recipe_url_score(nu)
        if s <= 0:
            continue
        scored.append((s, nu))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out: list[str] = []
    seen: set[str] = set()
    for _, u in scored:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= 80:
            break
    if out:
        return out
    return links[:40]


def _extract_lead_image(soup: BeautifulSoup, *, base_url: str) -> str | None:
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        return urljoin(base_url, og["content"].strip())
    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        return urljoin(base_url, tw["content"].strip())
    img = soup.find("img")
    if img and img.get("src"):
        return urljoin(base_url, img["src"].strip())
    return None


def scrape_url(url: str) -> ScrapedContent:
    with get_client() as client:
        resp = client.get(url, headers={"user-agent": "PostHubBot/1.0"})
        resp.raise_for_status()
        html = resp.text
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    article = soup.find("article")
    text = None
    if article:
        text = article.get_text(separator="\n", strip=True)
    if not text and soup.body:
        text = soup.body.get_text(separator="\n", strip=True)
    lead_image_url = _extract_lead_image(soup, base_url=url)
    return ScrapedContent(
        canonical_url=str(resp.url),
        title=title,
        raw_html=html,
        extracted_text=text,
        lead_image_url=lead_image_url,
    )
