from __future__ import annotations

import hashlib
import html
import json
import os
import random
import re
import socket
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.crypto import CryptoError, decrypt_json
from app.db import db_session
from app.models import (
    ActionDestination,
    AiAction,
    AutomationProfile,
    CollectedContent,
    Integration,
    IntegrationType,
    Job,
    JobStatus,
    Post,
    PostStatus,
    Source,
    SourceType,
)
from app.queue import JOB_AI, JOB_CLEAN, JOB_COLLECT, JOB_FACEBOOK_PUBLISH, JOB_MEDIA, JOB_PUBLISH_WP, enqueue_job, get_due_job, log_event, schedule_retry
from app.services.clean import clean_text
from app.services.facebook import FacebookError, comment_on_post, publish_page_photo, publish_page_post
from app.services.gemini import GeminiError, generate_text as gemini_generate_text
from app.services.openai_service import OpenAIError, generate_text as openai_generate_text
from app.services.images import download_and_prepare_image
from app.services.rss import fetch_rss_items, fetch_site_feed_items, keyword_to_google_news_rss
from app.services.scrape import discover_deep_start_links, discover_recipe_links, is_probably_homepage, looks_like_recipe_page, scrape_url
from app.services.wordpress import WordPressError, create_post, delete_post, get_or_create_tag_id, list_categories, upload_media


def _fingerprint(*, user_id: str, canonical_url: str) -> str:
    canonical_url = _normalize_url_for_dedupe(canonical_url)
    h = hashlib.sha256()
    h.update(user_id.encode("utf-8"))
    h.update(b"|")
    h.update(canonical_url.encode("utf-8"))
    return h.hexdigest()


_TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
}


def _normalize_url_for_dedupe(url: str) -> str:
    u = urlparse((url or "").strip())
    if not u.scheme or not u.netloc:
        return (url or "").strip()
    path = u.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    q = [(k, v) for (k, v) in parse_qsl(u.query, keep_blank_values=True) if k not in _TRACKING_QUERY_KEYS]
    query = urlencode(q, doseq=True)
    return urlunparse((u.scheme, u.netloc, path, "", query, ""))


def _ensure_post_processing(db, post: Post):
    if post.status == PostStatus.pending:
        post.status = PostStatus.processing
        post.updated_at = datetime.utcnow()
        db.add(post)


def _is_post_canceled(db, post: Post) -> bool:
    try:
        db.refresh(post)
    except Exception:
        pass
    return isinstance(post.outputs_json, dict) and bool(post.outputs_json.get("canceled_by_user"))


def _is_profile_active(db, profile_id: str | None, user_id: str) -> bool:
    if not profile_id:
        return True
    profile = db.scalar(select(AutomationProfile).where(AutomationProfile.id == profile_id, AutomationProfile.user_id == user_id))
    cfg = dict((profile.publish_config_json if profile else {}) or {})
    return bool(profile and profile.active and not cfg.get("run_stopped_at"))


def _mark_job_skipped_for_inactive_profile(db, job: Job) -> None:
    now = datetime.utcnow()
    if job.post_id:
        post = db.scalar(select(Post).where(Post.id == job.post_id, Post.user_id == job.user_id))
        if post and post.status != PostStatus.completed:
            outputs = dict(post.outputs_json or {})
            outputs["canceled_by_user"] = True
            post.outputs_json = outputs
            post.status = PostStatus.failed
            post.updated_at = now
            db.add(post)
    log_event(
        db,
        user_id=job.user_id,
        profile_id=job.profile_id,
        post_id=job.post_id,
        stage=job.type,
        status="skipped",
        message="profile_inactive_or_stopped",
    )


