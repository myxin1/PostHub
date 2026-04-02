from __future__ import annotations

import html
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select, update

from app.api.deps import get_current_user, get_db, require_admin
from app.config import settings
from app.crypto import CryptoError, decrypt_json, encrypt_json
from app.models import (
    ActionDestination,
    AiAction,
    AutomationProfile,
    CollectedContent,
    EmailOutbox,
    Integration,
    IntegrationType,
    PasswordTokenType,
    Job,
    JobStatus,
    Post,
    PostStatus,
    JobLog,
    Source,
    SourceType,
    User,
    UserRole,
)
from app.queue import JOB_AI, JOB_COLLECT, enqueue_job
from app.security import create_access_token, hash_password, verify_password
from app.services.wordpress import WordPressError, delete_post


router = APIRouter(tags=["web"])


def _user_zoneinfo(user: User) -> ZoneInfo:
    tz_name = (user.timezone or "").strip()
    if not tz_name or tz_name.upper() == "UTC":
        tz_name = "America/Sao_Paulo"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        if tz_name != "UTC":
            return timezone(timedelta(hours=-3))
        return timezone.utc


def _fmt_dt(dt: datetime | None, *, user: User) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    local = dt.astimezone(_user_zoneinfo(user))
    return local.strftime("%d/%m/%Y %H:%M:%S")


DEFAULT_RECIPE_CATEGORIES = [
    "Acompanhamentos",
    "Alimentos na dieta low carb",
    "Almoço e Janta",
    "Barriguinha",
    "Biscoitos",
    "Bolos e Pães",
    "Café da manhã",
    "Cardápios",
    "Chás",
    "Depoimentos",
    "Detox",
    "Dicas",
    "Dicas para o Dia a Dia",
    "Dieta Low carb",
    "Dieta para Homens",
    "Diversos",
    "Doces e Sobremesas",
    "Doces Low Carb",
    "Dúvidas",
    "Dúvidas sobre Alimentação",
    "Frango e Carne",
    "Jejum Intermitente",
    "Lanches",
    "Massas",
    "Molhos",
    "Natal",
    "Peixes",
    "Petiscos",
    "Pizza",
    "Receitas",
    "Receitas Caseiras",
    "Receitas FIT",
    "Receitas Rápidas",
    "Receitas Saudáveis",
    "Receitas sem Glúten",
    "Recetas ES",
    "Remédios e Dicas Caseiras",
    "Review",
    "Saladas",
    "Sopas",
    "Sucos",
    "Tortas",
]


def _get_profile_for_user(db, *, profile_id: str, user: User) -> AutomationProfile | None:
    if user.role == UserRole.ADMIN:
        return db.scalar(select(AutomationProfile).where(AutomationProfile.id == profile_id))
    return db.scalar(select(AutomationProfile).where(AutomationProfile.id == profile_id, AutomationProfile.user_id == user.id))


def _base_css() -> str:
    return """
    :root {
      --bg: #07060b;
      --bg2: #0b0a10;
      --surface: rgba(18, 16, 28, 0.78);
      --surface2: rgba(10, 9, 15, 0.75);
      --border: rgba(255, 255, 255, 0.10);
      --border2: rgba(255, 255, 255, 0.14);
      --text: #f9fafb;
      --muted: rgba(249, 250, 251, 0.65);
      --primary: #8b5cf6;
      --primary2: #7c3aed;
      --pink: #ec4899;
      --shadow: 0 16px 50px rgba(0,0,0,0.50);
      --radius: 18px;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      color: var(--text);
      background:
        radial-gradient(1200px 700px at 15% 15%, rgba(139, 92, 246, 0.30), transparent 55%),
        radial-gradient(900px 600px at 85% 25%, rgba(236, 72, 153, 0.22), transparent 60%),
        radial-gradient(900px 700px at 60% 80%, rgba(124, 58, 237, 0.18), transparent 60%),
        linear-gradient(180deg, var(--bg), var(--bg2));
    }
    a { color: inherit; text-decoration: none; }
    .app { display: grid; grid-template-columns: 280px 1fr; min-height: 100vh; }
    .sidebar {
      position: sticky;
      top: 0;
      height: 100vh;
      padding: 20px 16px;
      border-right: 1px solid var(--border);
      background: linear-gradient(180deg, rgba(10, 9, 15, 0.9), rgba(7, 6, 11, 0.7));
      backdrop-filter: blur(12px);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 10px 12px;
      border-radius: 14px;
      border: 1px solid var(--border);
      background: rgba(18, 16, 28, 0.55);
      box-shadow: var(--shadow);
      margin-bottom: 14px;
    }
    .brand-logo-wide {
      width: 100%;
      height: 44px;
      object-fit: contain;
      display: block;
      filter: drop-shadow(0 14px 30px rgba(0,0,0,0.35));
    }
    .brand-logo-login {
      width: 100%;
      height: 84px;
      object-fit: contain;
      display: block;
      filter: drop-shadow(0 18px 40px rgba(0,0,0,0.42));
      margin-bottom: 14px;
    }
    .logo {
      width: 34px;
      height: 34px;
      border-radius: 12px;
      background: linear-gradient(135deg, var(--primary), var(--pink));
      box-shadow: 0 8px 30px rgba(139, 92, 246, 0.35);
      display: grid;
      place-items: center;
      font-weight: 800;
      font-size: 12px;
      letter-spacing: 0.6px;
      color: rgba(255, 255, 255, 0.96);
      text-shadow: 0 8px 20px rgba(0,0,0,0.35);
    }
    .brand h1 { font-size: 14px; margin: 0; letter-spacing: 0.3px; }
    .brand .sub { font-size: 12px; color: var(--muted); margin-top: 2px; }
    .nav { display: grid; gap: 8px; margin-top: 12px; }
    .nav a {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      border-radius: 14px;
      border: 1px solid transparent;
      color: rgba(249, 250, 251, 0.86);
      background: rgba(18, 16, 28, 0.25);
    }
    .nav a:hover {
      border-color: var(--border2);
      background: rgba(18, 16, 28, 0.55);
    }
    .nav .dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: rgba(139, 92, 246, 0.6);
      box-shadow: 0 0 0 4px rgba(139, 92, 246, 0.10);
    }
    .nav a:hover .dot {
      background: rgba(236, 72, 153, 0.8);
      box-shadow: 0 0 0 4px rgba(236, 72, 153, 0.12);
    }
    .sidebar-footer {
      position: absolute;
      left: 16px;
      right: 16px;
      bottom: 16px;
      padding: 12px;
      border-radius: 16px;
      border: 1px solid var(--border);
      background: rgba(18, 16, 28, 0.50);
    }
    .sidebar-footer .muted { color: var(--muted); font-size: 12px; margin: 0; }
    .main { padding: 24px 24px 40px; }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 14px;
    }
    .title { font-size: 18px; margin: 0; letter-spacing: 0.2px; }
    .muted { color: var(--muted); }
    .content { max-width: 1120px; }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 18px;
      box-shadow: var(--shadow);
    }
    .card + .card { margin-top: 14px; }
    .row { display: flex; gap: 12px; flex-wrap: wrap; }
    .col { flex: 1; min-width: 280px; }
    label { display: block; font-size: 12px; color: var(--muted); margin: 10px 0 6px; }
    input, select, textarea {
      width: 100%;
      padding: 12px 12px;
      border-radius: 14px;
      border: 1px solid var(--border);
      background: rgba(10, 9, 15, 0.72);
      color: var(--text);
      outline: none;
    }
    input:focus, select:focus, textarea:focus {
      border-color: rgba(139, 92, 246, 0.55);
      box-shadow: 0 0 0 4px rgba(139, 92, 246, 0.14);
    }
    textarea { min-height: 140px; resize: vertical; }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      padding: 12px 14px;
      border-radius: 14px;
      border: 1px solid rgba(255, 255, 255, 0.12);
      background: linear-gradient(135deg, var(--primary), var(--pink));
      color: white;
      cursor: pointer;
      font-weight: 600;
      letter-spacing: 0.2px;
      box-shadow: 0 14px 40px rgba(139, 92, 246, 0.18);
    }
    .btn.dirty {
      border-color: rgba(245, 158, 11, 0.55);
      box-shadow:
        0 0 0 4px rgba(245, 158, 11, 0.16),
        0 14px 40px rgba(245, 158, 11, 0.18);
      filter: brightness(1.06);
    }
    .btn.secondary {
      background: rgba(18, 16, 28, 0.45);
      color: rgba(249, 250, 251, 0.90);
      box-shadow: none;
    }
    .btn:hover { filter: brightness(1.05); }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid var(--border); padding: 12px 10px; text-align: left; vertical-align: top; }
    th { color: rgba(249, 250, 251, 0.75); font-size: 12px; font-weight: 600; letter-spacing: 0.5px; text-transform: uppercase; }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 4px 10px;
      border: 1px solid var(--border);
      border-radius: 999px;
      font-size: 12px;
      background: rgba(18, 16, 28, 0.45);
      color: rgba(249, 250, 251, 0.85);
    }
    .scrollbox { max-height: 380px; overflow-y: auto; border: 1px solid var(--border); border-radius: 12px; }
    .toolbar { display:flex; justify-content: space-between; align-items:center; gap:8px; }
    .toolbar .small { font-size: 12px; padding: 6px 10px; border-radius: 10px; }
    .grid2 { display: grid; gap: 14px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .hero {
      padding: 20px;
      border-radius: var(--radius);
      border: 1px solid var(--border);
      background:
        radial-gradient(600px 300px at 15% 20%, rgba(139, 92, 246, 0.25), transparent 55%),
        radial-gradient(600px 300px at 85% 30%, rgba(236, 72, 153, 0.16), transparent 60%),
        rgba(18, 16, 28, 0.50);
    }
    .hero h2 { margin: 0 0 8px; font-size: 18px; }
    .hero p { margin: 0; color: var(--muted); }
    .public-wrap { max-width: 520px; margin: 10vh auto; padding: 0 18px; }
    @media (max-width: 900px) {
      .app { grid-template-columns: 1fr; }
      .sidebar { position: relative; height: auto; border-right: none; border-bottom: 1px solid var(--border); }
      .sidebar-footer { position: relative; left: 0; right: 0; bottom: 0; margin-top: 12px; }
      .main { padding: 18px; }
      .grid2 { grid-template-columns: 1fr; }
    }
    """


