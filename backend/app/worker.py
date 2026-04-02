from __future__ import annotations

import hashlib
import html
import json
import os
import re
import socket
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import select
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
from app.services.facebook import FacebookError, comment_on_post, publish_page_post
from app.services.gemini import GeminiError, generate_text
from app.services.images import download_and_prepare_image
from app.services.rss import fetch_rss_items, keyword_to_google_news_rss
from app.services.scrape import discover_recipe_links, is_probably_homepage, looks_like_recipe_page, scrape_url
from app.services.wordpress import WordPressError, create_post, get_or_create_tag_id, list_categories, upload_media


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


def _handle_collect(db, job: Job):
    profile_id = job.profile_id
    if not profile_id:
        raise ValueError("missing_profile_id")
    profile = db.scalar(select(AutomationProfile).where(AutomationProfile.id == profile_id, AutomationProfile.user_id == job.user_id))
    if not profile or not profile.active:
        log_event(db, user_id=job.user_id, profile_id=profile_id, stage=JOB_COLLECT, status="skipped", message="profile_inactive_or_missing")
        return
    sources = list(db.scalars(select(Source).where(Source.profile_id == profile_id, Source.active.is_(True))))
    sched = dict(profile.schedule_config_json or {})
    default_limit = int(sched.get("posts_per_day") or 15)
    default_interval = int(sched.get("interval_minutes") or 0)
    respect = int(job.payload_json.get("respect_schedule") or sched.get("respect_schedule") or 0) == 1
    limit = int(job.payload_json.get("limit") or default_limit)
    if respect:
        interval_minutes = int(job.payload_json.get("interval_minutes") or default_interval)
    else:
        interval_minutes = 0
    created = 0
    skipped_duplicate = 0
    skipped_non_recipe = 0
    skipped_error = 0
    base_run_at = datetime.utcnow()
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
        post = Post(user_id=job.user_id, profile_id=profile_id, collected_content_id=content.id, status=PostStatus.pending)
        db.add(post)
        db.flush()
        run_at = None
        if interval_minutes:
            run_at = base_run_at + timedelta(minutes=interval_minutes * created)
        enqueue_job(
            db,
            user_id=job.user_id,
            profile_id=profile_id,
            post_id=post.id,
            job_type=JOB_CLEAN,
            payload={"collected_content_id": content.id},
            run_at=run_at,
        )
        created += 1

    for source in sources:
        if limit and created >= limit:
            break
        if source.type == SourceType.RSS:
            items = fetch_rss_items(source.value, limit=20)
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
                _enqueue_scraped(s=s, source_id=source.id, title_fallback=item.title)
        elif source.type == SourceType.KEYWORD:
            feed_url = keyword_to_google_news_rss(source.value)
            items = fetch_rss_items(feed_url, limit=20)
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
                _enqueue_scraped(s=s, source_id=source.id, title_fallback=item.title)
        elif source.type == SourceType.URL:
            if limit and created >= limit:
                break
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

            picked_links = discover_recipe_links(raw_html=scraped.raw_html, base_url=scraped.canonical_url, max_links=220)
            if picked_links:
                day = datetime.utcnow().strftime("%Y-%m-%d")
                h = hashlib.sha256((source.value + "|" + day).encode("utf-8")).hexdigest()
                start = int(h[:8], 16) % len(picked_links)
                picked_links = picked_links[start:] + picked_links[:start]
            max_attempts = 220 if not limit else max(120, limit * 12)
            attempts = 0
            for u in picked_links:
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
        },
    )
    if limit and created < limit:
        round_n = int(job.payload_json.get("collect_round") or 0)
        if round_n < 2:
            enqueue_job(
                db,
                user_id=job.user_id,
                profile_id=profile_id,
                job_type=JOB_COLLECT,
                payload={
                    "limit": int(limit - created),
                    "interval_minutes": interval_minutes,
                    "respect_schedule": 1 if respect else 0,
                    "collect_round": round_n + 1,
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
    db.add(content)
    db.flush()
    enqueue_job(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id, job_type=JOB_AI, payload={"collected_content_id": content_id})
    log_event(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id, stage=JOB_CLEAN, status="ok", message="clean_completed")


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
        if job.profile_id:
            gi = db.scalar(select(Integration).where(Integration.profile_id == job.profile_id, Integration.type == IntegrationType.GEMINI))
            if gi:
                try:
                    gcreds = decrypt_json(gi.credentials_encrypted)
                    gemini_key = str(gcreds.get("api_key") or "").strip() or None
                except Exception:
                    gemini_key = None
        publish_cfg = dict((profile.publish_config_json if profile else {}) or {})
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
        site_instr = str(site_prompt or "").strip()
        fb_instr = str(fb_prompt or "").strip()
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
        res = generate_text(prompt=prompt, content=f"TÍTULO: {seed_title}\n\n{base_text}", api_key=gemini_key)
        parsed = _parse_ai_json(res.text)
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
                "Regras:\n"
                "- Não copie frases do texto original\n"
                "- Não invente ingredientes\n"
                "- No site: incluir Título, Introdução, Ingredientes, Modo de preparo, Dicas\n"
            )
            res2 = generate_text(prompt=fallback_prompt, content=f"TÍTULO: {seed_title}\n\n{base_text}", api_key=gemini_key)
            parsed2 = _parse_ai_json(res2.text)
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
    enqueue_job(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id, job_type=JOB_MEDIA, payload={"collected_content_id": content_id})
    log_event(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id, stage=JOB_AI, status="ok", message="ai_completed")


def _handle_media(db, job: Job):
    content_id = job.payload_json.get("collected_content_id")
    if not content_id or not job.post_id:
        raise ValueError("missing_content_or_post")
    content = db.scalar(select(CollectedContent).where(CollectedContent.id == content_id, CollectedContent.user_id == job.user_id))
    post = db.scalar(select(Post).where(Post.id == job.post_id, Post.user_id == job.user_id))
    if not content or not post:
        raise ValueError("content_or_post_not_found")
    _ensure_post_processing(db, post)
    if content.lead_image_url:
        outputs: dict = dict(post.outputs_json or {})
        outputs["image"] = {"url": content.lead_image_url}
        post.outputs_json = outputs
        post.updated_at = datetime.utcnow()
        db.add(post)
        db.flush()
    if job.profile_id:
        has_wp_action = (
            db.scalar(
                select(AiAction.id).where(
                    AiAction.profile_id == job.profile_id,
                    AiAction.active.is_(True),
                    AiAction.destination == ActionDestination.WORDPRESS,
                )
            )
            is not None
        )
    else:
        has_wp_action = (
            db.scalar(
                select(AiAction.id).where(
                    AiAction.user_id == job.user_id,
                    AiAction.active.is_(True),
                    AiAction.destination == ActionDestination.WORDPRESS,
                )
            )
            is not None
        )
    if has_wp_action:
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
    if post.status == PostStatus.completed and post.wp_post_id:
        log_event(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id, stage=JOB_PUBLISH_WP, status="skipped", message="already_published")
        return
    content = db.scalar(select(CollectedContent).where(CollectedContent.id == post.collected_content_id, CollectedContent.user_id == job.user_id))
    if not content:
        raise ValueError("content_not_found")
    _ensure_post_processing(db, post)
    outputs = post.outputs_json or {}
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
    if isinstance(recipe, dict):
        profile = None
        if job.profile_id:
            profile = db.scalar(select(AutomationProfile).where(AutomationProfile.id == job.profile_id))
        publish_cfg = dict((profile.publish_config_json if profile else {}) or {})
        allowed_categories = list(publish_cfg.get("categories") or [])
        default_category = str(publish_cfg.get("default_category") or (allowed_categories[0] if allowed_categories else "Receitas"))
        category_name = str(recipe.get("categoria") or "").strip()
        if allowed_categories and category_name not in allowed_categories:
            category_name = default_category
        cats = list_categories(base_url=creds["base_url"], username=creds["username"], app_password=creds["app_password"])
        cat_map = {str(c["name"]).strip().lower(): int(c["id"]) for c in cats if c.get("id") and c.get("name")}
        cat_id = cat_map.get(category_name.strip().lower()) or cat_map.get(default_category.strip().lower())
        if cat_id:
            category_ids = [int(cat_id)]
        tags = recipe.get("tags") or []
        names: list[str] = []
        if isinstance(tags, list):
            for t in tags:
                s = str(t).strip()
                if s:
                    names.append(s)
        if names:
            tag_ids = [get_or_create_tag_id(base_url=creds["base_url"], username=creds["username"], app_password=creds["app_password"], tag_name=n) for n in names[:12]]
    featured_media_id = None
    image_url = (outputs.get("image") or {}).get("url")
    if image_url:
        prepared = download_and_prepare_image(str(image_url))
        featured_media_id = upload_media(
            base_url=creds["base_url"],
            username=creds["username"],
            app_password=creds["app_password"],
            filename=prepared.filename,
            content_type=prepared.content_type,
            data=prepared.data,
        )
    title = content.title or "Post"
    if isinstance(recipe, dict):
        t = str(recipe.get("title") or "").strip()
        if not t:
            t = _pt_title_case(_sanitize_source_title(content.title or ""))
        if t:
            title = t
    title = title.strip() or "Post"
    wp_text = _strip_duplicate_title(title=title, text=wp_text)
    wp_text = _to_plain_text(wp_text)
    if "<strong>" not in wp_text and title:
        wp_text = _bold_first_occurrence(text=wp_text, phrase=title)
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
    wp_post = create_post(
        base_url=creds["base_url"],
        username=creds["username"],
        app_password=creds["app_password"],
        title=title,
        content_html=_render_wp_html(wp_text),
        status="publish",
        featured_media_id=featured_media_id,
        tags=tag_ids,
        categories=category_ids,
    )
    post.wp_post_id = wp_post.post_id
    post.wp_url = wp_post.link
    post.status = PostStatus.completed
    post.published_at = datetime.utcnow()
    post.updated_at = datetime.utcnow()
    db.add(post)
    log_event(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id, stage=JOB_PUBLISH_WP, status="ok", message="wordpress_published", meta={"wp_post_id": wp_post.post_id, "wp_url": wp_post.link})
    if job.profile_id:
        profile = db.scalar(select(AutomationProfile).where(AutomationProfile.id == job.profile_id))
        publish_cfg = dict((profile.publish_config_json if profile else {}) or {})
        if bool(publish_cfg.get("facebook_enabled")):
            fb_link_place = str(publish_cfg.get("facebook_link") or "comments")
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
                    payload={"page_id": page_id, "link_placement": ("body" if fb_link_place == "body" else "comments")},
                )