def _handle_collect(db, job: Job):
    profile_id = job.profile_id
    if not profile_id:
        raise ValueError("missing_profile_id")
    profile = db.scalar(select(AutomationProfile).where(AutomationProfile.id == profile_id, AutomationProfile.user_id == job.user_id))
    profile_cfg = dict((profile.publish_config_json if profile else {}) or {})
    if not profile or not profile.active or profile_cfg.get("run_stopped_at"):
        log_event(db, user_id=job.user_id, profile_id=profile_id, stage=JOB_COLLECT, status="skipped", message="profile_inactive_or_missing")
        return
    sources = list(db.scalars(select(Source).where(Source.profile_id == profile_id, Source.active.is_(True))))
    sched = dict(profile.schedule_config_json or {})
    payload = job.payload_json if isinstance(job.payload_json, dict) else {}
    default_limit = int(sched.get("posts_per_day") or 15)
    default_interval = int(sched.get("interval_minutes") or 0)
    respect = int(payload.get("respect_schedule") or sched.get("respect_schedule") or 0) == 1
    limit = int(payload.get("limit") or default_limit)
    # interval_minutes always applied when set; respect_schedule only gates start_at_utc
    interval_minutes = int(payload.get("interval_minutes") or default_interval)
    schedule_index_start = int(payload.get("schedule_index_start") or 0)
    fast_publish_enabled = bool(profile_cfg.get("fast_publish_enabled"))
    try:
        rss_fallback_after_seconds = int(profile_cfg.get("rss_fallback_after_seconds") or 20)
    except Exception:
        rss_fallback_after_seconds = 20
    rss_fallback_after_seconds = max(5, min(rss_fallback_after_seconds, 180))
    created = 0
    skipped_duplicate = 0
    skipped_non_recipe = 0
    skipped_error = 0
    _collect_deadline = time.perf_counter() + 480  # hard stop after 8 min
    base_run_at = datetime.utcnow()
    base_payload = str(payload.get("base_run_at_utc") or "").strip()
    if base_payload:
        try:
            dt = datetime.fromisoformat(base_payload.replace("Z", "+00:00"))
            base_run_at = dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
        except Exception:
            pass
    start_at_utc = str(sched.get("start_at_utc") or "").strip()
    if respect and start_at_utc:
        try:
            dt = datetime.fromisoformat(start_at_utc.replace("Z", "+00:00"))
            dt_utc = dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
            if dt_utc > base_run_at:
                base_run_at = dt_utc
        except Exception:
            pass

    def _enqueue_scraped(*, s, source_id: str, title_fallback: str | None):
        nonlocal created, skipped_duplicate
        try:
            db.refresh(profile)
        except Exception:
            pass
        profile_cfg = dict(profile.publish_config_json or {})
        if not profile.active or profile_cfg.get("run_stopped_at"):
            return
        fp = _fingerprint(user_id=job.user_id, canonical_url=s.canonical_url)
        content = CollectedContent(
            user_id=job.user_id,
            profile_id=profile_id,
            source_id=source_id,
            canonical_url=s.canonical_url,
            fingerprint=fp,
            title=s.title or title_fallback,
            raw_html=s.raw_html,
            extracted_text=s.extracted_text,
            lead_image_url=s.lead_image_url,
        )
        try:
            with db.begin_nested():
                db.add(content)
                db.flush()
        except IntegrityError:
            skipped_duplicate += 1
            return
        schedule_index = schedule_index_start + created
        run_at = None
        if interval_minutes:
            run_at = base_run_at + timedelta(minutes=interval_minutes * schedule_index)
        post = Post(
            user_id=job.user_id,
            profile_id=profile_id,
            collected_content_id=content.id,
            status=PostStatus.pending,
            scheduled_for=run_at or base_run_at,
        )
        db.add(post)
        db.flush()
        enqueue_job(
            db,
            user_id=job.user_id,
            profile_id=profile_id,
            post_id=post.id,
            job_type=JOB_CLEAN,
            payload={"collected_content_id": content.id},
            run_at=run_at,
        )
        # Commit after each item so the write lock is released between scrapes.
        # profile is re-fetched via db.refresh at next _enqueue_scraped call.
        db.commit()
        created += 1

    def _collect_from_site_rss(*, site_url: str, raw_html: str | None, source_id: str) -> int:
        nonlocal skipped_error, skipped_non_recipe
        try:
            feed_url, items = fetch_site_feed_items(site_url=site_url, raw_html=raw_html, limit=50)
        except Exception:
            return 0
        if not items:
            return 0
        random.shuffle(items)
        before = created
        for item in items:
            if limit and created >= limit:
                break
            try:
                s = scrape_url(item.url)
            except Exception:
                skipped_error += 1
                continue
            if not looks_like_recipe_page(url=s.canonical_url, extracted_text=s.extracted_text, raw_html=s.raw_html):
                skipped_non_recipe += 1
                continue
            _enqueue_scraped(s=s, source_id=source_id, title_fallback=item.title)
        found = created - before
        if found > 0 and feed_url:
            log_event(
                db,
                user_id=job.user_id,
                profile_id=profile_id,
                stage=JOB_COLLECT,
                status="info",
                message="rss_fallback_used",
                meta={"source_id": source_id, "feed_url": feed_url, "created": found},
            )
        return found

    random.shuffle(sources)  # randomize source order each run
    for source in sources:
        if limit and created >= limit:
            break
        if time.perf_counter() >= _collect_deadline:
            break
        if source.type == SourceType.RSS:
            items = fetch_rss_items(source.value, limit=50)
            random.shuffle(items)
            for item in items:
                if limit and created >= limit:
                    break
                if time.perf_counter() >= _collect_deadline:
                    break
                try:
                    s = scrape_url(item.url)
                except Exception:
                    skipped_error += 1
                    continue
                if not looks_like_recipe_page(url=s.canonical_url, extracted_text=s.extracted_text, raw_html=s.raw_html):
                    skipped_non_recipe += 1
                    continue
                _enqueue_scraped(s=s, source_id=source.id, title_fallback=item.title)
        elif source.type == SourceType.KEYWORD:
            feed_url = keyword_to_google_news_rss(source.value)
            items = fetch_rss_items(feed_url, limit=50)
            random.shuffle(items)
            for item in items:
                if limit and created >= limit:
                    break
                if time.perf_counter() >= _collect_deadline:
                    break
                try:
                    s = scrape_url(item.url)
                except Exception:
                    skipped_error += 1
                    continue
                if not looks_like_recipe_page(url=s.canonical_url, extracted_text=s.extracted_text, raw_html=s.raw_html):
                    skipped_non_recipe += 1
                    continue
                _enqueue_scraped(s=s, source_id=source.id, title_fallback=item.title)
        elif source.type == SourceType.URL:
            if limit and created >= limit:
                break
            source_started_at = time.perf_counter()
            try:
                scraped = scrape_url(source.value)
            except Exception:
                skipped_error += 1
                continue
            if not is_probably_homepage(url=scraped.canonical_url) and looks_like_recipe_page(
                url=scraped.canonical_url, extracted_text=scraped.extracted_text, raw_html=scraped.raw_html
            ):
                _enqueue_scraped(s=scraped, source_id=source.id, title_fallback=None)
                continue

            rss_used = False
            if fast_publish_enabled and (time.perf_counter() - source_started_at) >= rss_fallback_after_seconds:
                rss_used = _collect_from_site_rss(
                    site_url=scraped.canonical_url or source.value,
                    raw_html=scraped.raw_html,
                    source_id=source.id,
                ) > 0
                if rss_used and (not limit or created >= limit):
                    continue

            # Deep exploration: find category/pagination links and randomly start
            # from one of them instead of always the same homepage links.
            deep_starts = discover_deep_start_links(raw_html=scraped.raw_html, base_url=scraped.canonical_url)
            explore_raw = scraped.raw_html
            explore_base = scraped.canonical_url
            if deep_starts and time.perf_counter() < _collect_deadline:
                random.shuffle(deep_starts)
                for deep_url in deep_starts[:4]:
                    if time.perf_counter() >= _collect_deadline:
                        break
                    try:
                        deep_scraped = scrape_url(deep_url)
                        explore_raw = deep_scraped.raw_html
                        explore_base = deep_scraped.canonical_url
                        break
                    except Exception:
                        continue

            picked_links = discover_recipe_links(raw_html=explore_raw, base_url=explore_base, max_links=220)
            # Also include links from the original page for variety
            if explore_base != scraped.canonical_url:
                extra = discover_recipe_links(raw_html=scraped.raw_html, base_url=scraped.canonical_url, max_links=80)
                picked_links = list({u: None for u in picked_links + extra}.keys())
            if picked_links:
                random.shuffle(picked_links)
            max_attempts = 220 if not limit else max(120, limit * 12)
            attempts = 0
            for u in picked_links:
                if limit and created >= limit:
                    break
                if time.perf_counter() >= _collect_deadline:
                    break
                if fast_publish_enabled and not rss_used and (time.perf_counter() - source_started_at) >= rss_fallback_after_seconds:
                    rss_used = _collect_from_site_rss(
                        site_url=scraped.canonical_url or source.value,
                        raw_html=scraped.raw_html,
                        source_id=source.id,
                    ) > 0
                    if limit and created >= limit:
                        break
                if attempts >= max_attempts:
                    break
                attempts += 1
                try:
                    s2 = scrape_url(u)
                except Exception:
                    skipped_error += 1
                    continue
                if not looks_like_recipe_page(url=s2.canonical_url, extracted_text=s2.extracted_text, raw_html=s2.raw_html):
                    skipped_non_recipe += 1
                    continue
                _enqueue_scraped(s=s2, source_id=source.id, title_fallback=None)
            if fast_publish_enabled and not rss_used and (not picked_links or not limit or created < limit):
                _collect_from_site_rss(
                    site_url=scraped.canonical_url or source.value,
                    raw_html=scraped.raw_html,
                    source_id=source.id,
                )
    log_event(
        db,
        user_id=job.user_id,
        profile_id=profile_id,
        stage=JOB_COLLECT,
        status="ok",
        message="collect_completed",
        meta={
            "created": created,
            "skipped": skipped_duplicate,
            "skipped_duplicate": skipped_duplicate,
            "skipped_non_recipe": skipped_non_recipe,
            "skipped_error": skipped_error,
            "sources": len(sources),
            "requested": limit,
            "collect_round": int(payload.get("collect_round") or 0),
        },
    )
    try:
        db.refresh(profile)
    except Exception:
        pass
    profile_cfg = dict(profile.publish_config_json or {})
    open_posts = int(
        db.scalar(
            select(func.count()).select_from(Post).where(
                Post.profile_id == profile_id,
                Post.status.in_([PostStatus.pending, PostStatus.processing]),
            )
        )
        or 0
    )
    keep_collect_jobs = 0 if open_posts >= limit else 1
    removed_collect_jobs = _prune_collect_backlog(db, profile_id=profile_id, keep=keep_collect_jobs)
    if removed_collect_jobs:
        log_event(
            db,
            user_id=job.user_id,
            profile_id=profile_id,
            stage=JOB_COLLECT,
            status="info",
            message="collect_backlog_pruned",
            meta={"removed": removed_collect_jobs, "open_posts": open_posts, "limit": limit},
        )
    queued_collect_jobs = int(
        db.scalar(
            select(func.count()).select_from(Job).where(
                Job.profile_id == profile_id,
                Job.type == JOB_COLLECT,
                Job.status == JobStatus.queued,
            )
        )
        or 0
    )
    remaining_needed = max(0, int(limit or 0) - open_posts)
    if (
        profile.active
        and not profile_cfg.get("run_stopped_at")
        and limit
        and created < limit
        and remaining_needed > 0
        and queued_collect_jobs == 0
    ):
        round_n = int(payload.get("collect_round") or 0)
        if round_n < 10:
            enqueue_job(
                db,
                user_id=job.user_id,
                profile_id=profile_id,
                job_type=JOB_COLLECT,
                payload={
                    "limit": int(min(limit - created, remaining_needed)),
                    "interval_minutes": interval_minutes,
                    "respect_schedule": 1 if respect else 0,
                    "collect_round": round_n + 1,
                    "schedule_index_start": schedule_index_start + created,
                    "base_run_at_utc": base_run_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
                },
            )