def _layout(title: str, body: str, *, user: User | None = None) -> HTMLResponse:
    t = html.escape(title)
    page = f"""<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{t}</title>
  <style>
    {_base_css()}
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <img class="brand-logo-wide" src="/brand/logo_posthub.png" alt="PostHub" onerror="this.onerror=null;this.src='/static/logo.svg';" style="margin-bottom: 14px;" />
      <div class="brand">
        <div class="logo">PH</div>
        <div>
          <h1>PostHub</h1>
          <div class="sub">Automação de conteúdo</div>
        </div>
      </div>
      <nav class="nav">
        <a href="/app/robot"><span class="dot"></span>Robô</a>
        <a href="/app/bot"><span class="dot"></span>Configurar</a>
        <a href="/app/posts"><span class="dot"></span>Posts</a>
        <a href="/app/logs"><span class="dot"></span>Logs</a>
      </nav>
      <div class="sidebar-footer">
        <p class="muted">Sessão ativa</p>
        <form method="post" action="/app/logout" style="margin:10px 0 0">
          <button class="btn secondary" type="submit" style="width:100%">Sair</button>
        </form>
      </div>
    </aside>
    <main class="main">
      <div class="topbar">
        <h2 class="title">{t}</h2>
        <div></div>
      </div>
      <div class="content">{body}</div>
    </main>
  </div>
  <script>
    (function () {{
      function clearBox(selector) {{
        try {{
          var el = document.querySelector(selector);
          if (!el) return;
          el.innerHTML = "";
        }} catch (e) {{}}
      }}
      window.clearBox = clearBox;
      function setupForm(form) {{
        var buttons = Array.prototype.slice.call(form.querySelectorAll("button[type='submit']"));
        var saveBtn = buttons.find(function (b) {{
          var t = (b.textContent || "").trim().toLowerCase();
          return t === "salvar" || t.startsWith("salvar ");
        }});
        if (!saveBtn) return;

        saveBtn.classList.add("secondary");
        saveBtn.classList.remove("dirty");

        var dirty = false;
        function setDirty(v) {{
          dirty = v;
          if (dirty) {{
            saveBtn.classList.remove("secondary");
            saveBtn.classList.add("dirty");
          }} else {{
            saveBtn.classList.add("secondary");
            saveBtn.classList.remove("dirty");
          }}
        }}

        var fields = Array.prototype.slice.call(form.querySelectorAll("input, select, textarea"));
        fields.forEach(function (el) {{
          el.addEventListener("input", function () {{ setDirty(true); }});
          el.addEventListener("change", function () {{ setDirty(true); }});
        }});

        form.addEventListener("submit", function () {{ setDirty(false); }});
      }}

      Array.prototype.slice.call(document.querySelectorAll("form")).forEach(setupForm);
    }})();
  </script>
</body>
</html>"""
    return HTMLResponse(page)


@router.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/app/robot", status_code=status.HTTP_302_FOUND)


@router.get("/app/login", include_in_schema=False)
def login_page(request: Request):
    msg = html.escape(request.query_params.get("msg", ""))
    google_enabled = bool(settings.google_client_id and settings.google_client_secret)
    google_btn = (
        "<div style='margin-top:12px'><a class='btn secondary' style='width:100%; display:inline-flex' href='/app/login/google'>Entrar com Google</a></div>"
        if google_enabled
        else ""
    )
    page = f"""<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Entrar - PostHub</title>
  <style>{_base_css()}</style>
</head>
<body>
  <div class="public-wrap">
    <img class="brand-logo-login" src="/brand/logo_posthub.png" alt="PostHub" onerror="this.onerror=null;this.src='/static/logo.svg';" />
    <div class="card">
      <div class="hero">
        <h2>Entrar</h2>
        <p>Use seu usuário e senha para acessar o painel.</p>
      </div>
      <div style="margin-top:12px" class="muted">{msg}</div>
      <form method="post" action="/app/login" style="margin-top: 14px;">
        <label>Usuário / ID</label>
        <input name="email" type="text" placeholder="usuario" required />
        <label>Senha</label>
        <input name="password" type="password" placeholder="Sua senha" required />
        <div style="margin-top:12px">
          <button class="btn" type="submit" style="width:100%">Entrar</button>
        </div>
      </form>
      {google_btn}
    </div>
  </div>
</body>
</html>"""
    return HTMLResponse(page)


@router.post("/app/login", include_in_schema=False)
def login_action(email: str = Form(...), password: str = Form(...), db=Depends(get_db)):
    e = (email or "").strip().lower()
    if "@" in e:
        user = db.scalar(select(User).where(User.email == e))
    else:
        user = db.scalar(select(User).where(User.access_id == e))
    if not user or not verify_password(password, user.password_hash):
        return RedirectResponse("/app/login?msg=Credenciais+inv%C3%A1lidas", status_code=status.HTTP_302_FOUND)
    if getattr(user, "must_set_password", False):
        return RedirectResponse("/app/login?msg=Voc%C3%AA+precisa+criar+uma+senha+primeiro", status_code=status.HTTP_302_FOUND)
    token = create_access_token(subject=user.id, role=user.role.value)
    resp = RedirectResponse("/app/robot", status_code=status.HTTP_302_FOUND)
    resp.set_cookie("access_token", token, httponly=True, samesite="lax")
    return resp


@router.post("/app/logout", include_in_schema=False)
def logout_action():
    resp = RedirectResponse("/app/login", status_code=status.HTTP_302_FOUND)
    resp.delete_cookie("access_token")
    return resp


@router.get("/app", include_in_schema=False)
def dashboard(user: User = Depends(get_current_user), db=Depends(get_db)):
    return RedirectResponse("/app/robot", status_code=status.HTTP_302_FOUND)


def _get_or_create_single_bot(db, *, user: User) -> AutomationProfile:
    if user.role == UserRole.ADMIN:
        bot = db.scalar(select(AutomationProfile).order_by(AutomationProfile.created_at.asc()).limit(1))
    else:
        bot = db.scalar(
            select(AutomationProfile)
            .where(AutomationProfile.user_id == user.id)
            .order_by(AutomationProfile.created_at.asc())
            .limit(1)
        )
    if bot:
        return bot
    bot = AutomationProfile(
        user_id=user.id,
        name="Robô de Receitas",
        active=True,
        schedule_config_json={"posts_per_day": 15, "interval_minutes": 60},
        anti_block_config_json={},
        publish_config_json={
            "facebook_link": "comments",
            "default_category": "Receitas",
            "categories": DEFAULT_RECIPE_CATEGORIES,
        },
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)
    _ensure_default_recipe_actions(db, bot=bot)
    return bot


def _ensure_default_recipe_actions(db, *, bot: AutomationProfile) -> None:
    existing = db.scalar(select(AiAction.id).where(AiAction.profile_id == bot.id))
    if existing is not None:
        return
    a_site = AiAction(
        user_id=bot.user_id,
        profile_id=bot.id,
        name="Site (SEO) - Receitas",
        destination=ActionDestination.WORDPRESS,
        active=True,
        prompt_text=(
            "Você é um redator culinário. Reescreva a receita abaixo em PT-BR, sem copiar o texto original. "
            "Entregue um texto SEO completo com: Título, Introdução, Tempo de preparo e rendimento, Ingredientes, "
            "Modo de preparo (passo a passo), Dicas e variações."
            "\nResponda somente com o conteúdo final."
        ),
    )
    a_fb = AiAction(
        user_id=bot.user_id,
        profile_id=bot.id,
        name="Facebook - Receitas",
        destination=ActionDestination.FACEBOOK,
        active=True,
        prompt_text=(
            "Crie um texto curto e chamativo para Facebook sobre a receita abaixo, com emojis moderados e CTA. "
            "Finalize com: 👉 veja o modo de preparo nos comentários"
            "\nResponda somente com o texto final."
        ),
    )
    db.add_all([a_site, a_fb])
    db.commit()


