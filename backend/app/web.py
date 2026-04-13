from __future__ import annotations

import html


def _ph(name: str) -> str:
    """Placeholder de desenvolvimento — mostra label amarelo com ícones copiar/fechar."""
    return (
        f"<span class='dev-ph-wrap' style='display:inline-flex;align-items:center;gap:4px;font-size:9px;"
        f"font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:#f59e0b;"
        f"background:rgba(245,158,11,.12);border:1px dashed rgba(245,158,11,.4);"
        f"border-radius:4px;padding:2px 6px;margin:2px 2px;vertical-align:middle'>"
        f"<span class='dev-ph'>📌 {name}</span>"
        f"<button type='button' title='Copiar' onclick=\"navigator.clipboard.writeText('{name}').then(function(){{this.textContent='✓'}}.bind(this))\" "
        f"style='background:none;border:none;cursor:pointer;padding:0 2px;font-size:10px;color:#f59e0b;line-height:1' class='dev-ph'>⧉</button>"
        f"<button type='button' title='Ocultar' onclick=\"this.closest('.dev-ph-wrap').style.display='none'\" "
        f"style='background:none;border:none;cursor:pointer;padding:0 2px;font-size:10px;color:#f59e0b;line-height:1' class='dev-ph'>✕</button>"
        f"</span>"
    )
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select, update

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
    :root, [data-theme="roxo"] {
      --bg:#07060b; --bg2:#0b0a10;
      --surface:rgba(18,16,28,.78); --surface2:rgba(10,9,15,.75);
      --border:rgba(255,255,255,.10); --border2:rgba(255,255,255,.14);
      --text:#f9fafb; --muted:rgba(249,250,251,.65);
      --primary:#8b5cf6; --primary2:#7c3aed; --pink:#ec4899;
      --shadow:0 16px 50px rgba(0,0,0,.50); --radius:18px;
      --grad1:rgba(139,92,246,.30); --grad2:rgba(236,72,153,.22); --grad3:rgba(124,58,237,.18);
      --input-bg:rgba(10,9,15,.72); --sidebar-bg:linear-gradient(180deg,rgba(10,9,15,.9),rgba(7,6,11,.7));
    }
    [data-theme="oceano"] {
      --bg:#060b0f; --bg2:#080d14;
      --surface:rgba(8,20,36,.82); --surface2:rgba(5,13,24,.80);
      --border:rgba(14,165,233,.15); --border2:rgba(14,165,233,.28);
      --text:#f0f9ff; --muted:rgba(224,242,254,.60);
      --primary:#0ea5e9; --primary2:#0284c7; --pink:#38bdf8;
      --shadow:0 16px 50px rgba(0,0,0,.55); --radius:18px;
      --grad1:rgba(14,165,233,.28); --grad2:rgba(56,189,248,.18); --grad3:rgba(2,132,199,.16);
      --input-bg:rgba(4,12,24,.75); --sidebar-bg:linear-gradient(180deg,rgba(4,12,24,.92),rgba(6,11,15,.75));
    }
    [data-theme="floresta"] {
      --bg:#050c07; --bg2:#06100a;
      --surface:rgba(8,20,12,.82); --surface2:rgba(5,13,8,.80);
      --border:rgba(16,185,129,.15); --border2:rgba(16,185,129,.28);
      --text:#f0fdf4; --muted:rgba(220,252,231,.60);
      --primary:#10b981; --primary2:#059669; --pink:#34d399;
      --shadow:0 16px 50px rgba(0,0,0,.55); --radius:18px;
      --grad1:rgba(16,185,129,.28); --grad2:rgba(52,211,153,.18); --grad3:rgba(5,150,105,.16);
      --input-bg:rgba(4,12,7,.75); --sidebar-bg:linear-gradient(180deg,rgba(4,12,7,.92),rgba(5,10,6,.75));
    }
    [data-theme="aurora"] {
      --bg:#060a0c; --bg2:#07090e;
      --surface:rgba(8,18,24,.82); --surface2:rgba(5,12,16,.80);
      --border:rgba(20,184,166,.15); --border2:rgba(20,184,166,.28);
      --text:#f0fdfa; --muted:rgba(204,251,241,.60);
      --primary:#14b8a6; --primary2:#0d9488; --pink:#f59e0b;
      --shadow:0 16px 50px rgba(0,0,0,.55); --radius:18px;
      --grad1:rgba(20,184,166,.28); --grad2:rgba(245,158,11,.18); --grad3:rgba(13,148,136,.16);
      --input-bg:rgba(4,10,12,.75); --sidebar-bg:linear-gradient(180deg,rgba(4,10,14,.92),rgba(5,9,11,.75));
    }
    [data-theme="claro"] {
      --bg:#f8fafc; --bg2:#f1f5f9;
      --surface:rgba(255,255,255,.95); --surface2:rgba(248,250,252,.92);
      --border:rgba(0,0,0,.09); --border2:rgba(0,0,0,.16);
      --text:#0f172a; --muted:rgba(15,23,42,.52);
      --primary:#7c3aed; --primary2:#6d28d9; --pink:#db2777;
      --shadow:0 4px 24px rgba(0,0,0,.10); --radius:18px;
      --grad1:rgba(139,92,246,.08); --grad2:rgba(236,72,153,.06); --grad3:rgba(124,58,237,.05);
      --input-bg:rgba(248,250,252,.95); --sidebar-bg:linear-gradient(180deg,rgba(241,245,249,.98),rgba(248,250,252,.96));
    }
    [data-theme="rosa"] {
      --bg:#fff0f5; --bg2:#ffe4ed;
      --surface:rgba(255,255,255,.96); --surface2:rgba(255,240,245,.92);
      --border:rgba(225,29,72,.11); --border2:rgba(225,29,72,.20);
      --text:#1c0510; --muted:rgba(28,5,16,.52);
      --primary:#e11d48; --primary2:#be123c; --pink:#f43f5e;
      --shadow:0 4px 24px rgba(225,29,72,.12); --radius:18px;
      --grad1:rgba(244,63,94,.12); --grad2:rgba(251,113,133,.08); --grad3:rgba(225,29,72,.07);
      --input-bg:rgba(255,255,255,.97); --sidebar-bg:linear-gradient(180deg,rgba(255,228,232,.98),rgba(255,240,245,.96));
    }
    [data-theme="ceu"] {
      --bg:#f0f7ff; --bg2:#e0efff;
      --surface:rgba(255,255,255,.96); --surface2:rgba(240,247,255,.92);
      --border:rgba(59,130,246,.11); --border2:rgba(59,130,246,.20);
      --text:#0c1a2e; --muted:rgba(12,26,46,.52);
      --primary:#2563eb; --primary2:#1d4ed8; --pink:#60a5fa;
      --shadow:0 4px 24px rgba(37,99,235,.10); --radius:18px;
      --grad1:rgba(96,165,250,.14); --grad2:rgba(59,130,246,.10); --grad3:rgba(37,99,235,.08);
      --input-bg:rgba(255,255,255,.97); --sidebar-bg:linear-gradient(180deg,rgba(224,239,255,.98),rgba(240,247,255,.96));
    }
    [data-theme="claro"] input,[data-theme="claro"] select,[data-theme="claro"] textarea,
    [data-theme="rosa"] input,[data-theme="rosa"] select,[data-theme="rosa"] textarea,
    [data-theme="ceu"] input,[data-theme="ceu"] select,[data-theme="ceu"] textarea {
      color: var(--text) !important; border-color: var(--border2) !important;
    }
    [data-theme="claro"] .brand,[data-theme="rosa"] .brand,[data-theme="ceu"] .brand { background: var(--surface2); }
    [data-theme="claro"] .sidebar-footer,[data-theme="rosa"] .sidebar-footer,[data-theme="ceu"] .sidebar-footer { background: var(--surface2); }

    /* Topbar theme dropdown */
    .theme-bar { display:flex; align-items:center; gap:8px; position:relative; }
    .theme-bar label { font-size:12px; color:var(--muted); white-space:nowrap; }
    .theme-select {
      appearance:none; -webkit-appearance:none;
      padding:6px 28px 6px 10px; border-radius:10px;
      border:1px solid var(--border2); background:var(--surface);
      color:var(--text); font-size:13px; font-family:inherit; cursor:pointer;
      background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23888'/%3E%3C/svg%3E");
      background-repeat:no-repeat; background-position:right 9px center;
    }
    .theme-select:focus { outline:none; border-color:var(--primary); box-shadow:0 0 0 3px rgba(139,92,246,.15); }

    /* Nav submenus */
    .nav-group { display:flex; flex-direction:column; }
    .nav-parent {
      display:flex; align-items:center; gap:10px; padding:10px 12px;
      border-radius:14px; border:1px solid transparent;
      color:var(--text); opacity:.85;
      background:transparent; cursor:pointer;
      font-size:inherit; font-family:inherit; width:100%; text-align:left;
    }
    .nav-parent:hover { opacity:1; border-color:var(--border2); background:var(--border); }
    .nav-group .nav-sub { display:none; flex-direction:column; gap:2px; padding:4px 0 4px 20px; }
    .nav-group.open .nav-sub { display:flex; }
    .nav-sub a {
      display:flex; align-items:center; gap:8px;
      padding:7px 12px; border-radius:12px; border:1px solid transparent;
      color:var(--muted); font-size:13px;
    }
    .nav-sub a:hover { color:var(--text); background:var(--border); border-color:var(--border2); }
    .nav-sub a.active { color:var(--text); background:rgba(139,92,246,.18); border-color:rgba(139,92,246,.30); font-weight:600; }
    [data-theme="oceano"] .nav-sub a.active { background:rgba(14,165,233,.18); border-color:rgba(14,165,233,.30); }
    [data-theme="floresta"] .nav-sub a.active { background:rgba(16,185,129,.18); border-color:rgba(16,185,129,.30); }
    [data-theme="aurora"] .nav-sub a.active { background:rgba(20,184,166,.18); border-color:rgba(20,184,166,.30); }
    [data-theme="rosa"] .nav-sub a.active { background:rgba(225,29,72,.14); border-color:rgba(225,29,72,.28); }
    [data-theme="ceu"] .nav-sub a.active { background:rgba(37,99,235,.14); border-color:rgba(37,99,235,.28); }
    .nav-step-num { display:inline-flex; align-items:center; justify-content:center;
      width:16px; height:16px; border-radius:999px; background:var(--border2);
      font-size:9px; font-weight:700; flex-shrink:0; opacity:.7; }
    .nav-sub a.active .nav-step-num { background:var(--primary); opacity:1; color:#fff; }
    .nav-sub-arrow { margin-left:auto; font-size:9px; transition:transform .25s; }
    .nav-group.open .nav-sub-arrow { transform:rotate(90deg); }

    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      color: var(--text);
      background:
        radial-gradient(1200px 700px at 15% 15%, var(--grad1), transparent 55%),
        radial-gradient(900px 600px at 85% 25%, var(--grad2), transparent 60%),
        radial-gradient(900px 700px at 60% 80%, var(--grad3), transparent 60%),
        linear-gradient(180deg, var(--bg), var(--bg2));
    }
    a { color: inherit; text-decoration: none; }
    .app { display: grid; grid-template-columns: 280px 1fr; min-height: 100vh; transition: grid-template-columns .25s; }
    .app.sidebar-collapsed { grid-template-columns: 0 1fr; }
    .sidebar {
      position: sticky;
      top: 0;
      height: 100vh;
      width: 280px;
      padding: 20px 16px;
      border-right: 1px solid var(--border);
      background: var(--sidebar-bg);
      backdrop-filter: blur(12px);
      overflow: hidden;
      transition: width .25s, padding .25s, border .25s;
    }
    .app.sidebar-collapsed .sidebar { width: 0; padding: 0; border: none; }
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
    .nav { display: grid; gap: 6px; margin-top: 12px; }
    .nav a {
      display: flex; align-items: center; gap: 10px;
      padding: 10px 12px; border-radius: 14px;
      border: 1px solid transparent;
      color: var(--text); opacity: .85;
      background: transparent;
    }
    .nav a:hover { opacity:1; border-color: var(--border2); background: var(--border); }
    .nav .dot {
      width: 9px; height: 9px; border-radius: 999px; flex-shrink:0;
      background: var(--primary); opacity:.7;
      box-shadow: 0 0 0 3px rgba(0,0,0,.08);
    }
    .nav a:hover .dot { background: var(--pink); opacity:1; }
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
    .main { padding: 24px 32px 40px; min-width: 0; }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 14px;
    }
    .title { font-size: 18px; margin: 0; letter-spacing: 0.2px; }
    .muted { color: var(--muted); }
    .content { max-width: 1600px; width: 100%; }
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
      background: var(--input-bg);
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
      gap: 8px;
      padding: 10px 18px;
      border-radius: 12px;
      border: 1px solid rgba(255, 255, 255, 0.15);
      background: linear-gradient(135deg, var(--primary), var(--pink));
      color: white;
      cursor: pointer;
      font-weight: 600;
      font-size: 13px;
      letter-spacing: 0.2px;
      box-shadow: 0 4px 14px rgba(139, 92, 246, 0.28), 0 1px 3px rgba(0,0,0,.12);
      transition: transform .15s ease, box-shadow .15s ease, filter .15s ease, background .15s ease;
      white-space: nowrap;
    }
    .btn:hover {
      transform: translateY(-1px);
      box-shadow: 0 8px 22px rgba(139, 92, 246, 0.38), 0 2px 6px rgba(0,0,0,.16);
      filter: brightness(1.08);
    }
    .btn:active { transform: translateY(0); filter: brightness(.96); }
    .btn.dirty {
      border-color: rgba(245, 158, 11, 0.55);
      box-shadow:
        0 0 0 4px rgba(245, 158, 11, 0.16),
        0 6px 18px rgba(245, 158, 11, 0.22);
      filter: brightness(1.06);
    }
    .btn.secondary {
      background: rgba(18, 16, 28, 0.45);
      color: rgba(249, 250, 251, 0.90);
      box-shadow: 0 2px 8px rgba(0,0,0,.12);
      border: 1px solid var(--border2);
    }
    .btn.secondary:hover {
      background: rgba(18, 16, 28, 0.65);
      border-color: var(--primary);
      box-shadow: 0 4px 14px rgba(0,0,0,.18);
    }
    .sidebar-toggle-btn {
      background: none; border: 1px solid var(--border); border-radius: 10px;
      cursor: pointer; padding: 7px 10px; color: var(--muted); font-size: 17px;
      display: flex; align-items: center; justify-content: center; line-height: 1;
      transition: background .15s, color .15s, border-color .15s;
      flex-shrink: 0;
    }
    .sidebar-toggle-btn:hover { background: var(--surface2); color: var(--text); border-color: var(--border2); }
    /* ── icon action buttons ── */
    .act-btn {
      background: none; border: 1px solid transparent; cursor: pointer;
      padding: 7px; border-radius: 9px; color: var(--muted);
      display: inline-flex; align-items: center; justify-content: center;
      transition: background .15s, color .15s, border-color .15s;
      text-decoration: none; flex-shrink: 0;
    }
    .act-btn:hover { background: var(--surface2); color: var(--text); border-color: var(--border); }
    .act-btn.danger:hover { color: #ef4444; border-color: rgba(239,68,68,.3); background: rgba(239,68,68,.07); }
    .act-btn.primary-hover:hover { color: var(--primary); border-color: rgba(139,92,246,.3); background: rgba(139,92,246,.07); }
    /* online pill badge (active bot) */
    .bot-online-pill {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 5px 12px; border-radius: 20px; font-size: 12px; font-weight: 700;
      color: #10b981; background: rgba(16,185,129,.12); border: 1px solid rgba(16,185,129,.3);
      cursor: pointer; transition: background .15s, border-color .15s;
      white-space: nowrap;
    }
    .bot-online-pill:hover { background: rgba(239,68,68,.1); color: #ef4444; border-color: rgba(239,68,68,.3); }
    .bot-online-pill:hover .pill-dot { background: #ef4444; animation: none; }
    .pill-dot { width: 7px; height: 7px; border-radius: 50%; background: #10b981; flex-shrink: 0;
      box-shadow: 0 0 0 0 rgba(16,185,129,.6); animation: pulse-green 1.8s infinite; }
    /* ligar bot */
    .bot-ligar-btn {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 5px 12px; border-radius: 20px; font-size: 12px; font-weight: 700;
      color: var(--muted); background: var(--surface2); border: 1px solid var(--border);
      cursor: pointer; transition: all .15s; white-space: nowrap;
    }
    .bot-ligar-btn:hover { color: #10b981; background: rgba(16,185,129,.1); border-color: rgba(16,185,129,.3); }
    .bot-ligar-btn:disabled { opacity: .4; cursor: not-allowed; }
    /* ── toggle-section (collapsible cards) ── */
    details.toggle-section > summary { list-style:none; display:flex; align-items:center; justify-content:space-between; cursor:pointer; padding:14px 0 14px; border-bottom:1px solid var(--border); user-select:none; }
    details.toggle-section > summary::-webkit-details-marker { display:none; }
    details.toggle-section > summary .ts-title { font-size:16px; font-weight:700; display:flex; align-items:center; gap:8px; }
    details.toggle-section > summary .ts-arrow { font-size:11px; color:var(--muted); transition:transform .2s; flex-shrink:0; }
    details.toggle-section[open] > summary .ts-arrow { transform:rotate(90deg); }
    details.toggle-section > summary .ts-badge { font-size:11px; padding:2px 8px; border-radius:20px; background:var(--surface); border:1px solid var(--border); color:var(--muted); }
    details.toggle-section > .ts-body { padding-top:14px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid var(--border); padding: 12px 10px; text-align: left; vertical-align: top; color: var(--text); }
    th { color: var(--muted); font-size: 12px; font-weight: 600; letter-spacing: 0.5px; text-transform: uppercase; }
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
    .active-project-banner {
      display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px;
      padding:18px 22px; border-radius:var(--radius); margin-bottom:14px;
      border:1px solid var(--primary); background:linear-gradient(135deg,rgba(139,92,246,.15),rgba(236,72,153,.08));
      box-shadow:0 0 0 4px rgba(139,92,246,.07);
    }
    .active-project-label { font-size:11px; font-weight:600; letter-spacing:.8px; text-transform:uppercase; color:var(--primary); margin-bottom:4px; }
    .active-project-name { font-size:22px; font-weight:800; color:var(--text); }
    .active-project-stat { font-size:13px; color:var(--muted); padding:5px 10px; background:var(--surface); border:1px solid var(--border); border-radius:8px; }
    .badge-active { display:inline-flex; align-items:center; gap:5px; color:#10b981; font-size:12px; font-weight:700; }
    .badge-active .dot-pulse {
      width:8px; height:8px; border-radius:999px; background:#10b981; flex-shrink:0;
      box-shadow:0 0 0 0 rgba(16,185,129,.6);
      animation:pulse-green 1.8s infinite;
    }
    @keyframes pulse-green {
      0%   { box-shadow:0 0 0 0 rgba(16,185,129,.55); }
      70%  { box-shadow:0 0 0 7px rgba(16,185,129,0); }
      100% { box-shadow:0 0 0 0 rgba(16,185,129,0); }
    }
    .badge-inactive { display:inline-flex; align-items:center; gap:5px; color:var(--muted); font-size:12px; }
    .badge-inactive .dot-off { width:8px; height:8px; border-radius:999px; background:var(--muted); opacity:.4; flex-shrink:0; }
    tr.proj-row-active td { background:rgba(16,185,129,.06); border-bottom-color:rgba(16,185,129,.12); }
    tr.proj-row-active td:first-child { border-left:3px solid #10b981; padding-left:10px; }
    /* ── diagnostic modal ── */
    .diag-overlay {
      display:none; position:fixed; inset:0; z-index:10000;
      background:rgba(0,0,0,.55); backdrop-filter:blur(4px);
      align-items:center; justify-content:center; padding:20px;
    }
    .diag-overlay.open { display:flex; }
    .diag-modal {
      background:var(--bg2); border:1px solid var(--border);
      border-radius:20px; box-shadow:0 24px 64px rgba(0,0,0,.38);
      width:min(520px,100%); max-height:90vh; overflow-y:auto;
      display:flex; flex-direction:column;
    }
    .diag-header {
      display:flex; align-items:center; justify-content:space-between;
      padding:18px 22px 14px; border-bottom:1px solid var(--border);
    }
    .diag-header h3 { margin:0; font-size:16px; }
    .diag-close {
      background:none; border:none; cursor:pointer; color:var(--muted);
      font-size:20px; padding:4px 8px; border-radius:8px; line-height:1;
      transition:background .15s, color .15s;
    }
    .diag-close:hover { background:var(--surface2); color:var(--text); }
    .diag-body { padding:18px 22px; display:flex; flex-direction:column; gap:12px; }
    .diag-item {
      display:flex; align-items:flex-start; gap:12px;
      padding:14px 16px; border-radius:14px;
      border:1px solid var(--border); background:var(--surface2);
    }
    .diag-item.ok { border-color:rgba(16,185,129,.35); background:rgba(16,185,129,.07); }
    .diag-item.err { border-color:rgba(239,68,68,.35); background:rgba(239,68,68,.07); }
    .diag-item.warn { border-color:rgba(245,158,11,.35); background:rgba(245,158,11,.07); }
    .diag-item.loading { opacity:.6; }
    .diag-dot {
      width:32px; height:32px; border-radius:50%; flex-shrink:0;
      display:flex; align-items:center; justify-content:center; font-size:15px;
    }
    .diag-item.ok .diag-dot { background:rgba(16,185,129,.18); color:#10b981; }
    .diag-item.err .diag-dot { background:rgba(239,68,68,.15); color:#ef4444; }
    .diag-item.warn .diag-dot { background:rgba(245,158,11,.15); color:#f59e0b; }
    .diag-item.loading .diag-dot { background:var(--surface); color:var(--muted); }
    .diag-label { font-size:13px; font-weight:700; margin-bottom:3px; }
    .diag-desc { font-size:12px; color:var(--muted); line-height:1.5; }
    .diag-desc a { color:var(--primary); font-weight:600; }
    .diag-footer {
      padding:14px 22px 18px; border-top:1px solid var(--border);
      display:flex; gap:10px; justify-content:flex-end;
    }
    .robot-actions { display:flex; flex-direction:column; gap:8px; }
    .robot-action-card {
      display:flex; align-items:center; gap:14px;
      padding:14px 16px; border-radius:14px;
      border:1px solid var(--border); background:var(--surface2);
    }
    .robot-action-card.robot-action-primary { border-color:var(--primary); background:rgba(139,92,246,.08); }
    .robot-action-icon {
      width:38px; height:38px; border-radius:12px; flex-shrink:0;
      display:grid; place-items:center; font-size:16px;
      background:rgba(139,92,246,.15); color:var(--primary);
    }
    .robot-action-primary .robot-action-icon { background:linear-gradient(135deg,var(--primary),var(--pink)); color:#fff; }
    .robot-action-title { font-weight:600; font-size:14px; margin-bottom:2px; }
    .robot-action-desc { font-size:12px; }
    [data-theme="claro"] tr.proj-row-active td,[data-theme="rosa"] tr.proj-row-active td,[data-theme="ceu"] tr.proj-row-active td { background:rgba(16,185,129,.05); }
    /* ── notification bell ── */
    .notif-wrap { position:relative; }
    .notif-bell-btn {
      position:relative; background:none; border:none; cursor:pointer;
      width:36px; height:36px; border-radius:10px; display:flex; align-items:center; justify-content:center;
      color:var(--muted); transition:background .15s,color .15s;
    }
    .notif-bell-btn:hover { background:var(--surface2); color:var(--text); }
    .notif-badge {
      position:absolute; top:4px; right:4px;
      min-width:16px; height:16px; border-radius:999px;
      background:#ef4444; color:#fff; font-size:9px; font-weight:700;
      display:none; align-items:center; justify-content:center; padding:0 4px;
      border:2px solid var(--bg);
    }
    .notif-badge.visible { display:flex; }
    .notif-dropdown {
      position:absolute; right:0; top:calc(100% + 6px);
      width:min(340px,calc(100vw - 24px)); max-height:480px;
      background:var(--bg2); border:1px solid var(--border);
      border-radius:14px; box-shadow:0 8px 32px rgba(0,0,0,.22);
      display:none; flex-direction:column; z-index:9999; overflow:hidden;
    }
    .notif-dropdown.open { display:flex; }
    .notif-dd-header {
      display:flex; align-items:center; justify-content:space-between;
      padding:12px 16px; border-bottom:1px solid var(--border);
      font-size:13px; font-weight:600;
    }
    .notif-dd-header a { font-size:12px; font-weight:400; color:var(--primary); text-decoration:none; }
    .notif-dd-header a:hover { text-decoration:underline; }
    .notif-list { overflow-y:auto; flex:1; }
    .notif-item {
      display:flex; align-items:flex-start; gap:10px;
      padding:11px 16px; border-bottom:1px solid var(--border);
      cursor:default;
    }
    .notif-item:last-child { border-bottom:none; }
    .notif-icon {
      width:28px; height:28px; border-radius:50%; flex-shrink:0;
      display:flex; align-items:center; justify-content:center; font-size:12px;
    }
    .notif-icon.ok { background:rgba(16,185,129,.15); color:#10b981; }
    .notif-icon.err { background:rgba(239,68,68,.12); color:#ef4444; }
    .notif-item-text { flex:1; min-width:0; }
    .notif-item-title { font-size:12px; font-weight:600; color:var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .notif-item-sub { font-size:11px; color:var(--muted); margin-top:2px; }
    .notif-dd-empty { padding:28px 16px; text-align:center; color:var(--muted); font-size:13px; }
    @media (max-width: 900px) {
      .app { grid-template-columns: 1fr !important; }
      .sidebar { position: relative; height: auto; width: auto !important; border-right: none; border-bottom: 1px solid var(--border); overflow: visible !important; padding: 16px !important; }
      .app.sidebar-collapsed .sidebar { display: none; }
      .sidebar-footer { position: relative; left: 0; right: 0; bottom: 0; margin-top: 12px; }
      .main { padding: 16px; }
      .grid2 { grid-template-columns: 1fr; }
    }
    """


def _layout(title: str, body: str, *, user: User | None = None, profile_id: str | None = None, active_tab: str | None = None) -> HTMLResponse:
    t = html.escape(title)
    # Steps em ordem lógica de configuração
    _tabs = [
        ("integracoes", "Integrações",  "1"),
        ("fontes",       "Fontes",       "2"),
        ("ia",           "IA",           "3"),
        ("publicacao",   "Publicação",   "4"),
        ("agendamento",  "Agendamento",  "5"),
        ("posts",        "Posts",        ""),
    ]
    if profile_id:
        sub_links = "".join(
            f"<a href='/app/profiles/{profile_id}?tab={slug}' class='{'active' if slug==active_tab else ''}'>"
            f"<span class='nav-step-num'>{num if num else '●'}</span> {html.escape(label)}</a>"
            for slug, label, num in _tabs
        )
        _proj_label = html.escape(title) if active_tab else "Configurar"
        config_nav = f"""<div class="nav-group open" id="nav-config">
          <button class="nav-parent" id="nav-config-btn" style="flex-direction:column;align-items:flex-start;gap:2px">
            <div style="display:flex;align-items:center;gap:10px;width:100%">
              <span class="dot"></span><span style="flex:1;font-size:13px;font-weight:600">{_proj_label}</span><span class="nav-sub-arrow">▶</span>
            </div>
            <div style="font-size:11px;color:var(--primary);padding-left:19px;font-weight:500">projeto ativo</div>
          </button>
          <div class="nav-sub">{sub_links}</div>
        </div>"""
    else:
        config_nav = """<a href="/app/bot"><span class="dot"></span>Configurar</a>"""
    page = f"""<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{t} — PostHub</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet" />
  <style>
    {_base_css()}
    html, body, input, select, textarea, button {{ font-family: 'Inter', ui-sans-serif, system-ui, -apple-system, sans-serif; }}
  </style>
  <script>
    (function(){{
      var t = localStorage.getItem('posthub-theme') || 'roxo';
      document.documentElement.setAttribute('data-theme', t);
    }})();
  </script>
</head>
<body>
  <div class="app" id="app-root">
    <aside class="sidebar" id="sidebar">
      <img class="brand-logo-wide" src="/brand/logo_posthub.png" alt="PostHub" onerror="this.onerror=null;this.src='/static/logo.svg';" style="margin-bottom:14px;" />
      <div class="brand">
        <div class="logo">PH</div>
        <div>
          <h1>PostHub</h1>
          <div class="sub">Automação de conteúdo</div>
        </div>
      </div>
      <nav class="nav" style="margin-top:16px">
        <a href="/app/robot"><span class="dot"></span>Robô</a>
        {config_nav}
        <a href="/app/posts"><span class="dot"></span>Posts</a>
        <a href="/app/notifications"><span class="dot"></span>Notificações</a>
        <a href="/app/logs"><span class="dot"></span>Logs</a>
      </nav>
      <div class="sidebar-footer">
        <p class="muted" style="font-size:11px;margin:0 0 8px">Sessão ativa</p>
        <form method="post" action="/app/logout" style="margin:0">
          <button class="btn secondary" type="submit" style="width:100%;font-size:13px;padding:9px 12px">Sair</button>
        </form>
      </div>
    </aside>
    <main class="main">
      <div class="topbar">
        <div style="display:flex;align-items:center;gap:12px">
          <button class="sidebar-toggle-btn" id="sidebar-toggle-btn" title="Ocultar/Mostrar barra lateral" onclick="toggleSidebar()">☰</button>
          <h2 class="title">{t}</h2>
        </div>
        <div class="theme-bar" style="display:flex;align-items:center;gap:8px">
          <div class="notif-wrap" id="notif-wrap">
            <button class="notif-bell-btn" id="notif-bell" title="Notificações" aria-label="Notificações">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
                <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
              </svg>
              <span class="notif-badge" id="notif-badge"></span>
            </button>
            <div class="notif-dropdown" id="notif-dropdown">
              <div class="notif-dd-header">
                <span>Notificações</span>
                <a href="/app/notifications">Ver todas &rarr;</a>
              </div>
              <div class="notif-list" id="notif-dd-list">
                <div class="notif-dd-empty">Carregando...</div>
              </div>
            </div>
          </div>
          <div style="display:inline-flex;gap:4px;align-items:center">
            <button id="ph-restore-btn" title="Mostrar todos os placeholders" onclick="localStorage.removeItem('ph-hidden');document.querySelectorAll('.dev-ph-wrap').forEach(function(el){{el.style.display='inline-flex'}})" style="background:none;border:1px dashed rgba(245,158,11,.5);border-radius:6px;padding:4px 8px;cursor:pointer;font-size:11px;color:#f59e0b;white-space:nowrap">📌 Placeholders</button>
            <button title="Ocultar todos os placeholders" onclick="localStorage.setItem('ph-hidden','1');document.querySelectorAll('.dev-ph-wrap').forEach(function(el){{el.style.display='none'}})" style="background:none;border:1px dashed rgba(239,68,68,.4);border-radius:6px;padding:4px 8px;cursor:pointer;font-size:11px;color:#ef4444;white-space:nowrap">✕ Remover todos</button>
          </div>
          <select class="theme-select" id="theme-select">
            <option value="roxo">🌙 Roxo</option>
            <option value="oceano">🌊 Oceano</option>
            <option value="floresta">🌿 Floresta</option>
            <option value="aurora">✨ Aurora</option>
            <option value="claro">☀️ Claro</option>
            <option value="rosa">🌸 Rosa</option>
            <option value="ceu">🌤 Céu</option>
          </select>
        </div>
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

    (function(){{
      var current = localStorage.getItem('posthub-theme') || 'roxo';
      function applyTheme(name) {{
        document.documentElement.setAttribute('data-theme', name);
        localStorage.setItem('posthub-theme', name);
        var sel = document.getElementById('theme-select');
        if (sel) sel.value = name;
      }}
      applyTheme(current);
      var sel = document.getElementById('theme-select');
      if (sel) sel.addEventListener('change', function() {{ applyTheme(this.value); }});

      /* nav-sub toggle */
      var navBtn = document.getElementById('nav-config-btn');
      if (navBtn) {{
        navBtn.addEventListener('click', function() {{
          document.getElementById('nav-config').classList.toggle('open');
        }});
      }}
    }})();

    /* ── notification bell ── */
    (function(){{
      var _bell   = document.getElementById('notif-bell');
      var _drop   = document.getElementById('notif-dropdown');
      var _list   = document.getElementById('notif-dd-list');
      var _badge  = document.getElementById('notif-badge');
      if (!_bell) return;

      var LS_SEEN  = 'ph-notif-seen-id';
      var LS_NOTIF = 'ph-notif-settings';
      var _open    = false;
      var _lastFeed = [];

      function getSettings() {{
        try {{ return JSON.parse(localStorage.getItem(LS_NOTIF) || '{{"success":true,"error":true}}'); }}
        catch(e) {{ return {{"success":true,"error":true}}; }}
      }}

      function _renderDrop(items) {{
        var s = getSettings();
        var filtered = items.filter(function(n) {{
          return (n.type === 'success' && s.success) || (n.type === 'error' && s.error);
        }});
        if (!filtered.length) {{
          _list.innerHTML = '<div class="notif-dd-empty">Nenhuma notificação</div>';
          return;
        }}
        _list.innerHTML = filtered.slice(0, 8).map(function(n) {{
          var icon = n.type === 'success'
            ? '<span class="notif-icon ok">✓</span>'
            : '<span class="notif-icon err">✕</span>';
          var link = n.type === 'success' && n.wp_url
            ? '<a href="' + n.wp_url + '" target="_blank" rel="noopener" style="color:#10b981;font-size:11px;font-weight:600;text-decoration:none">Ver post</a>'
            : '';
          var errLine = '';
          if (n.type === 'error' && n.error_label) {{
            var fixPart = n.fix_url
              ? '<a href="' + _esc(n.fix_url) + '" style="display:inline-block;margin-top:5px;font-size:11px;font-weight:700;color:#fff;background:#ef4444;border-radius:6px;padding:3px 10px;text-decoration:none">→ Corrigir</a>'
              : '';
            var fixText = n.fix
              ? '<div style="font-size:11px;color:var(--muted,#888);margin-top:3px;line-height:1.5">' + _esc(n.fix) + '</div>'
              : '';
            errLine = '<div style="margin-top:5px;padding:8px 10px;background:rgba(239,68,68,.07);border:1px solid rgba(239,68,68,.18);border-radius:8px">'
              + '<div style="font-size:11px;color:#ef4444;font-weight:700">⚠ ' + _esc(n.error_label) + '</div>'
              + fixText
              + fixPart
              + '</div>';
          }}
          return '<div class="notif-item">' + icon +
            '<div class="notif-item-text">' +
              '<div class="notif-item-title">' + _esc(n.title) + '</div>' +
              '<div class="notif-item-sub">' + _esc(n.bot) + ' · ' + _esc(n.when) + (link ? ' · ' + link : '') + '</div>' +
              errLine +
            '</div></div>';
        }}).join('');
      }}

      function _esc(s) {{
        return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
      }}

      function _updateBadge(items) {{
        var s = getSettings();
        var seenId = localStorage.getItem(LS_SEEN) || '';
        var unseen = 0;
        for (var i = 0; i < items.length; i++) {{
          var n = items[i];
          if (n.id === seenId) break;
          if ((n.type === 'success' && s.success) || (n.type === 'error' && s.error)) unseen++;
        }}
        if (unseen > 0) {{
          _badge.textContent = unseen > 9 ? '9+' : String(unseen);
          _badge.classList.add('visible');
        }} else {{
          _badge.classList.remove('visible');
        }}
      }}

      function _markSeen() {{
        if (_lastFeed.length) localStorage.setItem(LS_SEEN, _lastFeed[0].id);
        _badge.classList.remove('visible');
      }}

      function _fetchFeed() {{
        fetch('/app/notifications/feed')
          .then(function(r){{ return r.ok ? r.json() : []; }})
          .then(function(data) {{
            _lastFeed = data || [];
            _updateBadge(_lastFeed);
            if (_open) _renderDrop(_lastFeed);
          }})
          .catch(function(){{}});
      }}

      _bell.addEventListener('click', function(e) {{
        e.stopPropagation();
        _open = !_open;
        if (_open) {{
          _drop.classList.add('open');
          _renderDrop(_lastFeed);
          _markSeen();
        }} else {{
          _drop.classList.remove('open');
        }}
      }});

      document.addEventListener('click', function(e) {{
        if (_open && !document.getElementById('notif-wrap').contains(e.target)) {{
          _open = false;
          _drop.classList.remove('open');
        }}
      }});

      _fetchFeed();
      setInterval(_fetchFeed, 20000);
    }})();

    // ── Sidebar toggle ──────────────────────────────────────────────────────
    (function() {{
      function toggleSidebar() {{
        var app = document.getElementById('app-root');
        if (!app) return;
        var collapsed = app.classList.toggle('sidebar-collapsed');
        localStorage.setItem('sidebar-collapsed', collapsed ? '1' : '0');
        var btn = document.getElementById('sidebar-toggle-btn');
        if (btn) btn.title = collapsed ? 'Mostrar barra lateral' : 'Ocultar barra lateral';
      }}
      window.toggleSidebar = toggleSidebar;
      if (localStorage.getItem('sidebar-collapsed') === '1') {{
        var app = document.getElementById('app-root');
        if (app) app.classList.add('sidebar-collapsed');
      }}
    }})();

    // ── Placeholder visibility persistence ──────────────────────────────────
    (function() {{
      if (localStorage.getItem('ph-hidden') === '1') {{
        document.querySelectorAll('.dev-ph-wrap').forEach(function(el) {{ el.style.display='none'; }});
      }}
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
    uid = user.id
    # Busca o perfil explicitamente ativo (active=1)
    bot = db.scalar(
        select(AutomationProfile)
        .where(AutomationProfile.user_id == uid, AutomationProfile.active.is_(True))
        .order_by(AutomationProfile.created_at.asc())
        .limit(1)
    )
    if bot:
        return bot
    # Nenhum ativo — retorna o primeiro sem forçar ativação
    bot = db.scalar(
        select(AutomationProfile)
        .where(AutomationProfile.user_id == uid)
        .order_by(AutomationProfile.created_at.asc())
        .limit(1)
    )
    if bot:
        return bot
    # Nenhum perfil — cria o primeiro
    bot = AutomationProfile(
        user_id=uid, name="Meu Primeiro Robô", active=True,
        schedule_config_json={"posts_per_day": 15, "interval_minutes": 60},
        anti_block_config_json={},
        publish_config_json={"facebook_link": "comments", "default_category": "Receitas", "categories": DEFAULT_RECIPE_CATEGORIES},
    )
    db.add(bot); db.commit(); db.refresh(bot)
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


_NEW_BOT_WIZARD_HTML = """
<style>
/* ── Wizard overlay ───────────────────────────────── */
#new-bot-wizard {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 9999;
  background: rgba(0,0,0,.6);
  backdrop-filter: blur(5px);
  -webkit-backdrop-filter: blur(5px);
}
#new-bot-wizard.wz-open { display: block !important; }

/* ── Panel — centrado por CSS, sem inline style ───── */
#wz-panel {
  position: fixed;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  width: min(500px, calc(100vw - 24px));
  max-height: min(92vh, 700px);
  display: flex;
  flex-direction: column;
  background: var(--bg2);
  border: 1px solid var(--border2);
  border-radius: 20px;
  box-shadow: 0 32px 80px rgba(0,0,0,.55);
  overflow: hidden;
}
#wz-panel.wz-anim { animation: wzIn .22s cubic-bezier(.16,1,.3,1); }

/* ── Body scroll area ─────────────────────────────── */
#wz-body {
  flex: 1;
  overflow-y: auto;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
}
.wz-step { padding: 22px 24px 14px; }

/* ── Drag handle ──────────────────────────────────── */
#wz-drag-handle {
  flex-shrink: 0;
  padding: 16px 20px 12px;
  background: var(--surface2);
  border-bottom: 1px solid var(--border);
  cursor: grab;
  user-select: none;
  -webkit-user-select: none;
}
#wz-drag-handle:active { cursor: grabbing; }

/* ── Footer ───────────────────────────────────────── */
#wz-footer {
  flex-shrink: 0;
  padding: 12px 20px 16px;
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--surface2);
  border-top: 1px solid var(--border);
}

/* ── WP fields row → stack on mobile ─────────────── */
.wz-wp-row { display: flex; gap: 10px; }
@media (max-width: 480px) {
  /* bottom-sheet on phones */
  #wz-panel {
    left: 0 !important;
    top: auto !important;
    bottom: 0 !important;
    transform: none !important;
    width: 100% !important;
    max-height: 88vh;
    border-radius: 20px 20px 0 0;
  }
  #wz-panel.wz-anim { animation: wzInMobile .28s cubic-bezier(.16,1,.3,1); }
  .wz-wp-row { flex-direction: column; gap: 0; }
  #wz-drag-handle { cursor: default; }
}

/* ── Hint box ─────────────────────────────────────── */
.wz-hint {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 14px;
  margin-bottom: 16px;
  font-size: 12px;
  color: var(--muted);
  line-height: 1.6;
}

/* ── Input focus helper ───────────────────────────── */
.wz-input {
  width: 100%;
  box-sizing: border-box;
  padding: 10px 13px;
  border-radius: 9px;
  border: 1px solid var(--border);
  background: var(--input-bg);
  color: var(--text);
  font-size: 14px;
  outline: none;
  transition: border-color .18s;
  -webkit-appearance: none;
}
.wz-input:focus { border-color: var(--primary); }
.wz-label {
  display: block;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .6px;
  text-transform: uppercase;
  color: var(--muted);
  margin: 0 0 6px;
}

@keyframes wzIn {
  from { opacity:0; transform:translate(-50%,-46%) scale(.95) }
  to   { opacity:1; transform:translate(-50%,-50%) scale(1)   }
}
@keyframes wzInMobile {
  from { opacity:0; transform:translateY(40px) }
  to   { opacity:1; transform:translateY(0) }
}
</style>

<!-- ===== WIZARD NOVO PROJETO ===== -->
<div id="new-bot-wizard">
  <div id="wz-panel">

    <!-- Header / drag handle -->
    <div id="wz-drag-handle">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:11px">
        <div style="display:flex;align-items:center;gap:10px">
          <div style="display:flex;gap:5px">
            <span style="width:11px;height:11px;border-radius:50%;background:#ef4444;display:inline-block;flex-shrink:0"></span>
            <span style="width:11px;height:11px;border-radius:50%;background:#f59e0b;display:inline-block;flex-shrink:0"></span>
            <span style="width:11px;height:11px;border-radius:50%;background:#10b981;display:inline-block;flex-shrink:0"></span>
          </div>
          <span id="wz-step-label" style="font-size:11px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--muted)">Passo 1 de 4</span>
        </div>
        <button type="button" onclick="closeWizard()"
          style="background:none;border:none;cursor:pointer;color:var(--muted);font-size:16px;width:28px;height:28px;border-radius:7px;display:flex;align-items:center;justify-content:center;transition:background .15s,color .15s"
          onmouseenter="this.style.background='var(--border)';this.style.color='var(--text)'"
          onmouseleave="this.style.background='none';this.style.color='var(--muted)'">✕</button>
      </div>
      <div style="height:3px;background:var(--border);border-radius:2px">
        <div id="wz-progress-bar" style="height:100%;background:var(--primary);border-radius:2px;transition:width .35s ease;width:25%"></div>
      </div>
    </div>

    <!-- Scrollable body -->
    <div id="wz-body">

      <!-- Step 1: Nome + Emote -->
      <div id="wz-step-1" class="wz-step">
        <div id="wz-emote-preview" style="font-size:34px;margin-bottom:8px;cursor:pointer;transition:transform .15s" title="Clique para trocar o emote" onclick="document.getElementById('wz-emote-picker').style.display=document.getElementById('wz-emote-picker').style.display==='none'?'flex':'none'">🤖</div>
        <h2 style="margin:0 0 5px;font-size:19px;font-weight:800;color:var(--text)">Nome do Projeto</h2>
        <p style="margin:0 0 14px;font-size:13px;color:var(--muted)">Vamos começar! Dê um nome e escolha um emote para identificar este robô.</p>
        <div class="wz-hint">💡 <b>Dica:</b> Use um nome que identifique o blog ou canal. Ex: <em>Blog de Receitas</em>, <em>Notícias Tech</em>, <em>Meu Site</em></div>
        <label class="wz-label">NOME DO PROJETO *</label>
        <input id="wz-name" class="wz-input" type="text" placeholder="Ex: Blog de Receitas" autocomplete="off"
          oninput="document.getElementById('wz-name-err').style.display='none'"
          onkeydown="if(event.key==='Enter')wzNext()" />
        <div id="wz-name-err" style="display:none;color:#ef4444;font-size:12px;margin-top:7px">⚠ Informe o nome do projeto para continuar.</div>
        <label class="wz-label" style="margin-top:14px">EMOTE DO PROJETO</label>
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
          <div id="wz-emote-selected" style="width:40px;height:40px;border-radius:10px;background:var(--surface);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0">🤖</div>
          <button type="button" onclick="document.getElementById('wz-emote-picker').style.display=document.getElementById('wz-emote-picker').style.display==='none'?'flex':'none'" style="background:none;border:1px solid var(--border);border-radius:8px;padding:6px 12px;cursor:pointer;font-size:12px;color:var(--muted)">Escolher emote ▾</button>
        </div>
        <div id="wz-emote-picker" style="display:none;flex-wrap:wrap;gap:6px;padding:10px;background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:6px">
          <script>
          (function(){
            var emotes=['🤖','🚀','📝','⚡','🌟','💡','🎯','📊','🔥','🌍','🎨','💼','📰','🍕','🌿','🏋','🎵','🏆','💻','🛒','✈','🏠','🌙','☀','🦁','🐉','🌺','🧠','🔮','💎'];
            var d=document.getElementById('wz-emote-picker');
            emotes.forEach(function(e){
              var b=document.createElement('button');
              b.type='button';b.textContent=e;
              b.style='background:none;border:1px solid var(--border);border-radius:7px;width:36px;height:36px;font-size:18px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .12s';
              b.onmouseenter=function(){this.style.background='var(--surface2)'};
              b.onmouseleave=function(){this.style.background='none'};
              b.onclick=function(){
                document.getElementById('wz-emote-selected').textContent=e;
                document.getElementById('wz-emote-preview').textContent=e;
                document.getElementById('wz-emote-picker').style.display='none';
              };
              d.appendChild(b);
            });
          })();
          </script>
        </div>
      </div>

      <!-- Step 2: WordPress -->
      <div id="wz-step-2" class="wz-step" style="display:none">
        <div style="margin-bottom:8px">
          <svg width="34" height="34" viewBox="0 0 24 24" fill="#3858e9"><path d="M12 2C6.486 2 2 6.486 2 12s4.486 10 10 10 10-4.486 10-10S17.514 2 12 2zM3.251 12c0-1.308.265-2.556.741-3.695L7.36 18.658A8.762 8.762 0 0 1 3.251 12zm8.749 8.75a8.773 8.773 0 0 1-2.496-.364l2.65-7.695 2.716 7.44a.96.96 0 0 0 .07.136 8.764 8.764 0 0 1-2.94.483zm1.211-12.981c.528-.028.999-.084.999-.084.47-.056.415-.748-.056-.72 0 0-1.415.111-2.329.111-.858 0-2.3-.111-2.3-.111-.47-.028-.526.692-.055.72 0 0 .444.056.914.084l1.358 3.72-1.908 5.721-3.176-8.441c.528-.028 1-.084 1-.084.47-.056.415-.748-.056-.72 0 0-1.415.111-2.329.111a12.65 12.65 0 0 1-.31-.005A8.752 8.752 0 0 1 12 3.25c2.294 0 4.389.879 5.963 2.315a2.885 2.885 0 0 0-.19-.013c-.858 0-1.468.748-1.468 1.551 0 .72.415 1.329.859 2.049.332.581.719 1.329.719 2.409 0 .748-.287 1.617-.663 2.825l-.871 2.907-3.138-9.534zm3.64 11.791-.012-.025 2.733-7.897c.51-1.274.68-2.293.68-3.199 0-.329-.021-.634-.059-.921A8.751 8.751 0 0 1 20.75 12c0 3.216-1.731 6.031-4.319 7.56l.42-1z"/></svg>
        </div>
        <h2 style="margin:0 0 4px;font-size:19px;font-weight:800;color:var(--text)">WordPress</h2>
        <p style="margin:0 0 12px;font-size:13px;color:var(--muted)">Publique posts automaticamente no seu site. <em style="color:var(--primary);font-style:normal;font-weight:600">Opcional.</em></p>
        <div class="wz-hint">💡 <b>App Password:</b> WordPress → <b>Usuários → Perfil → Application Passwords</b> → dê um nome (ex: PostHub) → Adicionar. Copie a senha gerada.</div>
        <label class="wz-label">URL DO SITE</label>
        <input id="wz-wp-url" class="wz-input" type="url" placeholder="https://meublog.com" autocomplete="off" style="margin-bottom:12px" />
        <div class="wz-wp-row">
          <div style="flex:1">
            <label class="wz-label">USUÁRIO</label>
            <input id="wz-wp-user" class="wz-input" type="text" placeholder="admin" autocomplete="off" />
          </div>
          <div style="flex:1">
            <label class="wz-label" style="margin-top:0">APP PASSWORD</label>
            <input id="wz-wp-pass" class="wz-input" type="password" placeholder="xxxx xxxx xxxx" autocomplete="off" />
          </div>
        </div>
      </div>

      <!-- Step 3: Gemini -->
      <div id="wz-step-3" class="wz-step" style="display:none">
        <div style="font-size:34px;margin-bottom:8px">✨</div>
        <h2 style="margin:0 0 4px;font-size:19px;font-weight:800;color:var(--text)">Gemini AI</h2>
        <p style="margin:0 0 12px;font-size:13px;color:var(--muted)">Reescreve e formata posts com IA. <em style="color:var(--primary);font-style:normal;font-weight:600">Opcional.</em></p>
        <div class="wz-hint" style="line-height:1.7">
          <b>Como obter a API Key (gratuito):</b><br>
          1. Acesse <a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener"
               style="color:var(--primary);font-weight:600;text-decoration:underline">aistudio.google.com/apikey</a><br>
          2. Faça login com sua conta Google<br>
          3. Clique em <b>Create API Key</b><br>
          4. Copie a chave gerada e cole abaixo
        </div>
        <label class="wz-label">GEMINI API KEY</label>
        <input id="wz-gemini-key" class="wz-input" type="password" placeholder="AIzaSy..." autocomplete="off" style="margin-bottom:14px" />
        <label class="wz-label">MODELO</label>
        <select id="wz-gemini-model" class="wz-input" style="padding:10px 13px;cursor:pointer">
          <option value="gemini-2.0-flash">gemini-2.0-flash — Rápido, mais recente ⚡</option>
          <option value="gemini-2.0-flash-lite">gemini-2.0-flash-lite — Leve e econômico 🪶</option>
          <option value="gemini-1.5-flash-latest" selected>gemini-1.5-flash-latest — Padrão recomendado ✅</option>
          <option value="gemini-1.5-flash-8b">gemini-1.5-flash-8b — Ultra rápido, menor 🏎</option>
          <option value="gemini-1.5-pro-latest">gemini-1.5-pro-latest — Mais inteligente, mais lento 🧠</option>
          <option value="gemini-2.0-pro-exp">gemini-2.0-pro-exp — Experimental, Pro 2.0 🔬</option>
        </select>
        <div style="margin-top:8px;font-size:11px;color:var(--muted)">Dúvida? Deixe <b>gemini-1.5-flash-latest</b> — funciona bem para a maioria dos casos.</div>
      </div>

      <!-- Step 4: Resumo -->
      <div id="wz-step-4" class="wz-step" style="display:none">
        <div style="font-size:34px;margin-bottom:8px">🎉</div>
        <h2 style="margin:0 0 4px;font-size:19px;font-weight:800;color:var(--text)">Tudo pronto!</h2>
        <p style="margin:0 0 14px;font-size:13px;color:var(--muted)">Revise antes de criar o projeto.</p>
        <div id="wz-summary" style="display:flex;flex-direction:column;gap:8px"></div>
      </div>

    </div><!-- /wz-body -->

    <!-- Footer -->
    <div id="wz-footer">
      <button id="wz-back-btn" type="button" onclick="wzBack()"
        style="display:none;background:none;border:none;cursor:pointer;color:var(--muted);font-size:13px;font-weight:600;padding:8px 4px;white-space:nowrap">← Voltar</button>
      <div style="flex:1"></div>
      <button id="wz-skip-btn" type="button" onclick="wzSkip()"
        style="background:none;border:1px solid var(--border);cursor:pointer;color:var(--muted);font-size:13px;font-weight:600;padding:8px 14px;border-radius:9px;white-space:nowrap">Pular →</button>
      <button id="wz-next-btn" type="button" onclick="wzNext()"
        style="background:var(--primary);color:#fff;border:none;cursor:pointer;font-size:14px;font-weight:700;padding:10px 22px;border-radius:10px;min-width:130px;white-space:nowrap;transition:opacity .15s">Avançar →</button>
    </div>

  </div><!-- /wz-panel -->
</div><!-- /new-bot-wizard -->

<!-- Hidden submit form -->
<form id="wz-form" method="post" action="/app/profiles/create-wizard" style="display:none">
  <input type="hidden" name="name"            id="wz-form-name" />
  <input type="hidden" name="wp_base_url"     id="wz-form-wp-url" />
  <input type="hidden" name="wp_username"     id="wz-form-wp-user" />
  <input type="hidden" name="wp_app_password" id="wz-form-wp-pass" />
  <input type="hidden" name="gemini_api_key"  id="wz-form-gemini" />
  <input type="hidden" name="gemini_model"    id="wz-form-gemini-model" />
  <input type="hidden" name="emoji"           id="wz-form-emoji" value="🤖" />
</form>

<script>
(function() {
  var _step = 1, _total = 4;
  var _skippable = { 2: true, 3: true };
  var _panel  = document.getElementById('wz-panel');
  var _handle = document.getElementById('wz-drag-handle');

  /* ── Open / close ──────────────────────────────── */
  window.openWizard = function() {
    /* Remove any drag-set inline positions so CSS centers it */
    _panel.style.removeProperty('left');
    _panel.style.removeProperty('top');
    _panel.style.removeProperty('bottom');
    _panel.style.removeProperty('transform');
    /* Re-trigger animation */
    _panel.classList.remove('wz-anim');
    void _panel.offsetWidth;
    _panel.classList.add('wz-anim');
    document.getElementById('new-bot-wizard').classList.add('wz-open');
    _step = 1;
    _render();
    setTimeout(function(){ var n=document.getElementById('wz-name'); if(n) n.focus(); }, 120);
  };

  window.closeWizard = function() {
    document.getElementById('new-bot-wizard').classList.remove('wz-open');
  };

  /* ── Navigation ────────────────────────────────── */
  window.wzNext = function() {
    if (_step === 1) {
      var n = (document.getElementById('wz-name').value || '').trim();
      if (!n) { document.getElementById('wz-name-err').style.display = 'block'; return; }
    }
    if (_step < _total) { _step++; _render(); document.getElementById('wz-body').scrollTop = 0; }
    else { _submit(); }
  };
  window.wzBack = function() {
    if (_step > 1) { _step--; _render(); document.getElementById('wz-body').scrollTop = 0; }
  };
  window.wzSkip = function() {
    if (_step < _total) { _step++; _render(); document.getElementById('wz-body').scrollTop = 0; }
  };

  function _render() {
    for (var i = 1; i <= _total; i++) {
      var el = document.getElementById('wz-step-' + i);
      if (el) el.style.display = (i === _step) ? 'block' : 'none';
    }
    document.getElementById('wz-progress-bar').style.width = ((_step / _total) * 100) + '%';
    document.getElementById('wz-step-label').textContent = 'Passo ' + _step + ' de ' + _total;
    document.getElementById('wz-back-btn').style.display = _step > 1 ? 'inline-block' : 'none';
    document.getElementById('wz-skip-btn').style.display = (_skippable[_step] && _step < _total) ? 'inline-block' : 'none';
    var nb = document.getElementById('wz-next-btn');
    if (_step === _total) {
      nb.textContent = '🚀 Criar Projeto';
      nb.style.background = 'linear-gradient(135deg,var(--primary),#ec4899)';
      _buildSummary();
    } else {
      nb.textContent = 'Avançar →';
      nb.style.background = 'var(--primary)';
    }
  }

  function _buildSummary() {
    var name   = (document.getElementById('wz-name').value || '').trim();
    var emoji  = (document.getElementById('wz-emote-selected').textContent || '🤖').trim();
    var wpUrl  = (document.getElementById('wz-wp-url').value || '').trim();
    var wpUser = (document.getElementById('wz-wp-user').value || '').trim();
    var gem    = (document.getElementById('wz-gemini-key').value || '').trim();
    var rows   = '';
    rows += _row(emoji, 'Projeto',   name || '<em style="color:#ef4444">Não informado</em>', !!name);
    rows += (wpUrl && wpUser)
      ? _row('🔵', 'WordPress', wpUrl + ' <span style="opacity:.7;font-weight:400">(@' + wpUser + ')</span>', true)
      : _row('🔧', 'WordPress', '<span style="opacity:.6;font-weight:400">Não configurado — adicione depois</span>', false);
    rows += gem
      ? _row('✨', 'Gemini AI', 'Chave configurada ✓', true)
      : _row('✨', 'Gemini AI', '<span style="opacity:.6;font-weight:400">Não configurado — adicione depois</span>', false);
    document.getElementById('wz-summary').innerHTML = rows;
  }

  function _row(icon, label, value, ok) {
    return '<div style="display:flex;align-items:flex-start;gap:11px;padding:9px 13px;border-radius:10px;'
         + 'background:' + (ok ? 'var(--surface2)' : 'var(--surface)') + ';'
         + 'border:1px solid ' + (ok ? 'var(--border2)' : 'var(--border)') + '">'
         + '<span style="font-size:18px;line-height:1.4;flex-shrink:0">' + icon + '</span>'
         + '<div style="min-width:0">'
         + '<div style="font-size:10px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;color:var(--muted);margin-bottom:2px">' + label + '</div>'
         + '<div style="font-size:13px;font-weight:600;word-break:break-all">' + value + '</div>'
         + '</div></div>';
  }

  function _submit() {
    document.getElementById('wz-form-name').value          = (document.getElementById('wz-name').value || '').trim();
    document.getElementById('wz-form-wp-url').value        = (document.getElementById('wz-wp-url').value || '').trim();
    document.getElementById('wz-form-wp-user').value       = (document.getElementById('wz-wp-user').value || '').trim();
    document.getElementById('wz-form-wp-pass').value       = (document.getElementById('wz-wp-pass').value || '').trim();
    document.getElementById('wz-form-gemini').value        = (document.getElementById('wz-gemini-key').value || '').trim();
    document.getElementById('wz-form-gemini-model').value  = (document.getElementById('wz-gemini-model').value || 'gemini-1.5-flash-latest');
    document.getElementById('wz-form-emoji').value         = (document.getElementById('wz-emote-selected').textContent || '🤖').trim();
    document.getElementById('wz-form').submit();
  }

  /* ── Close on overlay click ────────────────────── */
  var _justDragged = false;
  document.getElementById('new-bot-wizard').addEventListener('click', function(e) {
    if (e.target === this && !_justDragged) closeWizard();
    _justDragged = false;
  });

  /* ── Drag (mouse — desktop) ────────────────────── */
  var _drag = { on: false, sx:0, sy:0, ox:0, oy:0 };

  function _isMobile() { return window.matchMedia('(max-width:480px)').matches; }

  function _anchorPanel() {
    var r = _panel.getBoundingClientRect();
    _panel.style.left      = r.left + 'px';
    _panel.style.top       = r.top  + 'px';
    _panel.style.transform = 'none';
    _panel.style.removeProperty('bottom');
  }

  _handle.addEventListener('mousedown', function(e) {
    if (e.button !== 0 || _isMobile()) return;
    _anchorPanel();
    _drag.on = true;
    _drag.sx = e.clientX; _drag.sy = e.clientY;
    _drag.ox = parseFloat(_panel.style.left);
    _drag.oy = parseFloat(_panel.style.top);
    _handle.style.cursor = 'grabbing';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });

  document.addEventListener('mousemove', function(e) {
    if (!_drag.on) return;
    var nx = Math.max(0, Math.min(window.innerWidth  - _panel.offsetWidth,  _drag.ox + (e.clientX - _drag.sx)));
    var ny = Math.max(0, Math.min(window.innerHeight - _panel.offsetHeight, _drag.oy + (e.clientY - _drag.sy)));
    _panel.style.left = nx + 'px';
    _panel.style.top  = ny + 'px';
  });

  document.addEventListener('mouseup', function() {
    if (!_drag.on) return;
    _drag.on = false; _justDragged = true;
    _handle.style.cursor = 'grab';
    document.body.style.userSelect = '';
  });

  /* ── Touch drag (mobile/tablet) ────────────────── */
  var _touch = { on: false, sx:0, sy:0, ox:0, oy:0 };

  _handle.addEventListener('touchstart', function(e) {
    if (_isMobile()) return; /* bottom-sheet on phone — no drag */
    var t = e.touches[0];
    _anchorPanel();
    _touch.on = true;
    _touch.sx = t.clientX; _touch.sy = t.clientY;
    _touch.ox = parseFloat(_panel.style.left);
    _touch.oy = parseFloat(_panel.style.top);
    e.preventDefault();
  }, { passive: false });

  document.addEventListener('touchmove', function(e) {
    if (!_touch.on) return;
    var t = e.touches[0];
    var nx = Math.max(0, Math.min(window.innerWidth  - _panel.offsetWidth,  _touch.ox + (t.clientX - _touch.sx)));
    var ny = Math.max(0, Math.min(window.innerHeight - _panel.offsetHeight, _touch.oy + (t.clientY - _touch.sy)));
    _panel.style.left = nx + 'px';
    _panel.style.top  = ny + 'px';
    e.preventDefault();
  }, { passive: false });

  document.addEventListener('touchend', function() {
    if (!_touch.on) return;
    _touch.on = false; _justDragged = true;
  });

})();
</script>
"""


@router.get("/app/robot", include_in_schema=False)
def robot_panel(request: Request, user: User = Depends(get_current_user), db=Depends(get_db)):
    # todos os projetos do usuário
    all_profiles = list(db.scalars(
        select(AutomationProfile)
        .where(AutomationProfile.user_id == user.id)
        .order_by(AutomationProfile.created_at.asc())
    ))
    # Se não há nenhum bot, mostra tela de estado vazio
    if not all_profiles:
        empty_body = _NEW_BOT_WIZARD_HTML + f"""
        {_ph("active-project-banner")}
        <div class="active-project-banner" style="margin-bottom:14px;border-color:var(--border);background:var(--surface)">
          <div>
            <div class="active-project-label">Nenhum projeto</div>
            <div class="active-project-name" style="color:var(--muted)">Clique em Novo Projeto para começar</div>
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <button class="btn" onclick="openWizard()" type="button">+ Novo Projeto</button>
          </div>
        </div>
        {_ph("secao-projetos-robos")}
        <div class="card">
          <div style="text-align:center;padding:48px 20px 36px;color:var(--muted)">
            <div style="font-size:56px;margin-bottom:16px">🤖</div>
            <div style="font-size:18px;font-weight:700;margin-bottom:8px;color:var(--text)">Nenhum robô criado ainda</div>
            <div style="font-size:13px;margin-bottom:24px">Configure o seu primeiro projeto para começar a automatizar publicações.</div>
            <button class="btn" onclick="openWizard()" type="button" style="font-size:14px;padding:12px 28px">+ Criar meu primeiro projeto</button>
          </div>
        </div>
        """
        return _layout("Robô", empty_body, user=user)

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
    # Verifica WordPress do bot ativo
    _wp_integ_check = db.scalar(select(Integration).where(Integration.profile_id == bot.id, Integration.type == IntegrationType.WORDPRESS))
    wp_configured = False
    if _wp_integ_check:
        try:
            _wpc = decrypt_json(_wp_integ_check.credentials_encrypted)
            _users = _wpc.get("users") if isinstance(_wpc.get("users"), list) else []
            if not _users and _wpc.get("username"):
                _users = [{"username": _wpc["username"], "app_password": _wpc.get("app_password", "")}]
            _au = str(_wpc.get("active_username") or "")
            _au_obj = next((u for u in _users if u.get("username") == _au), _users[0] if _users else None)
            wp_configured = bool(_au_obj and _au_obj.get("username") and _au_obj.get("app_password") and _wpc.get("base_url"))
        except Exception:
            wp_configured = False
    wp_status_label = "Configurado" if wp_configured else "Não configurado"
    wp_status_color = "#10b981" if wp_configured else "#ef4444"
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
    # accelerate agora está inline no robot-actions
    # stats por projeto
    def _proj_stats(pr):
        wp = db.scalar(select(Integration).where(Integration.profile_id == pr.id, Integration.type == IntegrationType.WORDPRESS))
        wp_url = ""
        if wp:
            try:
                creds = decrypt_json(wp.credentials_encrypted)
                wp_url = (creds.get("base_url") or "") if isinstance(creds, dict) else ""
            except Exception:
                pass
        completed = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == pr.id, Post.status == PostStatus.completed)) or 0)
        failed    = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == pr.id, Post.status == PostStatus.failed)) or 0)
        pending   = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == pr.id, Post.status == PostStatus.pending)) or 0)
        return wp_url, completed, failed, pending

    # SVG icons (16px, no fill, stroke only)
    _ico_gear = ("<svg width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'>"
                 "<circle cx='12' cy='12' r='3'/>"
                 "<path d='M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z'/>"
                 "</svg>")
    _ico_trash = ("<svg width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'>"
                  "<polyline points='3 6 5 6 21 6'/><path d='M19 6l-1 14H6L5 6'/><path d='M10 11v6'/><path d='M14 11v6'/><path d='M9 6V4h6v2'/>"
                  "</svg>")
    _ico_power = ("<svg width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'>"
                  "<path d='M18.36 6.64A9 9 0 1 1 5.64 5.64'/><line x1='12' y1='2' x2='12' y2='12'/>"
                  "</svg>")

    # Conta quantos bots ativos existem (para controle do limite)
    active_count = int(db.scalar(
        select(func.count()).select_from(AutomationProfile)
        .where(AutomationProfile.user_id == user.id, AutomationProfile.active.is_(True))
    ) or 0)
    MAX_ACTIVE = 3
    can_activate_more = active_count < MAX_ACTIVE

    proj_rows = ""
    for pr in all_profiles:
        is_active = pr.active
        wp_url, p_done, p_fail, p_pend = _proj_stats(pr)
        pr_emoji = (pr.publish_config_json or {}).get("emoji") or "🤖"

        # Badge de status na coluna "Projeto"
        if is_active:
            status_badge = "<span class='badge-active' style='font-size:11px'><span class='dot-pulse'></span>Online</span>"
        else:
            status_badge = "<span class='badge-inactive' style='font-size:11px'><span class='dot-off'></span>Inativo</span>"

        # WordPress info
        if wp_url:
            wp_info = f"<a href='{html.escape(wp_url)}' target='_blank' style='font-size:12px;color:var(--primary);text-decoration:none;word-break:break-all'>{html.escape(wp_url)}</a>"
        else:
            wp_info = "<span style='font-size:12px;color:var(--muted)'>Não configurado</span>"

        # Botão toggle ON/OFF
        if is_active:
            toggle_btn = (
                f"<form method='post' action='/app/robot/toggle/{pr.id}' style='margin:0'>"
                f"<button type='submit' class='bot-online-pill' title='Clique para desligar'>"
                f"<span class='pill-dot'></span>Online"
                f"</button></form>"
            )
        elif can_activate_more:
            toggle_btn = (
                f"<form method='post' action='/app/robot/toggle/{pr.id}' style='margin:0'>"
                f"<button type='submit' class='bot-ligar-btn' title='Ligar este robô'>"
                f"{_ico_power} Ligar"
                f"</button></form>"
            )
        else:
            toggle_btn = (
                f"<button type='button' class='bot-ligar-btn' disabled title='Limite de {MAX_ACTIVE} robôs ativos atingido'>"
                f"{_ico_power} Ligar"
                f"</button>"
            )

        # Botão Config (ícone puro)
        config_btn = (
            f"<a href='/app/profiles/{pr.id}?tab=integracoes' class='act-btn primary-hover' title='Configurar'>"
            f"{_ico_gear}</a>"
        )

        # Botão Excluir (ícone puro, vermelho no hover)
        if is_active and len(all_profiles) > 1:
            del_confirm = f"'{html.escape(pr.name)}' está Online. Desligar e excluir?"
        else:
            del_confirm = f"Excluir '{html.escape(pr.name)}'? Não pode ser desfeito."
        del_btn = (
            f"<form method='post' action='/app/robot/delete/{pr.id}' style='margin:0' onsubmit=\"return confirm('{del_confirm}')\">"
            f"<button type='submit' class='act-btn danger' title='Excluir'>{_ico_trash}</button></form>"
        )

        proj_rows += f"""
        <tr class="{'proj-row-active' if is_active else ''}">
          <td style="padding:12px 14px">
            <div style="display:flex;align-items:center;gap:10px">
              <div style="width:34px;height:34px;border-radius:10px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:18px;background:{'linear-gradient(135deg,var(--primary),var(--pink))' if is_active else 'var(--surface2)'}">{pr_emoji}</div>
              <div>
                <div style="font-size:14px;font-weight:700;color:var(--text);white-space:nowrap">{html.escape(pr.name)}</div>
                <div style="margin-top:3px">{status_badge}</div>
              </div>
            </div>
          </td>
          <td style="padding:12px 10px;max-width:220px">{wp_info}</td>
          <td style="padding:12px 10px;white-space:nowrap">
            <div style="display:flex;gap:14px">
              <div style="text-align:center"><div style="font-size:16px;font-weight:800;color:#10b981">{p_done}</div><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Public.</div></div>
              <div style="text-align:center"><div style="font-size:16px;font-weight:800;color:var(--muted)">{p_pend}</div><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Pend.</div></div>
              <div style="text-align:center"><div style="font-size:16px;font-weight:800;color:#ef4444">{p_fail}</div><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Falhas</div></div>
            </div>
          </td>
          <td style="padding:12px 10px">
            <div style="display:flex;gap:6px;align-items:center">{toggle_btn} {config_btn} {del_btn}</div>
          </td>
        </tr>"""

    # _ph definido no nível de módulo

    import random as _random
    _sleep_msgs = [
        ("😴", "zzZZ... todos os bots tirando uma soneca."),
        ("🛌", "Hora do descanso. Seus robôs estão off."),
        ("💤", "Silêncio total. Nem um post saindo por aqui."),
        ("🌙", "Modo noturno ativado. Bots desligados."),
        ("🧸", "Os robôs foram dormir. Desligue a luz."),
        ("☕", "Sem robô ativo. Hora do café enquanto isso."),
        ("🌑", "Escuridão total nos servidores. Ninguém em casa."),
        ("🐢", "Mais devagar que uma tartaruga... porque não tem ninguém rodando."),
        ("📻", "...apenas estática. Nenhum robô no ar."),
        ("🎭", "Robôs de férias. Destino: desconhecido."),
    ]
    _sleep_icon, _sleep_text = _random.choice(_sleep_msgs)

    if active_count == 0:
        _active_banner = f"""
    <div class="active-project-banner" style="margin-bottom:14px;border-color:var(--border);background:var(--surface);opacity:.85">
      <div style="display:flex;align-items:center;gap:14px">
        <div style="font-size:36px;line-height:1;filter:grayscale(40%)">{_sleep_icon}</div>
        <div>
          <div class="active-project-label" style="color:var(--muted)">Nenhum robô ativo</div>
          <div class="active-project-name" style="font-size:17px;color:var(--muted);font-weight:600">{_sleep_text}</div>
          <div style="font-size:12px;color:var(--muted);margin-top:6px">Ligue um robô na tabela abaixo para começar.</div>
        </div>
      </div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <button class="btn secondary" style="font-size:13px;padding:7px 14px" type="button"
          onclick="openWizard()">+ Novo Projeto</button>
      </div>
    </div>"""
    else:
        _active_banner = f"""
    <div class="active-project-banner" style="margin-bottom:14px">
      <div style="display:flex;align-items:center;gap:12px">
        <div style="width:44px;height:44px;border-radius:12px;background:linear-gradient(135deg,var(--primary),var(--pink));display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0">{html.escape((bot.publish_config_json or {{}}).get('emoji') or '🤖')}</div>
        <div>
          <div class="active-project-label">Projeto ativo</div>
          <div class="active-project-name">{html.escape(bot.name)}</div>
          <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">
            <span class="active-project-stat" style="border-color:{wp_status_color};color:{wp_status_color}">
              <b>WP:</b> {wp_status_label}
            </span>
            <span class="active-project-stat" style="border-color:{'#10b981' if gemini_ok else '#ef4444'};color:{'#10b981' if gemini_ok else '#ef4444'}">
              <b>Gemini:</b> {gemini_status}
            </span>
          </div>
        </div>
      </div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <button class="btn secondary" style="font-size:13px;padding:7px 14px" type="button"
          onclick="openWizard()">+ Novo Projeto</button>
        <a href="/app/profiles/{bot.id}?tab=integracoes" class="btn secondary" style="font-size:13px;padding:7px 14px">⚙ Configurar</a>
      </div>
    </div>"""

    body = _NEW_BOT_WIZARD_HTML + f"""

    {_ph("active-project-banner")}
    {_active_banner}

    {_ph("secao-projetos-robos")}
    <div class="card" style="margin-bottom:14px">
      <details class="toggle-section" open>
        <summary>
          <span class="ts-title">
            Projetos / Robôs
            <span class="ts-badge">{len(all_profiles)} total</span>
            <span class="ts-badge" style="color:#10b981;border-color:rgba(16,185,129,.3);background:rgba(16,185,129,.08)">{active_count} online</span>
            <span class="ts-badge" style="color:var(--muted)">{MAX_ACTIVE} max</span>
          </span>
          <span class="ts-arrow">▶</span>
        </summary>
        <div class="ts-body" style="padding-top:0">
          {_ph("tabela-projetos")}
          <div style="overflow-x:auto">
            <table style="width:100%;border-collapse:collapse">
              <thead>
                <tr>
                  <th style="padding:10px 14px;text-align:left">Projeto</th>
                  <th style="padding:10px 10px;text-align:left">WordPress</th>
                  <th style="padding:10px 10px;text-align:left">Publicações</th>
                  <th style="padding:10px 10px;text-align:left">Ações</th>
                </tr>
              </thead>
              <tbody>
                {proj_rows}
              </tbody>
            </table>
          </div>
        </div>
      </details>
    </div>

    {_ph("secao-controle-robo")}
    <div class="card" style="margin-bottom:14px">
      <details class="toggle-section" open>
        <summary>
          <span class="ts-title">
            <span class="badge-active"><span class="dot-pulse"></span>Ativo</span>
            {html.escape(bot.name)}
          </span>
          <span class="ts-arrow">▶</span>
        </summary>
        <div class="ts-body">
          {_ph("stats-coleta")}
          <div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:16px">
            <span class="active-project-stat">Coleta: <b>{created}</b> novos / <b>{skipped}</b> rep. / <b>{ignored}</b> ign.</span>
            <span class="active-project-stat">Fila: <b>{queued_due}</b> prontos / <b>{queued_scheduled}</b> agend. / <b>{running_jobs}</b> rod.</span>
            <span class="active-project-stat">Posts: <b>{pending_posts}</b> pend. / <b>{processing_posts}</b> proc.</span>
          </div>
          {_ph("botao-iniciar-parar")}
          <div class="robot-actions">
            <div class="robot-action-card robot-action-primary" style="{'border-color:#10b981;background:rgba(16,185,129,.08)' if in_progress else ''}">
              <div class="robot-action-icon" style="{'background:linear-gradient(135deg,#10b981,#059669);color:#fff' if in_progress else ''}">{"●" if in_progress else "▶"}</div>
              <div style="flex:1">
                <div class="robot-action-title">{"Robô Ativo" if in_progress else "Iniciar Robô"}</div>
                <div class="robot-action-desc muted">{"Processando automaticamente — clique para parar" if in_progress else "Busca fontes e processa automaticamente"}</div>
                {f"""<div id="wp-error-msg" style="display:none;margin-top:8px;padding:8px 12px;background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);border-radius:8px;font-size:12px;color:#ef4444">
                  ⚠ WordPress não configurado. Vá em <a href="/app/profiles/{bot.id}?tab=integracoes" style="color:#ef4444;font-weight:600">Configurar → Integrações → WordPress</a> e adicione a URL, usuário e App Password.
                </div>""" if not in_progress else ""}
              </div>
              {"<form method='post' action='/app/robot/stop' style='margin-left:auto'><button class='btn' type='submit' style='white-space:nowrap;min-width:110px;justify-content:center;background:#10b981;border-color:#10b981;color:#fff;box-shadow:0 0 12px rgba(16,185,129,.4)'>● ATIVO</button></form>"
              if in_progress else
              """<div style="margin-left:auto">
                <button class='btn' type='button' id='btn-iniciar-diag' style='white-space:nowrap;min-width:110px;justify-content:center' onclick='openDiagModal()'>▶ Iniciar</button>
              </div>"""}
            </div>
            {f'''{_ph("botao-rodar-agora")}<div class="robot-action-card">
              <div class="robot-action-icon" style="background:rgba(245,158,11,.15);color:#f59e0b">⚡</div>
              <div><div class="robot-action-title">Rodar pendentes agora</div><div class="robot-action-desc muted">{queued_scheduled} jobs agendados aguardando</div></div>
              <form method="post" action="/app/robot/run-now" style="margin-left:auto">
                <button class="btn secondary" type="submit" style="white-space:nowrap">Rodar agora</button>
              </form>
            </div>''' if queued_scheduled > 0 and running_jobs == 0 else ''}
            {_ph("botao-reprocessar-ia")}
            <div class="robot-action-card">
              <div class="robot-action-icon" style="background:rgba(99,102,241,.15);color:#6366f1">↺</div>
              <div><div class="robot-action-title">Reprocessar IA</div><div class="robot-action-desc muted">{failed_count} posts com falha</div></div>
              <form method="post" action="/app/robot/retry-ai" style="margin-left:auto">
                <button class="btn secondary" type="submit" style="white-space:nowrap">↺ Reprocessar ({failed_count})</button>
              </form>
            </div>
          </div>
          {_ph("acoes-avancadas")}
          <details style="margin-top:12px">
            <summary style="cursor:pointer;font-size:13px;color:var(--muted);padding:8px 4px;list-style:none;display:flex;align-items:center;gap:6px;border-top:1px solid var(--border)">
              <span style="font-size:9px">▶</span> Ações avançadas
            </summary>
            <div style="display:flex;flex-direction:column;gap:8px;margin-top:10px">
              {_ph("btn-limpar-falhas")}
              <div class="robot-action-card">
                <div class="robot-action-icon" style="background:rgba(239,68,68,.12);color:#ef4444">🗑</div>
                <div><div class="robot-action-title">Limpar falhas</div><div class="robot-action-desc muted">Remove posts com erro da fila</div></div>
                <form method="post" action="/app/robot/clear-failures" style="margin-left:auto">
                  <button class="btn secondary" type="submit">Limpar falhas</button>
                </form>
              </div>
              {_ph("btn-limpar-historico")}
              <div class="robot-action-card">
                <div class="robot-action-icon" style="background:rgba(239,68,68,.12);color:#ef4444">🗑</div>
                <div><div class="robot-action-title">Limpar histórico</div><div class="robot-action-desc muted">Remove posts do PostHub (não apaga do WP)</div></div>
                <form method="post" action="/app/robot/clear-posts" style="margin-left:auto">
                  <button class="btn secondary" type="submit">Limpar posts</button>
                </form>
              </div>
            </div>
          </details>
        </div>
      </details>
    </div>

    {_ph("secao-posts")}
    <div class="card">
      <details class="toggle-section" open>
        <summary>
          <span class="ts-title">Posts <span class="ts-badge">{len(posts)}</span></span>
          <span class="ts-arrow">▶</span>
        </summary>
        <div class="ts-body">
          {_ph("tabela-posts")}
          <div class="scrollbox">
            <table id="robot-posts-table">
              <thead><tr><th>Título</th><th>Status</th><th>Criado</th><th>WP URL</th></tr></thead>
              <tbody>{rows}</tbody>
            </table>
          </div>
        </div>
      </details>
    </div>
    """
    diag_modal = f"""
    <div class="diag-overlay" id="diagOverlay" onclick="if(event.target===this)closeDiagModal()">
      <div class="diag-modal" style="padding:22px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px">
          <div style="font-weight:700;font-size:17px">Diagnóstico antes de iniciar</div>
          <button onclick="closeDiagModal()" style="background:none;border:none;color:var(--muted);font-size:22px;cursor:pointer;line-height:1;padding:0 4px">&times;</button>
        </div>
        <div id="diagItems" style="display:flex;flex-direction:column;gap:10px;min-height:80px">
          <div style="text-align:center;padding:32px;color:var(--muted)">
            <div style="font-size:28px;margin-bottom:8px">⏳</div>
            Verificando configurações...
          </div>
        </div>
        <div id="diagFooter" style="display:flex;gap:10px;margin-top:20px;justify-content:flex-end"></div>
      </div>
    </div>
    <form id="diagStartForm" method="post" action="/app/robot/start" style="display:none"></form>
    <script>
    function openDiagModal(){{
      var overlay = document.getElementById('diagOverlay');
      overlay.classList.add('open');
      document.getElementById('diagItems').innerHTML = '<div style="text-align:center;padding:32px;color:var(--muted)"><div style="font-size:28px;margin-bottom:8px">⏳</div>Verificando configurações...</div>';
      document.getElementById('diagFooter').innerHTML = '';
      fetch('/app/robot/diagnose')
        .then(function(r){{ return r.json(); }})
        .then(function(data){{
          var icons = {{ok:'✅', warn:'⚠️', err:'❌'}};
          var html = '';
          data.results.forEach(function(item){{
            html += '<div class="diag-item ' + item.status + '" style="border-radius:12px;padding:12px 14px;border:1px solid transparent">';
            html += '<div style="display:flex;align-items:flex-start;gap:10px">';
            html += '<span style="font-size:18px;flex-shrink:0;margin-top:1px">' + (icons[item.status]||'•') + '</span>';
            html += '<div style="flex:1">';
            html += '<div style="font-weight:600;font-size:14px;margin-bottom:3px">' + item.label + '</div>';
            if(item.desc) html += '<div style="font-size:12px;color:var(--muted);line-height:1.4">' + item.desc + '</div>';
            if(item.fix) html += '<div style="font-size:12px;margin-top:6px;padding:6px 10px;background:rgba(0,0,0,.12);border-radius:7px;line-height:1.5">' + item.fix + '</div>';
            html += '</div></div></div>';
          }});
          document.getElementById('diagItems').innerHTML = html;
          var footer = '';
          footer += '<button type="button" class="btn secondary" onclick="closeDiagModal()" style="min-width:80px">Fechar</button>';
          if(data.can_start){{
            footer += '<button type="button" class="btn" onclick="document.getElementById(\'diagStartForm\').submit()" style="min-width:110px;background:#10b981;border-color:#10b981;color:#fff">▶ Iniciar</button>';
          }} else {{
            footer += '<button type="button" class="btn" disabled style="min-width:110px;opacity:.45;cursor:not-allowed">▶ Iniciar</button>';
          }}
          document.getElementById('diagFooter').innerHTML = footer;
        }})
        .catch(function(){{
          document.getElementById('diagItems').innerHTML = '<div style="text-align:center;padding:24px;color:#ef4444">Erro ao verificar. Tente novamente.</div>';
          document.getElementById('diagFooter').innerHTML = '<button type="button" class="btn secondary" onclick="closeDiagModal()">Fechar</button>';
        }});
    }}
    function closeDiagModal(){{
      document.getElementById('diagOverlay').classList.remove('open');
    }}
    </script>
    """
    body = body + diag_modal
    return _layout("Robô", body, user=user)


@router.get("/app/bot", include_in_schema=False)
def bot_redirect(user: User = Depends(get_current_user), db=Depends(get_db)):
    bot = _get_or_create_single_bot(db, user=user)
    return RedirectResponse(f"/app/profiles/{bot.id}", status_code=status.HTTP_302_FOUND)


@router.post("/app/robot/start", include_in_schema=False)
def robot_start(user: User = Depends(get_current_user), db=Depends(get_db)):
    bot = _get_or_create_single_bot(db, user=user)

    # Verifica se WordPress está configurado com usuário e senha
    wp_integ = db.scalar(select(Integration).where(Integration.profile_id == bot.id, Integration.type == IntegrationType.WORDPRESS))
    wp_ok = False
    if wp_integ:
        try:
            wp_creds = decrypt_json(wp_integ.credentials_encrypted)
            users = wp_creds.get("users") if isinstance(wp_creds.get("users"), list) else []
            if not users and wp_creds.get("username"):
                users = [{"username": wp_creds["username"], "app_password": wp_creds.get("app_password", "")}]
            active_uname = str(wp_creds.get("active_username") or "")
            active_user = next((u for u in users if u.get("username") == active_uname), users[0] if users else None)
            if active_user and active_user.get("username") and active_user.get("app_password") and wp_creds.get("base_url"):
                wp_ok = True
        except Exception:
            wp_ok = False
    if not wp_ok:
        return RedirectResponse(
            f"/app/robot?msg={quote_plus('WordPress não configurado. Vá em Configurar → Integrações → WordPress e adicione a URL do site, usuário e App Password.')}",
            status_code=status.HTTP_302_FOUND,
        )

    queued_jobs = int(db.scalar(select(func.count()).select_from(Job).where(Job.profile_id == bot.id, Job.status == JobStatus.queued)) or 0)
    running_jobs = int(db.scalar(select(func.count()).select_from(Job).where(Job.profile_id == bot.id, Job.status == JobStatus.running)) or 0)
    pending_posts = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == bot.id, Post.status == PostStatus.pending)) or 0)
    processing_posts = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == bot.id, Post.status == PostStatus.processing)) or 0)
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


@router.get("/app/robot/diagnose", include_in_schema=False)
def robot_diagnose(user: User = Depends(get_current_user), db=Depends(get_db)):
    """Diagnóstico rápido: verifica WP credentials + fontes antes de iniciar."""
    from fastapi.responses import JSONResponse
    import base64 as _b64
    bot = _get_or_create_single_bot(db, user=user)
    results = []

    # ── 1. WordPress ────────────────────────────────────────────
    wp_integ = db.scalar(select(Integration).where(Integration.profile_id == bot.id, Integration.type == IntegrationType.WORDPRESS))
    if not wp_integ:
        results.append({"key": "wordpress", "status": "err", "label": "WordPress não configurado",
                        "desc": "Nenhuma integração WordPress encontrada.",
                        "fix": f"Vá em <a href='/app/profiles/{bot.id}?tab=integracoes'>Integrações → WordPress</a> e adicione URL, usuário e App Password."})
    else:
        try:
            wp_creds = decrypt_json(wp_integ.credentials_encrypted)
            base_url = (wp_creds.get("base_url") or "").rstrip("/")
            users = wp_creds.get("users") if isinstance(wp_creds.get("users"), list) else []
            if not users and wp_creds.get("username"):
                users = [{"username": wp_creds["username"], "app_password": wp_creds.get("app_password", "")}]
            active_uname = str(wp_creds.get("active_username") or "")
            active_user = next((u for u in users if u.get("username") == active_uname), users[0] if users else None)

            if not base_url:
                results.append({"key": "wordpress", "status": "err", "label": "URL do site não informada",
                                 "desc": "O campo Base URL está vazio.",
                                 "fix": f"Vá em <a href='/app/profiles/{bot.id}?tab=integracoes'>Integrações → WordPress</a> e preencha a URL do site."})
            elif not active_user or not active_user.get("username") or not active_user.get("app_password"):
                results.append({"key": "wordpress", "status": "err", "label": "Usuário WordPress sem credenciais",
                                 "desc": "Usuário ou App Password estão vazios.",
                                 "fix": f"Vá em <a href='/app/profiles/{bot.id}?tab=integracoes'>Integrações → WordPress</a> e adicione o App Password."})
            else:
                # Testa conexão real com a API do WordPress
                import httpx as _httpx
                token = _b64.b64encode(f"{active_user['username']}:{active_user['app_password']}".encode()).decode()
                test_url = f"{base_url}/wp-json/wp/v2/users/me"
                try:
                    resp = _httpx.get(test_url, headers={"Authorization": f"Basic {token}"}, timeout=8, follow_redirects=True, verify=False)
                    if resp.status_code == 200:
                        data = resp.json()
                        display_name = data.get("name") or active_user["username"]
                        roles = data.get("roles") or []
                        if not any(r in roles for r in ("administrator", "editor", "author")):
                            results.append({"key": "wordpress", "status": "warn", "label": f"WordPress conectado — {display_name}",
                                             "desc": f"Usuário autenticado mas pode não ter permissão para publicar (role: {', '.join(roles) or 'desconhecido'}).",
                                             "fix": "Use um usuário com role <b>Administrator</b> ou <b>Editor</b>."})
                        else:
                            results.append({"key": "wordpress", "status": "ok", "label": f"WordPress OK — {display_name}",
                                             "desc": f"Conectado em <b>{base_url}</b> com role <b>{', '.join(roles)}</b>."})
                    elif resp.status_code == 401:
                        results.append({"key": "wordpress", "status": "err", "label": "Credenciais inválidas",
                                         "desc": f"O WordPress retornou 401 Unauthorized para o usuário <b>{active_user['username']}</b>.",
                                         "fix": f"Gere um novo App Password em <b>{base_url}/wp-admin/profile.php</b> e atualize em <a href='/app/profiles/{bot.id}?tab=integracoes'>Integrações</a>."})
                    elif resp.status_code == 403:
                        results.append({"key": "wordpress", "status": "err", "label": "Acesso bloqueado (403)",
                                         "desc": "O WordPress negou o acesso. A API REST pode estar desativada ou bloqueada por plugin de segurança.",
                                         "fix": "Verifique se a REST API está ativa. Plugins como Wordfence ou iThemes Security podem bloqueá-la."})
                    else:
                        results.append({"key": "wordpress", "status": "warn", "label": f"WordPress respondeu {resp.status_code}",
                                         "desc": f"Resposta inesperada de {base_url}.",
                                         "fix": "Verifique se a URL está correta e se o WordPress está online."})
                except Exception as e:
                    results.append({"key": "wordpress", "status": "err", "label": "WordPress inacessível",
                                     "desc": f"Não foi possível conectar em <b>{base_url}</b>: {str(e)[:120]}",
                                     "fix": "Verifique se a URL está correta e se o site está no ar."})
        except Exception as e:
            results.append({"key": "wordpress", "status": "err", "label": "Erro ao ler credenciais",
                             "desc": str(e)[:120], "fix": "Reconfigure a integração WordPress."})

    # ── 2. Fontes ───────────────────────────────────────────────
    sources = list(db.scalars(select(Source).where(Source.profile_id == bot.id, Source.active.is_(True))))
    if not sources:
        results.append({"key": "sources", "status": "err", "label": "Nenhuma fonte configurada",
                         "desc": "O robô precisa de ao menos uma fonte (URL, RSS ou Palavra-chave) para buscar conteúdo.",
                         "fix": f"Vá em <a href='/app/profiles/{bot.id}?tab=fontes'>Configurar → Fontes</a> e adicione uma fonte."})
    else:
        results.append({"key": "sources", "status": "ok", "label": f"{len(sources)} fonte{'s' if len(sources)!=1 else ''} configurada{'s' if len(sources)!=1 else ''}",
                         "desc": ", ".join(f"<b>{html.escape(s.value[:40])}</b>" for s in sources[:3]) + ("..." if len(sources) > 3 else "")})

    # ── 3. Gemini ───────────────────────────────────────────────
    gem = db.scalar(select(Integration).where(Integration.profile_id == bot.id, Integration.type == IntegrationType.GEMINI))
    if not gem:
        results.append({"key": "gemini", "status": "warn", "label": "Gemini não configurado",
                         "desc": "Sem IA configurada os posts não serão reescritos.",
                         "fix": f"Vá em <a href='/app/profiles/{bot.id}?tab=integracoes'>Integrações → Gemini</a> e adicione sua API Key gratuita."})
    else:
        results.append({"key": "gemini", "status": "ok", "label": "Gemini configurado", "desc": "IA pronta para reescrever os posts."})

    can_start = all(r["status"] != "err" for r in results)
    return JSONResponse({"results": results, "can_start": can_start})


@router.post("/app/robot/stop", include_in_schema=False)
def robot_stop(user: User = Depends(get_current_user), db=Depends(get_db)):
    bot = _get_or_create_single_bot(db, user=user)
    # Cancela todos os jobs em fila e em execução — marca como failed para parar o worker
    db.execute(
        update(Job)
        .where(Job.profile_id == bot.id, or_(Job.status == JobStatus.queued, Job.status == JobStatus.running))
        .values(status=JobStatus.failed, error="Parado manualmente pelo usuário")
    )
    # Volta posts em processamento para pendente
    db.execute(
        update(Post)
        .where(Post.profile_id == bot.id, Post.status == PostStatus.processing)
        .values(status=PostStatus.pending)
    )
    db.commit()
    return RedirectResponse(f"/app/robot?msg={quote_plus('Robô parado.')}", status_code=status.HTTP_302_FOUND)


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


@router.post("/app/robot/switch/{profile_id}", include_in_schema=False)
def robot_switch(profile_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    """Legacy: ativa exclusivamente um bot (mantido para compatibilidade)."""
    p = db.scalar(select(AutomationProfile).where(AutomationProfile.id == profile_id, AutomationProfile.user_id == user.id))
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    db.execute(update(AutomationProfile).where(AutomationProfile.user_id == user.id).values(active=False))
    db.flush()
    db.execute(update(AutomationProfile).where(AutomationProfile.id == profile_id).values(active=True))
    db.commit()
    return RedirectResponse("/app/robot", status_code=status.HTTP_302_FOUND)


@router.post("/app/robot/toggle/{profile_id}", include_in_schema=False)
def robot_toggle(profile_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    """Liga/desliga um bot. Permite até 3 ativos simultaneamente."""
    MAX_ACTIVE = 3
    p = db.scalar(select(AutomationProfile).where(AutomationProfile.id == profile_id, AutomationProfile.user_id == user.id))
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if p.active:
        p.active = False
        db.add(p); db.commit()
        return RedirectResponse("/app/robot", status_code=status.HTTP_302_FOUND)
    else:
        # Ligar: verifica limite de 3
        active_count = db.scalar(
            select(func.count()).select_from(AutomationProfile)
            .where(AutomationProfile.user_id == user.id, AutomationProfile.active.is_(True))
        ) or 0
        if active_count >= MAX_ACTIVE:
            return RedirectResponse(f"/app/robot?msg={quote_plus(f'Limite de {MAX_ACTIVE} robôs ativos atingido. Desligue um antes de ligar outro.')}", status_code=status.HTTP_302_FOUND)
        p.active = True
        db.add(p); db.commit()
        return RedirectResponse("/app/robot", status_code=status.HTTP_302_FOUND)


@router.post("/app/robot/delete/{profile_id}", include_in_schema=False)
def robot_delete(profile_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    p = db.scalar(select(AutomationProfile).where(AutomationProfile.id == profile_id, AutomationProfile.user_id == user.id))
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    was_active = p.active
    db.delete(p); db.commit()
    # Se era o ativo, ativa o próximo disponível
    if was_active:
        nxt = db.scalar(select(AutomationProfile).where(AutomationProfile.user_id == user.id).order_by(AutomationProfile.created_at.asc()).limit(1))
        if nxt:
            nxt.active = True; db.add(nxt); db.commit()
    return RedirectResponse("/app/robot?msg=Projeto+exclu%C3%ADdo", status_code=status.HTTP_302_FOUND)


@router.post("/app/robot/rename/{profile_id}", include_in_schema=False)
def robot_rename(profile_id: str, name: str = Form(...), user: User = Depends(get_current_user), db=Depends(get_db)):
    p = db.scalar(select(AutomationProfile).where(AutomationProfile.id == profile_id, AutomationProfile.user_id == user.id))
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    p.name = name.strip() or p.name
    db.add(p)
    db.commit()
    return RedirectResponse("/app/robot", status_code=status.HTTP_302_FOUND)


@router.get("/app/profiles", include_in_schema=False)
def profiles_page(user: User = Depends(get_current_user), db=Depends(get_db)):
    return RedirectResponse("/app/bot", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/create", include_in_schema=False)
def profiles_create(name: str = Form(...), active: str = Form("1"), user: User = Depends(get_current_user), db=Depends(get_db)):
    p = AutomationProfile(user_id=user.id, name=name.strip(), active=(active == "1"), schedule_config_json={}, anti_block_config_json={})
    db.add(p)
    db.commit()
    return RedirectResponse("/app/profiles", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/create-wizard", include_in_schema=False)
def profiles_create_wizard(
    name: str = Form(...),
    wp_base_url: str = Form(""),
    wp_username: str = Form(""),
    wp_app_password: str = Form(""),
    gemini_api_key: str = Form(""),
    gemini_model: str = Form("gemini-1.5-flash-latest"),
    emoji: str = Form("🤖"),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    safe_emoji = (emoji or "🤖").strip() or "🤖"
    p = AutomationProfile(user_id=user.id, name=name.strip(), active=True, schedule_config_json={}, anti_block_config_json={}, publish_config_json={"emoji": safe_emoji})
    db.add(p)
    db.flush()

    wp_url = (wp_base_url or "").strip().replace("/wp-admin/", "/").replace("/wp-admin", "").rstrip("/")
    wp_user = (wp_username or "").strip()
    wp_pass = (wp_app_password or "").strip()
    if wp_url and wp_user and wp_pass:
        wp_creds = {"base_url": wp_url, "users": [{"username": wp_user, "app_password": wp_pass}], "active_username": wp_user}
        db.add(Integration(user_id=p.user_id, profile_id=p.id, type=IntegrationType.WORDPRESS, name="WordPress", credentials_encrypted=encrypt_json(wp_creds)))

    gem_key = (gemini_api_key or "").strip()
    gem_mdl = (gemini_model or "gemini-1.5-flash-latest").strip()
    if gem_key:
        db.add(Integration(user_id=p.user_id, profile_id=p.id, type=IntegrationType.GEMINI, name="Gemini", credentials_encrypted=encrypt_json({"api_key": gem_key, "model": gem_mdl})))

    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}?tab=integracoes", status_code=status.HTTP_302_FOUND)


@router.get("/app/profiles/{profile_id}", include_in_schema=False)
def profile_detail(profile_id: str, request: Request, user: User = Depends(get_current_user), db=Depends(get_db)):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    _ensure_default_recipe_actions(db, bot=p)
    tab = (request.query_params.get("tab") or "integracoes").strip().lower()
    tabs = [
        ("integracoes", "Integrações"),
        ("fontes",      "Fontes"),
        ("ia",          "IA"),
        ("publicacao",  "Publicação"),
        ("agendamento", "Agendamento"),
        ("posts",       "Posts"),
    ]
    _tab_label = dict(tabs).get(tab, tab)
    body = f"""
    {_ph("banner-projeto-configurar")}
    <div class="active-project-banner" style="margin-bottom:14px">
      <div>
        <div class="active-project-label">Projeto</div>
        <div class="active-project-name">{html.escape(p.name)}</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <span class="active-project-stat">Configurando: <b>{html.escape(_tab_label)}</b></span>
        <a href="/app/robot" class="btn secondary" style="font-size:13px;padding:7px 14px">← Voltar ao Robô</a>
      </div>
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
        {_ph("tab-fontes")}
        <div class="card" style="margin-bottom:14px">
          <details class="toggle-section" open>
            <summary><span class="ts-title">Adicionar Fonte</span><span class="ts-arrow">▶</span></summary>
            <div class="ts-body">
              {_ph("form-adicionar-fonte")}
              <div class="row">
                <div class="col card">
                  <form method="post" action="/app/profiles/{p.id}/sources/create">
                    {_ph("select-tipo-fonte")}
                    <label>Tipo</label>
                    <select name="type">
                      <option value="URL">URL</option>
                      <option value="RSS">RSS</option>
                      <option value="KEYWORD">PALAVRA-CHAVE</option>
                    </select>
                    <label style="margin-top:8px">Valor</label>
                    {_ph("input-valor-fonte")}
                    <input name="value" required />
                    <div style="margin-top:12px">{_ph("btn-salvar-fonte")}<button class="btn" type="submit">Salvar</button></div>
                  </form>
                </div>
              </div>
            </div>
          </details>
        </div>
        <div class="card">
          <details class="toggle-section" open>
            <summary><span class="ts-title">Fontes Cadastradas <span class="ts-badge">{len(sources)}</span></span><span class="ts-arrow">▶</span></summary>
            <div class="ts-body">
              {_ph("tabela-fontes-cadastradas")}
              <div class="scrollbox">
                <table id="sources-table"><thead><tr><th>Tipo</th><th>Valor</th><th></th></tr></thead><tbody>{rows}</tbody></table>
              </div>
            </div>
          </details>
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
        {_ph("tab-publicacao")}
        <div class="card">
          <details class="toggle-section" open>
            <summary><span class="ts-title">Publicação</span><span class="ts-arrow">▶</span></summary>
            <div class="ts-body">
          <div class="row">
            <div class="col card">
              {_ph("publicacao-wordpress")}
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
              {_ph("publicacao-facebook")}
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
          </details>
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
                f"<tr style='border-top:1px solid var(--border)'>"
                f"<td style='padding:12px 18px;font-size:13px;font-weight:600'>{html.escape(nm) or '—'}</td>"
                f"<td style='padding:12px 18px;font-size:13px;color:var(--muted);font-family:monospace'>{html.escape(pid)}</td>"
                f"<td style='padding:12px 18px'><span class='pill'>{html.escape(token_state)}</span></td>"
                f"<td style='padding:12px 18px;text-align:right'><form method='post' action='/app/profiles/{p.id}/integrations/facebook/pages/remove' style='margin:0'>"
                f"<input type='hidden' name='page_id' value='{html.escape(pid)}' />"
                f"<button class='btn secondary' type='submit' style='font-size:12px;padding:5px 12px;color:#ef4444'>Remover</button></form></td></tr>"
            )
        if not fb_rows:
            fb_rows = "<tr><td colspan='4' style='padding:20px 18px;text-align:center;color:var(--muted);font-size:13px'>Nenhuma página cadastrada.</td></tr>"
        # Monta linhas da tabela Conexões com URL extraída dos dados cifrados
        conn_rows = ""
        for i in integrations:
            try:
                icreds = decrypt_json(i.credentials_encrypted)
            except Exception:
                icreds = {}
            if i.type == IntegrationType.WORDPRESS:
                conn_url = str(icreds.get("base_url") or "—")
            elif i.type == IntegrationType.FACEBOOK:
                pages_list = icreds.get("pages") or []
                conn_url = f"{len(pages_list)} página(s)" if pages_list else "—"
            elif i.type == IntegrationType.GEMINI:
                model_name = str(icreds.get("model") or "gemini-1.5-flash-latest")
                conn_url = model_name
            else:
                conn_url = i.name
            status_color = "#10b981" if i.status.value == "CONNECTED" else "#f59e0b"
            conn_rows += (
                f"<tr style='border-top:1px solid var(--border)'>"
                f"<td style='padding:12px 18px'><span class='pill'>{html.escape(i.type.value)}</span></td>"
                f"<td style='padding:12px 18px;font-size:13px;word-break:break-all;max-width:260px'>{html.escape(conn_url)}</td>"
                f"<td style='padding:12px 18px'><span style='color:{status_color};font-size:12px;font-weight:700'>{html.escape(i.status.value)}</span></td>"
                f"<td style='padding:12px 18px;text-align:right'><form method='post' action='/app/profiles/{p.id}/integrations/{i.id}/delete' style='margin:0'>"
                f"<button class='btn secondary' style='font-size:12px;padding:5px 12px;color:#ef4444' type='submit'>"
                f"<svg width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' style='margin-right:4px'>"
                f"<polyline points='3 6 5 6 21 6'/><path d='M19 6l-1 14H6L5 6'/><path d='M10 11v6'/><path d='M14 11v6'/><path d='M9 6V4h6v2'/>"
                f"</svg>Excluir</button></form></td></tr>"
            )
        if not conn_rows:
            conn_rows = "<tr><td colspan='4' style='padding:20px 18px;text-align:center;color:var(--muted);font-size:13px'>Nenhuma conexão cadastrada.</td></tr>"

        # Busca dados da integração WordPress para mostrar lista de usuários
        wp_integ = db.scalar(select(Integration).where(Integration.profile_id == p.id, Integration.type == IntegrationType.WORDPRESS))
        wp_base_url = ""
        wp_users: list[dict] = []
        wp_active_username = ""
        if wp_integ:
            try:
                wp_creds = decrypt_json(wp_integ.credentials_encrypted)
                wp_base_url = str(wp_creds.get("base_url") or "")
                wp_active_username = str(wp_creds.get("active_username") or "")
                raw_users = wp_creds.get("users") if isinstance(wp_creds.get("users"), list) else []
                if not raw_users and wp_creds.get("username"):
                    raw_users = [{"username": wp_creds["username"], "app_password": wp_creds.get("app_password", "")}]
                    wp_active_username = wp_creds["username"]
                wp_users = raw_users
            except Exception:
                wp_users = []

        wp_user_rows = ""
        for _wu_idx, wu in enumerate(wp_users):
            uname = html.escape(str(wu.get("username") or ""))
            raw_pass = html.escape(str(wu.get("app_password") or ""), quote=True)
            is_active_wu = (wu.get("username") == wp_active_username)
            _pid = f"wpp-{p.id}-{_wu_idx}"
            status_badge = "<span class='badge-active' style='font-size:11px;padding:3px 8px'><span class='dot-pulse'></span>Em uso</span>" if is_active_wu else "<span class='badge-inactive' style='font-size:11px;padding:3px 8px'><span class='dot-off'></span>Inativo</span>"
            usar_btn = (
                "<span style='font-size:11px;color:var(--muted)'>—</span>"
                if is_active_wu else
                f"<form method='post' action='/app/profiles/{p.id}/integrations/wordpress/set-active-user' style='margin:0'>"
                f"<input type='hidden' name='username' value='{uname}' />"
                f"<button class='btn' style='font-size:12px;padding:4px 12px' type='submit'>Usar</button></form>"
            )
            del_btn = (
                "" if is_active_wu else
                f"<form method='post' action='/app/profiles/{p.id}/integrations/wordpress/remove-user' style='margin:0'>"
                f"<input type='hidden' name='username' value='{uname}' />"
                f"<button class='btn secondary' style='font-size:12px;padding:4px 10px;color:#ef4444' type='submit'>"
                f"<svg width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2'>"
                f"<polyline points='3 6 5 6 21 6'/><path d='M19 6l-1 14H6L5 6'/><path d='M10 11v6'/><path d='M14 11v6'/><path d='M9 6V4h6v2'/>"
                f"</svg></button></form>"
            )
            pass_cell = (
                f"<div style='display:flex;align-items:center;gap:5px'>"
                f"<span id='{_pid}' data-pass='{raw_pass}' data-shown='0' "
                f"style='font-family:monospace;font-size:12px;color:var(--muted);letter-spacing:1px'>••••••••</span>"
                f"<button type='button' id='{_pid}-btn' "
                f"onclick=\"var s=document.getElementById('{_pid}');var shown=s.dataset.shown==='1';"
                f"s.textContent=shown?'••••••••':s.dataset.pass;s.dataset.shown=shown?'0':'1';"
                f"document.getElementById('{_pid}-btn').innerHTML=shown?'<svg width=\\'13\\'height=\\'13\\'viewBox=\\'0 0 24 24\\'fill=\\'none\\'stroke=\\'currentColor\\'stroke-width=\\'2\\'><path d=\\'M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z\\'/><circle cx=\\'12\\'cy=\\'12\\'r=\\'3\\'/></svg>':'<svg width=\\'13\\'height=\\'13\\'viewBox=\\'0 0 24 24\\'fill=\\'none\\'stroke=\\'currentColor\\'stroke-width=\\'2\\'><path d=\\'M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24\\'/><line x1=\\'1\\'y1=\\'1\\'x2=\\'23\\'y2=\\'23\\'/></svg>'\" "
                f"style='background:none;border:none;cursor:pointer;color:var(--muted);padding:2px;display:flex;align-items:center'>"
                f"<svg width='13' height='13' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2'>"
                f"<path d='M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z'/><circle cx='12' cy='12' r='3'/></svg>"
                f"</button></div>"
            )
            wp_user_rows += (
                f"<tr style='border-top:1px solid var(--border)'>"
                f"<td style='padding:13px 18px'><span style='font-size:14px;font-weight:600'>{uname}</span></td>"
                f"<td style='padding:13px 18px'>{status_badge}</td>"
                f"<td style='padding:13px 18px'>{pass_cell}</td>"
                f"<td style='padding:13px 18px;text-align:right'><div style='display:flex;gap:8px;align-items:center;justify-content:flex-end'>{usar_btn}{del_btn}</div></td>"
                f"</tr>"
            )
        if not wp_user_rows:
            wp_user_rows = "<tr><td colspan='4' style='padding:20px 18px;text-align:center;color:var(--muted);font-size:13px'>Nenhum usuário cadastrado.</td></tr>"

        wp_base_field = "" if wp_integ else """
            <label>Base URL do Site</label>
            <input name="base_url" placeholder="https://seusite.com" required />"""
        wp_base_info = (
            f"<div style='margin-bottom:14px;padding:8px 12px;background:var(--input-bg);border-radius:8px;border:1px solid var(--border);display:flex;align-items:center;gap:10px'>"
            f"<svg width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='var(--primary)' stroke-width='2'><circle cx='12' cy='12' r='10'/><path d='M2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20'/></svg>"
            f"<div><span style='font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px'>Site</span>"
            f"<div style='font-size:13px;font-weight:600'>{html.escape(wp_base_url)}</div></div></div>"
        ) if wp_integ and wp_base_url else ""

        # Gemini integration
        gem_integ = db.scalar(select(Integration).where(Integration.profile_id == p.id, Integration.type == IntegrationType.GEMINI))
        gem_current_model = "gemini-1.5-flash-latest"
        gem_configured = False
        if gem_integ:
            try:
                gem_creds = decrypt_json(gem_integ.credentials_encrypted)
                gem_current_model = str(gem_creds.get("model") or "gemini-1.5-flash-latest").strip() or "gemini-1.5-flash-latest"
                gem_configured = bool(gem_creds.get("api_key"))
            except Exception:
                pass

        # Aba ativa dentro de integrações (via query param itab)
        itab = (request.query_params.get("itab") or "wordpress").strip().lower()

        _itab_icons = {
            "wordpress": "<svg width='16' height='16' viewBox='0 0 24 24' fill='currentColor'><path d='M12 2C6.486 2 2 6.486 2 12s4.486 10 10 10 10-4.486 10-10S17.514 2 12 2zM3.251 12c0-1.308.265-2.556.741-3.695L7.36 18.658A8.762 8.762 0 0 1 3.251 12zm8.749 8.75a8.773 8.773 0 0 1-2.496-.364l2.65-7.695 2.716 7.44a.96.96 0 0 0 .07.136 8.764 8.764 0 0 1-2.94.483zm1.211-12.981c.528-.028.999-.084.999-.084.47-.056.415-.748-.056-.72 0 0-1.415.111-2.329.111-.858 0-2.3-.111-2.3-.111-.47-.028-.526.692-.055.72 0 0 .444.056.914.084l1.358 3.72-1.908 5.721-3.176-8.441c.528-.028 1-.084 1-.084.47-.056.415-.748-.056-.72 0 0-1.415.111-2.329.111a12.65 12.65 0 0 1-.31-.005A8.752 8.752 0 0 1 12 3.25c2.294 0 4.389.879 5.963 2.315a2.885 2.885 0 0 0-.19-.013c-.858 0-1.468.748-1.468 1.551 0 .72.415 1.329.859 2.049.332.581.719 1.329.719 2.409 0 .748-.287 1.617-.663 2.825l-.871 2.907-3.138-9.534zm3.64 11.791-.012-.025 2.733-7.897c.51-1.274.68-2.293.68-3.199 0-.329-.021-.634-.059-.921A8.751 8.751 0 0 1 20.75 12c0 3.216-1.731 6.031-4.319 7.56l.42-1z'/></svg>",
            "gemini": "<svg width='16' height='16' viewBox='0 0 24 24' fill='currentColor'><path d='M12 2l2.4 7.4H22l-6.3 4.6 2.4 7.4L12 17l-6.1 4.4 2.4-7.4L2 9.4h7.6z' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linejoin='round'/><path d='M12 2C10.5 7 8 9.5 2 12c6 2.5 8.5 5 10 10 1.5-5 4-7.5 10-10-6-2.5-8.5-5-10-10z'/></svg>",
            "facebook": "<svg width='16' height='16' viewBox='0 0 24 24' fill='currentColor'><path d='M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z'/></svg>",
            "conexoes": "<svg width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71'/><path d='M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71'/></svg>",
        }
        itabs = [("wordpress", "WordPress"), ("gemini", "Gemini"), ("facebook", "Facebook"), ("conexoes", "Conexões")]

        def itab_btn(key, label):
            is_active = itab == key
            active_style = "background:var(--primary);color:#fff;border-color:var(--primary)" if is_active else "background:transparent;color:var(--muted)"
            icon = _itab_icons.get(key, "")
            return (f"<a href='?tab=integracoes&itab={key}' style='display:inline-flex;align-items:center;gap:6px;padding:7px 18px;border-radius:8px;font-size:13px;font-weight:600;"
                    f"border:1px solid var(--border);text-decoration:none;transition:all .2s;{active_style}'>{icon}{label}</a>")

        itab_nav = "<div style='display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin-bottom:24px'>" + "".join(itab_btn(k, l) for k, l in itabs) + "</div>"

        # Conteúdo de cada aba interna
        if itab == "wordpress":
            itab_content = f"""
            {_ph("wp-info-site")}
            {wp_base_info}
            {_ph("tabela-usuarios-wp")}
            <div style="border:1px solid var(--border);border-radius:14px;overflow:hidden;margin-bottom:20px">
              <table style="width:100%;border-collapse:collapse">
                <thead>
                  <tr style="background:var(--surface2)">
                    <th style="padding:12px 18px;text-align:left;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted);width:28%">Usuário</th>
                    <th style="padding:12px 18px;text-align:left;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted);width:18%">Status</th>
                    <th style="padding:12px 18px;text-align:left;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted);width:30%">App Password</th>
                    <th style="padding:12px 18px;text-align:right;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted);width:24%">Ações</th>
                  </tr>
                </thead>
                <tbody>{wp_user_rows}</tbody>
              </table>
            </div>
            <details>
              <summary style="cursor:pointer;font-size:13px;font-weight:600;color:var(--primary);user-select:none;padding:10px 0;display:flex;align-items:center;gap:6px">
                <span style="font-size:16px">{"⊕" if wp_integ else "⊕"}</span>
                {"Adicionar usuário" if wp_integ else "Configurar WordPress"}
              </summary>
              <div style="padding:20px;background:var(--surface2);border:1px solid var(--border);border-radius:14px;margin-top:10px">
                <form method="post" action="/app/profiles/{p.id}/integrations/wordpress">
                  {wp_base_field}
                  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:4px">
                    <div>
                      <label>Usuário WordPress</label>
                      <input name="username" placeholder="Ex: admin" required />
                    </div>
                    <div>
                      <label>App Password</label>
                      <input name="app_password" type="password" placeholder="xxxx xxxx xxxx xxxx" required />
                    </div>
                  </div>
                  <div style="margin-top:16px"><button class="btn" type="submit">Salvar usuário</button></div>
                </form>
              </div>
            </details>"""
        elif itab == "gemini":
            _gem_models = [
                ("gemini-2.0-flash",       "gemini-2.0-flash — Rápido, mais recente ⚡"),
                ("gemini-2.0-flash-lite",  "gemini-2.0-flash-lite — Leve e econômico 🪶"),
                ("gemini-1.5-flash-latest","gemini-1.5-flash-latest — Padrão recomendado ✅"),
                ("gemini-1.5-flash-8b",    "gemini-1.5-flash-8b — Ultra rápido, menor 🏎"),
                ("gemini-1.5-pro-latest",  "gemini-1.5-pro-latest — Mais inteligente, mais lento 🧠"),
                ("gemini-2.0-pro-exp",     "gemini-2.0-pro-exp — Experimental, Pro 2.0 🔬"),
            ]
            _gem_model_opts = "".join(
                f"<option value='{v}' {'selected' if v == gem_current_model else ''}>{html.escape(l)}</option>"
                for v, l in _gem_models
            )
            # Masked API key for display
            if gem_configured and gem_integ:
                try:
                    _raw_key = decrypt_json(gem_integ.credentials_encrypted).get("api_key", "")
                    _masked_key = (_raw_key[:8] + "•" * min(16, max(4, len(_raw_key) - 8))) if len(_raw_key) > 8 else "••••••••"
                except Exception:
                    _masked_key = "••••••••"
                _gem_integ_id = str(gem_integ.id)
                _gem_integ_status = str(gem_integ.status.value) if gem_integ.status else "UNKNOWN"
                _status_color = "#10b981" if _gem_integ_status == "CONNECTED" else "#f59e0b"
                _gem_list = f"""
                <div style="border:1px solid var(--border2);border-radius:14px;overflow:hidden;margin-bottom:24px">
                  <div style="background:var(--surface2);padding:12px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
                    <div style="display:flex;align-items:center;gap:10px">
                      <span style="font-size:20px">✨</span>
                      <div>
                        <div style="font-size:13px;font-weight:700;color:var(--text)">Gemini AI</div>
                        <div style="font-size:11px;color:var(--muted)">Integração configurada</div>
                      </div>
                    </div>
                    <span style="color:{_status_color};font-size:12px;font-weight:700;background:{'rgba(16,185,129,.1)' if _gem_integ_status=='CONNECTED' else 'rgba(245,158,11,.1)'};border:1px solid {'rgba(16,185,129,.3)' if _gem_integ_status=='CONNECTED' else 'rgba(245,158,11,.3)'};padding:3px 10px;border-radius:20px">{html.escape(_gem_integ_status)}</span>
                  </div>
                  <div style="padding:18px 20px;display:grid;grid-template-columns:1fr 1fr;gap:16px">
                    <div>
                      <div style="font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted);margin-bottom:5px">Chave API</div>
                      <div style="font-family:monospace;font-size:13px;color:var(--text);background:var(--input-bg);border:1px solid var(--border);border-radius:8px;padding:7px 12px;letter-spacing:.5px">{html.escape(_masked_key)}</div>
                    </div>
                    <div>
                      <div style="font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted);margin-bottom:5px">Modelo</div>
                      <div style="font-size:13px;font-weight:600;background:var(--input-bg);border:1px solid var(--border);border-radius:8px;padding:7px 12px">{html.escape(gem_current_model)}</div>
                    </div>
                    <div>
                      <div style="font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted);margin-bottom:5px">Bot</div>
                      <div style="font-size:13px;font-weight:600;padding:7px 0">{html.escape(p.name)}</div>
                    </div>
                    <div style="display:flex;align-items:flex-end;gap:8px;padding-bottom:2px">
                      <button type="button"
                        onclick="var f=document.getElementById('gem-edit-form');f.style.display=f.style.display==='none'?'block':'none'"
                        class="btn secondary" style="flex:1;justify-content:center">✏️ Editar</button>
                      <form method="post" action="/app/profiles/{p.id}/integrations/{_gem_integ_id}/delete" style="margin:0;flex:1">
                        <button class="btn secondary" type="submit" style="width:100%;justify-content:center;color:#ef4444"
                          onclick="return confirm('Remover integração Gemini?')">
                          <svg width='13' height='13' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' style='margin-right:4px'>
                            <polyline points='3 6 5 6 21 6'/><path d='M19 6l-1 14H6L5 6'/><path d='M10 11v6'/><path d='M14 11v6'/><path d='M9 6V4h6v2'/>
                          </svg> Excluir
                        </button>
                      </form>
                    </div>
                  </div>
                </div>"""
                _gem_form_display = "display:none"
                _gem_form_label = "✏️ Editar chave / modelo"
            else:
                _gem_list = ""
                _gem_form_display = "display:block"
                _gem_form_label = "Configurar Gemini"

            itab_content = f"""
            {_ph("form-gemini-api-key")}
            {_gem_list}
            <div id="gem-edit-form" style="{_gem_form_display};background:var(--surface2);border:1px solid var(--border);border-radius:14px;padding:24px">
              <div style="font-size:15px;font-weight:700;color:var(--text);margin-bottom:16px">{_gem_form_label}</div>
              <div style="background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:20px;font-size:13px;color:var(--muted);line-height:1.9">
                <div style="font-weight:700;color:var(--text);margin-bottom:4px">Como obter a API Key (gratuito)</div>
                <div>1. Acesse &nbsp;<a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener"
                  style="color:var(--primary);font-weight:600;text-decoration:underline">aistudio.google.com/apikey</a></div>
                <div>2. Faça login com sua conta Google</div>
                <div>3. Clique em <b>Create API Key</b></div>
                <div>4. Copie a chave gerada e cole no campo abaixo</div>
              </div>
              <form method="post" action="/app/profiles/{p.id}/integrations/gemini">
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
                  <div style="grid-column:1/-1">
                    <label>Gemini API Key</label>
                    <input name="api_key" type="password" placeholder="AIzaSy..." required
                      style="font-family:monospace;letter-spacing:.5px" />
                  </div>
                  <div style="grid-column:1/-1">
                    <label>Modelo</label>
                    <select name="model" style="width:100%;padding:10px 13px;border-radius:9px;border:1px solid var(--border);background:var(--input-bg);color:var(--text);font-size:13px;cursor:pointer">
                      {_gem_model_opts}
                    </select>
                    <div style="margin-top:6px;font-size:11px;color:var(--muted)">Dúvida? Deixe <b>gemini-1.5-flash-latest</b> — bom para a maioria dos casos.</div>
                  </div>
                </div>
                <div style="margin-top:20px;display:flex;gap:10px">
                  <button class="btn" type="submit">Salvar</button>
                  {"<button type='button' class='btn secondary' onclick=\"document.getElementById('gem-edit-form').style.display='none'\">Cancelar</button>" if gem_configured else ""}
                </div>
              </form>
            </div>"""
        elif itab == "facebook":
            itab_content = f"""
            <div style="background:var(--surface2);border:1px solid var(--border);border-radius:14px;padding:24px;margin-bottom:24px">
              <div style="font-size:15px;font-weight:700;color:var(--text);margin-bottom:16px">Adicionar página do Facebook</div>
              <form method="post" action="/app/profiles/{p.id}/integrations/facebook/pages/add">
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
                  <div>
                    <label>Nome (opcional)</label>
                    <input name="name" placeholder="Ex: Minha Página" />
                  </div>
                  <div>
                    <label>Page ID</label>
                    <input name="page_id" placeholder="Ex: 1234567890" required />
                  </div>
                  <div style="grid-column:1/-1">
                    <label>Page Access Token</label>
                    <input name="access_token" type="password" placeholder="Cole o token da página" required />
                  </div>
                </div>
                <div style="margin-top:16px"><button class="btn" type="submit">Adicionar página</button></div>
              </form>
            </div>
            <div style="font-size:12px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;color:var(--muted);margin-bottom:10px">Páginas cadastradas</div>
            <div style="border:1px solid var(--border);border-radius:14px;overflow:hidden">
              <table style="width:100%;border-collapse:collapse">
                <thead>
                  <tr style="background:var(--surface2)">
                    <th style="padding:11px 18px;text-align:left;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">Nome</th>
                    <th style="padding:11px 18px;text-align:left;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">Page ID</th>
                    <th style="padding:11px 18px;text-align:left;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">Token</th>
                    <th style="padding:11px 18px;text-align:right;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">Ações</th>
                  </tr>
                </thead>
                <tbody>{fb_rows}</tbody>
              </table>
            </div>"""
        else:  # conexoes
            itab_content = f"""
            <div style="border:1px solid var(--border);border-radius:14px;overflow:hidden">
              <table style="width:100%;border-collapse:collapse">
                <thead>
                  <tr style="background:var(--surface2)">
                    <th style="padding:11px 18px;text-align:left;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">Tipo</th>
                    <th style="padding:11px 18px;text-align:left;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">URL / Detalhe</th>
                    <th style="padding:11px 18px;text-align:left;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">Status</th>
                    <th style="padding:11px 18px;text-align:right;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">Ações</th>
                  </tr>
                </thead>
                <tbody>{conn_rows}</tbody>
              </table>
            </div>"""

        body += f"""
        {_ph("tab-integracoes")}
        <div class="card">
          <details class="toggle-section" open>
            <summary><span class="ts-title">Integrações</span><span class="ts-arrow">▶</span></summary>
            <div class="ts-body">
              {_ph("abas-internas-integracoes")}
              {itab_nav}
              {_ph(f"conteudo-aba-{itab}")}
              {itab_content}
            </div>
          </details>
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
        {_ph("tab-agendamento")}
        <div class="card">
          <details class="toggle-section" open>
            <summary><span class="ts-title">Agendamento</span><span class="ts-arrow">▶</span></summary>
            <div class="ts-body">
              {_ph("form-agendamento")}
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
          </details>
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
        {_ph("tab-ia-comandos")}
        <div class="card">
          <details class="toggle-section" open>
            <summary><span class="ts-title">Comandos da IA</span><span class="ts-arrow">▶</span></summary>
            <div class="ts-body">
              {_ph("form-prompts-ia")}
              <p class="muted">Você pode editar o comando do site e do Facebook quando quiser.</p>
              <form method="post" action="/app/profiles/{p.id}/ai-prompts">
                <div class="row">
                  <div class="col card">
                    {_ph("prompt-wordpress")}
                    <h4>Site (WordPress)</h4>
                    <textarea name="site_prompt" placeholder="Cole o comando do site aqui">{html.escape(site_prompt)}</textarea>
                  </div>
                  <div class="col card">
                    {_ph("prompt-facebook")}
                    <h4>Facebook</h4>
                    <textarea name="facebook_prompt" placeholder="Cole o comando do Facebook aqui">{html.escape(fb_prompt)}</textarea>
                  </div>
                </div>
                <div style="margin-top:12px"><button class="btn" type="submit">Salvar</button></div>
              </form>
            </div>
          </details>
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
        pending_rows = completed_rows = failed_rows = ""
        n_completed = n_failed = n_pending = 0
        for post, title in posts:
            when = _fmt_dt(post.published_at or post.created_at, user=user)
            is_canceled = isinstance(post.outputs_json, dict) and bool(post.outputs_json.get("canceled_by_user"))
            st = "cancelado" if (post.status == PostStatus.failed and is_canceled) else post.status.value
            safe_title = html.escape(str(title or "Sem título"))
            wp_url = post.wp_url or ""
            chk = f"<input type='checkbox' name='post_id' value='{html.escape(post.id)}' style='width:15px;height:15px;cursor:pointer'/>"

            if post.status in (PostStatus.pending, PostStatus.processing):
                n_pending += 1
                icon = "⚙️" if post.status == PostStatus.processing else "📝"
                st_badge = f"<span style='font-size:11px;font-weight:700;color:{'#f59e0b' if post.status==PostStatus.processing else 'var(--muted)'};text-transform:uppercase;letter-spacing:.4px'>{html.escape(st)}</span>"
                pending_rows += (
                    f"<tr style='border-top:1px solid var(--border)'>"
                    f"<td style='padding:10px 14px;width:36px'>{chk}</td>"
                    f"<td style='padding:10px 14px;font-size:13px'>{icon} {safe_title}</td>"
                    f"<td style='padding:10px 14px'>{st_badge}</td>"
                    f"<td style='padding:10px 14px;font-size:12px;color:var(--muted);white-space:nowrap'>{html.escape(when)}</td>"
                    f"<td style='padding:10px 14px;font-size:12px;color:var(--muted)'>—</td>"
                    f"</tr>"
                )
            elif post.status == PostStatus.completed:
                n_completed += 1
                wp_link = (f"<a href='{html.escape(wp_url)}' target='_blank' rel='noopener' "
                           f"style='display:inline-flex;align-items:center;gap:4px;color:#10b981;font-size:12px;font-weight:600;text-decoration:none'>"
                           f"🔗 Ver post</a>") if wp_url else "<span style='color:var(--muted);font-size:12px'>—</span>"
                completed_rows += (
                    f"<tr style='border-top:1px solid rgba(16,185,129,.15);border-left:3px solid #10b981;background:rgba(16,185,129,.04)'>"
                    f"<td style='padding:11px 14px;width:36px'>{chk}</td>"
                    f"<td style='padding:11px 14px'>"
                    f"  <div style='display:flex;align-items:center;gap:8px'>"
                    f"    <span style='width:22px;height:22px;border-radius:50%;background:#10b981;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:11px'>✓</span>"
                    f"    <span style='font-size:13px;font-weight:600;color:var(--text)'>{safe_title}</span>"
                    f"  </div>"
                    f"</td>"
                    f"<td style='padding:11px 14px'><span style='color:#10b981;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;background:rgba(16,185,129,.12);padding:3px 8px;border-radius:20px'>✓ Publicado</span></td>"
                    f"<td style='padding:11px 14px;font-size:12px;color:var(--muted);white-space:nowrap'>{html.escape(when)}</td>"
                    f"<td style='padding:11px 14px'>{wp_link}</td>"
                    f"</tr>"
                )
            else:
                n_failed += 1
                err_msg = ""
                if isinstance(post.outputs_json, dict):
                    err_msg = str(post.outputs_json.get("error") or "")[:80]
                failed_rows += (
                    f"<tr style='border-top:1px solid rgba(239,68,68,.15);border-left:3px solid #ef4444;background:rgba(239,68,68,.04)'>"
                    f"<td style='padding:11px 14px;width:36px'>{chk}</td>"
                    f"<td style='padding:11px 14px'>"
                    f"  <div>"
                    f"    <div style='display:flex;align-items:center;gap:8px'>"
                    f"      <span style='width:22px;height:22px;border-radius:50%;background:#ef4444;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:11px;color:#fff'>✕</span>"
                    f"      <span style='font-size:13px;font-weight:600;color:var(--text)'>{safe_title}</span>"
                    f"    </div>"
                    f"    {'<div style=\"font-size:11px;color:#ef4444;margin-top:3px;padding-left:30px\">'+html.escape(err_msg)+'</div>' if err_msg else ''}"
                    f"  </div>"
                    f"</td>"
                    f"<td style='padding:11px 14px'><span style='color:#ef4444;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;background:rgba(239,68,68,.12);padding:3px 8px;border-radius:20px'>{'Cancelado' if is_canceled else 'Erro'}</span></td>"
                    f"<td style='padding:11px 14px;font-size:12px;color:var(--muted);white-space:nowrap'>{html.escape(when)}</td>"
                    f"<td style='padding:11px 14px;font-size:12px;color:var(--muted)'>—</td>"
                    f"</tr>"
                )

        def _posts_table(tid, rows, empty_msg):
            thead = ("<tr style='background:var(--surface2)'>"
                     "<th style='padding:10px 14px;width:36px'></th>"
                     "<th style='padding:10px 14px;text-align:left;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)'>Título</th>"
                     "<th style='padding:10px 14px;text-align:left;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)'>Status</th>"
                     "<th style='padding:10px 14px;text-align:left;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)'>Quando</th>"
                     "<th style='padding:10px 14px;text-align:left;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)'>Link</th>"
                     "</tr>")
            body_rows = rows or f"<tr><td colspan='5' style='padding:20px;text-align:center;color:var(--muted);font-size:13px'>{empty_msg}</td></tr>"
            return (f"<div style='border:1px solid var(--border);border-radius:12px;overflow:hidden'>"
                    f"<table id='{tid}' style='width:100%;border-collapse:collapse'>"
                    f"<thead>{thead}</thead><tbody>{body_rows}</tbody></table></div>")

        body += f"""
        {_ph("tab-posts-gerenciar")}
        <div class="card" style="margin-bottom:14px">
          <details class="toggle-section" open>
            <summary><span class="ts-title">Ações em Massa</span><span class="ts-arrow">▶</span></summary>
            <div class="ts-body">
              {_ph("acoes-bulk-posts")}
              <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
                <form method="post" action="/app/profiles/{p.id}/posts/cancel-all" style="margin:0">
                  <button class="btn secondary" type="submit" style="font-size:12px;padding:6px 12px">Cancelar pendentes</button>
                </form>
                <form method="post" action="/app/profiles/{p.id}/posts/delete-completed" style="margin:0">
                  <button class="btn secondary" type="submit" style="font-size:12px;padding:6px 12px">Apagar publicados</button>
                </form>
              </div>
              <p class="muted" style="font-size:12px;margin:0">Cancelar = para a fila (não apaga do WordPress). Apagar = remove do PostHub.</p>
            </div>
          </details>
        </div>

        <div class="card" style="margin-bottom:14px">
          <details class="toggle-section" open>
            <summary>
              <span class="ts-title">
                <span style="width:22px;height:22px;border-radius:6px;background:rgba(16,185,129,.15);display:inline-flex;align-items:center;justify-content:center;font-size:12px">✓</span>
                Publicados <span class="ts-badge" style="color:#10b981;border-color:rgba(16,185,129,.3)">{n_completed}</span>
              </span>
              <span class="ts-arrow">▶</span>
            </summary>
            <div class="ts-body">
              <div style="display:flex;justify-content:flex-end;margin-bottom:10px">
                <button class="btn secondary" style="font-size:12px;padding:5px 12px" type="button" onclick="clearBox('#posts-completed-table tbody')">Limpar lista</button>
              </div>
              <form method="post" action="/app/profiles/{p.id}/posts/bulk">
                {_posts_table("posts-completed-table", completed_rows, "Nenhum post publicado ainda.")}
                <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">
                  <button class="btn secondary" type="submit" name="mode" value="delete" style="font-size:12px">Excluir selecionados (PostHub)</button>
                  <button class="btn secondary" type="submit" name="mode" value="delete_wp" style="font-size:12px;color:#ef4444"
                    onclick="return confirm('Tem certeza que quer APAGAR do site (WordPress)?')">Apagar do WordPress</button>
                </div>
              </form>
            </div>
          </details>
        </div>

        <div class="card" style="margin-bottom:14px">
          <details class="toggle-section" open>
            <summary>
              <span class="ts-title">
                <span style="width:22px;height:22px;border-radius:6px;background:rgba(245,158,11,.15);display:inline-flex;align-items:center;justify-content:center;font-size:12px">⏳</span>
                Pendentes / Processando <span class="ts-badge" style="color:#f59e0b;border-color:rgba(245,158,11,.3)">{n_pending}</span>
              </span>
              <span class="ts-arrow">▶</span>
            </summary>
            <div class="ts-body">
              <div style="display:flex;justify-content:flex-end;margin-bottom:10px">
                <button class="btn secondary" style="font-size:12px;padding:5px 12px" type="button" onclick="clearBox('#posts-pending-table tbody')">Limpar lista</button>
              </div>
              <form method="post" action="/app/profiles/{p.id}/posts/bulk">
                {_posts_table("posts-pending-table", pending_rows, "Nenhum post pendente.")}
                <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">
                  <button class="btn secondary" type="submit" name="mode" value="cancel" style="font-size:12px">Cancelar selecionados</button>
                  <button class="btn secondary" type="submit" name="mode" value="delete" style="font-size:12px">Excluir selecionados</button>
                </div>
              </form>
            </div>
          </details>
        </div>

        <div class="card">
          <details class="toggle-section" open>
            <summary>
              <span class="ts-title">
                <span style="width:22px;height:22px;border-radius:6px;background:rgba(239,68,68,.15);display:inline-flex;align-items:center;justify-content:center;font-size:12px">✕</span>
                Falhas <span class="ts-badge" style="color:#ef4444;border-color:rgba(239,68,68,.3)">{n_failed}</span>
              </span>
              <span class="ts-arrow">▶</span>
            </summary>
            <div class="ts-body">
              <div style="display:flex;justify-content:flex-end;margin-bottom:10px">
                <button class="btn secondary" style="font-size:12px;padding:5px 12px" type="button" onclick="clearBox('#posts-failed-table tbody')">Limpar lista</button>
              </div>
              <form method="post" action="/app/profiles/{p.id}/posts/bulk">
                {_posts_table("posts-failed-table", failed_rows, "Nenhuma falha registrada.")}
              </form>
            </div>
          </details>
        </div>
        """

    return _layout(dict(tabs).get(tab, "Configurar"), body, user=user, profile_id=p.id, active_tab=tab)


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
    # Suporte ao novo formato com lista de usuários
    users = creds.get("users") if isinstance(creds.get("users"), list) else []
    if users:
        active_username = str(creds.get("active_username") or "")
        active_user = next((u for u in users if u.get("username") == active_username), users[0])
        username = str(active_user.get("username") or "")
        app_password = str(active_user.get("app_password") or "")
    else:
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
    name: str = Form("WordPress"),
    base_url: str = Form(""),
    username: str = Form(...),
    app_password: str = Form(...),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    existing = db.scalar(select(Integration).where(Integration.profile_id == p.id, Integration.type == IntegrationType.WORDPRESS))

    uname = username.strip()
    apwd = app_password.strip()

    if existing:
        # Atualiza integração existente — adiciona ou atualiza o usuário na lista
        try:
            creds = decrypt_json(existing.credentials_encrypted)
        except CryptoError:
            creds = {}
        # Atualiza base_url se fornecida
        if base_url.strip():
            clean_base = base_url.strip().replace("/wp-admin/", "/").replace("/wp-admin", "").rstrip("/")
            creds["base_url"] = clean_base
        users = creds.get("users") if isinstance(creds.get("users"), list) else []
        # Migra formato antigo para novo
        if not users and creds.get("username"):
            users = [{"username": creds["username"], "app_password": creds.get("app_password", "")}]
        # Adiciona ou atualiza usuário na lista
        existing_idx = next((i for i, u in enumerate(users) if u.get("username") == uname), None)
        if existing_idx is not None:
            users[existing_idx]["app_password"] = apwd
        else:
            users.append({"username": uname, "app_password": apwd})
        creds["users"] = users
        creds["active_username"] = uname  # Recém adicionado torna-se o ativo
        # Limpa campos antigos
        creds.pop("username", None)
        creds.pop("app_password", None)
        existing.credentials_encrypted = encrypt_json(creds)
        db.commit()
    else:
        clean_base = (base_url or "").strip().replace("/wp-admin/", "/").replace("/wp-admin", "").rstrip("/")
        creds = {
            "base_url": clean_base,
            "users": [{"username": uname, "app_password": apwd}],
            "active_username": uname,
        }
        integ = Integration(
            user_id=p.user_id,
            profile_id=p.id,
            type=IntegrationType.WORDPRESS,
            name=name.strip() or "WordPress",
            credentials_encrypted=encrypt_json(creds),
        )
        db.add(integ)
        db.commit()

    return RedirectResponse(f"/app/profiles/{p.id}?tab=integracoes&msg={quote_plus('Usuário WordPress salvo.')}", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/integrations/wordpress/set-active-user", include_in_schema=False)
def profile_wp_set_active_user(
    profile_id: str,
    username: str = Form(...),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    integ = db.scalar(select(Integration).where(Integration.profile_id == p.id, Integration.type == IntegrationType.WORDPRESS))
    if not integ:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        creds = decrypt_json(integ.credentials_encrypted)
    except CryptoError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    creds["active_username"] = username.strip()
    integ.credentials_encrypted = encrypt_json(creds)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}?tab=integracoes&msg={quote_plus('Usuário ativo atualizado.')}", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/integrations/wordpress/remove-user", include_in_schema=False)
def profile_wp_remove_user(
    profile_id: str,
    username: str = Form(...),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    integ = db.scalar(select(Integration).where(Integration.profile_id == p.id, Integration.type == IntegrationType.WORDPRESS))
    if not integ:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        creds = decrypt_json(integ.credentials_encrypted)
    except CryptoError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    users = [u for u in (creds.get("users") or []) if u.get("username") != username.strip()]
    creds["users"] = users
    if creds.get("active_username") == username.strip():
        creds["active_username"] = users[0]["username"] if users else ""
    integ.credentials_encrypted = encrypt_json(creds)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}?tab=integracoes&msg={quote_plus('Usuário removido.')}", status_code=status.HTTP_302_FOUND)


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
      <details class="toggle-section" open>
        <summary><span class="ts-title">Posts Publicados <span class="ts-badge">{len(posts)}</span></span><span class="ts-arrow">▶</span></summary>
        <div class="ts-body">
          <p class="muted">Mostrando as <b>15</b> últimas receitas publicadas.</p>
          <table><thead><tr><th>Título</th><th>Quando</th><th>Link</th></tr></thead><tbody>{rows}</tbody></table>
        </div>
      </details>
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
    {_ph("pagina-logs")}
    <div class="card">
      <details class="toggle-section" open>
        <summary>
          <span class="ts-title">Logs <span class="ts-badge">{len(logs)}</span></span>
          <span style="display:flex;align-items:center;gap:10px">
            <button class="btn secondary" style="font-size:12px;padding:5px 10px" type="button" onclick="event.stopPropagation();clearBox('#logs-table tbody')">Limpar dados</button>
            <span class="ts-arrow">▶</span>
          </span>
        </summary>
        <div class="ts-body">
          {_ph("tabela-logs")}
          <div class="scrollbox">
            <table id="logs-table"><thead><tr><th>Etapa</th><th>Status</th><th>Mensagem</th><th>Owner</th><th>Quando</th></tr></thead><tbody>{rows}</tbody></table>
          </div>
        </div>
      </details>
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
    {_ph("pagina-admin-usuarios")}
    {"<div class='card' style='border-color:rgba(245,158,11,.4);background:rgba(245,158,11,.06);margin-bottom:14px'><b style='color:#f59e0b'>"+msg+"</b></div>" if msg else ""}
    <div class="card" style="margin-bottom:14px">
      <details class="toggle-section" open>
        <summary><span class="ts-title">Criar Usuário</span><span class="ts-arrow">▶</span></summary>
        <div class="ts-body">
          {_ph("form-criar-usuario")}
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
      </details>
    </div>
    <div class="card">
      <details class="toggle-section" open>
        <summary><span class="ts-title">Usuários <span class="ts-badge">{len(users)}</span></span><span class="ts-arrow">▶</span></summary>
        <div class="ts-body">
          {_ph("tabela-usuarios-admin")}
          <table>
            <thead><tr><th>Usuário/ID</th><th>Email</th><th>Role</th><th>Criado</th><th></th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
      </details>
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


# ─────────────────────────── NOTIFICATIONS ────────────────────────────────

import re as _re  # noqa: E402
from fastapi.responses import JSONResponse as _JSONResponse  # noqa: E402


def _translate_error(code: str) -> tuple[str, str, str]:
    """Return (label, fix_suggestion, fix_url) for a given error code string."""
    c = str(code or "").lower().strip()

    # ── WordPress errors ──────────────────────────────────────────────────
    if "missing_wordpress_integration" in c:
        return (
            "WordPress não configurado",
            "Nenhuma integração WordPress foi encontrada para este bot. Adicione a URL, usuário e senha de aplicativo nas integrações.",
            "/app/bot",
        )
    if "invalid_wordpress_credentials" in c:
        return (
            "Credenciais WordPress inválidas",
            "A URL base, o usuário ou a Senha de Aplicativo do WordPress estão incorretos ou vazios. Acesse as integrações do bot, edite o WordPress e preencha todos os campos corretamente.",
            "/app/bot",
        )
    if "post_create_failed:401" in c or "401" in c:
        return (
            "Autenticação WordPress recusada (HTTP 401)",
            "A Senha de Aplicativo está errada ou foi revogada. No WordPress acesse: Usuários → seu perfil → role até 'Senhas de Aplicativo' → gere uma nova e cole nas integrações.",
            "/app/bot",
        )
    if "post_create_failed:403" in c or "403" in c:
        return (
            "Sem permissão para publicar (HTTP 403)",
            "O usuário WordPress não tem permissão de Autor ou Editor. No WordPress vá em Usuários → edite o usuário → altere o Perfil para Autor ou Editor.",
            "/app/bot",
        )
    if "post_create_failed:404" in c or "404" in c:
        return (
            "API REST do WordPress não encontrada (HTTP 404)",
            "A URL base está incorreta ou a API REST está desativada. Verifique se a URL termina sem barra (ex: https://seusite.com) e se o WordPress está acessível.",
            "/app/bot",
        )
    if "post_create_failed" in c:
        m = _re.search(r":(\d{3}):", c)
        code_n = m.group(1) if m else "?"
        return (
            f"Erro ao criar post no WordPress (HTTP {code_n})",
            f"O WordPress retornou o código de erro {code_n}. Verifique as credenciais, a URL base e se o site está online.",
            "/app/bot",
        )
    if "media_upload_failed" in c:
        return (
            "Falha ao enviar imagem para o WordPress",
            "O upload da imagem destaque falhou. Verifique se o usuário tem permissão de upload e se o site tem espaço em disco.",
            "/app/bot",
        )
    if "missing_wordpress_output" in c:
        return (
            "IA não gerou conteúdo para o WordPress",
            "O comando da IA não produziu texto para o site. Verifique se o prompt do WordPress está preenchido nas configurações de IA do bot.",
            "/app/bot",
        )
    if "wordpress_output_too_short" in c:
        return (
            "Conteúdo gerado pela IA é muito curto",
            "O texto gerado tem menos de 120 caracteres. Melhore o prompt da IA para gerar artigos mais completos.",
            "/app/bot",
        )
    if "duplicate_detected" in c:
        return (
            "Post duplicado — ignorado automaticamente",
            "Um post com título ou URL muito similar já foi publicado. O sistema buscará novo conteúdo automaticamente.",
            "",
        )

    # ── Gemini / IA errors ────────────────────────────────────────────────
    if "rate_limited" in c:
        return (
            "Limite de requisições da API Gemini atingido",
            "A API gratuita do Gemini tem cota diária. O sistema tentará novamente em breve. Se isso ocorrer com frequência, considere usar um modelo diferente.",
            "/app/bot",
        )
    if "gemini" in c or "missing_gemini" in c or "invalid_api_key" in c:
        return (
            "Chave da API Gemini inválida ou não configurada",
            "Acesse as integrações do bot, aba Gemini, e verifique se a API key está correta. Gere uma nova em aistudio.google.com/apikey se necessário.",
            "/app/bot",
        )

    # ── Canceled ──────────────────────────────────────────────────────────
    if "canceled_by_user" in c:
        return ("Cancelado manualmente", "Este post foi cancelado pelo usuário.", "")

    # ── Generic ───────────────────────────────────────────────────────────
    if c:
        return (
            f"Erro interno",
            f"Código de erro: {c[:120]}. Consulte a página de Logs para mais detalhes.",
            "/app/logs",
        )
    return ("Erro desconhecido", "Consulte a página de Logs para mais detalhes.", "/app/logs")


def _get_post_error(db, post_id: str, user_id: str) -> str:
    """Return the most recent error string from job_logs for a given post."""
    log = db.scalar(
        select(JobLog)
        .where(
            JobLog.post_id == post_id,
            JobLog.user_id == user_id,
            JobLog.status == "error",
        )
        .order_by(JobLog.created_at.desc())
        .limit(1)
    )
    if not log:
        return ""
    meta = log.meta_json or {}
    return str(meta.get("error") or log.message or "")


@router.get("/app/notifications/feed", include_in_schema=False)
def notifications_feed(user: User = Depends(get_current_user), db=Depends(get_db)):
    """JSON feed of recent completed/failed posts for the notification bell."""
    rows = list(
        db.execute(
            select(Post, CollectedContent.title, AutomationProfile.name.label("bot_name"))
            .join(CollectedContent, CollectedContent.id == Post.collected_content_id)
            .join(AutomationProfile, AutomationProfile.id == Post.profile_id)
            .where(
                Post.user_id == user.id,
                Post.status.in_([PostStatus.completed, PostStatus.failed]),
            )
            .order_by(Post.updated_at.desc())
            .limit(30)
        ).all()
    )
    tz = _user_zoneinfo(user)
    feed = []
    for post, title, bot_name in rows:
        is_canceled = isinstance(post.outputs_json, dict) and bool(post.outputs_json.get("canceled_by_user"))
        ntype = "success" if post.status == PostStatus.completed else "error"
        label = "Cancelado" if (post.status == PostStatus.failed and is_canceled) else (
            "Publicado" if post.status == PostStatus.completed else "Falhou"
        )
        ts = post.updated_at or post.created_at
        try:
            when_str = ts.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%d/%m %H:%M")
        except Exception:
            when_str = str(ts)[:16]

        error_label, fix, fix_url = "", "", ""
        if post.status == PostStatus.failed:
            raw_err = _get_post_error(db, post.id, user.id)
            if not raw_err and is_canceled:
                raw_err = "canceled_by_user"
            error_label, fix, fix_url = _translate_error(raw_err)

        feed.append({
            "id": post.id,
            "type": ntype,
            "title": str(title or "Sem título"),
            "bot": str(bot_name or ""),
            "status": label,
            "when": when_str,
            "wp_url": post.wp_url or "",
            "error_label": error_label,
            "fix": fix,
            "fix_url": fix_url,
        })
    return _JSONResponse(feed)


@router.get("/app/notifications", include_in_schema=False)
def notifications_page(user: User = Depends(get_current_user), db=Depends(get_db)):
    """Full notifications page with recent events, error details and toggle settings."""
    rows = list(
        db.execute(
            select(Post, CollectedContent.title, AutomationProfile.name.label("bot_name"))
            .join(CollectedContent, CollectedContent.id == Post.collected_content_id)
            .join(AutomationProfile, AutomationProfile.id == Post.profile_id)
            .where(
                Post.user_id == user.id,
                Post.status.in_([PostStatus.completed, PostStatus.failed]),
            )
            .order_by(Post.updated_at.desc())
            .limit(60)
        ).all()
    )
    tz = _user_zoneinfo(user)
    items_html = ""
    for post, title, bot_name in rows:
        is_canceled = isinstance(post.outputs_json, dict) and bool(post.outputs_json.get("canceled_by_user"))
        is_ok = post.status == PostStatus.completed
        label = "Cancelado" if (not is_ok and is_canceled) else ("Publicado" if is_ok else "Falhou")
        ts = post.updated_at or post.created_at
        try:
            when_str = ts.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%d/%m/%Y às %H:%M")
        except Exception:
            when_str = str(ts)[:16]
        safe_title = html.escape(str(title or "Sem título"))
        safe_bot   = html.escape(str(bot_name or ""))
        safe_when  = html.escape(when_str)
        wp_url     = post.wp_url or ""

        if is_ok:
            icon_html  = "<span style='width:32px;height:32px;border-radius:50%;background:rgba(16,185,129,.15);color:#10b981;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:15px;font-weight:700'>✓</span>"
            badge_html = f"<span style='color:#10b981;font-size:11px;font-weight:700;background:rgba(16,185,129,.12);padding:3px 10px;border-radius:20px;white-space:nowrap'>✓ {html.escape(label)}</span>"
            row_style  = "border-left:3px solid #10b981;background:rgba(16,185,129,.03)"
            link_html  = (
                f"<a href='{html.escape(wp_url)}' target='_blank' rel='noopener' "
                f"style='color:#10b981;font-size:12px;font-weight:600;text-decoration:none'>🔗 Ver post</a>"
            ) if wp_url else ""
            error_block = ""
        else:
            icon_html  = "<span style='width:32px;height:32px;border-radius:50%;background:rgba(239,68,68,.12);color:#ef4444;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:15px;font-weight:700'>✕</span>"
            badge_html = f"<span style='color:#ef4444;font-size:11px;font-weight:700;background:rgba(239,68,68,.08);padding:3px 10px;border-radius:20px;white-space:nowrap'>✕ {html.escape(label)}</span>"
            row_style  = "border-left:3px solid #ef4444;background:rgba(239,68,68,.02)"
            link_html  = ""

            raw_err = _get_post_error(db, post.id, user.id)
            if not raw_err and is_canceled:
                raw_err = "canceled_by_user"
            error_label, fix, fix_url = _translate_error(raw_err)

            fix_btn = ""
            if fix_url:
                fix_btn = (
                    f"<a href='{html.escape(fix_url)}' "
                    f"style='display:inline-flex;align-items:center;gap:5px;margin-top:8px;"
                    f"font-size:12px;font-weight:600;color:#fff;background:#ef4444;"
                    f"border-radius:8px;padding:6px 14px;text-decoration:none;width:fit-content'>"
                    f"→ Corrigir agora</a>"
                )

            error_block = f"""
            <div style='margin-top:10px;padding:12px 14px;background:rgba(239,68,68,.06);border:1px solid rgba(239,68,68,.18);border-radius:10px'>
              <div style='font-size:12px;font-weight:700;color:#ef4444;margin-bottom:4px'>
                ⚠ {html.escape(error_label)}
              </div>
              <div style='font-size:12px;color:var(--text);line-height:1.6'>
                {html.escape(fix)}
              </div>
              {fix_btn}
            </div>"""

        items_html += f"""
        <div style='{row_style};padding:16px 20px;border-bottom:1px solid var(--border)'>
          <div style='display:flex;align-items:center;gap:12px'>
            {icon_html}
            <div style='flex:1;min-width:0'>
              <div style='font-size:13px;font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>{safe_title}</div>
              <div style='font-size:12px;color:var(--muted);margin-top:2px'>{safe_bot} · {safe_when}</div>
            </div>
            <div style='display:flex;align-items:center;gap:8px;flex-shrink:0'>
              {badge_html}
              {link_html}
            </div>
          </div>
          {error_block}
        </div>"""

    if not items_html:
        items_html = "<div style='padding:40px;text-align:center;color:var(--muted);font-size:14px'>Nenhuma notificação ainda. Quando posts forem publicados ou falharem, aparecerão aqui.</div>"

    body = f"""
    <div style='display:flex;flex-direction:column;gap:20px'>
      <div class="card" style='padding:0;overflow:hidden'>
        <div style='padding:18px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px'>
          <div>
            <h3 style='margin:0 0 2px'>Notificações</h3>
            <p class='muted' style='margin:0;font-size:13px'>Posts publicados e erros — com causa e como corrigir</p>
          </div>
          <button onclick="document.getElementById('notif-list').innerHTML='<div style=\\'padding:40px;text-align:center;color:var(--muted)\\'>Marcadas como lidas</div>';var b=document.getElementById('notif-badge');if(b)b.classList.remove('visible');" class='btn secondary' style='font-size:12px;padding:7px 14px'>Marcar todas como lidas</button>
        </div>
        <div id='notif-list'>
          {items_html}
        </div>
      </div>

      <div class="card">
        <h3 style='margin:0 0 4px'>Configurações de Notificação</h3>
        <p class='muted' style='font-size:13px;margin:0 0 18px'>Escolha quais eventos geram notificações no sino.</p>
        <div style='display:flex;flex-direction:column;gap:14px'>
          <div style='display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border:1px solid var(--border);border-radius:12px'>
            <div>
              <div style='font-size:13px;font-weight:600'>Posts publicados com sucesso</div>
              <div style='font-size:12px;color:var(--muted);margin-top:2px'>Notificar quando um post for publicado no WordPress</div>
            </div>
            <label style='position:relative;display:inline-block;width:44px;height:24px;flex-shrink:0'>
              <input type='checkbox' id='ns-success' style='opacity:0;width:0;height:0' onchange='_saveNotifSettings()'>
              <span class='ns-track'></span>
            </label>
          </div>
          <div style='display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border:1px solid var(--border);border-radius:12px'>
            <div>
              <div style='font-size:13px;font-weight:600'>Erros e falhas</div>
              <div style='font-size:12px;color:var(--muted);margin-top:2px'>Notificar quando um post falhar ou for cancelado</div>
            </div>
            <label style='position:relative;display:inline-block;width:44px;height:24px;flex-shrink:0'>
              <input type='checkbox' id='ns-error' style='opacity:0;width:0;height:0' onchange='_saveNotifSettings()'>
              <span class='ns-track'></span>
            </label>
          </div>
        </div>
      </div>
    </div>
    <style>
      .ns-track {{
        position:absolute; cursor:pointer; inset:0;
        background:var(--surface2); border-radius:999px;
        transition:.25s; border:1px solid var(--border);
      }}
      .ns-track::after {{
        content:''; position:absolute; left:3px; top:3px;
        width:16px; height:16px; border-radius:50%; background:#fff;
        transition:.25s; box-shadow:0 1px 3px rgba(0,0,0,.25);
      }}
      input:checked + .ns-track {{ background:var(--primary); border-color:var(--primary); }}
      input:checked + .ns-track::after {{ transform:translateX(20px); }}
    </style>
    <script>
      (function(){{
        var LS = 'ph-notif-settings';
        function load() {{
          try {{ return JSON.parse(localStorage.getItem(LS) || '{{"success":true,"error":true}}'); }}
          catch(e) {{ return {{"success":true,"error":true}}; }}
        }}
        var s = load();
        var cb_s = document.getElementById('ns-success');
        var cb_e = document.getElementById('ns-error');
        if (cb_s) cb_s.checked = s.success !== false;
        if (cb_e) cb_e.checked = s.error !== false;
        window._saveNotifSettings = function() {{
          localStorage.setItem(LS, JSON.stringify({{
            success: cb_s ? cb_s.checked : true,
            error:   cb_e ? cb_e.checked : true,
          }}));
        }};
      }})();
    </script>
    """
    return _layout("Notificações", body, user=user)