def _handle_clean(db, job: Job):
    content_id = job.payload_json.get("collected_content_id")
    if not content_id or not job.post_id:
        raise ValueError("missing_content_or_post")
    content = db.scalar(select(CollectedContent).where(CollectedContent.id == content_id, CollectedContent.user_id == job.user_id))
    post = db.scalar(select(Post).where(Post.id == job.post_id, Post.user_id == job.user_id))
    if not content or not post:
        raise ValueError("content_or_post_not_found")
    _ensure_post_processing(db, post)
    content.extracted_text = clean_text(content.extracted_text)
    if _is_post_canceled(db, post):
        return
    db.add(content)
    db.flush()
    enqueue_job(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id, job_type=JOB_AI, payload={"collected_content_id": content_id})
    log_event(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id, stage=JOB_CLEAN, status="ok", message="clean_completed")


def _resolve_prompt(prompt_text: str) -> str:
    """Resolve prompt text — supports multi-variant JSON or plain string."""
    text = (prompt_text or "").strip()
    if not text or not text.startswith("{"):
        return text
    try:
        data = json.loads(text)
        variants: list = data.get("v") or []
        mode = str(data.get("mode") or "1")
        valid = [str(v).strip() for v in variants if str(v).strip()]
        if not valid:
            return ""
        if mode == "random":
            return random.choice(valid)
        idx = int(mode) - 1
        return valid[idx] if 0 <= idx < len(valid) else valid[0]
    except Exception:
        return text


def _build_fast_site_prompt(site_instr: str) -> str:
    return (
        f"{site_instr}\n\n"
        "Modo rapido ativado.\n"
        "Mantenha a mesma estrutura editorial, mas entregue uma versao objetiva para publicar mais rapido.\n"
        "Use titulo, introducao curta, ## Ingredientes, ## Modo de Preparo e ## Dicas.\n"
        "Evite paragrafos longos, contexto excessivo e repeticao.\n"
        "Foque em clareza, completude e leitura agil."
    )


def _has_wordpress_action(db, *, user_id: str, profile_id: str | None) -> bool:
    if profile_id:
        return (
            db.scalar(
                select(AiAction.id).where(
                    AiAction.profile_id == profile_id,
                    AiAction.active.is_(True),
                    AiAction.destination == ActionDestination.WORDPRESS,
                )
            )
            is not None
        )
    return (
        db.scalar(
            select(AiAction.id).where(
                AiAction.user_id == user_id,
                AiAction.active.is_(True),
                AiAction.destination == ActionDestination.WORDPRESS,
            )
        )
        is not None
    )


def _continue_after_ai(db, *, job: Job, post: Post, content: CollectedContent, content_id: str) -> None:
    outputs: dict = dict(post.outputs_json or {})
    if content.lead_image_url and not outputs.get("image"):
        outputs["image"] = {"url": content.lead_image_url}
        post.outputs_json = outputs
        post.updated_at = datetime.utcnow()
        db.add(post)
        db.flush()
    if _is_post_canceled(db, post):
        return
    if _has_wordpress_action(db, user_id=job.user_id, profile_id=job.profile_id):
        enqueue_job(
            db,
            user_id=job.user_id,
            profile_id=job.profile_id,
            post_id=job.post_id,
            job_type=JOB_PUBLISH_WP,
            payload={"collected_content_id": content_id},
        )
    else:
        post.status = PostStatus.completed
        post.updated_at = datetime.utcnow()
        db.add(post)
        log_event(
            db,
            user_id=job.user_id,
            profile_id=job.profile_id,
            post_id=job.post_id,
            stage=JOB_PUBLISH_WP,
            status="skipped",
            message="no_wordpress_action_configured",
        )


def _get_output_image_url(outputs: dict | None, *, fallback_url: str | None = None) -> str:
    """Return the best image URL stored in outputs, tolerating old and new shapes."""
    data = outputs if isinstance(outputs, dict) else {}
    image_val = data.get("image")
    if isinstance(image_val, dict):
        url = str(image_val.get("url") or "").strip()
        if url:
            return url
    elif isinstance(image_val, str):
        url = image_val.strip()
        if url:
            return url
    return str(fallback_url or "").strip()