@router.get("/app/robot", include_in_schema=False)
def robot_panel(request: Request, user: User = Depends(get_current_user), db=Depends(get_db)):
    bot = _get_or_create_single_bot(db, user=user)
    _ensure_default_recipe_actions(db, bot=bot)
    now = datetime.utcnow()
    last_collect = db.scalar(
        select(JobLog)
        .where(JobLog.profile_id == bot.id, JobLog.stage == JOB_COLLECT, JobLog.message == "collect_completed")
        .order_by(JobLog.created_at.desc())
        .limit(1)
    )
    meta = (last_collect.meta_json or {}) if last_collect else {}
    created = int(meta.get("created") or 0)
    skipped = int(meta.get("skipped_duplicate") or meta.get("skipped") or 0)
    ignored = int(meta.get("skipped_non_recipe") or 0) + int(meta.get("skipped_error") or 0)
    queued_jobs = int(db.scalar(select(func.count()).select_from(Job).where(Job.profile_id == bot.id, Job.status == JobStatus.queued)) or 0)
    queued_due = int(
        db.scalar(
            select(func.count()).select_from(Job).where(Job.profile_id == bot.id, Job.status == JobStatus.queued, Job.run_at <= now)
        )
        or 0
    )
    queued_scheduled = int(
        db.scalar(
            select(func.count()).select_from(Job).where(Job.profile_id == bot.id, Job.status == JobStatus.queued, Job.run_at > now)
        )
        or 0
    )
    running_jobs = int(
        db.scalar(select(func.count()).select_from(Job).where(Job.profile_id == bot.id, Job.status == JobStatus.running)) or 0
    )
    pending_posts = int(
        db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == bot.id, Post.status == PostStatus.pending)) or 0
    )
    processing_posts = int(
        db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == bot.id, Post.status == PostStatus.processing))
        or 0
    )
    in_progress = (queued_jobs + running_jobs + pending_posts + processing_posts) > 0
    gemini_ok = (
        db.scalar(select(Integration.id).where(Integration.profile_id == bot.id, Integration.type == IntegrationType.GEMINI)) is not None
    )
    gemini_status = "OK" if gemini_ok else "FALTANDO"
    failed_count = int(
        db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == bot.id, Post.status == PostStatus.failed)) or 0
    )
    posts = list(
        db.execute(
            select(Post, CollectedContent.title)
            .join(CollectedContent, CollectedContent.id == Post.collected_content_id)
            .where(Post.profile_id == bot.id)
            .order_by(Post.created_at.desc())
            .limit(50)
        ).all()
    )
    rows = "".join(
        f"<tr><td>{html.escape(str(title or ''))}</td><td><span class='pill'>{html.escape(p.status.value)}</span></td><td class='muted'>{html.escape(_fmt_dt(p.created_at, user=user))}</td><td>{html.escape(p.wp_url or '')}</td></tr>"
        for p, title in posts
    )
    msg = (request.query_params.get("msg") or "").strip()
    banner = ""
    if msg:
        banner = f"<div class='card' style='border-color: rgba(255,255,255,.08)'><b>{html.escape(msg)}</b></div>"
    accelerate = ""
    if queued_scheduled > 0 and running_jobs == 0:
        accelerate = f"""
        <div class="col card">
          <h3>Rodar pendentes agora</h3>
          <p class="muted">Existem <b>{queued_scheduled}</b> jobs agendados (com hora futura). Clique para liberar e rodar agora.</p>
          <form method="post" action="/app/robot/run-now">
            <button class="btn secondary" type="submit">Rodar agora</button>
          </form>
        </div>
        """
    body = f"""
    {banner}
    <div class="card">
      <h2>Robô</h2>
      <p class="muted">Bot atual: <b>{html.escape(bot.name)}</b></p>
      <p class="muted">Gemini: <b>{gemini_status}</b> • Última coleta: <b>{created}</b> novos / <b>{skipped}</b> repetidos / <b>{ignored}</b> ignorados</p>
      <p class="muted">Fila: <b>{queued_due}</b> prontos / <b>{queued_scheduled}</b> agendados / <b>{running_jobs}</b> rodando • Posts: <b>{pending_posts}</b> pendentes / <b>{processing_posts}</b> processando • Status: <b>{"EM ANDAMENTO" if in_progress else "LIVRE"}</b></p>
      <div class="row">
        <div class="col card">
          <h3>Iniciar robô</h3>
          <p class="muted">Clique para buscar receitas nas fontes e processar automaticamente.</p>
          <form method="post" action="/app/robot/start">
            <button class="btn" type="submit">Iniciar</button>
          </form>
        </div>
        {accelerate}
        <div class="col card">
          <h3>Reprocessar falhas</h3>
          <p class="muted">Se antes falhou por chave/modelo, reprocessa os posts com erro.</p>
          <form method="post" action="/app/robot/retry-ai">
            <button class="btn secondary" type="submit">Reprocessar IA ({failed_count})</button>
          </form>
          <form method="post" action="/app/robot/clear-failures" style="margin-top:8px">
            <button class="btn secondary" type="submit">Limpar falhas</button>
          </form>
        </div>
        <div class="col card">
          <h3>Limpar histórico</h3>
          <p class="muted">Remove os registros de posts deste bot no PostHub (não apaga do seu WordPress).</p>
          <form method="post" action="/app/robot/clear-posts">
            <button class="btn secondary" type="submit">Limpar posts</button>
          </form>
        </div>
      </div>
    </div>
    <div class="card">
      <h3 style="margin:0">Posts</h3>
      <div class="scrollbox">
        <table id="robot-posts-table"><thead><tr><th>Título</th><th>Status</th><th>Criado</th><th>WP URL</th></tr></thead><tbody>{rows}</tbody></table>
      </div>
    </div>
    """
    return _layout("Robô", body, user=user)


@router.get("/app/bot", include_in_schema=False)
def bot_redirect(user: User = Depends(get_current_user), db=Depends(get_db)):
    bot = _get_or_create_single_bot(db, user=user)
    return RedirectResponse(f"/app/profiles/{bot.id}", status_code=status.HTTP_302_FOUND)


@router.post("/app/robot/start", include_in_schema=False)
def robot_start(user: User = Depends(get_current_user), db=Depends(get_db)):
    bot = _get_or_create_single_bot(db, user=user)
    queued_jobs = int(
        db.scalar(select(func.count()).select_from(Job).where(Job.profile_id == bot.id, Job.status == JobStatus.queued)) or 0
    )
    running_jobs = int(
        db.scalar(select(func.count()).select_from(Job).where(Job.profile_id == bot.id, Job.status == JobStatus.running)) or 0
    )
    pending_posts = int(
        db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == bot.id, Post.status == PostStatus.pending)) or 0
    )
    processing_posts = int(
        db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == bot.id, Post.status == PostStatus.processing))
        or 0
    )
    if (queued_jobs + running_jobs + pending_posts + processing_posts) > 0:
        return RedirectResponse("/app/robot?msg=Postagens+em+andamento.+Aguarde+terminar+a+fila+atual+para+iniciar+de+novo.", status_code=status.HTTP_302_FOUND)
    cfg = bot.schedule_config_json or {}
    limit = int(cfg.get("posts_per_day") or 15)
    respect = int(cfg.get("respect_schedule") or 0) == 1
    interval_minutes = int(cfg.get("interval_minutes") or 0) if respect else 0
    enqueue_job(
        db,
        user_id=bot.user_id,
        profile_id=bot.id,
        job_type=JOB_COLLECT,
        payload={"limit": limit, "interval_minutes": interval_minutes, "respect_schedule": 1 if respect else 0},
    )
    db.commit()
    return RedirectResponse("/app/robot", status_code=status.HTTP_302_FOUND)


@router.post("/app/robot/run-now", include_in_schema=False)
def robot_run_now(user: User = Depends(get_current_user), db=Depends(get_db)):
    bot = _get_or_create_single_bot(db, user=user)
    now = datetime.utcnow()
    db.execute(update(Job).where(Job.profile_id == bot.id, Job.status == JobStatus.queued).values(run_at=now))
    db.commit()
    return RedirectResponse("/app/robot?msg=Fila+liberada.+Rodando+agora.", status_code=status.HTTP_302_FOUND)


@router.post("/app/robot/retry-ai", include_in_schema=False)
def robot_retry_ai(user: User = Depends(get_current_user), db=Depends(get_db)):
    bot = _get_or_create_single_bot(db, user=user)
    posts = list(
        db.scalars(
            select(Post)
            .where(Post.profile_id == bot.id, Post.status == PostStatus.failed)
            .order_by(Post.updated_at.desc())
            .limit(50)
        )
    )
    for p in posts:
        p.status = PostStatus.pending
        p.updated_at = datetime.utcnow()
        db.add(p)
        enqueue_job(
            db,
            user_id=p.user_id,
            profile_id=p.profile_id,
            post_id=p.id,
            job_type=JOB_AI,
            payload={"collected_content_id": p.collected_content_id},
        )
    db.commit()
    return RedirectResponse("/app/robot", status_code=status.HTTP_302_FOUND)


@router.post("/app/robot/clear-failures", include_in_schema=False)
def robot_clear_failures(user: User = Depends(get_current_user), db=Depends(get_db)):
    bot = _get_or_create_single_bot(db, user=user)
    ids = list(db.scalars(select(Post.id).where(Post.profile_id == bot.id, Post.status == PostStatus.failed)))
    _delete_posts(db, profile_id=bot.id, post_ids=[str(x) for x in ids])
    db.commit()
    return RedirectResponse("/app/robot?msg=Falhas+removidas", status_code=status.HTTP_302_FOUND)


@router.post("/app/robot/clear-posts", include_in_schema=False)
def robot_clear_posts(user: User = Depends(get_current_user), db=Depends(get_db)):
    bot = _get_or_create_single_bot(db, user=user)
    ids = list(db.scalars(select(Post.id).where(Post.profile_id == bot.id)))
    _delete_posts(db, profile_id=bot.id, post_ids=[str(x) for x in ids])
    db.commit()
    return RedirectResponse("/app/robot?msg=Posts+removidos", status_code=status.HTTP_302_FOUND)