def _handle_publish_facebook(db, job: Job):
    if not job.post_id or not job.profile_id:
        raise ValueError("missing_post_or_profile")
    post = db.scalar(select(Post).where(Post.id == job.post_id, Post.user_id == job.user_id))
    if not post:
        raise ValueError("post_not_found")
    if post.status != PostStatus.completed or not post.wp_url:
        raise ValueError("post_not_published_wordpress")
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

    if placement == "body":
        post_res = publish_page_post(page_id=page_id, page_access_token=token, message=fb_text, link=wp_url)
        comment_id = None
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
    log_event(db, user_id=job.user_id, profile_id=job.profile_id, post_id=job.post_id, stage=JOB_FACEBOOK_PUBLISH, status="ok", message="facebook_published", meta={"page_id": page_id, "placement": placement, "fb_post_id": post_res.post_id})


def _parse_ai_json(text: str) -> dict:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    if raw.startswith("{") and raw.endswith("}"):
        return json.loads(raw)
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        raise GeminiError("invalid_json")
    return json.loads(m.group(0))


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
    for ln in lines[:10]:
        s = ln.strip()
        if not s:
            continue
        s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s).strip()
        s = re.sub(r"^[*_]{1,3}\s*", "", s).strip()
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
    raw = (text or "").strip()
    if not raw:
        return ""
    lines = raw.splitlines()
    out: list[str] = []

    def close_list(state: dict):
        lt = state.get("list")
        if lt == "ul":
            out.append("</ul>")
        elif lt == "ol":
            out.append("</ol>")
        state["list"] = None

    state = {"list": None, "last_step_heading": False}
    main_heads = {
        "ingredientes",
        "modo de preparo",
        "preparo",
        "dicas",
        "tempo de preparo",
        "tempo e rendimento",
        "rendimento",
        "montagem",
        "finalizacao",
        "finalização",
        "como fazer",
        "como assar",
        "como servir",
    }
    main_norms = {_norm_title(x) for x in main_heads}

    i = 0
    para: list[str] = []
    while i < len(lines):
        ln = (lines[i] or "").rstrip()
        s = ln.strip()
        if not s:
            if para:
                close_list(state)
                out.append(f"<p>{_escape_keep_strong(' '.join(para).strip())}</p>")
                para = []
            else:
                close_list(state)
            i += 1
            continue

        is_bullet = s.startswith("• ")
        candidate = re.sub(r"[:：]\s*$", "", s).strip()
        candidate_plain = re.sub(r"</?strong>", "", candidate, flags=re.IGNORECASE).strip()
        is_number = bool(re.match(r"^\d+[.)]\s+", candidate_plain))
        norm = _norm_title(candidate_plain)

        is_main_heading = norm in main_norms or norm.startswith("ingredientes") or norm.startswith("como ")

        words = [w for w in re.split(r"\s+", candidate_plain) if w]
        cap_words = [w for w in words if w[:1].isupper()]
        title_caseish = bool(words) and (len(cap_words) / max(1, len(words))) >= 0.75 and len(candidate_plain) <= 90 and len(words) <= 14

        para_headingish = (
            candidate_plain.lower().startswith("para ")
            or candidate_plain.lower().startswith("para a ")
            or candidate_plain.lower().startswith("para o ")
            or candidate_plain.lower().startswith("para os ")
            or candidate_plain.lower().startswith("para as ")
        ) and len(candidate_plain) <= 90

        m_step = re.match(r"^(\d+)[.)]\s+(.+)$", candidate_plain)
        step_heading = False
        if m_step:
            rest = m_step.group(2).strip()
            rest_words = [w for w in re.split(r"\s+", rest) if w]
            rest_caps = [w for w in rest_words if w[:1].isupper()]
            rest_title_caseish = bool(rest_words) and (len(rest_caps) / max(1, len(rest_words))) >= 0.5
            step_heading = len(rest) <= 90 and len(rest_words) <= 14 and rest_title_caseish

        looks_heading = (
            is_main_heading
            or para_headingish
            or title_caseish
            or step_heading
            or (candidate_plain.isupper() and len(candidate_plain) <= 80 and len(words) <= 10 and not is_number and not is_bullet)
            or (len(candidate_plain) <= 80 and candidate_plain.endswith(":") and not is_number and not is_bullet)
        )

        if looks_heading:
            if para:
                close_list(state)
                out.append(f"<p>{_escape_keep_strong(' '.join(para).strip())}</p>")
                para = []
            close_list(state)
            heading_text = candidate_plain
            tag = "h2" if is_main_heading else "h3"
            out.append(f"<{tag}>{_escape_keep_strong(heading_text)}</{tag}>")
            state["last_step_heading"] = bool(step_heading)
            i += 1
            continue

        if is_bullet:
            if para:
                close_list(state)
                out.append(f"<p>{_escape_keep_strong(' '.join(para).strip())}</p>")
                para = []
            if state.get("list") != "ul":
                close_list(state)
                out.append("<ul>")
                state["list"] = "ul"
            out.append(f"<li>{_escape_keep_strong(s[2:].strip())}</li>")
            state["last_step_heading"] = False
            i += 1
            continue

        if is_number:
            if state.get("last_step_heading"):
                item = re.sub(r"^\d+[.)]\s+", "", candidate_plain).strip()
                para.append(item)
                i += 1
                continue
            nxt = ""
            j = i + 1
            while j < len(lines):
                nxt = (lines[j] or "").strip()
                if nxt:
                    break
                j += 1
            next_plain = re.sub(r"</?strong>", "", nxt, flags=re.IGNORECASE).strip() if nxt else ""
            next_is_number = bool(next_plain and re.match(r"^\d+[.)]\s+", next_plain))
            next_is_bullet = bool(next_plain and next_plain.startswith("• "))
            if para:
                close_list(state)
                out.append(f"<p>{_escape_keep_strong(' '.join(para).strip())}</p>")
                para = []
            if next_is_number:
                if state.get("list") != "ol":
                    close_list(state)
                    out.append("<ol>")
                    state["list"] = "ol"
                item = re.sub(r"^\d+[.)]\s+", "", candidate_plain).strip()
                out.append(f"<li>{_escape_keep_strong(item)}</li>")
            else:
                close_list(state)
                item = re.sub(r"^\d+[.)]\s+", "", candidate_plain).strip()
                para.append(item)
            state["last_step_heading"] = False
            i += 1
            continue

        close_list(state)
        para.append(s)
        state["last_step_heading"] = False
        i += 1

    if para:
        close_list(state)
        out.append(f"<p>{_escape_keep_strong(' '.join(para).strip())}</p>")
    else:
        close_list(state)
    return "\n".join(out).strip()

def process_job(db, job: Job):
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


def run_worker_tick(*, worker_id: str) -> bool:
    with db_session() as db:
        job = get_due_job(db, worker_id=worker_id)
        if not job:
            db.commit()
            return False
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
            job.status = JobStatus.succeeded
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
                if isinstance(e, GeminiError) and str(e).startswith("rate_limited:"):
                    try:
                        secs = int(str(e).split(":", 1)[1])
                    except Exception:
                        secs = 30
                    j.status = JobStatus.queued
                    j.run_at = datetime.utcnow() + timedelta(seconds=max(5, secs))
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