def _handle_ai(db, job: Job):
    content_id = job.payload_json.get("collected_content_id")
    if not content_id or not job.post_id:
        raise ValueError("missing_content_or_post")
    content = db.scalar(select(CollectedContent).where(CollectedContent.id == content_id, CollectedContent.user_id == job.user_id))
    post = db.scalar(select(Post).where(Post.id == job.post_id, Post.user_id == job.user_id))
    if not content or not post:
        raise ValueError("content_or_post_not_found")
    _ensure_post_processing(db, post)
    base_text = content.extracted_text or ""
    outputs: dict = dict(post.outputs_json or {})
    if "recipe" not in outputs:
        profile = None
        if job.profile_id:
            profile = db.scalar(select(AutomationProfile).where(AutomationProfile.id == job.profile_id))
        gemini_key = None
        openai_key = None
        openai_model = None
        if job.profile_id:
            gi = db.scalar(select(Integration).where(Integration.profile_id == job.profile_id, Integration.type == IntegrationType.GEMINI))
            if gi:
                try:
                    gcreds = decrypt_json(gi.credentials_encrypted)
                    gemini_key = str(gcreds.get("api_key") or "").strip() or None
                except Exception:
                    gemini_key = None
            oi = db.scalar(select(Integration).where(Integration.profile_id == job.profile_id, Integration.type == IntegrationType.OPENAI))
            if oi:
                try:
                    ocreds = decrypt_json(oi.credentials_encrypted)
                    openai_key = str(ocreds.get("api_key") or "").strip() or None
                    openai_model = str(ocreds.get("model") or "").strip() or None
                except Exception:
                    openai_key = None
        publish_cfg = dict((profile.publish_config_json if profile else {}) or {})
        fast_publish_enabled = bool(publish_cfg.get("fast_publish_enabled"))
        allowed_categories = list(publish_cfg.get("categories") or [])
        if not allowed_categories:
            allowed_categories = ["Receitas"]
        default_category = str(publish_cfg.get("default_category") or allowed_categories[0] or "Receitas")
        categories_text = "\n".join(f"- {c}" for c in allowed_categories[:120])
        if job.profile_id:
            site_prompt = db.scalar(
                select(AiAction.prompt_text)
                .where(
                    AiAction.profile_id == job.profile_id,
                    AiAction.active.is_(True),
                    AiAction.destination == ActionDestination.WORDPRESS,
                )
                .order_by(AiAction.created_at.asc())
                .limit(1)
            )
            fb_prompt = db.scalar(
                select(AiAction.prompt_text)
                .where(
                    AiAction.profile_id == job.profile_id,
                    AiAction.active.is_(True),
                    AiAction.destination == ActionDestination.FACEBOOK,
                )
                .order_by(AiAction.created_at.asc())
                .limit(1)
            )
        else:
            site_prompt = db.scalar(
                select(AiAction.prompt_text)
                .where(
                    AiAction.user_id == job.user_id,
                    AiAction.active.is_(True),
                    AiAction.destination == ActionDestination.WORDPRESS,
                )
                .order_by(AiAction.created_at.asc())
                .limit(1)
            )
            fb_prompt = db.scalar(
                select(AiAction.prompt_text)
                .where(
                    AiAction.user_id == job.user_id,
                    AiAction.active.is_(True),
                    AiAction.destination == ActionDestination.FACEBOOK,
                )
                .order_by(AiAction.created_at.asc())
                .limit(1)
            )
        site_instr = _resolve_prompt(str(site_prompt or ""))
        fb_instr = _resolve_prompt(str(fb_prompt or ""))
        if not site_instr:
            site_instr = (
                "Você é um redator culinário. Reescreva a receita abaixo em PT-BR, sem copiar o texto original. "
                "Entregue um texto SEO completo com: Título, Introdução, Tempo de preparo e rendimento, Ingredientes, "
                "Modo de preparo (passo a passo), Dicas e variações."
            )
        if not fb_instr:
            fb_instr = (
                "Crie um texto curto e chamativo para Facebook sobre a receita abaixo, com emojis moderados e CTA. "
                "Finalize com: 👉 veja o modo de preparo nos comentários"
            )
        if fast_publish_enabled:
            site_instr = _build_fast_site_prompt(site_instr)
            fb_instr = (
                f"{fb_instr}\n"
                "Modo rapido ativado. Use um texto curto, direto, com no maximo 3 frases antes do CTA."
            )
        prompt = (
            "Você é um especialista em reescrita de receitas. Reescreva SEM copiar o texto original.\n"
            "Saída obrigatória em JSON (sem texto fora do JSON), com este formato:\n"
            "{\n"
            '  "site": "texto do site",\n'
            '  "facebook": "texto do facebook",\n'
            '  "categoria": "deve ser exatamente UM dos nomes abaixo",\n'
            '  "tags": ["tag1", "tag2", "tag3"]\n'
            "}\n\n"
            "Instruções do campo site:\n"
            f"{site_instr}\n\n"
            "FORMATO OBRIGATÓRIO do campo site (Markdown):\n"
            "- Primeira linha obrigatória: # Título da Receita (usando # markdown)\n"
            "- Em seguida, parágrafo de introdução (sem prefixo 'Introdução:')\n"
            "- Use ## para seções: ## Ingredientes, ## Modo de Preparo, ## Dicas\n"
            "- Use - para cada ingrediente na lista\n"
            "- Use 1. 2. 3. para os passos numerados do modo de preparo\n"
            "- Não use emojis no campo site\n\n"
            "Instruções do campo facebook:\n"
            f"{fb_instr}\n\n"
            "Categorias permitidas (escolha exatamente uma):\n"
            f"{categories_text}\n\n"
            f"Se não tiver certeza, use esta categoria padrão: {default_category}\n\n"
            "Regras:\n"
            "- Não copie frases do texto original\n"
            "- O texto do site deve ser completo e bem formatado\n"
            "- Tags: 3 a 8, específicas (evite genéricos como 'receita')\n"
        )
        seed_title = _sanitize_source_title(content.title or "")

        def _run_ai(p: str, c: str) -> str:
            # Primary: Gemini. Fallback: OpenAI. Vice-versa if only OpenAI is configured.
            if gemini_key:
                try:
                    return gemini_generate_text(prompt=p, content=c, api_key=gemini_key).text
                except Exception as _gem_err:
                    if openai_key:
                        log_event(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id,
                                  stage=JOB_AI, status="warning", message="gemini_failed_fallback_to_openai",
                                  meta={"reason": str(_gem_err)[:160]})
                        return openai_generate_text(prompt=p, content=c, model=openai_model, api_key=openai_key).text
                    raise
            if openai_key:
                try:
                    return openai_generate_text(prompt=p, content=c, model=openai_model, api_key=openai_key).text
                except Exception as _oai_err:
                    if gemini_key:
                        log_event(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id,
                                  stage=JOB_AI, status="warning", message="openai_failed_fallback_to_gemini",
                                  meta={"reason": str(_oai_err)[:160]})
                        return gemini_generate_text(prompt=p, content=c, api_key=gemini_key).text
                    raise
            raise GeminiError("no_ai_key_configured")

        res_text = _run_ai(prompt, f"TÍTULO: {seed_title}\n\n{base_text}")
        if _is_post_canceled(db, post):
            return
        parsed = _parse_ai_json(res_text)
        site_text = str(parsed.get("site") or "").strip()
        fb_text = str(parsed.get("facebook") or "").strip()
        site_ok = len(site_text) >= 200 and ("ingred" in site_text.lower()) and ("preparo" in site_text.lower())
        if not site_ok:
            fallback_prompt = (
                "Você é um especialista em reescrita de receitas.\n"
                "Saída obrigatória em JSON (sem texto fora do JSON), com este formato:\n"
                "{\n"
                '  "site": "texto SEO completo, bem estruturado e fácil de ler (sem emojis)",\n'
                '  "facebook": "texto curto e chamativo (com emojis) e terminar exatamente com: 👉 veja o modo de preparo nos comentários",\n'
                '  "categoria": "deve ser exatamente UM dos nomes abaixo",\n'
                '  "tags": ["tag1", "tag2", "tag3"]\n'
                "}\n\n"
                "Categorias permitidas (escolha exatamente uma):\n"
                f"{categories_text}\n\n"
                f"Se não tiver certeza, use esta categoria padrão: {default_category}\n\n"
                "FORMATO OBRIGATÓRIO do campo site (Markdown):\n"
                "- Primeira linha: # Título da Receita (usando # markdown)\n"
                "- Em seguida, introdução sem prefixo\n"
                "- Use ## para seções: ## Ingredientes, ## Modo de Preparo, ## Dicas\n"
                "- Use - para ingredientes e 1. 2. 3. para os passos\n"
                "- Não use emojis no campo site\n\n"
                "Regras:\n"
                "- Não copie frases do texto original\n"
                "- Não invente ingredientes\n"
            )
            res2_text = _run_ai(fallback_prompt, f"TÍTULO: {seed_title}\n\n{base_text}")
            if _is_post_canceled(db, post):
                return
            parsed2 = _parse_ai_json(res2_text)
            site_text = str(parsed2.get("site") or "").strip()
            fb_text = str(parsed2.get("facebook") or "").strip()
            if len(site_text) < 200:
                raise GeminiError("site_output_too_short")
        required_end = "👉 veja o modo de preparo nos comentários"
        if fb_text and required_end not in fb_text:
            fb_text = fb_text.rstrip() + "\n\n" + required_end
        if fb_text.endswith(required_end) is False:
            fb_text = fb_text.rstrip() + "\n" + required_end
        category = str(parsed.get("categoria") or "").strip()
        if category not in allowed_categories:
            category = default_category if default_category in allowed_categories else allowed_categories[0]
        tags_val = parsed.get("tags") or []
        tags: list[str] = []
        if isinstance(tags_val, list):
            for t in tags_val:
                s = str(t).strip()
                if s:
                    tags.append(s)
        tags = tags[:12]
        extracted_title = _extract_title_from_site_text(site_text)
        final_title = _pt_title_case(extracted_title or seed_title or (content.title or ""))
        outputs["recipe"] = {
            "title": final_title.strip() or _pt_title_case(seed_title or (content.title or "Post")),
            "site": site_text,
            "facebook": fb_text,
            "categoria": category,
            "tags": tags,
        }
    post.outputs_json = outputs
    post.updated_at = datetime.utcnow()
    db.add(post)
    db.flush()
    _continue_after_ai(db, job=job, post=post, content=content, content_id=content_id)
    log_event(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id, stage=JOB_AI, status="ok", message="ai_completed")


def _handle_media(db, job: Job):
    # Legacy compatibility: old queued jobs may still point here.
    content_id = job.payload_json.get("collected_content_id")
    if not content_id or not job.post_id:
        raise ValueError("missing_content_or_post")
    content = db.scalar(select(CollectedContent).where(CollectedContent.id == content_id, CollectedContent.user_id == job.user_id))
    post = db.scalar(select(Post).where(Post.id == job.post_id, Post.user_id == job.user_id))
    if not content or not post:
        raise ValueError("content_or_post_not_found")
    _ensure_post_processing(db, post)
    _continue_after_ai(db, job=job, post=post, content=content, content_id=content_id)
    log_event(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id, stage=JOB_MEDIA, status="ok", message="media_completed")


def _get_wordpress_integration(db, *, user_id: str, profile_id: str | None) -> dict:
    if profile_id:
        integ = db.scalar(
            select(Integration).where(
                Integration.profile_id == profile_id,
                Integration.type == IntegrationType.WORDPRESS,
            )
        )
    else:
        integ = db.scalar(
            select(Integration).where(
                Integration.user_id == user_id,
                Integration.type == IntegrationType.WORDPRESS,
            )
        )
    if not integ:
        raise WordPressError("missing_wordpress_integration")
    try:
        creds = decrypt_json(integ.credentials_encrypted)
    except CryptoError as e:
        raise WordPressError(str(e)) from e
    base_url = str(creds.get("base_url") or "")
    if "users" in creds:
        users = creds["users"] or []
        active_username = str(creds.get("active_username") or "")
        active_user = next((u for u in users if u.get("username") == active_username), users[0] if users else {})
        username = str(active_user.get("username") or "")
        app_password = str(active_user.get("app_password") or "")
    else:
        username = str(creds.get("username") or "")
        app_password = str(creds.get("app_password") or "")
    if not base_url or not username or not app_password:
        raise WordPressError("invalid_wordpress_credentials")
    return {"base_url": base_url, "username": username, "app_password": app_password}