@router.get("/app/profiles", include_in_schema=False)
def profiles_page(user: User = Depends(get_current_user), db=Depends(get_db)):
    return RedirectResponse("/app/bot", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/create", include_in_schema=False)
def profiles_create(name: str = Form(...), active: str = Form("1"), user: User = Depends(get_current_user), db=Depends(get_db)):
    p = AutomationProfile(user_id=user.id, name=name.strip(), active=(active == "1"), schedule_config_json={}, anti_block_config_json={})
    db.add(p)
    db.commit()
    return RedirectResponse("/app/profiles", status_code=status.HTTP_302_FOUND)


@router.get("/app/profiles/{profile_id}", include_in_schema=False)
def profile_detail(profile_id: str, request: Request, user: User = Depends(get_current_user), db=Depends(get_db)):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    _ensure_default_recipe_actions(db, bot=p)
    tab = (request.query_params.get("tab") or "fontes").strip().lower()
    tabs = [
        ("fontes", "Fontes"),
        ("publicacao", "Publicação"),
        ("integracoes", "Integrações"),
        ("agendamento", "Agendamento"),
        ("ia", "IA"),
        ("posts", "Posts"),
    ]
    tab_bar = "".join(
        f"<a class='pill {('active' if t == tab else '')}' href='/app/profiles/{p.id}?tab={t}' style='text-decoration:none'>{html.escape(label)}</a>"
        for t, label in tabs
    )
    body = f"""
    <div class="card">
      <h2>Configurar: {html.escape(p.name)}</h2>
      <div class="row" style="gap:10px; flex-wrap:wrap">{tab_bar}</div>
    </div>
    """
    msg = (request.query_params.get("msg") or "").strip()
    if msg:
        body += f"<div class='card' style='border-color: rgba(255,255,255,.08)'><b>{html.escape(msg)}</b></div>"

    if tab == "fontes":
        sources = list(db.scalars(select(Source).where(Source.profile_id == p.id).order_by(Source.created_at.desc())))
        rows = "".join(
            f"<tr><td><span class='pill'>{html.escape(s.type.value)}</span></td><td>{html.escape(s.value)}</td>"
            f"<td><form method='post' action='/app/profiles/{p.id}/sources/{s.id}/delete' style='margin:0'><button class='btn secondary' type='submit'>Remover</button></form></td></tr>"
            for s in sources
        )
        body += f"""
        <div class="card">
          <div class="row">
            <div class="col card">
              <h3>Adicionar fonte</h3>
              <form method="post" action="/app/profiles/{p.id}/sources/create">
                <label>Tipo</label>
                <select name="type">
                  <option value="URL">URL</option>
                  <option value="RSS">RSS</option>
                  <option value="KEYWORD">PALAVRA-CHAVE</option>
                </select>
                <label style="margin-top:8px">Valor</label>
                <input name="value" required />
                <div style="margin-top:12px"><button class="btn" type="submit">Salvar</button></div>
              </form>
            </div>
          </div>
          <h3 style="margin:0">Fontes</h3>
          <div class="scrollbox">
            <table id="sources-table"><thead><tr><th>Tipo</th><th>Valor</th><th></th></tr></thead><tbody>{rows}</tbody></table>
          </div>
        </div>
        """
    elif tab == "publicacao":
        publish_cfg = dict(p.publish_config_json or {})
        fb_link_place = str(publish_cfg.get("facebook_link", "comments"))
        fb_enabled = bool(publish_cfg.get("facebook_enabled"))
        fb_selected = publish_cfg.get("facebook_page_ids") or []
        fb_selected_ids = (
            {str(x).strip() for x in fb_selected if str(x).strip()} if isinstance(fb_selected, list) else set()
        )
        fb_pages: list[dict] = []
        fb_integ = db.scalar(select(Integration).where(Integration.profile_id == p.id, Integration.type == IntegrationType.FACEBOOK))
        if fb_integ:
            try:
                fb_creds = decrypt_json(fb_integ.credentials_encrypted)
                pages_val = fb_creds.get("pages") if isinstance(fb_creds, dict) else None
                if isinstance(pages_val, list):
                    fb_pages = [x for x in pages_val if isinstance(x, dict)]
            except Exception:
                fb_pages = []
        cats_lines = "\n".join(str(c) for c in (publish_cfg.get("categories") or []) if str(c).strip())
        default_cat = str(publish_cfg.get("default_category") or "Receitas")
        fb_pages_html = ""
        if fb_pages:
            items = []
            for pg in fb_pages:
                pid = str(pg.get("page_id") or "").strip()
                if not pid:
                    continue
                nm = str(pg.get("name") or "").strip()
                label = f"{html.escape(nm)} <span class='muted'>({html.escape(pid)})</span>" if nm else f"<span class='muted'>{html.escape(pid)}</span>"
                checked = "checked" if (not fb_selected_ids or pid in fb_selected_ids) else ""
                items.append(
                    f"<label style='display:flex; gap:8px; align-items:center; margin:6px 0'>"
                    f"<input type='checkbox' name='facebook_page_ids' value='{html.escape(pid)}' {checked} />"
                    f"<span>{label}</span>"
                    f"</label>"
                )
            fb_pages_html = "".join(items) or "<div class='muted'>Nenhuma página válida cadastrada.</div>"
        else:
            fb_pages_html = f"<div class='muted'>Nenhuma página cadastrada. Vá em <a href='/app/profiles/{p.id}?tab=integracoes'>Integrações</a> e adicione suas páginas.</div>"
        body += f"""
        <div class="card">
          <h3>Publicação</h3>
          <div class="row">
            <div class="col card">
              <h4>WordPress</h4>
              <form method="post" action="/app/profiles/{p.id}/publish/wordpress">
                <label>Categoria padrão (se a IA errar)</label>
                <input name="default_category" value="{html.escape(default_cat)}" required />
                <label>Categorias do seu site (uma por linha)</label>
                <textarea name="categories" placeholder="Cole aqui as categorias do seu WordPress (uma por linha)">{html.escape(cats_lines)}</textarea>
                <div style="margin-top:12px"><button class="btn" type="submit">Salvar</button></div>
              </form>
              <p class="muted" style="margin-top:10px">A IA escolhe 1 categoria exatamente como está nessa lista.</p>
            </div>
            <div class="col card">
              <h4>Facebook</h4>
              <form method="post" action="/app/profiles/{p.id}/publish/facebook">
                <label style="display:flex; gap:10px; align-items:center">
                  <input type="checkbox" name="facebook_enabled" value="1" {"checked" if fb_enabled else ""} />
                  <span>Ativar postagem no Facebook</span>
                </label>
                <label style="margin-top:10px">Páginas</label>
                <div class="scrollbox" style="max-height: 220px; padding: 8px 10px; border: 1px solid rgba(255,255,255,.08); border-radius: 10px;">
                  {fb_pages_html}
                </div>
                <label>Link</label>
                <select name="facebook_link">
                  <option value="comments" {"selected" if fb_link_place == "comments" else ""}>Nos comentários</option>
                  <option value="body" {"selected" if fb_link_place == "body" else ""}>No texto</option>
                </select>
                <div style="margin-top:12px"><button class="btn" type="submit">Salvar</button></div>
              </form>
              <p class="muted" style="margin-top:10px">A postagem no Facebook roda depois do WordPress. Você escolhe as páginas e onde entra o link.</p>
            </div>
          </div>
        </div>
        """
    elif tab == "integracoes":
        integrations = list(db.scalars(select(Integration).where(Integration.profile_id == p.id).order_by(Integration.created_at.desc())))
        fb_pages: list[dict] = []
        fb_integ = db.scalar(select(Integration).where(Integration.profile_id == p.id, Integration.type == IntegrationType.FACEBOOK))
        if fb_integ:
            try:
                fb_creds = decrypt_json(fb_integ.credentials_encrypted)
                pages_val = fb_creds.get("pages") if isinstance(fb_creds, dict) else None
                if isinstance(pages_val, list):
                    fb_pages = [x for x in pages_val if isinstance(x, dict)]
            except Exception:
                fb_pages = []
        fb_rows = ""
        for pg in fb_pages:
            pid = str(pg.get("page_id") or "").strip()
            if not pid:
                continue
            nm = str(pg.get("name") or "").strip()
            token = str(pg.get("access_token") or "").strip()
            token_state = "salvo" if token else "faltando"
            fb_rows += (
                f"<tr><td>{html.escape(nm)}</td><td class='muted'>{html.escape(pid)}</td><td><span class='pill'>{html.escape(token_state)}</span></td>"
                f"<td><form method='post' action='/app/profiles/{p.id}/integrations/facebook/pages/remove' style='margin:0'>"
                f"<input type='hidden' name='page_id' value='{html.escape(pid)}' />"
                f"<button class='btn secondary' type='submit'>Remover</button></form></td></tr>"
            )
        if not fb_rows:
            fb_rows = "<tr><td colspan='4' class='muted'>Nenhuma página cadastrada.</td></tr>"
        rows = "".join(
            f"<tr><td><span class='pill'>{html.escape(i.type.value)}</span></td><td>{html.escape(i.name)}</td><td>{html.escape(i.status.value)}</td>"
            f"<td><form method='post' action='/app/profiles/{p.id}/integrations/{i.id}/delete' style='margin:0'><button class='btn secondary' type='submit'>Remover</button></form></td></tr>"
            for i in integrations
        )
        body += f"""
        <div class="card">
          <h3>Integrações</h3>
          <div class="row">
            <div class="col card">
              <h4>WordPress</h4>
              <form method="post" action="/app/profiles/{p.id}/integrations/wordpress">
                <label>Nome</label>
                <input name="name" value="WordPress" required />
                <label>Base URL</label>
                <input name="base_url" placeholder="https://seusite.com" required />
                <label>Usuário</label>
                <input name="username" required />
                <label>App Password</label>
                <input name="app_password" required />
                <div style="margin-top:12px"><button class="btn" type="submit">Salvar</button></div>
              </form>
            </div>
            <div class="col card">
              <h4>Gemini</h4>
              <form method="post" action="/app/profiles/{p.id}/integrations/gemini">
                <label>Gemini API Key</label>
                <input name="api_key" type="password" placeholder="Cole sua chave aqui" required />
                <label>Modelo (opcional)</label>
                <input name="model" placeholder="gemini-1.5-flash-latest" />
                <div style="margin-top:12px"><button class="btn" type="submit">Salvar</button></div>
              </form>
            </div>
            <div class="col card">
              <h4>Facebook (Páginas)</h4>
              <form method="post" action="/app/profiles/{p.id}/integrations/facebook/pages/add">
                <label>Nome (opcional)</label>
                <input name="name" placeholder="Ex: Minha Página" />
                <label>Page ID</label>
                <input name="page_id" placeholder="Ex: 1234567890" required />
                <label>Page Access Token</label>
                <input name="access_token" type="password" placeholder="Cole o token da página" required />
                <div style="margin-top:12px"><button class="btn" type="submit">Adicionar</button></div>
              </form>
              <h4 style="margin-top:18px">Páginas cadastradas</h4>
              <div class="scrollbox" style="max-height: 220px">
                <table><thead><tr><th>Nome</th><th>Page ID</th><th>Token</th><th></th></tr></thead><tbody>{fb_rows}</tbody></table>
              </div>
            </div>
          </div>
          <h4>Conexões</h4>
          <table><thead><tr><th>Tipo</th><th>Nome</th><th>Status</th><th></th></tr></thead><tbody>{rows}</tbody></table>
        </div>
        """
    elif tab == "agendamento":
        cfg = dict(p.schedule_config_json or {})
        posts_per_day = int(cfg.get("posts_per_day") or 15)
        interval_minutes = int(cfg.get("interval_minutes") or 0)
        start_at_utc = str(cfg.get("start_at_utc") or "").strip()
        start_local_value = ""
        if start_at_utc:
            try:
                dt = datetime.fromisoformat(start_at_utc.replace("Z", "+00:00"))
                start_local_value = dt.astimezone(_user_zoneinfo(user)).strftime("%Y-%m-%dT%H:%M")
            except Exception:
                start_local_value = ""
        body += f"""
        <div class="card">
          <h3>Agendamento</h3>
          <p class="muted">Define quantidade, intervalo e data/hora de início. Esse agendamento vai valer pro WordPress e depois pro Facebook também.</p>
          <form method="post" action="/app/profiles/{p.id}/schedule">
            <div class="row">
              <div class="col">
                <label>Quantidade</label>
                <input name="posts_per_day" type="number" min="1" step="1" value="{posts_per_day}" />
              </div>
              <div class="col">
                <label>Tempo entre postagens (min)</label>
                <input name="interval_minutes" type="number" min="0" value="{interval_minutes}" />
                <div class="muted" style="margin-top:6px">0 = roda tudo seguido</div>
              </div>
              <div class="col">
                <label>Começar em (data/hora)</label>
                <input name="start_at" type="datetime-local" value="{html.escape(start_local_value)}" />
                <div class="muted" style="margin-top:6px">Vazio = começar agora</div>
              </div>
            </div>
            <div class="row">
              <div class="col">
                <label>Respeitar agendamento</label>
                <select name="respect_schedule">
                  <option value="0">Não (rodar agora)</option>
                  <option value="1" {"selected" if int(cfg.get("respect_schedule") or 0) == 1 else ""}>Sim (usar intervalo e data/hora)</option>
                </select>
              </div>
            </div>
            <div style="margin-top:12px"><button class="btn" type="submit">Salvar</button></div>
          </form>
        </div>
        """
    elif tab == "ia":
        site_action = db.scalar(
            select(AiAction)
            .where(AiAction.profile_id == p.id, AiAction.destination == ActionDestination.WORDPRESS)
            .order_by(AiAction.created_at.asc())
            .limit(1)
        )
        fb_action = db.scalar(
            select(AiAction)
            .where(AiAction.profile_id == p.id, AiAction.destination == ActionDestination.FACEBOOK)
            .order_by(AiAction.created_at.asc())
            .limit(1)
        )
        site_prompt = (site_action.prompt_text if site_action else "").strip()
        fb_prompt = (fb_action.prompt_text if fb_action else "").strip()
        body += f"""
        <div class="card">
          <h3>Comandos da IA</h3>
          <p class="muted">Você pode editar o comando do site e do Facebook quando quiser.</p>
          <form method="post" action="/app/profiles/{p.id}/ai-prompts">
            <div class="row">
              <div class="col card">
                <h4>Site (WordPress)</h4>
                <textarea name="site_prompt" placeholder="Cole o comando do site aqui">{html.escape(site_prompt)}</textarea>
              </div>
              <div class="col card">
                <h4>Facebook</h4>
                <textarea name="facebook_prompt" placeholder="Cole o comando do Facebook aqui">{html.escape(fb_prompt)}</textarea>
              </div>
            </div>
            <div style="margin-top:12px"><button class="btn" type="submit">Salvar</button></div>
          </form>
        </div>
        """
    else:
        posts = list(
            db.execute(
                select(Post, CollectedContent.title)
                .join(CollectedContent, CollectedContent.id == Post.collected_content_id)
                .where(Post.profile_id == p.id)
                .order_by(Post.created_at.desc())
                .limit(200)
            ).all()
        )
        pending_rows = ""
        completed_rows = ""
        failed_rows = ""
        for post, title in posts:
            when = _fmt_dt(post.published_at or post.created_at, user=user)
            is_canceled = isinstance(post.outputs_json, dict) and bool(post.outputs_json.get("canceled_by_user"))
            st = "cancelado" if (post.status == PostStatus.failed and is_canceled) else post.status.value
            tr = (
                f"<tr>"
                f"<td><input type='checkbox' name='post_id' value='{html.escape(post.id)}' /></td>"
                f"<td>{html.escape(str(title or ''))}</td>"
                f"<td><span class='pill'>{html.escape(st)}</span></td>"
                f"<td class='muted'>{html.escape(when)}</td>"
                f"<td>{html.escape(post.wp_url or '')}</td>"
                f"</tr>"
            )
            if post.status in (PostStatus.pending, PostStatus.processing):
                pending_rows += tr
            elif post.status == PostStatus.completed:
                completed_rows += tr
            else:
                failed_rows += tr
        body += f"""
        <div class="card">
          <h3>Posts (gerenciar)</h3>
          <p class="muted">Cancelar = para a fila do PostHub (não apaga do seu WordPress). Apagar = remove do PostHub.</p>
          <div class="row" style="gap:10px; flex-wrap:wrap">
            <form method="post" action="/app/profiles/{p.id}/posts/cancel-all" style="margin:0">
              <button class="btn secondary" type="submit">Cancelar pendentes</button>
            </form>
            <form method="post" action="/app/profiles/{p.id}/posts/delete-completed" style="margin:0">
              <button class="btn secondary" type="submit">Apagar publicados (PostHub)</button>
            </form>
          </div>
        </div>
        <div class="card">
          <div class="toolbar">
            <h4 style="margin:0">Pendentes / Processando</h4>
            <button class="btn secondary small" type="button" onclick="clearBox('#posts-pending-table tbody')">Limpar dados</button>
          </div>
          <form method="post" action="/app/profiles/{p.id}/posts/bulk">
            <div class="scrollbox">
              <table id="posts-pending-table"><thead><tr><th></th><th>Título</th><th>Status</th><th>Quando</th><th>Link</th></tr></thead><tbody>{pending_rows}</tbody></table>
            </div>
            <div class="row" style="margin-top:12px; gap:10px; flex-wrap:wrap">
              <button class="btn secondary" type="submit" name="mode" value="cancel">Cancelar selecionados</button>
              <button class="btn secondary" type="submit" name="mode" value="delete">Excluir selecionados (PostHub)</button>
            </div>
          </form>
        </div>
        <div class="card">
          <div class="toolbar">
            <h4 style="margin:0">Publicados</h4>
            <button class="btn secondary small" type="button" onclick="clearBox('#posts-completed-table tbody')">Limpar dados</button>
          </div>
          <form method="post" action="/app/profiles/{p.id}/posts/bulk">
            <div class="scrollbox">
              <table id="posts-completed-table"><thead><tr><th></th><th>Título</th><th>Status</th><th>Quando</th><th>Link</th></tr></thead><tbody>{completed_rows}</tbody></table>
            </div>
            <div class="row" style="margin-top:12px; gap:10px; flex-wrap:wrap">
              <button class="btn secondary" type="submit" name="mode" value="delete">Excluir selecionados (PostHub)</button>
              <button class="btn secondary" type="submit" name="mode" value="delete_wp" onclick="return confirm('Tem certeza que quer APAGAR do site (WordPress)?')">Apagar do site (WordPress)</button>
            </div>
            <p class="muted" style="margin-top:10px">Em breve: apagar também do Facebook (quando a integração estiver ativa).</p>
          </form>
        </div>
        <div class="card">
          <div class="toolbar">
            <h4 style="margin:0">Falhas</h4>
            <button class="btn secondary small" type="button" onclick="clearBox('#posts-failed-table tbody')">Limpar dados</button>
          </div>
          <div class="scrollbox">
            <table id="posts-failed-table"><thead><tr><th></th><th>Título</th><th>Status</th><th>Quando</th><th>Link</th></tr></thead><tbody>{failed_rows}</tbody></table>
          </div>
        </div>
        """

    return _layout("Perfil", body, user=user)


@router.post("/app/profiles/{profile_id}/schedule", include_in_schema=False)
def profile_schedule_save(
    profile_id: str,
    posts_per_day: str = Form("15"),
    interval_minutes: str = Form("0"),
    start_at: str = Form(""),
    respect_schedule: str = Form("0"),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    cfg = dict(p.schedule_config_json or {})
    try:
        v = int(posts_per_day or 15)
        cfg["posts_per_day"] = 1 if v < 1 else v
    except Exception:
        cfg["posts_per_day"] = 15
    try:
        cfg["interval_minutes"] = int(interval_minutes or 0)
    except Exception:
        cfg["interval_minutes"] = 0
    s = (start_at or "").strip()
    if s:
        try:
            local = datetime.fromisoformat(s)
            local = local.replace(tzinfo=_user_zoneinfo(user))
            utc = local.astimezone(timezone.utc)
            cfg["start_at_utc"] = utc.isoformat().replace("+00:00", "Z")
        except Exception:
            cfg["start_at_utc"] = ""
    else:
        cfg["start_at_utc"] = ""
    try:
        cfg["respect_schedule"] = 1 if int(respect_schedule or 0) == 1 else 0
    except Exception:
        cfg["respect_schedule"] = 0
    p.schedule_config_json = cfg
    db.add(p)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}?tab=agendamento", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/ai-prompts", include_in_schema=False)