def _handle_publish_wp(db, job: Job):
    if not job.post_id:
        raise ValueError("missing_post_id")
    post = db.scalar(select(Post).where(Post.id == job.post_id, Post.user_id == job.user_id))
    if not post:
        raise ValueError("post_not_found")
    outputs = post.outputs_json or {}
    correction_requested = isinstance(outputs, dict) and bool(outputs.get("correction_requested"))
    if post.status == PostStatus.completed and post.wp_post_id and not correction_requested:
        log_event(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id, stage=JOB_PUBLISH_WP, status="skipped", message="already_published")
        return
    content = db.scalar(select(CollectedContent).where(CollectedContent.id == post.collected_content_id, CollectedContent.user_id == job.user_id))
    if not content:
        raise ValueError("content_not_found")
    _ensure_post_processing(db, post)
    recipe = outputs.get("recipe") if isinstance(outputs, dict) else None
    wp_text = str((recipe or {}).get("site") or "").strip() if isinstance(recipe, dict) else ""
    if not wp_text:
        for k, v in outputs.items():
            if isinstance(k, str) and k.startswith(f"{ActionDestination.WORDPRESS}:") and isinstance(v, dict) and v.get("text"):
                wp_text = str(v["text"])
                break
    if not wp_text:
        post.status = PostStatus.failed
        post.updated_at = datetime.utcnow()
        db.add(post)
        log_event(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id, stage=JOB_PUBLISH_WP, status="error", message="missing_wordpress_output")
        return
    if len(wp_text.strip()) < 120:
        post.status = PostStatus.failed
        post.updated_at = datetime.utcnow()
        db.add(post)
        log_event(
            db,
            user_id=job.user_id,
            profile_id=job.profile_id,
            post_id=job.post_id,
            stage=JOB_PUBLISH_WP,
            status="error",
            message="wordpress_output_too_short",
        )
        return
    creds = _get_wordpress_integration(db, user_id=job.user_id, profile_id=job.profile_id)
    category_ids: list[int] | None = None
    tag_ids: list[int] | None = None
    skip_wp_image = False
    skip_wp_tags = False
    if isinstance(recipe, dict):
        profile = None
        if job.profile_id:
            profile = db.scalar(select(AutomationProfile).where(AutomationProfile.id == job.profile_id))
        publish_cfg = dict((profile.publish_config_json if profile else {}) or {})
        skip_wp_image = bool(publish_cfg.get("fast_skip_wp_image"))
        skip_wp_tags = bool(publish_cfg.get("fast_skip_wp_tags"))
        allowed_categories = list(publish_cfg.get("categories") or [])
        default_category = str(publish_cfg.get("default_category") or (allowed_categories[0] if allowed_categories else "Receitas"))
        category_name = str(recipe.get("categoria") or "").strip()
        if allowed_categories and category_name not in allowed_categories:
            category_name = default_category
        try:
            cats = list_categories(base_url=creds["base_url"], username=creds["username"], app_password=creds["app_password"])
            cat_map = {str(c["name"]).strip().lower(): int(c["id"]) for c in cats if c.get("id") and c.get("name")}
            cat_id = cat_map.get(category_name.strip().lower()) or cat_map.get(default_category.strip().lower())
            if cat_id:
                category_ids = [int(cat_id)]
        except WordPressError as _cat_err:
            log_event(
                db,
                user_id=job.user_id,
                profile_id=job.profile_id,
                post_id=job.post_id,
                stage=JOB_PUBLISH_WP,
                status="warning",
                message="category_lookup_skipped",
                meta={"reason": str(_cat_err)[:120]},
            )
        tags = recipe.get("tags") or []
        names: list[str] = []
        if isinstance(tags, list):
            for t in tags:
                s = str(t).strip()
                if s:
                    names.append(s)
        if names and not skip_wp_tags:
            try:
                tag_ids = [get_or_create_tag_id(base_url=creds["base_url"], username=creds["username"], app_password=creds["app_password"], tag_name=n) for n in names[:12]]
            except WordPressError as _tag_err:
                # User may lack permission to create tags — publish without tags
                log_event(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id,
                          stage=JOB_PUBLISH_WP, status="warning", message="tag_creation_skipped",
                          meta={"reason": str(_tag_err)[:120]})
                tag_ids = None
    featured_media_id = None
    image_url = _get_output_image_url(outputs, fallback_url=content.lead_image_url)
    if image_url and not skip_wp_image:
        try:
            prepared = download_and_prepare_image(str(image_url))
            featured_media_id = upload_media(
                base_url=creds["base_url"],
                username=creds["username"],
                app_password=creds["app_password"],
                filename=prepared.filename,
                content_type=prepared.content_type,
                data=prepared.data,
            )
        except Exception as _img_err:
            log_event(
                db,
                user_id=job.user_id,
                profile_id=job.profile_id,
                post_id=job.post_id,
                stage=JOB_PUBLISH_WP,
                status="warning",
                message="featured_image_skipped",
                meta={"reason": str(_img_err)[:160]},
            )
            featured_media_id = None
    title = content.title or "Post"
    if isinstance(recipe, dict):
        t = str(recipe.get("title") or "").strip()
        if not t:
            t = _pt_title_case(_sanitize_source_title(content.title or ""))
        if t:
            title = t
    title = title.strip() or "Post"
    wp_text = _strip_duplicate_title(title=title, text=wp_text)
    if _is_post_canceled(db, post):
        return
    if job.profile_id and _is_duplicate_candidate(
        db, profile_id=job.profile_id, current_post_id=post.id, title=title, canonical_url=content.canonical_url
    ):
        outputs2 = dict(post.outputs_json or {})
        outputs2["duplicate_skipped"] = True
        if not outputs2.get("replacement_requested"):
            outputs2["replacement_requested"] = True
            enqueue_job(
                db,
                user_id=job.user_id,
                profile_id=job.profile_id,
                job_type=JOB_COLLECT,
                payload={"limit": 1, "interval_minutes": 0, "respect_schedule": 0},
            )
        post.outputs_json = outputs2
        post.status = PostStatus.failed
        post.updated_at = datetime.utcnow()
        db.add(post)
        log_event(
            db,
            user_id=job.user_id,
            profile_id=job.profile_id,
            post_id=job.post_id,
            stage=JOB_PUBLISH_WP,
            status="skipped",
            message="duplicate_detected",
            meta={"title": title, "canonical_url": content.canonical_url},
        )
        return
    content_html = _render_wp_html(wp_text)
    if correction_requested and post.wp_post_id:
        # Delete the old WP post first, then create a clean new one
        try:
            delete_post(
                base_url=creds["base_url"],
                username=creds["username"],
                app_password=creds["app_password"],
                post_id=int(post.wp_post_id),
                force=True,
            )
        except WordPressError as _del_err:
            # If the post is already gone on WP, that's fine — continue to create
            log_event(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id,
                      stage=JOB_PUBLISH_WP, status="warning", message="wordpress_delete_skipped",
                      meta={"reason": str(_del_err)[:160]})
        post.wp_post_id = None
        post.wp_url = None
        db.add(post)
    wp_post = create_post(
        base_url=creds["base_url"],
        username=creds["username"],
        app_password=creds["app_password"],
        title=title,
        content_html=content_html,
        status="publish",
        featured_media_id=featured_media_id,
        tags=tag_ids,
        categories=category_ids,
    )
    log_message = "wordpress_corrected" if correction_requested else "wordpress_published"
    post.wp_post_id = wp_post.post_id
    post.wp_url = wp_post.link
    post.status = PostStatus.completed
    post.published_at = datetime.utcnow()
    post.updated_at = datetime.utcnow()
    if isinstance(post.outputs_json, dict) and post.outputs_json.get("correction_requested"):
        outputs3 = dict(post.outputs_json or {})
        outputs3.pop("correction_requested", None)
        post.outputs_json = outputs3
    db.add(post)
    log_event(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id, stage=JOB_PUBLISH_WP, status="ok", message=log_message, meta={"wp_post_id": wp_post.post_id, "wp_url": wp_post.link})
    if job.profile_id:
        profile = db.scalar(select(AutomationProfile).where(AutomationProfile.id == job.profile_id))
        publish_cfg = dict((profile.publish_config_json if profile else {}) or {})
        if bool(publish_cfg.get("facebook_enabled")):
            fb_link_place = str(publish_cfg.get("facebook_link") or "comments")
            fb_img_mode = str(publish_cfg.get("facebook_image") or "link_preview")
            selected = publish_cfg.get("facebook_page_ids") or []
            selected_ids = {str(x) for x in selected} if isinstance(selected, list) else set()
            integ = db.scalar(select(Integration).where(Integration.profile_id == job.profile_id, Integration.type == IntegrationType.FACEBOOK))
            pages: list[dict] = []
            if integ:
                try:
                    creds = decrypt_json(integ.credentials_encrypted)
                    pages_val = creds.get("pages") if isinstance(creds, dict) else None
                    if isinstance(pages_val, list):
                        pages = [p for p in pages_val if isinstance(p, dict)]
                except Exception:
                    pages = []
            for p in pages:
                page_id = str(p.get("page_id") or "").strip()
                if not page_id:
                    continue
                if selected_ids and page_id not in selected_ids:
                    continue
                enqueue_job(
                    db,
                    user_id=job.user_id,
                    profile_id=job.profile_id,
                    post_id=job.post_id,
                    job_type=JOB_FACEBOOK_PUBLISH,
                    payload={"page_id": page_id, "link_placement": ("body" if fb_link_place == "body" else "comments"), "image_mode": fb_img_mode},
                )


def _handle_publish_facebook(db, job: Job):
    if not job.post_id or not job.profile_id:
        raise ValueError("missing_post_or_profile")
    post = db.scalar(select(Post).where(Post.id == job.post_id, Post.user_id == job.user_id))
    if not post:
        raise ValueError("post_not_found")
    if post.status != PostStatus.completed or not post.wp_url:
        raise ValueError("post_not_published_wordpress")
    content = None
    if post.collected_content_id:
        content = db.scalar(
            select(CollectedContent).where(
                CollectedContent.id == post.collected_content_id,
                CollectedContent.user_id == job.user_id,
            )
        )
    outputs = post.outputs_json or {}
    recipe = outputs.get("recipe") if isinstance(outputs, dict) else None
    fb_text = str((recipe or {}).get("facebook") or "").strip() if isinstance(recipe, dict) else ""
    if not fb_text:
        raise ValueError("missing_facebook_text")
    wp_url = str(post.wp_url or "").strip()
    if not wp_url:
        raise ValueError("missing_wp_url")

    page_id = str(job.payload_json.get("page_id") or "").strip()
    if not page_id:
        raise ValueError("missing_page_id")
    placement = str(job.payload_json.get("link_placement") or "comments").strip().lower()
    placement = "body" if placement == "body" else "comments"
    image_mode = str(job.payload_json.get("image_mode") or "link_preview").strip().lower()
    if image_mode not in ("link_preview", "direct_photo", "none"):
        image_mode = "link_preview"

    fb_state = outputs.get("facebook") if isinstance(outputs, dict) else None
    fb_state = fb_state if isinstance(fb_state, dict) else {}
    pages_state = fb_state.get("pages") if isinstance(fb_state.get("pages"), list) else []
    for it in pages_state:
        if isinstance(it, dict) and str(it.get("page_id") or "").strip() == page_id and it.get("fb_post_id"):
            log_event(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id, stage=JOB_FACEBOOK_PUBLISH, status="skipped", message="already_posted_facebook", meta={"page_id": page_id})
            return

    integ = db.scalar(select(Integration).where(Integration.profile_id == job.profile_id, Integration.type == IntegrationType.FACEBOOK))
    if not integ:
        raise FacebookError("missing_facebook_integration")
    creds = decrypt_json(integ.credentials_encrypted)
    pages_val = creds.get("pages") if isinstance(creds, dict) else None
    if not isinstance(pages_val, list):
        raise FacebookError("missing_facebook_pages")
    page = None
    for p in pages_val:
        if isinstance(p, dict) and str(p.get("page_id") or "").strip() == page_id:
            page = p
            break
    if not page:
        raise FacebookError("facebook_page_not_found")
    token = str(page.get("access_token") or "").strip()
    if not token:
        raise FacebookError("missing_facebook_page_token")

    comment_id = None
    if image_mode == "direct_photo":
        # Upload the post image directly as a photo with caption
        fallback_image_url = content.lead_image_url if content else None
        image_url = _get_output_image_url(outputs, fallback_url=fallback_image_url)
        if image_url:
            caption = fb_text
            if placement == "body":
                caption = f"{fb_text}\n\n{wp_url}"
            post_res = publish_page_photo(page_id=page_id, page_access_token=token, photo_url=image_url, caption=caption)
            if placement == "comments":
                comment_id = comment_on_post(post_id=post_res.post_id, page_access_token=token, message=wp_url)
        else:
            # No image available — fall back to text post
            if placement == "body":
                post_res = publish_page_post(page_id=page_id, page_access_token=token, message=fb_text, link=wp_url)
            else:
                post_res = publish_page_post(page_id=page_id, page_access_token=token, message=fb_text, link=None)
                comment_id = comment_on_post(post_id=post_res.post_id, page_access_token=token, message=wp_url)
    elif image_mode == "none":
        # Text only — no link, no image
        post_res = publish_page_post(page_id=page_id, page_access_token=token, message=fb_text, link=None)
        if placement == "comments":
            comment_id = comment_on_post(post_id=post_res.post_id, page_access_token=token, message=wp_url)
    else:
        # link_preview (default): Facebook generates preview from the link
        if placement == "body":
            post_res = publish_page_post(page_id=page_id, page_access_token=token, message=fb_text, link=wp_url)
        else:
            post_res = publish_page_post(page_id=page_id, page_access_token=token, message=fb_text, link=None)
            comment_id = comment_on_post(post_id=post_res.post_id, page_access_token=token, message=wp_url)

    pages_state.append(
        {
            "page_id": page_id,
            "page_name": str(page.get("name") or "").strip(),
            "fb_post_id": post_res.post_id,
            "fb_comment_id": comment_id,
            "link_placement": placement,
            "wp_url": wp_url,
        }
    )
    fb_state["pages"] = pages_state
    outputs["facebook"] = fb_state
    post.outputs_json = outputs
    post.updated_at = datetime.utcnow()
    db.add(post)
    log_event(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id, stage=JOB_FACEBOOK_PUBLISH, status="ok", message="facebook_published", meta={"page_id": page_id, "placement": placement, "image_mode": image_mode, "fb_post_id": post_res.post_id})


def _parse_ai_json(text: str) -> dict:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    if raw.startswith("{") and raw.endswith("}"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return json.loads(_sanitize_json_blob(raw))
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        raise GeminiError("invalid_json")
    blob = m.group(0)
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return json.loads(_sanitize_json_blob(blob))


def _sanitize_json_blob(blob: str) -> str:
    # Some model outputs include literal control characters inside JSON strings.
    # Replacing them with spaces preserves the structure well enough for parsing.
    return re.sub(r"[\x00-\x1F]+", " ", blob or "")


def _prune_collect_backlog(db, *, profile_id: str, keep: int = 1) -> int:
    collect_jobs = list(
        db.scalars(
            select(Job)
            .where(
                Job.profile_id == profile_id,
                Job.type == JOB_COLLECT,
                Job.status == JobStatus.queued,
            )
            .order_by(Job.run_at.asc(), Job.created_at.asc())
        )
    )
    removed = 0
    for job in collect_jobs[max(0, keep):]:
        db.delete(job)
        removed += 1
    if removed:
        db.flush()
    return removed


def _norm_title(s: str) -> str:
    t = (s or "").strip()
    if not t:
        return ""
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.lower()
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"[^\w\s-]", "", t).strip()
    return t