def profile_ai_prompts_save(
    profile_id: str,
    site_prompt: str = Form(""),
    facebook_prompt: str = Form(""),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    _ensure_default_recipe_actions(db, bot=p)
    site_action = db.scalar(
        select(AiAction)
        .where(AiAction.profile_id == p.id, AiAction.destination == ActionDestination.WORDPRESS)
        .order_by(AiAction.created_at.asc())
        .limit(1)
    )
    fb_action = db.scalar(
        select(AiAction)
        .where(AiAction.profile_id == p.id, AiAction.destination == ActionDestination.FACEBOOK)
        .order_by(AiAction.created_at.asc())
        .limit(1)
    )
    sp = (site_prompt or "").strip()
    fp = (facebook_prompt or "").strip()
    if site_action:
        site_action.prompt_text = sp
        site_action.active = True
        db.add(site_action)
    else:
        db.add(
            AiAction(
                user_id=p.user_id,
                profile_id=p.id,
                name="Site - Receitas",
                destination=ActionDestination.WORDPRESS,
                prompt_text=sp,
                active=True,
            )
        )
    if fb_action:
        fb_action.prompt_text = fp
        fb_action.active = True
        db.add(fb_action)
    else:
        db.add(
            AiAction(
                user_id=p.user_id,
                profile_id=p.id,
                name="Facebook - Receitas",
                destination=ActionDestination.FACEBOOK,
                prompt_text=fp,
                active=True,
            )
        )
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}?tab=ia", status_code=status.HTTP_302_FOUND)