def _sanitize_source_title(s: str) -> str:
    t = (s or "").strip()
    if not t:
        return ""
    t = re.sub(r"^receita de\s+", "", t, flags=re.I).strip()
    t = re.sub(r",\s*enviad[oa]\s+por\s+[^-–—|]+", "", t, flags=re.I).strip()
    t = re.sub(r"\s*[-|]\s*tudogostoso\s*$", "", t, flags=re.I).strip()
    t = re.sub(r"\s*[-|]\s*receiteria\s*$", "", t, flags=re.I).strip()
    t = re.sub(r"\s*[-|]\s*equipe\s+mais\s+saude\s*$", "", t, flags=re.I).strip()
    parts = [p.strip() for p in re.split(r"\s*[-|]\s*", t) if p.strip()]
    if len(parts) >= 2:
        tail = " ".join(parts[1:]).lower()
        if any(x in tail for x in ("tudogostoso", "receiteria", "equipe mais saude")):
            t = parts[0]
    t = re.sub(r"\s+", " ", t).strip(" -–—|")
    return t


def _extract_title_from_site_text(site_text: str) -> str:
    raw = (site_text or "").lstrip()
    if not raw:
        return ""
    lines = raw.splitlines()
    for ln in lines[:15]:
        s = ln.strip()
        if not s:
            continue
        # Strip markdown heading markers
        s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s).strip()
        # Strip bold markers at start/end
        s = re.sub(r"^\*{1,3}\s*", "", s).strip()
        s = re.sub(r"\s*\*{1,3}$", "", s).strip()
        # Strip "Título:", "Title:", "título:" labels that AI adds as section prefixes
        s = re.sub(r"^(?:título|titulo|title)\s*[:\-]\s*", "", s, flags=re.IGNORECASE).strip()
        # Skip pure section labels (short line ending with colon)
        if s.endswith(":") and len(s.split()) <= 5:
            continue
        if s and len(s) >= 5:
            return s
    return ""


def _pt_title_case(s: str) -> str:
    txt = re.sub(r"\s+", " ", (s or "").strip())
    if not txt:
        return ""
    small = {"de", "do", "da", "dos", "das", "e", "com", "sem", "a", "o", "as", "os", "em", "no", "na", "nos", "nas", "por", "para"}
    words = txt.split(" ")
    out: list[str] = []
    for i, w in enumerate(words):
        lw = w.lower()
        if i > 0 and lw in small:
            out.append(lw)
        else:
            out.append(lw[:1].upper() + lw[1:])
    return " ".join(out).strip()


def _strip_duplicate_title(*, title: str, text: str) -> str:
    raw = (text or "").lstrip()
    if not raw:
        return ""
    lines = raw.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return raw
    first = lines[i].strip()
    first_clean = re.sub(r"^\s{0,3}#{1,6}\s*", "", first).strip()
    first_clean = re.sub(r"^\*+\s*", "", first_clean).strip()
    first_clean = re.sub(r"^_+\s*", "", first_clean).strip()
    if _norm_title(first_clean) and _norm_title(first_clean) == _norm_title(title):
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        return "\n".join(lines[:i] + lines[j:]).lstrip()
    return raw


def _looks_like_same_title(a: str, b: str) -> bool:
    na = _norm_title(a)
    nb = _norm_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 10 and len(nb) >= 10 and (na in nb or nb in na):
        return True
    return False


def _bold_first_occurrence(*, text: str, phrase: str) -> str:
    if not text or not phrase:
        return text
    escaped = re.escape(phrase.strip())
    if not escaped:
        return text
    pattern = re.compile(escaped, flags=re.IGNORECASE)
    m = pattern.search(text)
    if not m:
        return text
    return text[: m.start()] + "<strong>" + text[m.start() : m.end()] + "</strong>" + text[m.end() :]


def _is_duplicate_candidate(db, *, profile_id: str, current_post_id: str, title: str, canonical_url: str) -> bool:
    title_norm = _norm_title(title)
    canon_norm = _normalize_url_for_dedupe(canonical_url)
    rows = list(
        db.execute(
            select(Post, CollectedContent)
            .join(CollectedContent, CollectedContent.id == Post.collected_content_id)
            .where(Post.profile_id == profile_id, Post.status == PostStatus.completed, Post.id != current_post_id)
            .order_by(Post.created_at.desc())
            .limit(500)
        ).all()
    )
    for p, c in rows:
        if canon_norm and canon_norm == _normalize_url_for_dedupe(c.canonical_url):
            return True
        if title_norm:
            existing_title = ""
            if isinstance(p.outputs_json, dict):
                r = p.outputs_json.get("recipe")
                if isinstance(r, dict):
                    existing_title = str(r.get("title") or "").strip()
            if not existing_title:
                existing_title = str(c.title or "").strip()
            if _looks_like_same_title(title, existing_title):
                return True
    return False


def _to_plain_text(text: str) -> str:
    s = (text or "").strip("\n\r")
    if not s:
        return ""
    lines = s.splitlines()
    out: list[str] = []
    for ln in lines:
        x = ln.rstrip()
        x = re.sub(r"^\s{0,3}#{1,6}\s*", "", x).strip()
        x = re.sub(r"^\s*[-*]\s+", "• ", x)
        x = re.sub(r"^\s*(\*\*+|__+)\s*", "", x).strip()
        x = re.sub(r"\s*(\*\*+|__+)\s*$", "", x).strip()
        x = re.sub(r"\*\*\s*(.*?)\s*\*\*", r"<strong>\1</strong>", x)
        x = re.sub(r"__\s*(.*?)\s*__", r"<strong>\1</strong>", x)
        x = re.sub(r"(?<!\S)\*\s*(.*?)\s*\*(?!\S)", r"\1", x)
        x = re.sub(r"(?<!\S)_\s*(.*?)\s*_(?!\S)", r"\1", x)
        x = re.sub(r"^\s*\*{3,}\s*$", "", x)
        out.append(x)
    plain = "\n".join(out)
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    return plain.strip()


def _escape_keep_strong(s: str) -> str:
    if not s:
        return ""
    tmp = s.replace("<strong>", "\u0000STRONG_OPEN\u0000").replace("</strong>", "\u0000STRONG_CLOSE\u0000")
    tmp = html.escape(tmp)
    tmp = tmp.replace("\u0000STRONG_OPEN\u0000", "<strong>").replace("\u0000STRONG_CLOSE\u0000", "</strong>")
    return tmp