def _cancel_posts(db, *, profile_id: str, post_ids: list[str], user: User):
    now = datetime.utcnow()
    posts = list(db.scalars(select(Post).where(Post.profile_id == profile_id, Post.id.in_(post_ids))))
    for p in posts:
        outputs = dict(p.outputs_json or {})
        outputs["canceled_by_user"] = True
        p.outputs_json = outputs
        p.status = PostStatus.failed
        p.updated_at = now
        db.add(p)
    if post_ids:
        db.execute(
            update(Job)
            .where(Job.profile_id == profile_id, Job.post_id.in_(post_ids), Job.status == JobStatus.queued)
            .values(status=JobStatus.failed, last_error="canceled_by_user", locked_at=None, locked_by=None, updated_at=now)
        )


def _delete_posts(db, *, profile_id: str, post_ids: list[str]):
    posts = list(db.scalars(select(Post).where(Post.profile_id == profile_id, Post.id.in_(post_ids))))
    content_ids = [p.collected_content_id for p in posts]
    if post_ids:
        db.query(JobLog).filter(JobLog.profile_id == profile_id, JobLog.post_id.in_(post_ids)).delete(synchronize_session=False)
        db.query(Job).filter(Job.profile_id == profile_id, Job.post_id.in_(post_ids)).delete(synchronize_session=False)
        db.query(Post).filter(Post.profile_id == profile_id, Post.id.in_(post_ids)).delete(synchronize_session=False)
    if content_ids:
        db.query(CollectedContent).filter(CollectedContent.profile_id == profile_id, CollectedContent.id.in_(content_ids)).delete(
            synchronize_session=False
        )


def _get_wordpress_creds_for_profile(db, *, profile_id: str, user_id: str) -> dict:
    integ = db.scalar(select(Integration).where(Integration.profile_id == profile_id, Integration.type == IntegrationType.WORDPRESS))
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


@router.post("/app/profiles/{profile_id}/posts/bulk", include_in_schema=False)
def profile_posts_bulk(
    profile_id: str,
    mode: str = Form(...),
    post_id: list[str] = Form([]),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ids = [str(x) for x in (post_id or []) if str(x).strip()]
    if not ids:
        return RedirectResponse(f"/app/profiles/{p.id}?tab=posts", status_code=status.HTTP_302_FOUND)
    if mode == "cancel":
        _cancel_posts(db, profile_id=p.id, post_ids=ids, user=user)
        db.commit()
        return RedirectResponse(f"/app/profiles/{p.id}?tab=posts", status_code=status.HTTP_302_FOUND)
    if mode == "delete_wp":
        try:
            creds = _get_wordpress_creds_for_profile(db, profile_id=p.id, user_id=user.id)
        except WordPressError as e:
            return RedirectResponse(f"/app/profiles/{p.id}?tab=posts&msg={quote_plus(str(e))}", status_code=status.HTTP_302_FOUND)
        posts = list(db.scalars(select(Post).where(Post.profile_id == p.id, Post.id.in_(ids))))
        ok_ids: list[str] = []
        failed = 0
        skipped = 0
        for post in posts:
            if post.status != PostStatus.completed or not post.wp_post_id:
                skipped += 1
                continue
            try:
                delete_post(
                    base_url=creds["base_url"],
                    username=creds["username"],
                    app_password=creds["app_password"],
                    post_id=int(post.wp_post_id),
                    force=True,
                )
                ok_ids.append(str(post.id))
            except WordPressError:
                failed += 1
        if ok_ids:
            _delete_posts(db, profile_id=p.id, post_ids=ok_ids)
        db.commit()
        msg = f"Apagados do site: {len(ok_ids)} • Falhas: {failed} • Ignorados: {skipped}"
        return RedirectResponse(f"/app/profiles/{p.id}?tab=posts&msg={quote_plus(msg)}", status_code=status.HTTP_302_FOUND)
    _delete_posts(db, profile_id=p.id, post_ids=ids)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}?tab=posts", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/posts/cancel-all", include_in_schema=False)
def profile_posts_cancel_all(profile_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ids = list(
        db.scalars(select(Post.id).where(Post.profile_id == p.id, Post.status.in_([PostStatus.pending, PostStatus.processing])))
    )
    _cancel_posts(db, profile_id=p.id, post_ids=[str(x) for x in ids], user=user)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}?tab=posts", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/posts/delete-completed", include_in_schema=False)
def profile_posts_delete_completed(profile_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ids = list(db.scalars(select(Post.id).where(Post.profile_id == p.id, Post.status == PostStatus.completed)))
    _delete_posts(db, profile_id=p.id, post_ids=[str(x) for x in ids])
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}?tab=posts", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/sources/create", include_in_schema=False)
def source_create(profile_id: str, type: str = Form(...), value: str = Form(...), user: User = Depends(get_current_user), db=Depends(get_db)):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if type == "URL":
        st = SourceType.URL
    elif type == "RSS":
        st = SourceType.RSS
    else:
        st = SourceType.KEYWORD
    s = Source(profile_id=p.id, type=st, value=value.strip(), active=True)
    db.add(s)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/sources/{source_id}/delete", include_in_schema=False)
def source_delete(profile_id: str, source_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    s = db.scalar(select(Source).where(Source.id == source_id, Source.profile_id == p.id))
    if s:
        db.delete(s)
        db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/run", include_in_schema=False)
def profile_run(profile_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    enqueue_job(db, user_id=p.user_id, profile_id=p.id, job_type=JOB_COLLECT, payload={})
    db.commit()
    return RedirectResponse("/app/posts", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/actions/create", include_in_schema=False)
def profile_action_create(
    profile_id: str,
    name: str = Form(...),
    destination: str = Form(...),
    prompt_text: str = Form(...),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    dest = ActionDestination[destination]
    a = AiAction(
        user_id=p.user_id,
        profile_id=p.id,
        name=name.strip(),
        destination=dest,
        prompt_text=prompt_text.strip(),
        active=True,
    )
    db.add(a)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/actions/{action_id}/delete", include_in_schema=False)
def profile_action_delete(
    profile_id: str,
    action_id: str,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    a = db.scalar(select(AiAction).where(AiAction.id == action_id, AiAction.profile_id == p.id))
    if a:
        db.delete(a)
        db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/integrations/wordpress", include_in_schema=False)
def profile_wp_integration_create(
    profile_id: str,
    name: str = Form(...),
    base_url: str = Form(...),
    username: str = Form(...),
    app_password: str = Form(...),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    clean_base = (base_url or "").strip()
    clean_base = clean_base.replace("/wp-admin/", "/").replace("/wp-admin", "")
    clean_base = clean_base.rstrip("/")
    creds = {"base_url": clean_base, "username": username.strip(), "app_password": app_password.strip()}
    encrypted = encrypt_json(creds)
    integ = Integration(
        user_id=p.user_id,
        profile_id=p.id,
        type=IntegrationType.WORDPRESS,
        name=name.strip(),
        credentials_encrypted=encrypted,
    )
    db.add(integ)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/integrations/gemini", include_in_schema=False)
def profile_gemini_integration_create(
    profile_id: str,
    api_key: str = Form(...),
    model: str = Form(""),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    key = (api_key or "").strip()
    if not key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="missing_api_key")
    creds = {"api_key": key, "model": (model or "").strip()}
    encrypted = encrypt_json(creds)
    existing = db.scalar(select(Integration).where(Integration.profile_id == p.id, Integration.type == IntegrationType.GEMINI))
    if existing:
        existing.credentials_encrypted = encrypted
        existing.name = "Gemini"
        db.add(existing)
    else:
        integ = Integration(
            user_id=p.user_id,
            profile_id=p.id,
            type=IntegrationType.GEMINI,
            name="Gemini",
            credentials_encrypted=encrypted,
        )
        db.add(integ)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/integrations/{integration_id}/delete", include_in_schema=False)
def profile_integration_delete(
    profile_id: str,
    integration_id: str,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    i = db.scalar(select(Integration).where(Integration.id == integration_id, Integration.profile_id == p.id))
    if i:
        if i.type == IntegrationType.FACEBOOK:
            cfg = dict(p.publish_config_json or {})
            cfg["facebook_enabled"] = False
            cfg["facebook_page_ids"] = []
            p.publish_config_json = cfg
            db.add(p)
        db.delete(i)
        db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/publish/facebook", include_in_schema=False)
def profile_publish_facebook(
    profile_id: str,
    facebook_link: str = Form("comments"),
    facebook_enabled: str = Form(""),
    facebook_page_ids: list[str] = Form([]),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    cfg = dict(p.publish_config_json or {})
    cfg["facebook_link"] = "body" if facebook_link == "body" else "comments"
    cfg["facebook_enabled"] = True if str(facebook_enabled or "").strip() == "1" else False
    cfg["facebook_page_ids"] = [str(x).strip() for x in (facebook_page_ids or []) if str(x).strip()]
    p.publish_config_json = cfg
    db.add(p)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/integrations/facebook/pages/add", include_in_schema=False)
def profile_facebook_pages_add(
    profile_id: str,
    page_id: str = Form(...),
    access_token: str = Form(...),
    name: str = Form(""),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    pid = str(page_id or "").strip()
    token = str(access_token or "").strip()
    nm = str(name or "").strip()
    if not pid or not token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="missing_page_id_or_token")
    existing = db.scalar(select(Integration).where(Integration.profile_id == p.id, Integration.type == IntegrationType.FACEBOOK))
    creds: dict = {}
    pages: list[dict] = []
    if existing:
        try:
            creds = decrypt_json(existing.credentials_encrypted)
        except Exception:
            creds = {}
        pages_val = creds.get("pages") if isinstance(creds, dict) else None
        if isinstance(pages_val, list):
            pages = [x for x in pages_val if isinstance(x, dict)]
    pages = [x for x in pages if str(x.get("page_id") or "").strip() != pid]
    pages.append({"page_id": pid, "access_token": token, "name": nm})
    new_creds = dict(creds) if isinstance(creds, dict) else {}
    new_creds["pages"] = pages
    encrypted = encrypt_json(new_creds)
    if existing:
        existing.credentials_encrypted = encrypted
        existing.name = "Facebook"
        db.add(existing)
    else:
        integ = Integration(
            user_id=p.user_id,
            profile_id=p.id,
            type=IntegrationType.FACEBOOK,
            name="Facebook",
            credentials_encrypted=encrypted,
        )
        db.add(integ)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}?tab=integracoes&msg={quote_plus('Página adicionada/atualizada.')}", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/integrations/facebook/pages/remove", include_in_schema=False)
def profile_facebook_pages_remove(
    profile_id: str,
    page_id: str = Form(...),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    pid = str(page_id or "").strip()
    if not pid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="missing_page_id")
    existing = db.scalar(select(Integration).where(Integration.profile_id == p.id, Integration.type == IntegrationType.FACEBOOK))
    if not existing:
        return RedirectResponse(f"/app/profiles/{p.id}?tab=integracoes&msg={quote_plus('Nenhuma integração do Facebook encontrada.')}", status_code=status.HTTP_302_FOUND)
    try:
        creds = decrypt_json(existing.credentials_encrypted)
    except Exception:
        creds = {}
    pages_val = creds.get("pages") if isinstance(creds, dict) else None
    pages = [x for x in pages_val if isinstance(x, dict)] if isinstance(pages_val, list) else []
    pages = [x for x in pages if str(x.get("page_id") or "").strip() != pid]
    new_creds = dict(creds) if isinstance(creds, dict) else {}
    new_creds["pages"] = pages
    existing.credentials_encrypted = encrypt_json(new_creds)
    existing.name = "Facebook"
    db.add(existing)
    cfg = dict(p.publish_config_json or {})
    sel = cfg.get("facebook_page_ids") or []
    if isinstance(sel, list):
        cfg["facebook_page_ids"] = [str(x).strip() for x in sel if str(x).strip() and str(x).strip() != pid]
    p.publish_config_json = cfg
    db.add(p)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}?tab=integracoes&msg={quote_plus('Página removida.')}", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/publish/wordpress", include_in_schema=False)
def profile_publish_wordpress(
    profile_id: str,
    default_category: str = Form("Receitas"),
    categories: str = Form(""),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    lines = [ln.strip() for ln in (categories or "").splitlines()]
    cats = [c for c in lines if c]
    cfg = dict(p.publish_config_json or {})
    cfg["default_category"] = (default_category or "Receitas").strip()
    cfg["categories"] = cats
    p.publish_config_json = cfg
    db.add(p)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}", status_code=status.HTTP_302_FOUND)


@router.get("/app/actions", include_in_schema=False)
def actions_page(user: User = Depends(get_current_user), db=Depends(get_db)):
    return RedirectResponse("/app/profiles", status_code=status.HTTP_302_FOUND)


@router.post("/app/actions/create", include_in_schema=False)
def actions_create(
    name: str = Form(...),
    destination: str = Form(...),
    prompt_text: str = Form(...),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return RedirectResponse("/app/actions", status_code=status.HTTP_302_FOUND)


@router.get("/app/integrations", include_in_schema=False)
def integrations_page(user: User = Depends(get_current_user), db=Depends(get_db)):
    return RedirectResponse("/app/profiles", status_code=status.HTTP_302_FOUND)


@router.post("/app/integrations/wordpress", include_in_schema=False)
def integrations_wordpress(
    name: str = Form(...),
    base_url: str = Form(...),
    username: str = Form(...),
    app_password: str = Form(...),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return RedirectResponse("/app/integrations", status_code=status.HTTP_302_FOUND)


@router.get("/app/posts", include_in_schema=False)
def posts_page(user: User = Depends(get_current_user), db=Depends(get_db)):
    bot = _get_or_create_single_bot(db, user=user)
    q = (
        select(Post, CollectedContent.title)
        .join(CollectedContent, CollectedContent.id == Post.collected_content_id)
        .where(Post.profile_id == bot.id, Post.status == PostStatus.completed, Post.wp_url.is_not(None))
        .order_by(Post.published_at.desc().nullslast(), Post.created_at.desc())
        .limit(15)
    )
    posts = list(db.execute(q).all())
    rows = "".join(
        f"<tr><td>{html.escape(str(title or ''))}</td><td class='muted'>{html.escape(_fmt_dt(p.published_at or p.created_at, user=user))}</td><td><a href='{html.escape(p.wp_url or '')}' target='_blank' rel='noopener noreferrer'>{html.escape(p.wp_url or '')}</a></td></tr>"
        for p, title in posts
    )
    body = f"""
    <div class="card">
      <h2>Posts</h2>
      <p class="muted">Mostrando as <b>15</b> últimas receitas publicadas.</p>
      <table><thead><tr><th>Título</th><th>Quando</th><th>Link</th></tr></thead><tbody>{rows}</tbody></table>
    </div>
    """
    return _layout("Posts", body, user=user)


@router.get("/app/logs", include_in_schema=False)
def logs_page(user: User = Depends(get_current_user), db=Depends(get_db)):
    if user.role == UserRole.ADMIN:
        logs = list(db.scalars(select(JobLog).order_by(JobLog.created_at.desc()).limit(500)))
    else:
        logs = list(db.scalars(select(JobLog).where(JobLog.user_id == user.id).order_by(JobLog.created_at.desc()).limit(500)))
    def _msg(l: JobLog) -> str:
        meta = l.meta_json or {}
        if l.stage == JOB_COLLECT and l.message == "collect_completed":
            c = int(meta.get("created") or 0)
            d = int(meta.get("skipped_duplicate") or meta.get("skipped") or 0)
            n = int(meta.get("skipped_non_recipe") or 0)
            e = int(meta.get("skipped_error") or 0)
            return f"collect_completed (novos={c}, repetidos={d}, ignorados={n + e})"
        return str(meta.get("error") or l.message)
    rows = "".join(
        f"<tr><td>{html.escape(l.stage)}</td><td><span class='pill'>{html.escape(l.status)}</span></td><td>{html.escape(_msg(l))}</td><td class='muted'>{html.escape(l.user_id)}</td><td class='muted'>{html.escape(_fmt_dt(l.created_at, user=user))}</td></tr>"
        for l in logs
    )
    body = f"""
    <div class="card">
      <div class="toolbar">
        <h2 style="margin:0">Logs</h2>
        <button class="btn secondary small" type="button" onclick="clearBox('#logs-table tbody')">Limpar dados</button>
      </div>
      <div class="scrollbox">
        <table id="logs-table"><thead><tr><th>Etapa</th><th>Status</th><th>Mensagem</th><th>Owner</th><th>Quando</th></tr></thead><tbody>{rows}</tbody></table>
      </div>
    </div>
    """
    return _layout("Logs", body, user=user)


@router.get("/app/set-password", include_in_schema=False)
def set_password_page(token: str = ""):
    tk = html.escape(token or "")
    body = f"""
    <div class="card">
      <h2>Criar senha</h2>
      <form method="post" action="/app/set-password">
        <input type="hidden" name="token" value="{tk}" />
        <div class="row">
          <div class="col">
            <label>Senha</label>
            <input name="password" type="password" required />
          </div>
          <div class="col">
            <label>Confirmar senha</label>
            <input name="password_confirm" type="password" required />
          </div>
        </div>
        <p class="muted" style="margin-top:8px">Mínimo 6 caracteres.</p>
        <div style="margin-top:12px"><button type="submit">Salvar</button></div>
      </form>
    </div>
    """
    return HTMLResponse(f"<!doctype html><meta charset='utf-8' />{body}")


@router.post("/app/set-password", include_in_schema=False)
def set_password_action(
    token: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db=Depends(get_db),
):
    from app.api.auth import set_password as set_password_api
    from app.api.schemas import SetPasswordRequest

    try:
        set_password_api(SetPasswordRequest(token=token, password=password, password_confirm=password_confirm), db=db)
    except HTTPException:
        return RedirectResponse("/app/login?msg=N%C3%A3o+foi+poss%C3%ADvel+definir+a+senha", status_code=status.HTTP_302_FOUND)
    return RedirectResponse("/app/login?msg=Senha+criada.+Fa%C3%A7a+login", status_code=status.HTTP_302_FOUND)


@router.post("/app/presets/recipes/apply", include_in_schema=False)
def apply_recipe_preset(user: User = Depends(get_current_user), db=Depends(get_db)):
    existing = db.scalar(select(AiAction.id).where(AiAction.user_id == user.id))
    if existing is None:
        a1 = AiAction(
            user_id=user.id,
            name="Receita - Post WordPress",
            destination=ActionDestination.WORDPRESS,
            active=True,
            prompt_text=(
                "Você é um redator culinário. Transforme o texto abaixo em uma receita completa em português (PT-BR) com:"
                "\n- Título chamativo\n- Introdução curta\n- Tempo de preparo, rendimento\n- Lista de ingredientes\n- Modo de preparo (passo a passo)\n- Dicas e variações\n- Sugestão de tags\n"
                "\nResponda somente com o conteúdo final (sem explicações)."
            ),
        )
        a2 = AiAction(
            user_id=user.id,
            name="Receita - Post Facebook",
            destination=ActionDestination.FACEBOOK,
            active=True,
            prompt_text=(
                "Crie um texto curto e envolvente para Facebook sobre a receita abaixo. Use emojis moderadamente, inclua CTA e 3 a 6 hashtags."
                "\nResponda somente com o texto final."
            ),
        )
        a3 = AiAction(
            user_id=user.id,
            name="Receita - Legenda Instagram",
            destination=ActionDestination.INSTAGRAM,
            active=True,
            prompt_text=(
                "Crie uma legenda de Instagram para a receita abaixo com tom leve e apetitoso, CTA e 8 a 15 hashtags relevantes."
                "\nResponda somente com a legenda final."
            ),
        )
        db.add_all([a1, a2, a3])
    profile = db.scalar(select(AutomationProfile).where(AutomationProfile.user_id == user.id).order_by(AutomationProfile.created_at.asc()))
    if profile is None:
        profile = AutomationProfile(
            user_id=user.id,
            name="Receitas",
            active=True,
            schedule_config_json={"posts_per_day": 3, "window": {"start": "08:00", "end": "20:00"}},
            anti_block_config_json={"time_jitter_minutes": 12},
        )
        db.add(profile)
    db.commit()
    return RedirectResponse("/app/profiles", status_code=status.HTTP_302_FOUND)


@router.get("/app/admin", include_in_schema=False)
def admin_page(_admin: User = Depends(require_admin), db=Depends(get_db)):
    return RedirectResponse("/app/admin/users", status_code=status.HTTP_302_FOUND)


def _generate_access_id() -> str:
    n1 = secrets.randbelow(10000)
    n2 = secrets.randbelow(10000)
    return f"ph-{n1:04d}-{n2:04d}"


def _normalize_access_id(v: str) -> str:
    x = (v or "").strip().lower()
    if not x:
        return x
    for ch in x:
        ok = ch.isalnum() or ch in ("-", "_")
        if not ok:
            raise ValueError("invalid_access_id")
    if len(x) < 3 or len(x) > 32:
        raise ValueError("invalid_access_id")
    return x


@router.get("/app/admin/users", include_in_schema=False)
def admin_users_page(request: Request, _admin: User = Depends(require_admin), db=Depends(get_db)):
    msg = html.escape(request.query_params.get("msg", ""))
    users = list(db.scalars(select(User).order_by(User.created_at.desc()).limit(200)))
    rows = "".join(
        f"<tr><td>{html.escape(u.access_id or '')}</td><td>{html.escape(u.email)}</td><td><span class='pill'>{html.escape(u.role.value)}</span></td>"
        f"<td class='muted'>{html.escape(str(u.created_at))}</td>"
        f"<td><form method='post' action='/app/admin/users/{u.id}/delete' style='margin:0'>"
        f"<button class='btn secondary' type='submit' {'disabled' if u.id == _admin.id else ''}>Excluir</button></form></td></tr>"
        for u in users
    )
    body = f"""
    <div class="card">
      <h2>Admin · Usuários</h2>
      <p class="muted">{msg}</p>
      <div class="card">
        <form method="post" action="/app/admin/users/create">
          <div class="row">
            <div class="col">
              <label>Usuário/ID (opcional)</label>
              <input name="access_id" placeholder="usuario ou vazio pra gerar" />
            </div>
            <div class="col">
              <label>Role</label>
              <select name="role">
                <option value="USER" selected>USER</option>
                <option value="ADMIN">ADMIN</option>
              </select>
            </div>
          </div>
          <div class="row" style="margin-top: 8px">
            <div class="col">
              <label>Senha (mínimo 6, vazio = 000000)</label>
              <input name="password" type="password" placeholder="000000" />
            </div>
            <div class="col">
              <label>Email interno (opcional)</label>
              <input name="email" placeholder="opcional" />
            </div>
          </div>
          <div style="margin-top:12px">
            <button class="btn" type="submit">Criar usuário</button>
          </div>
        </form>
      </div>
      <table>
        <thead><tr><th>Usuário/ID</th><th>Email</th><th>Role</th><th>Criado</th><th></th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    """
    return _layout("Administração", body, user=_admin)


@router.post("/app/admin/users/create", include_in_schema=False)
def admin_users_create(
    access_id: str = Form(""),
    role: str = Form("USER"),
    password: str = Form(""),
    email: str = Form(""),
    _admin: User = Depends(require_admin),
    db=Depends(get_db),
):
    try:
        login_id = _normalize_access_id(access_id)
    except ValueError:
        return RedirectResponse("/app/admin/users?msg=ID+inv%C3%A1lido", status_code=status.HTTP_302_FOUND)
    if not login_id:
        for _ in range(10):
            candidate = _generate_access_id()
            if db.scalar(select(User.id).where(User.access_id == candidate)) is None:
                login_id = candidate
                break
    if not login_id:
        return RedirectResponse("/app/admin/users?msg=N%C3%A3o+foi+poss%C3%ADvel+gerar+ID", status_code=status.HTTP_302_FOUND)
    if db.scalar(select(User.id).where(User.access_id == login_id)) is not None:
        return RedirectResponse("/app/admin/users?msg=ID+j%C3%A1+existe", status_code=status.HTTP_302_FOUND)

    pw = password or "000000"
    if len(pw) < 6:
        return RedirectResponse("/app/admin/users?msg=Senha+m%C3%ADnima+%C3%A9+6", status_code=status.HTTP_302_FOUND)

    e = (email or "").strip().lower()
    if not e:
        e = f"{login_id}@posthub.local"
    if db.scalar(select(User.id).where(User.email == e)) is not None:
        return RedirectResponse("/app/admin/users?msg=Email+j%C3%A1+existe", status_code=status.HTTP_302_FOUND)

    r = UserRole.ADMIN if role == "ADMIN" else UserRole.USER
    u = User(email=e, access_id=login_id, password_hash=hash_password(pw), must_set_password=False, role=r)
    db.add(u)
    db.commit()
    return RedirectResponse("/app/admin/users?msg=Usu%C3%A1rio+criado", status_code=status.HTTP_302_FOUND)


@router.post("/app/admin/users/{user_id}/delete", include_in_schema=False)
def admin_users_delete(user_id: str, _admin: User = Depends(require_admin), db=Depends(get_db)):
    if user_id == _admin.id:
        return RedirectResponse("/app/admin/users?msg=N%C3%A3o+pode+excluir+voc%C3%AA+mesmo", status_code=status.HTTP_302_FOUND)
    u = db.scalar(select(User).where(User.id == user_id))
    if u:
        db.delete(u)
        db.commit()
    return RedirectResponse("/app/admin/users?msg=Usu%C3%A1rio+exclu%C3%ADdo", status_code=status.HTTP_302_FOUND)