def _render_wp_html(text: str) -> str:
    """Convert markdown (or plain text) from AI output to WordPress-ready HTML."""
    raw = (text or "").strip()
    if not raw:
        return ""

    main_heads = {
        "ingredientes", "modo de preparo", "preparo", "dicas",
        "tempo de preparo", "tempo e rendimento", "rendimento",
        "montagem", "finalizacao", "finalização", "como fazer",
        "como assar", "como servir", "introducao", "introdução",
    }
    main_norms = {_norm_title(x) for x in main_heads}

    def _strip_inline_md(s: str) -> str:
        s = re.sub(r"\*\*(.*?)\*\*", r"\1", s)
        s = re.sub(r"__(.*?)__", r"\1", s)
        s = re.sub(r"\*(.*?)\*", r"\1", s)
        return s.strip("*_").strip()

    def _process_inline(s: str) -> str:
        # Convert markdown **bold** → <strong>, then HTML-escape preserving <strong>
        s = re.sub(r"\*\*\s*(.*?)\s*\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"__\s*(.*?)\s*__", r"<strong>\1</strong>", s)
        # Remove leftover single * or _
        s = re.sub(r"(?<!\*)\*(?!\*)([^*\n]*?)(?<!\*)\*(?!\*)", r"\1", s)
        return _escape_keep_strong(s)

    lines = raw.splitlines()
    out: list[str] = []
    state: dict = {"list": None}
    para: list[str] = []

    def close_list() -> None:
        lt = state.get("list")
        if lt == "ul":
            out.append("</ul>")
        elif lt == "ol":
            out.append("</ol>")
        state["list"] = None

    def flush_para() -> None:
        if para:
            close_list()
            out.append(f"<p>{_process_inline(' '.join(para).strip())}</p>")
            para.clear()

    i = 0
    while i < len(lines):
        ln = (lines[i] or "").rstrip()
        s = ln.strip()

        if not s:
            flush_para()
            close_list()
            i += 1
            continue

        # 1. Markdown heading: # Title or ## Title or ### Title
        m_head = re.match(r"^(#{1,6})\s+(.+)$", s)
        if m_head:
            flush_para()
            close_list()
            level = len(m_head.group(1))
            heading_text = _strip_inline_md(m_head.group(2).strip())
            tag = "h2" if level <= 2 else "h3"
            out.append(f"<{tag}>{_escape_keep_strong(heading_text)}</{tag}>")
            i += 1
            continue

        # 2. Bullet list: • item or - item or * item
        m_bullet = re.match(r"^[•\-\*]\s+(.+)$", s)
        if m_bullet:
            flush_para()
            if state.get("list") != "ul":
                close_list()
                out.append("<ul>")
                state["list"] = "ul"
            out.append(f"<li>{_process_inline(_strip_inline_md(m_bullet.group(1).strip()))}</li>")
            i += 1
            continue

        # 3. Numbered list: 1. item or 1) item
        m_num = re.match(r"^\d+[.)]\s+(.+)$", s)
        if m_num:
            flush_para()
            if state.get("list") != "ol":
                close_list()
                out.append("<ol>")
                state["list"] = "ol"
            out.append(f"<li>{_process_inline(_strip_inline_md(m_num.group(1).strip()))}</li>")
            i += 1
            continue

        # 4. Bold-only line as heading: **Section Name** or **Section Name:**
        m_bold_only = re.match(r"^\*\*\s*(.+?)\s*\*\*\s*:?\s*$", s)
        if m_bold_only:
            candidate = _strip_inline_md(m_bold_only.group(1).strip())
            if len(candidate) <= 90:
                flush_para()
                close_list()
                norm = _norm_title(candidate)
                tag = "h2" if (norm in main_norms or norm.startswith("ingredientes") or norm.startswith("como ")) else "h3"
                out.append(f"<{tag}>{_escape_keep_strong(candidate)}</{tag}>")
                i += 1
                continue

        # 5. Known section name (plain text, possibly ending with colon)
        candidate_plain = _strip_inline_md(re.sub(r"[:：]\s*$", "", s).strip())
        norm = _norm_title(candidate_plain)
        is_main = norm in main_norms or norm.startswith("ingredientes") or norm.startswith("como ")
        is_short_colon = (s.endswith(":") or s.endswith("：")) and len(candidate_plain) <= 60 and len(candidate_plain.split()) <= 8
        if is_main or is_short_colon:
            flush_para()
            close_list()
            tag = "h2" if is_main else "h3"
            out.append(f"<{tag}>{_escape_keep_strong(candidate_plain)}</{tag}>")
            i += 1
            continue

        # 6. Regular paragraph text
        close_list()
        para.append(s)
        i += 1

    flush_para()
    return "\n".join(out).strip()

def process_job(db, job: Job):
    if not _is_profile_active(db, job.profile_id, job.user_id):
        _mark_job_skipped_for_inactive_profile(db, job)
        return
    if job.post_id and job.type != JOB_COLLECT:
        p = db.scalar(select(Post).where(Post.id == job.post_id, Post.user_id == job.user_id))
        if p and isinstance(p.outputs_json, dict) and p.outputs_json.get("canceled_by_user"):
            log_event(
                db,
                user_id=job.user_id,
                profile_id=job.profile_id,
                post_id=job.post_id,
                stage=job.type,
                status="skipped",
                message="canceled_by_user",
            )
            return
    if job.type == JOB_COLLECT:
        _handle_collect(db, job)
    elif job.type == JOB_CLEAN:
        _handle_clean(db, job)
    elif job.type == JOB_AI:
        _handle_ai(db, job)
    elif job.type == JOB_MEDIA:
        _handle_media(db, job)
    elif job.type == JOB_PUBLISH_WP:
        _handle_publish_wp(db, job)
    elif job.type == JOB_FACEBOOK_PUBLISH:
        _handle_publish_facebook(db, job)
    else:
        raise ValueError("unknown_job_type")


def run_worker_tick(*, worker_id: str, user_id: str | None = None, profile_id: str | None = None) -> bool:
    with db_session() as db:
        job = get_due_job(db, worker_id=worker_id, user_id=user_id, profile_id=profile_id)
        if not job:
            db.commit()
            return False
        # Commit the running lock immediately so concurrent workers (dedicated + cron tick) see it
        db.commit()
        try:
            log_event(
                db,
                user_id=job.user_id,
                profile_id=job.profile_id,
                post_id=job.post_id,
                stage=job.type,
                status="start",
                message="job_started",
                meta={"attempt": job.attempts + 1},
            )
            process_job(db, job)
            canceled_after = False
            if job.post_id:
                p = db.scalar(select(Post).where(Post.id == job.post_id, Post.user_id == job.user_id))
                canceled_after = bool(p and isinstance(p.outputs_json, dict) and p.outputs_json.get("canceled_by_user"))
            job.status = JobStatus.failed if canceled_after else JobStatus.succeeded
            if canceled_after:
                job.last_error = "canceled_by_user"
            job.updated_at = datetime.utcnow()
            db.add(job)
            db.commit()
            return True
        except (FacebookError, GeminiError, WordPressError, Exception) as e:
            db.rollback()
            with db_session() as db2:
                j = db2.scalar(select(Job).where(Job.id == job.id))
                if not j:
                    return False
                j.last_error = str(e)
                j.locked_at = None
                j.locked_by = None
                _wp_terminal = (
                    isinstance(e, WordPressError)
                    and (
                        str(e).startswith("invalid_wordpress_credentials")
                        or str(e).startswith("insufficient_wp_permissions")
                        or str(e).startswith("missing_wordpress_integration")
                    )
                )
                if isinstance(e, GeminiError) and str(e).startswith("rate_limited:"):
                    try:
                        secs = int(str(e).split(":", 1)[1])
                    except Exception:
                        secs = 30
                    j.status = JobStatus.queued
                    j.run_at = datetime.utcnow() + timedelta(seconds=max(5, secs))
                elif _wp_terminal:
                    # Auth failures won't self-heal — fail immediately without retrying
                    j.attempts = j.max_attempts
                    j.status = JobStatus.failed
                else:
                    j.attempts = int(j.attempts) + 1
                    if j.attempts >= j.max_attempts:
                        j.status = JobStatus.failed
                    else:
                        j.status = JobStatus.queued
                        j.run_at = schedule_retry(j)
                j.updated_at = datetime.utcnow()
                db2.add(j)
                log_event(
                    db2,
                    user_id=j.user_id,
                    profile_id=j.profile_id,
                    post_id=j.post_id,
                    stage=j.type,
                    status="error",
                    message="job_failed",
                    meta={"error": str(e), "attempts": j.attempts, "max_attempts": j.max_attempts},
                )
                if j.post_id and j.status == JobStatus.failed:
                    p = db2.scalar(select(Post).where(Post.id == j.post_id))
                    if p and p.status != PostStatus.completed:
                        p.status = PostStatus.failed
                        p.updated_at = datetime.utcnow()
                        db2.add(p)
                        if (
                            j.profile_id
                            and isinstance(p.outputs_json, dict)
                            and not p.outputs_json.get("canceled_by_user")
                            and not p.outputs_json.get("replacement_requested")
                            and j.type in (JOB_CLEAN, JOB_AI, JOB_MEDIA, JOB_PUBLISH_WP)
                        ):
                            outputs = dict(p.outputs_json or {})
                            outputs["replacement_requested"] = True
                            p.outputs_json = outputs
                            db2.add(p)
                            enqueue_job(
                                db2,
                                user_id=j.user_id,
                                profile_id=j.profile_id,
                                job_type=JOB_COLLECT,
                                payload={"limit": 1, "interval_minutes": 0},
                            )
                db2.commit()
            return True


def run_worker():
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    while True:
        did_work = run_worker_tick(worker_id=worker_id)
        if not did_work:
            time.sleep(1.0)


if __name__ == "__main__":
    run_worker()
