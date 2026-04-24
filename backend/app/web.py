from __future__ import annotations

import html
import json


def _safe(s: object) -> str:
    """Return s as a string with surrogate/invalid Unicode characters replaced."""
    try:
        t = str(s)
        return t.encode("utf-8", errors="replace").decode("utf-8")
    except Exception:
        return ""


def _ph(name: str) -> str:
    """Placeholder de desenvolvimento ” mostra label amarelo com ícones copiar/fechar."""
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

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import case, func, or_, select, update

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
    IntegrationStatus,
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
from app.queue import JOB_AI, JOB_CLEAN, JOB_COLLECT, JOB_MEDIA, JOB_PUBLISH_WP, enqueue_job
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


def _to_user_local(dt: datetime | None, *, user: User) -> datetime | None:
    if not dt:
        return None
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.astimezone(_user_zoneinfo(user)).replace(tzinfo=None)
    except Exception:
        return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt


def _local_input_to_utc_naive(value: str, *, user: User) -> datetime:
    local = datetime.fromisoformat((value or "").strip().replace("T", " "))
    if local.tzinfo is None:
        local = local.replace(tzinfo=_user_zoneinfo(user))
    return local.astimezone(timezone.utc).replace(tzinfo=None)


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


def _ensure_publish_config_defaults(db, *, bot: AutomationProfile) -> bool:
    cfg = dict(bot.publish_config_json or {})
    changed = False
    if not str(cfg.get("facebook_link") or "").strip():
        cfg["facebook_link"] = "comments"
        changed = True
    if not str(cfg.get("default_category") or "").strip():
        cfg["default_category"] = "Receitas"
        changed = True
    cats = cfg.get("categories")
    clean_cats = [str(c).strip() for c in cats if str(c).strip()] if isinstance(cats, list) else []
    if not clean_cats:
        cfg["categories"] = list(DEFAULT_RECIPE_CATEGORIES)
        changed = True
    if changed:
        bot.publish_config_json = cfg
        db.add(bot)
    return changed


def _set_bot_run_stopped(db, *, bot: AutomationProfile, stopped: bool) -> None:
    cfg = dict(bot.publish_config_json or {})
    if stopped:
        cfg["run_stopped_at"] = datetime.utcnow().isoformat()
    else:
        cfg.pop("run_stopped_at", None)
    bot.publish_config_json = cfg
    db.add(bot)


def _get_profile_for_user(db, *, profile_id: str, user: User) -> AutomationProfile | None:
    if user.role == UserRole.ADMIN:
        return db.scalar(select(AutomationProfile).where(AutomationProfile.id == profile_id))
    return db.scalar(select(AutomationProfile).where(AutomationProfile.id == profile_id, AutomationProfile.user_id == user.id))


def _base_css() -> str:
    return ""


def _layout(title: str, body: str, *, user: User | None = None, profile_id: str | None = None, active_tab: str | None = None, active_nav: str | None = None) -> HTMLResponse:
    t = html.escape(title)
    # Steps em ordem lógica de configuração
    _tabs = [
        ("integracoes", "Integrações",  "1"),
        ("fontes",       "Fontes",       "2"),
        ("ia",           "IA",           "3"),
        ("publicacao",   "Publicação",   "4"),
        ("agendamento",  "Agendamento",  "5"),
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
  <title>{t} ” PostHub</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet" />
  <link rel="stylesheet" href="/static/posthub.css?v=8" />
  <style>
    /* ── Mobile crítico inline (nunca cacheia) ── */
    @media (max-width: 900px) {{
      .notif-dropdown {{
        position: fixed !important;
        top: 62px !important;
        left: 12px !important;
        right: 12px !important;
        width: auto !important;
        max-height: 72vh !important;
        max-height: 72dvh !important;
        border-radius: 16px !important;
        z-index: 10000 !important;
      }}
    }}
    body.ph-mob-active .notif-dropdown {{
      position: fixed !important;
      top: 108px !important;
      left: 50% !important;
      right: auto !important;
      transform: translateX(-50%) !important;
      width: min(320px, calc(100vw - 40px)) !important;
      z-index: 200000 !important;
    }}
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
    <div id="phMobBackdrop" onclick="phCloseMobNav()"></div>
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
        <div class="nav-group {'open' if active_nav in ('posts','history') else ''}" id="nav-posts-group">
          <button class="nav-parent" onclick="this.closest('.nav-group').classList.toggle('open')" style="display:flex;align-items:center;gap:10px">
            <span class="dot"></span><span style="flex:1;font-size:13px">Posts</span><span class="nav-sub-arrow" style="font-size:9px;transition:transform .2s">&#9654;</span>
          </button>
          <div class="nav-sub">
            <a href="/app/posts" class="{'active' if active_nav=='posts' else ''}"><span style="font-size:9px">&#9654;</span> Pendências</a>
            <a href="/app/history" class="{'active' if active_nav=='history' else ''}"><span style="font-size:9px">&#9654;</span> Histórico</a>
          </div>
        </div>
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
        <div style="display:flex;align-items:center;gap:8px">
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
          <div class="dev-menu-wrap" id="devMenuWrap">
            <button class="dev-menu-btn" id="devMenuBtn" onclick="toggleDevMenu()" title="Menu Dev">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
              Dev
            </button>
            <div class="dev-menu-dd" id="devMenuDd">
              <div class="dev-menu-section">
                <div class="dev-menu-label">&#127912; Temas</div>
                <div class="dev-theme-grid" id="devThemeGrid">
                  <button class="dev-theme-btn" onclick="setThemeAndSave('roxo')">&#127769; Roxo</button>
                  <button class="dev-theme-btn" onclick="setThemeAndSave('oceano')">&#127754; Oceano</button>
                  <button class="dev-theme-btn" onclick="setThemeAndSave('floresta')">&#127807; Floresta</button>
                  <button class="dev-theme-btn" onclick="setThemeAndSave('claro')">&#9728;&#65039; Claro</button>
                  <button class="dev-theme-btn" onclick="setThemeAndSave('rosa')">&#127800; Rosa</button>
                  <button class="dev-theme-btn" onclick="setThemeAndSave('ceu')">&#127780; C&#233;u</button>
                  <button class="dev-theme-btn" onclick="setThemeAndSave('corporativo')">&#128188; Corp.</button>
                </div>
              </div>
              <div class="dev-divider"></div>
              <div class="dev-menu-section">
                <div class="dev-menu-label">&#128266; Som</div>
                <div style="display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:10px;border:1px solid var(--border2);background:var(--surface)">
                  <span style="font-size:18px;flex-shrink:0">&#128266;</span>
                  <div style="flex:1;min-width:0">
                    <div style="font-weight:600;font-size:13px;margin-bottom:2px">Som ao publicar</div>
                    <select id="ph-success-sound" title="Som ao publicar com sucesso" style="width:100%;border:1px solid var(--border2);background:var(--surface2);color:var(--text);font-size:12px;font-weight:500;padding:4px 6px;border-radius:6px;outline:none">
                      <option value="cash">&#129534; Caixa registradora</option>
                      <option value="money">&#128176; Dinheiro</option>
                      <option value="off">&#128263; Sem som</option>
                    </select>
                  </div>
                  <button type="button" id="ph-success-sound-test" title="Testar som" style="width:30px;height:30px;border:none;background:rgba(139,92,246,.15);color:var(--primary);border-radius:8px;padding:0;font-size:12px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;flex-shrink:0">&#9654;</button>
                </div>
              </div>
              <div class="dev-divider"></div>
              <div class="dev-menu-section">
                <div class="dev-menu-label">&#128295; Utilidades</div>
                <button class="dev-action-btn" onclick="phOpenMobPreview()" style="border-color:rgba(139,92,246,.35)">
                  <span style="font-size:16px">&#128241;</span>
                  <div>
                    <div style="font-weight:600">Preview Mobile</div>
                    <div style="font-size:11px;color:var(--muted)">Visualizar como celular (390/412/768px)</div>
                  </div>
                </button>
                <button class="dev-action-btn" onclick="devLimparCache(this)">
                  <span style="font-size:16px">&#128260;</span>
                  <div><div style="font-weight:600">Limpar Cache</div><div style="font-size:11px;color:var(--muted)">Recarrega a p&#225;gina ignorando cache</div></div>
                </button>
                <button class="dev-action-btn" id="devPhBtn" onclick="devTogglePh()">
                  <span style="font-size:16px">&#128204;</span>
                  <div><div style="font-weight:600">Placeholders</div><div style="font-size:11px;color:var(--muted)" id="devPhStatus">Carregando...</div></div>
                </button>
              </div>
            </div>
          </div>
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
      /* ── Theme ── */
      var current = localStorage.getItem('posthub-theme') || 'roxo';
      function applyTheme(name) {{
        document.documentElement.setAttribute('data-theme', name);
        localStorage.setItem('posthub-theme', name);
        document.querySelectorAll('.dev-theme-btn').forEach(function(b) {{
          b.classList.toggle('active', b.getAttribute('onclick') && b.getAttribute('onclick').indexOf("'"+name+"'") !== -1);
        }});
      }}
      window.setThemeAndSave = function(name) {{ applyTheme(name); }};
      applyTheme(current);

      /* ── Dev Menu toggle ── */
      window.toggleDevMenu = function() {{
        var btn = document.getElementById('devMenuBtn');
        var dd  = document.getElementById('devMenuDd');
        var open = dd.classList.toggle('open');
        btn.classList.toggle('open', open);
        updatePhStatus();
      }};
      document.addEventListener('click', function(e) {{
        var wrap = document.getElementById('devMenuWrap');
        if (wrap && !wrap.contains(e.target)) {{
          document.getElementById('devMenuDd').classList.remove('open');
          document.getElementById('devMenuBtn').classList.remove('open');
        }}
      }});

      /* ── Limpar Cache ── */
      window.devLimparCache = function(btn) {{
        if (btn) {{ btn.textContent = 'Limpando...'; btn.disabled = true; }}
        var toRemove = [];
        for (var i = 0; i < localStorage.length; i++) {{
          var k = localStorage.key(i);
          if (k && k.startsWith('ph-')) toRemove.push(k);
        }}
        toRemove.forEach(function(k) {{ localStorage.removeItem(k); }});
        window.location.replace(window.location.pathname + '?_cb=' + Date.now());
      }};

      /* ── Prompt variant tabs ── */
      window.phSwitchTab = function(iid, dest, num) {{
        for (var i = 1; i <= 4; i++) {{
          var panel = document.getElementById('s' + dest + '_' + iid + '_' + i);
          var btn   = document.getElementById('s' + dest + 'tab_' + iid + '_' + i);
          if (!panel || !btn) continue;
          var active = (i === num);
          panel.style.display = active ? 'block' : 'none';
          if (active) {{
            btn.style.background = dest === 'wp' ? 'rgba(139,92,246,.15)' : 'rgba(24,119,242,.12)';
            btn.style.borderColor = dest === 'wp' ? 'rgba(139,92,246,.35)' : 'rgba(24,119,242,.35)';
            btn.style.color = 'var(--fg)';
          }} else {{
            btn.style.background = 'transparent';
            btn.style.borderColor = dest === 'wp' ? 'rgba(139,92,246,.15)' : 'rgba(24,119,242,.15)';
            btn.style.color = 'var(--muted)';
          }}
        }}
      }};

      /* ── Placeholders toggle ── */
      function updatePhStatus() {{
        var hidden = localStorage.getItem('ph-hidden') === '1';
        var el = document.getElementById('devPhStatus');
        if (el) el.textContent = hidden ? 'Ocultos \u2014 clique para mostrar' : 'Vis\u00edveis \u2014 clique para ocultar';
      }}
      window.devTogglePh = function() {{
        var hidden = localStorage.getItem('ph-hidden') === '1';
        if (hidden) {{
          localStorage.removeItem('ph-hidden');
          document.querySelectorAll('.dev-ph-wrap').forEach(function(el){{ el.style.display='inline-flex'; }});
        }} else {{
          localStorage.setItem('ph-hidden','1');
          document.querySelectorAll('.dev-ph-wrap').forEach(function(el){{ el.style.display='none'; }});
        }}
        updatePhStatus();
      }};
      updatePhStatus();
      // Apply saved placeholder visibility on every page load
      if (localStorage.getItem('ph-hidden') === '1') {{
        document.querySelectorAll('.dev-ph-wrap').forEach(function(el){{ el.style.display='none'; }});
      }}

      /* ── nav-sub toggle ── */
      var navBtn = document.getElementById('nav-config-btn');
      if (navBtn) {{
        navBtn.addEventListener('click', function() {{
          document.getElementById('nav-config').classList.toggle('open');
        }});
      }}
    }})();

    /* ── notification bell ── */
    /* success sound menu */
    (function(){{
      var LS_SOUND = 'ph-success-sound';
      var sel = document.getElementById('ph-success-sound');
      var test = document.getElementById('ph-success-sound-test');
      function soundChoice() {{
        var v = localStorage.getItem(LS_SOUND) || 'cash';
        return v === 'coins' ? 'cash' : v;
      }}
      function saveChoice(v) {{
        localStorage.setItem(LS_SOUND, v || 'cash');
      }}
      if (sel) {{
        sel.value = soundChoice();
        sel.addEventListener('change', function() {{ saveChoice(sel.value); }});
      }}
      var SOUND_FILES = {{
        cash: '/static/mario-coins.mp3',
        money: '/static/plim.m4a'
      }};
      function playAudioFile(src) {{
        if (!src) return;
        try {{
          var a = new Audio(src);
          a.preload = 'auto';
          a.volume = 0.9;
          var p = a.play();
          if (p && typeof p.catch === 'function') p.catch(function(){{}});
        }} catch(e) {{}}
      }}
      window._phPlaySuccessSound = function(kind) {{
        kind = kind || soundChoice();
        if (kind === 'off') return;
        playAudioFile(SOUND_FILES[kind] || SOUND_FILES.cash);
      }};
      window._phPlayInfoSound = function() {{
        window._phPlaySuccessSound(soundChoice());
      }};
      window._phSoundLabel = function() {{
        var labels = {{
          cash: '&#129534; Caixa',
          money: '&#128176; Dinheiro',
          off: '&#128263; Sem som'
        }};
        return labels[soundChoice()] || labels.cash;
      }};
      if (test) test.addEventListener('click', function() {{
        if (sel) saveChoice(sel.value);
        var kind = soundChoice();
        playAudioFile(SOUND_FILES[kind === 'off' ? 'cash' : kind] || SOUND_FILES.cash);
      }});
    }})();

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
        try {{
          var s = JSON.parse(localStorage.getItem(LS_NOTIF) || '{{"success":true,"error":true,"desktop":false}}');
          if (typeof s.desktop === 'undefined') s.desktop = false;
          return s;
        }}
        catch(e) {{ return {{"success":true,"error":true,"desktop":false}}; }}
      }}
      function saveSettings(s) {{
        localStorage.setItem(LS_NOTIF, JSON.stringify(s || {{"success":true,"error":true,"desktop":false}}));
      }}
      window._phRequestDesktopNotifications = function() {{
        if (!('Notification' in window)) {{
          alert('Este navegador nao suporta notificacoes desktop.');
          return;
        }}
        Notification.requestPermission().then(function(permission) {{
          var s = getSettings();
          s.desktop = permission === 'granted';
          saveSettings(s);
          var cb = document.getElementById('ns-desktop');
          if (cb) cb.checked = s.desktop;
          if (permission === 'granted') {{
            try {{ new Notification('PostHUB', {{ body: 'Notificacoes desktop ativadas.' }}); }} catch(e) {{}}
          }} else {{
            alert('Permissao nao concedida. Ative as notificacoes do site no navegador.');
          }}
        }});
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
            var actionPart = '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:7px">'
              + '<form method="post" action="/app/notifications/' + encodeURIComponent(n.id) + '/complete" style="margin:0">'
              + '<button type="submit" style="font-size:11px;font-weight:700;color:#fff;background:#10b981;border:0;border-radius:6px;padding:4px 9px;cursor:pointer">Marcar concluida</button></form>'
              + '<form method="post" action="/app/notifications/' + encodeURIComponent(n.id) + '/delete" style="margin:0">'
              + '<button type="submit" style="font-size:11px;font-weight:700;color:#ef4444;background:transparent;border:1px solid rgba(239,68,68,.35);border-radius:6px;padding:3px 9px;cursor:pointer">Excluir</button></form>'
              + '</div>';
            var fixText = n.fix
              ? '<div style="font-size:11px;color:var(--muted,#888);margin-top:3px;line-height:1.5">' + _esc(n.fix) + '</div>'
              : '';
            errLine = '<div style="margin-top:5px;padding:8px 10px;background:rgba(239,68,68,.07);border:1px solid rgba(239,68,68,.18);border-radius:8px">'
              + '<div style="font-size:11px;color:#ef4444;font-weight:700">⚠ ' + _esc(n.error_label) + '</div>'
              + fixText
              + actionPart
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

      var LS_SOUND_LAST = 'ph-success-sound-last-id';
      function _maybePlaySuccess(items) {{
        var s = getSettings();
        if (!s.success) return;
        var latest = (items || []).find(function(n) {{ return n.type === 'success'; }});
        if (!latest) return;
        var last = localStorage.getItem(LS_SOUND_LAST) || '';
        if (!last) {{
          localStorage.setItem(LS_SOUND_LAST, latest.id);
          return;
        }}
        if (latest.id !== last) {{
          localStorage.setItem(LS_SOUND_LAST, latest.id);
          var recent = !latest.ts_ms || (Date.now() - Number(latest.ts_ms)) < 10 * 60 * 1000;
          if (recent && typeof window._phPlaySuccessSound === 'function') window._phPlaySuccessSound();
        }}
      }}

      var LS_DESKTOP_LAST = 'ph-desktop-notif-last-id';
      function _maybeDesktopNotify(items) {{
        var s = getSettings();
        if (!s.desktop || !('Notification' in window) || Notification.permission !== 'granted') return;
        var latest = (items || []).find(function(n) {{
          return (n.type === 'success' && s.success) || (n.type === 'error' && s.error);
        }});
        if (!latest) return;
        var last = localStorage.getItem(LS_DESKTOP_LAST) || '';
        if (!last) {{
          localStorage.setItem(LS_DESKTOP_LAST, latest.id);
          return;
        }}
        if (latest.id === last) return;
        localStorage.setItem(LS_DESKTOP_LAST, latest.id);
        var recent = !latest.ts_ms || (Date.now() - Number(latest.ts_ms)) < 10 * 60 * 1000;
        if (!recent) return;
        try {{
          var title = latest.type === 'error' ? 'PostHUB - Falha na postagem' : 'PostHUB - Post publicado';
          var body = (latest.title || 'Sem titulo') + (latest.bot ? ' - ' + latest.bot : '');
          var n = new Notification(title, {{ body: body, tag: 'posthub-' + latest.id, renotify: true }});
          n.onclick = function() {{
            try {{ window.focus(); }} catch(e) {{}}
            window.location.href = '/app/notifications';
          }};
        }} catch(e) {{}}
      }}

      function _fetchFeed() {{
        fetch('/app/notifications/feed')
          .then(function(r){{ return r.ok ? r.json() : []; }})
          .then(function(data) {{
            _lastFeed = data || [];
            _maybePlaySuccess(_lastFeed);
            _maybeDesktopNotify(_lastFeed);
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

      window._phFetchFeed = _fetchFeed;
      _fetchFeed();
      setInterval(_fetchFeed, 20000);
    }})();

    window._phUpdateCount = function(tid) {{
      var t = document.getElementById(tid);
      if (!t) return;
      var n = t.querySelectorAll('tbody input[name=post_id]:checked').length;
      var el = document.getElementById('cnt-' + tid);
      if (el) el.textContent = n > 0 ? n + ' selecionado(s)' : '';
    }};

    window.phRunBotNow = function(btn) {{
      if (!btn || btn.disabled) return;
      var botId = btn.getAttribute('data-bot-id') || '';
      btn.disabled = true;
      var orig = btn.innerHTML;
      btn.innerHTML = '&#8987; Processando...';
      btn.style.opacity = '0.7';
      var fd = new FormData();
      if (botId) fd.append('bot_id', botId);
      fd.append('force', '1');
      fetch('/app/robot/tick-now', {{method:'POST', body:fd, credentials:'same-origin'}})
        .then(function(r){{ return r.ok ? r.json() : {{ticks:0}}; }})
        .then(function(d){{
          btn.innerHTML = d.ticks > 0 ? ('&#10003; ' + d.ticks + ' job(s) processado(s)') : '&#10003; Sem jobs na fila';
          btn.style.opacity = '1';
          setTimeout(function(){{ location.reload(); }}, 1200);
        }})
        .catch(function(){{
          btn.disabled = false;
          btn.innerHTML = orig;
          btn.style.opacity = '1';
        }});
    }};

    window.phStopBot = function(btn) {{
      if (!btn || btn.disabled) return;
      var botId = btn.getAttribute('data-bot-id') || '';
      btn.disabled = true;
      var orig = btn.innerHTML;
      btn.innerHTML = '&#8987; Parando...';
      btn.style.opacity = '0.7';
      var fd = new FormData();
      if (botId) fd.append('bot_id', botId);
      fetch('/app/robot/stop', {{method:'POST', body:fd, credentials:'same-origin', headers:{{Accept:'application/json'}}}})
        .then(function(r){{ return r.ok ? r.json() : {{ok:false}}; }})
        .then(function(d){{
          btn.innerHTML = d.ok ? '&#10003; Bot parado' : '&#10007; Erro ao parar';
          btn.style.opacity = '1';
          setTimeout(function(){{ location.reload(); }}, 900);
        }})
        .catch(function(){{
          btn.disabled = false;
          btn.innerHTML = orig;
          btn.style.opacity = '1';
        }});
    }};

    document.addEventListener('click', function(e) {{
      var runBtn = e.target.closest('.ph-run-now-btn');
      if (runBtn) {{
        e.preventDefault();
        window.phRunBotNow(runBtn);
        return;
      }}
      var stopBtn = e.target.closest('.ph-stop-bot-btn');
      if (stopBtn) {{
        e.preventDefault();
        window.phStopBot(stopBtn);
      }}
    }});

    // ── Bot status polling + toast notifications ────────────────────────────
    (function() {{
      var _prevStatus = {{}};
      var _toastWrap = document.createElement('div');
      _toastWrap.id = 'posthub-toast';
      document.body.appendChild(_toastWrap);

      function _showToast(msg, type) {{
        var t = document.createElement('div');
        t.className = 'ph-toast ' + (type || 'success');
        t.innerHTML = '<span class="ph-toast-icon">' + (type === 'error' ? '&#10060;' : '&#9989;') + '</span><span>' + msg + '</span>';
        _toastWrap.appendChild(t);
        setTimeout(function(){{ t.classList.add('show'); }}, 20);
        setTimeout(function(){{
          t.classList.remove('show');
          setTimeout(function(){{ if (t.parentNode) t.parentNode.removeChild(t); }}, 350);
        }}, 5000);
      }}

      var _botStreak=0, _botDelay=8000, _botTid=null;
      function _scheduleBotPoll(){{ _botTid=setTimeout(_pollBotStatus,_botDelay); }}
      function _pollBotStatus() {{
        if(document.hidden){{ _scheduleBotPoll(); return; }}
        fetch('/app/robot/status')
          .then(function(r){{ return r.ok ? r.json() : []; }})
          .then(function(bots) {{
            var changed=false;
            (bots || []).forEach(function(b) {{
              var prev = _prevStatus[b.id];
              if (prev === true && !b.is_running) {{
                _showToast('&#9989; ' + b.name + ' parou. Veja as notificações.', 'success');
                if (typeof window._phFetchFeed === 'function') window._phFetchFeed();
                changed=true;
              }}
              if(prev!==b.is_running) changed=true;
              _prevStatus[b.id] = b.is_running;
            }});
            if(changed){{ _botStreak=0; _botDelay=8000; }}
            else{{ _botStreak++; if(_botStreak>=3) _botDelay=Math.min(30000,_botDelay+6000); }}
            var anyRunning=(bots||[]).some(function(b){{return b.is_running;}});
            if(anyRunning) _scheduleBotPoll();
          }})
          .catch(function(){{}});
      }}

      // Initialize state on page load
      fetch('/app/robot/status')
        .then(function(r){{ return r.ok ? r.json() : []; }})
        .then(function(bots) {{
          (bots || []).forEach(function(b) {{ _prevStatus[b.id] = b.is_running; }});
          var anyRunning = (bots || []).some(function(b){{ return b.is_running; }});
          if (anyRunning) _scheduleBotPoll();
        }}).catch(function(){{}});
    }})();

    // ── Live jobs log polling ──────────────────────────────────────────────
    (function() {{
      var _stage_labels = {{
        'collect_content':'\uD83D\uDD0D Coletar','clean_content':'\uD83E\uDDF9 Limpar',
        'ai_generate':'\uD83E\uDD16 IA','publish_wp':'\uD83D\uDCE4 Publicar WP',
        'facebook_publish':'\uD83D\uDCD8 Facebook','auto_stop':'\u23F9 Auto-stop'
      }};
      var _status_colors = {{
        'queued':'#f59e0b','running':'#6366f1','succeeded':'#10b981','failed':'#ef4444'
      }};
      var _status_labels = {{
        'queued':'\u23F3 Pendente','running':'\u26A1 Rodando','succeeded':'\u2713 Conclu\u00EDdo','failed':'\u2715 Falha'
      }};
      function _renderLog(rows) {{
        document.querySelectorAll('[id^="livelog-body-"]').forEach(function(tbody) {{
          var pid = tbody.id.replace('livelog-body-','');
          var filtered = rows.filter(function(r){{ return String(r.profile_id) === String(pid); }});
          if (!filtered.length) {{
            tbody.innerHTML = '<tr><td colspan="5" style="padding:16px;text-align:center;color:var(--muted)">Nenhuma atividade recente.</td></tr>';
            return;
          }}
          tbody.innerHTML = filtered.slice(0,30).map(function(r) {{
            var stageLabel = _stage_labels[r.stage] || r.stage;
            var statusColor = _status_colors[r.status] || 'var(--muted)';
            var statusLabel = _status_labels[r.status] || r.status;
            var titleTxt = r.title || r.url || '\u2014';
            var durTxt = r.dur > 0 ? r.dur + 's' : '\u2014';
            var errDiv = r.error ? '<div style="color:#ef4444;font-size:10px;margin-top:2px">'+r.error+'</div>' : '';
            return '<tr style="border-top:1px solid var(--border)">'
              +'<td style="padding:8px 12px;max-width:300px"><div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:500">'+titleTxt+'</div>'+errDiv+'</td>'
              +'<td style="padding:8px 12px;white-space:nowrap">'+stageLabel+'</td>'
              +'<td style="padding:8px 12px"><span style="font-size:11px;font-weight:700;color:'+statusColor+';background:'+statusColor+'1a;padding:2px 8px;border-radius:20px;white-space:nowrap">'+statusLabel+'</span></td>'
              +'<td style="padding:8px 12px;color:var(--muted);white-space:nowrap">'+r.when+'</td>'
              +'<td style="padding:8px 12px;color:var(--muted)">'+durTxt+'</td>'
              +'</tr>';
          }}).join('');
        }});
      }}
      var _liveLogJson='', _liveLogStreak=0, _liveLogDelay=5000;
      function _fetchLiveLog() {{
        if(document.hidden) {{ setTimeout(_fetchLiveLog,_liveLogDelay); return; }}
        fetch('/app/posts/live-jobs')
          .then(function(r){{ return r.ok ? r.json() : []; }})
          .then(function(rows){{
            var j=JSON.stringify(rows);
            if(j!==_liveLogJson){{
              _liveLogJson=j; _liveLogStreak=0; _liveLogDelay=5000;
              _renderLog(rows);
            }} else {{
              _liveLogStreak++;
              if(_liveLogStreak>=3) _liveLogDelay=Math.min(20000,_liveLogDelay+5000);
            }}
            setTimeout(_fetchLiveLog,_liveLogDelay);
          }}).catch(function(){{ setTimeout(_fetchLiveLog,_liveLogDelay); }});
      }}
      if (document.querySelector('[id^="livelog-body-"]')) {{
        _fetchLiveLog();
      }}
    }})();

    // ── Pending post countdowns ────────────────────────────────────────────
    (function() {{
      function _fmtCountdown(seconds) {{
        seconds = Math.max(0, Math.floor(seconds || 0));
        var h = Math.floor(seconds / 3600);
        var m = Math.floor((seconds % 3600) / 60);
        var s = seconds % 60;
        if (h > 0) return h + 'h ' + m + 'm ' + s + 's';
        if (m > 0) return m + 'm ' + s + 's';
        return s + 's';
      }}
      function _tickCountdowns() {{
        var now = Date.now();
        document.querySelectorAll('[data-ph-countdown-target]').forEach(function(el) {{
          var target = Number(el.getAttribute('data-ph-countdown-target') || '0');
          if (!target) return;
          var remaining = Math.ceil((target - now) / 1000);
          el.textContent = remaining > 0 ? _fmtCountdown(remaining) : 'agora';
        }});
        document.querySelectorAll('[data-ph-elapsed-since]').forEach(function(el) {{
          var since = Number(el.getAttribute('data-ph-elapsed-since') || '0');
          if (!since) return;
          var secs = Math.floor((now - since) / 1000);
          var h2 = Math.floor(secs / 3600);
          var m2 = Math.floor((secs % 3600) / 60);
          var s2 = secs % 60;
          if (h2 > 0) el.textContent = h2 + 'h ' + m2 + 'm';
          else if (m2 > 0) el.textContent = m2 + 'm ' + s2 + 's';
          else el.textContent = secs + 's';
        }});
      }}
      _tickCountdowns();
      setInterval(_tickCountdowns, 1000);
    }})();

    // ── Sidebar toggle ──────────────────────────────────────────────────────
    (function() {{
      function toggleSidebar() {{
        var app = document.getElementById('app-root');
        if (!app) return;
        if (document.body.classList.contains('ph-mob-active') || window.innerWidth <= 900) {{
          var isOpen = app.classList.toggle('mob-nav-open');
          document.body.classList.toggle('ph-nav-open', isOpen);
          return;
        }}
        var collapsed = app.classList.toggle('sidebar-collapsed');
        localStorage.setItem('sidebar-collapsed', collapsed ? '1' : '0');
        var btn = document.getElementById('sidebar-toggle-btn');
        if (btn) btn.title = collapsed ? 'Mostrar barra lateral' : 'Ocultar barra lateral';
      }}
      window.toggleSidebar = toggleSidebar;
      window.phCloseMobNav = function() {{
        var app = document.getElementById('app-root');
        if (app) app.classList.remove('mob-nav-open');
        document.body.classList.remove('ph-nav-open');
      }};
      /* Fecha drawer ao clicar em link de nav no mobile */
      document.addEventListener('click', function(e) {{
        if (window.innerWidth > 900 && !document.body.classList.contains('ph-mob-active')) return;
        if (e.target.closest('.nav a, .nav-sub a')) {{
          var app = document.getElementById('app-root');
          if (app) app.classList.remove('mob-nav-open');
          document.body.classList.remove('ph-nav-open');
        }}
      }});
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

    // ── Diagnostic modal ─────────────────────────────────────────────────────
    function phRenderDiagItem(item, icons) {{
      var html = '<div class="diag-item ' + item.status + '">';
      html += '<div class="diag-item-row">';
      html += '<div class="diag-status-icon">' + (icons[item.status] || '&bull;') + '</div>';
      html += '<div class="diag-item-content">';
      html += '<div class="diag-label">' + item.label + '</div>';
      if (item.desc) html += '<div class="diag-desc">' + item.desc + '</div>';
      if (item.fix) html += '<div class="diag-fix">' + item.fix + '</div>';
      html += '</div></div></div>';
      return html;
    }}
    function phRenderDiagSummary(summary, soundLabel, siteLabel) {{
      if (!summary) return '';
      var intervalText = summary.interval_minutes > 0 ? summary.interval_minutes + ' min entre posts' : 'sem intervalo';
      var sourceTypes = (summary.sources_types || []).join(', ') || '—';
      var html = '<div class="diag-summary">';
      html += '<div class="diag-summary-title">&#128203; Resumo da configura&#231;&#227;o</div>';
      html += '<div class="diag-summary-grid">';
      html += '<div><span style="color:var(--muted)">Posts por sess&#227;o:</span> <b>' + summary.posts_per_day + '</b></div>';
      html += '<div><span style="color:var(--muted)">Intervalo:</span> <b>' + intervalText + '</b></div>';
      html += '<div><span style="color:var(--muted)">Fontes:</span> <b>' + summary.sources_count + ' configurada' + (summary.sources_count !== 1 ? 's' : '') + '</b></div>';
      html += '<div><span style="color:var(--muted)">Tipos:</span> <b>' + sourceTypes + '</b></div>';
      html += '<div><span style="color:var(--muted)">Som:</span> <b>' + soundLabel + '</b></div>';
      if (summary.wp_url) html += '<div style="grid-column:1/-1"><span style="color:var(--muted)">' + siteLabel + ':</span> <b>' + summary.wp_url + '</b></div>';
      html += '</div></div>';
      return html;
    }}
    function phRenderDiagFooter(closeFnName, startFnName, canStart, canReconnectStart, startText, reconnectText) {{
      var footer = '<button type="button" class="btn secondary" onclick="' + closeFnName + '()">Fechar</button>';
      if (canStart) {{
        footer += '<button type="button" class="btn diag-action-primary" onclick="' + startFnName + '(false)">&#9658; ' + startText + '</button>';
      }} else if (canReconnectStart) {{
        footer += '<button type="button" class="btn diag-action-primary" onclick="' + startFnName + '(true)">&#8635; ' + reconnectText + '</button>';
      }} else {{
        footer += '<button type="button" class="btn" disabled style="opacity:.4;cursor:not-allowed">&#9658; ' + startText + '</button>';
      }}
      return footer;
    }}
    function closeDiagModal() {{
      var overlay = document.getElementById('diagOverlay');
      if (overlay) overlay.classList.remove('open');
    }}
    function confirmDiagStart(autoReconnect) {{
      var autoInput = document.getElementById('diagAutoReconnectInput');
      if (autoInput) autoInput.value = autoReconnect ? '1' : '0';
      var footer = document.getElementById('diagFooter');
      if (footer) {{
        footer.innerHTML = '<button type="button" class="btn" disabled style="opacity:.7">&#9203; Processando...</button>';
      }}
      closeDiagModal();
      document.getElementById('diagStartForm').submit();
    }}
    function openDiagModal(botId) {{
      var overlay = document.getElementById('diagOverlay');
      if (!overlay) return;
      var inp = document.getElementById('diagBotIdInput');
      if (inp) inp.value = botId || '';
      overlay.classList.add('open');
      if (typeof window._phPlayInfoSound === 'function') window._phPlayInfoSound();
      var loadingHtml = '<div style="text-align:center;padding:32px;color:var(--muted)">'
        + '<div style="font-size:28px;margin-bottom:8px">&#9203;</div>'
        + 'Verificando configura&#231;&#245;es...</div>';
      document.getElementById('diagItems').innerHTML = loadingHtml;
      document.getElementById('diagFooter').innerHTML = '';
      var nameEl = document.getElementById('diagBotName');
      if (nameEl) nameEl.textContent = '';
      var url = '/app/robot/diagnose' + (botId ? '?bot_id=' + encodeURIComponent(botId) : '');
      fetch(url, {{credentials: 'same-origin'}})
        .then(function(r) {{
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        }})
        .then(function(data) {{
          if (data.bot_name && nameEl) nameEl.textContent = 'Rob\u00f4: ' + data.bot_name;
          var icons = {{ok:'&#9989;', warn:'&#9888;&#65039;', err:'&#10060;'}};
          var out = '';
          (data.results || []).forEach(function(item) {{
            out += phRenderDiagItem(item, icons);
          }});
          document.getElementById('diagItems').innerHTML = out || '<div style="padding:20px;text-align:center;color:var(--muted)">Nenhum resultado.</div>';
          var snd = (typeof window._phSoundLabel === 'function') ? window._phSoundLabel() : '&#129534; Caixa';
          document.getElementById('diagItems').innerHTML += phRenderDiagSummary(data.summary, snd, 'Site WordPress');
          document.getElementById('diagFooter').innerHTML = phRenderDiagFooter(
            'closeDiagModal',
            'confirmDiagStart',
            !!data.can_start,
            !!data.can_reconnect_start,
            'Iniciar Rob\u00f4',
            'Reconectar e iniciar'
          );
        }})
        .catch(function(err) {{
          document.getElementById('diagItems').innerHTML = '<div style="text-align:center;padding:24px;color:#ef4444">&#10060; Erro: ' + err.message + '</div>';
          document.getElementById('diagFooter').innerHTML = '<button type="button" class="btn secondary" onclick="closeDiagModal()">Fechar</button>';
        }});
    }}

    // ── Mobile Preview ────────────────────────────────────────────────────────
    (function(){{
      var SIZES = [390, 412, 768];
      var LABELS = {{390:'iPhone 390', 412:'Pixel 412', 768:'iPad 768'}};

      function setWidth(w) {{
        localStorage.setItem('ph-mob-w', w);
        var root = document.getElementById('app-root');
        if (root) {{ root.style.width = w + 'px'; root.style.minWidth = w + 'px'; root.style.maxWidth = w + 'px'; }}
        SIZES.forEach(function(s) {{
          var b = document.getElementById('phMobBtn_' + s);
          if (b) b.classList.toggle('pmb-active', s === w);
        }});
      }}

      window.phOpenMobPreview = function() {{
        document.body.classList.add('ph-mob-active');
        localStorage.setItem('ph-mob-active', '1');
        var w = parseInt(localStorage.getItem('ph-mob-w') || '390', 10);
        setWidth(w);
        // close dev menu
        var dd = document.getElementById('devMenuDd');
        if (dd) dd.classList.remove('open');
        var btn = document.getElementById('devMenuBtn');
        if (btn) btn.classList.remove('open');
      }};
      window.phCloseMobPreview = function() {{
        document.body.classList.remove('ph-mob-active', 'ph-nav-open');
        localStorage.removeItem('ph-mob-active');
        var root = document.getElementById('app-root');
        if (root) {{ root.style.width = ''; root.style.minWidth = ''; root.style.maxWidth = ''; root.classList.remove('mob-nav-open'); }}
      }};
      window.phMobSetW = function(w) {{ setWidth(w); }};

      // Restore on load
      if (localStorage.getItem('ph-mob-active') === '1') {{
        document.body.classList.add('ph-mob-active');
        var _w = parseInt(localStorage.getItem('ph-mob-w') || '390', 10);
        // defer until DOM ready
        document.addEventListener('DOMContentLoaded', function() {{ setWidth(_w); }});
      }}
      document.addEventListener('keydown', function(e) {{
        if (e.key === 'Escape' && document.body.classList.contains('ph-mob-active')) phCloseMobPreview();
      }});
    }})();
  </script>

  <!-- Mobile Preview Bar (fixed, shows when ph-mob-active) -->
  <div id="phMobBar">
    <span style="font-size:12px;font-weight:700;color:#a78bfa;margin-right:4px">&#128241; Mobile Preview</span>
    <button class="ph-mob-size-btn pmb-active" id="phMobBtn_390" onclick="phMobSetW(390)">iPhone 390px</button>
    <button class="ph-mob-size-btn" id="phMobBtn_412" onclick="phMobSetW(412)">Pixel 412px</button>
    <button class="ph-mob-size-btn" id="phMobBtn_768" onclick="phMobSetW(768)">iPad 768px</button>
    <button class="ph-mob-close-btn" onclick="phCloseMobPreview()">&#10005; Sair do Preview</button>
  </div>
</body>
</html>"""
    # Strip surrogate characters that can come from DB emoji/JSON fields
    safe_page = page.encode("utf-8", errors="replace").decode("utf-8")
    return HTMLResponse(safe_page, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    })


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
  <title>Entrar ” PostHub</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet" />
  <link rel="stylesheet" href="/static/posthub.css?v=8" />
  <style>
    html, body {{
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 16px;
      box-sizing: border-box;
    }}
    .login-wrap {{
      width: 100%;
      max-width: 420px;
    }}
    .login-logo {{
      width: 100%;
      max-width: 200px;
      height: 56px;
      object-fit: contain;
      display: block;
      margin: 0 auto 24px;
      filter: drop-shadow(0 8px 24px rgba(139,92,246,.35));
    }}
    .login-card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 24px;
      padding: 32px 28px;
      box-shadow: 0 24px 60px rgba(0,0,0,.35);
    }}
    .login-title {{
      font-size: 22px;
      font-weight: 800;
      margin: 0 0 4px;
      color: var(--text);
    }}
    .login-sub {{
      font-size: 13px;
      color: var(--muted);
      margin: 0 0 24px;
    }}
    .login-err {{
      background: rgba(239,68,68,.1);
      border: 1px solid rgba(239,68,68,.3);
      color: #f87171;
      border-radius: 10px;
      padding: 10px 14px;
      font-size: 13px;
      margin-bottom: 16px;
    }}
    @media (max-width: 480px) {{
      .login-card {{ padding: 24px 18px; border-radius: 20px; }}
      .login-title {{ font-size: 20px; }}
    }}
  </style>
  <script>(function(){{ document.documentElement.setAttribute('data-theme', localStorage.getItem('posthub-theme') || 'roxo'); }})();</script>
</head>
<body>
  <div class="login-wrap">
    <img class="login-logo" src="/brand/logo_posthub.png" alt="PostHub"
         onerror="this.onerror=null;this.src='/static/logo.svg';" />
    <div class="login-card">
      <h1 class="login-title">Entrar no PostHub</h1>
      <p class="login-sub">Use seu usuário e senha para acessar o painel.</p>
      {f'<div class="login-err">{msg}</div>' if msg else ''}
      <form method="post" action="/app/login">
        <label>Usuário / E-mail</label>
        <input name="email" type="text" placeholder="usuario ou email@exemplo.com"
               autocomplete="username" autocapitalize="none" required />
        <label style="margin-top:14px">Senha</label>
        <input name="password" type="password" placeholder="••••••"
               autocomplete="current-password" required />
        <button class="btn" type="submit" style="width:100%;margin-top:20px;padding:13px">
          Entrar
        </button>
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
        if _ensure_publish_config_defaults(db, bot=bot):
            db.commit()
        return bot
    # Nenhum ativo ” retorna o primeiro sem forçar ativação
    bot = db.scalar(
        select(AutomationProfile)
        .where(AutomationProfile.user_id == uid)
        .order_by(AutomationProfile.created_at.asc())
        .limit(1)
    )
    if bot:
        if _ensure_publish_config_defaults(db, bot=bot):
            db.commit()
        return bot
    # Nenhum perfil ” cria o primeiro
    bot = AutomationProfile(
        user_id=uid, name="Meu Primeiro Robô", active=True,
        schedule_config_json={"posts_per_day": 15, "interval_minutes": 60},
        anti_block_config_json={},
        publish_config_json={"facebook_link": "comments", "default_category": "Receitas", "categories": list(DEFAULT_RECIPE_CATEGORIES)},
    )
    db.add(bot); db.commit(); db.refresh(bot)
    _ensure_default_recipe_actions(db, bot=bot)
    return bot


def _parse_prompt_variants(prompt_text: str) -> tuple[str, str, str, str, str]:
    """Parse prompt_text into (v1, v2, v3, v4, mode). Handles plain string (legacy) and JSON."""
    text = (prompt_text or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
            variants = data.get("v") or []
            mode = str(data.get("mode") or "1")
            v1 = str(variants[0]).strip() if len(variants) > 0 else ""
            v2 = str(variants[1]).strip() if len(variants) > 1 else ""
            v3 = str(variants[2]).strip() if len(variants) > 2 else ""
            v4 = str(variants[3]).strip() if len(variants) > 3 else ""
            return v1, v2, v3, v4, mode
        except Exception:
            pass
    return text, "", "", "", "1"


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

/* ── Panel ” centrado por CSS, sem inline style ───── */
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
          <option value="gemini-2.0-flash">gemini-2.0-flash ” Rápido, mais recente ⚡</option>
          <option value="gemini-2.0-flash-lite">gemini-2.0-flash-lite ” Leve e econômico 🪶</option>
          <option value="gemini-1.5-flash-latest" selected>gemini-1.5-flash-latest ” Padrão recomendado ✅</option>
          <option value="gemini-1.5-flash-8b">gemini-1.5-flash-8b ” Ultra rápido, menor 🏎</option>
          <option value="gemini-1.5-pro-latest">gemini-1.5-pro-latest ” Mais inteligente, mais lento 🧠</option>
          <option value="gemini-2.0-pro-exp">gemini-2.0-pro-exp ” Experimental, Pro 2.0 🔬</option>
        </select>
        <div style="margin-top:8px;font-size:11px;color:var(--muted)">Dúvida? Deixe <b>gemini-1.5-flash-latest</b> ” funciona bem para a maioria dos casos.</div>
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
      : _row('🔧', 'WordPress', '<span style="opacity:.6;font-weight:400">Não configurado ” adicione depois</span>', false);
    rows += gem
      ? _row('✨', 'Gemini AI', 'Chave configurada ✓', true)
      : _row('✨', 'Gemini AI', '<span style="opacity:.6;font-weight:400">Não configurado ” adicione depois</span>', false);
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

  /* ── Drag (mouse ” desktop) ────────────────────── */
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
    if (_isMobile()) return; /* bottom-sheet on phone ” no drag */
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
    if any(_ensure_publish_config_defaults(db, bot=pr) for pr in all_profiles):
        db.commit()
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
    # ── Contagens em 2 queries em vez de 8 ──────────────────────────────────────
    _jc = db.execute(
        select(
            func.sum(case((Job.status == JobStatus.queued, 1), else_=0)).label("queued"),
            func.sum(case(((Job.status == JobStatus.queued) & (Job.run_at <= now), 1), else_=0)).label("queued_due"),
            func.sum(case(((Job.status == JobStatus.queued) & (Job.run_at > now), 1), else_=0)).label("queued_sched"),
            func.sum(case((Job.status == JobStatus.running, 1), else_=0)).label("running"),
        ).where(Job.profile_id == bot.id)
    ).one()
    queued_jobs      = int(_jc.queued or 0)
    queued_due       = int(_jc.queued_due or 0)
    queued_scheduled = int(_jc.queued_sched or 0)
    running_jobs     = int(_jc.running or 0)

    _pc = db.execute(
        select(
            func.sum(case((Post.status == PostStatus.pending, 1), else_=0)).label("pending"),
            func.sum(case((Post.status == PostStatus.processing, 1), else_=0)).label("processing"),
            func.sum(case((Post.status == PostStatus.failed, 1), else_=0)).label("failed"),
        ).where(Post.profile_id == bot.id)
    ).one()
    pending_posts    = int(_pc.pending or 0)
    processing_posts = int(_pc.processing or 0)
    failed_count     = int(_pc.failed or 0)

    in_progress = (queued_jobs + running_jobs + pending_posts + processing_posts) > 0

    # ── Integrations: 1 query para buscar Gemini + WP juntos ────────────────────
    _integs = list(db.scalars(
        select(Integration).where(
            Integration.profile_id == bot.id,
            Integration.type.in_([IntegrationType.GEMINI, IntegrationType.WORDPRESS])
        )
    ))
    _gemini_integ = next((i for i in _integs if i.type == IntegrationType.GEMINI), None)
    _wp_integ_check = next((i for i in _integs if i.type == IntegrationType.WORDPRESS), None)
    gemini_ok = _gemini_integ is not None
    gemini_status = "OK" if gemini_ok else "FALTANDO"
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
    # ── Batch stats de todos os projetos ” 2 queries no total ───────────────────
    _all_ids = [pr.id for pr in all_profiles]
    # Post counts por projeto (1 query)
    _proj_post_rows = db.execute(
        select(
            Post.profile_id,
            func.sum(case((Post.status == PostStatus.completed, 1), else_=0)).label("completed"),
            func.sum(case((Post.status == PostStatus.failed, 1), else_=0)).label("failed"),
            func.sum(case((Post.status.in_([PostStatus.pending, PostStatus.processing]), 1), else_=0)).label("pending"),
        ).where(Post.profile_id.in_(_all_ids)).group_by(Post.profile_id)
    ).all()
    _proj_counts = {r.profile_id: (int(r.completed or 0), int(r.failed or 0), int(r.pending or 0)) for r in _proj_post_rows}
    _proj_collect_plan = {pr.id: _active_collect_plan(db, profile_id=pr.id) for pr in all_profiles}
    # WP integrations por projeto (1 query)
    _proj_wp_rows = list(db.scalars(
        select(Integration).where(
            Integration.profile_id.in_(_all_ids),
            Integration.type == IntegrationType.WORDPRESS
        )
    ))
    _proj_wp = {w.profile_id: w for w in _proj_wp_rows}

    def _proj_stats(pr):
        wp = _proj_wp.get(pr.id)
        wp_url = ""
        if wp:
            try:
                creds = decrypt_json(wp.credentials_encrypted)
                wp_url = (creds.get("base_url") or "") if isinstance(creds, dict) else ""
            except Exception:
                pass
        completed, failed, pending = _proj_counts.get(pr.id, (0, 0, 0))
        collect_plan = _proj_collect_plan.get(pr.id) or {}
        pending_total = pending + int(collect_plan.get("missing") or 0)
        requested = int(collect_plan.get("requested") or 0)
        return wp_url, completed, failed, pending_total, requested

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
        wp_url, p_done, p_fail, p_pend, p_goal = _proj_stats(pr)
        pr_emoji = _safe((pr.publish_config_json or {}).get("emoji") or "🤖")

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
              <div style="text-align:center"><div style="font-size:16px;font-weight:800;color:#6366f1">{p_goal if p_goal > 0 else '—'}</div><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Meta</div></div>
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
        # Per-bot cards com botão Iniciar individual
        _bot_cards_html = ""
        for _pr in all_profiles:
            if not _pr.active:
                continue
            _qj = int(db.scalar(select(func.count()).select_from(Job).where(Job.profile_id == _pr.id, Job.status == JobStatus.queued)) or 0)
            _rj = int(db.scalar(select(func.count()).select_from(Job).where(Job.profile_id == _pr.id, Job.status == JobStatus.running)) or 0)
            _pp = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == _pr.id, Post.status.in_([PostStatus.pending, PostStatus.processing]))) or 0)
            _ip = (_qj + _rj + _pp) > 0
            _pr_emoji = _safe((_pr.publish_config_json or {}).get("emoji") or "🤖")
            _pr_name_esc = html.escape(_pr.name)
            _pr_id = _pr.id
            if _ip:
                _card_btn = (f"<button type='button' class='btn-running ph-stop-bot-btn' data-bot-id='{_pr_id}'>"
                             f"&#9632; Parar Bot</button>")
                _icon_bg = "linear-gradient(135deg,#10b981,#059669)"
                _card_border = "2px solid #10b981"
                _card_bg = "rgba(16,185,129,.06)"
                _sub = "<span style='font-size:11px;color:var(--muted)'>Clique no bot&#227;o para parar</span>"
            else:
                _card_btn = f"<button type='button' class='btn' onclick=\"openDiagModal('{_pr_id}')\" style='font-size:13px;padding:8px 18px'>&#9658; Iniciar</button>"
                _icon_bg = "linear-gradient(135deg,var(--primary),var(--pink))"
                _card_border = "1px solid var(--border)"
                _card_bg = "var(--surface2)"
                _sub = "<span style='display:inline-flex;align-items:center;gap:5px;font-size:11px;color:#10b981;background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.25);border-radius:20px;padding:2px 9px'><span class='dot-pulse'></span>Online</span>"
            _bot_cards_html += (f"<div data-bot-id='{_pr_id}' data-bot-running='{'1' if _ip else '0'}'"
                                f" style='display:flex;align-items:center;justify-content:space-between;gap:12px;"
                                f"padding:10px 14px;background:{_card_bg};border:{_card_border};"
                                f"border-radius:12px;flex:1;min-width:200px'>"
                                f"<div style='display:flex;align-items:center;gap:10px'>"
                                f"<div style='width:38px;height:38px;border-radius:10px;background:{_icon_bg};"
                                f"display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0'>{_pr_emoji}</div>"
                                f"<div><div style='font-size:14px;font-weight:700'>{_pr_name_esc}</div>"
                                f"<div style='margin-top:3px'>{_sub}</div></div></div>"
                                f"<div style='flex-shrink:0'>{_card_btn}</div></div>")

        _any_running = any(
            (
                int(db.scalar(select(func.count()).select_from(Job).where(Job.profile_id == _pr.id, Job.status.in_([JobStatus.queued, JobStatus.running]))) or 0)
                + int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == _pr.id, Post.status.in_([PostStatus.pending, PostStatus.processing]))) or 0)
            ) > 0
            for _pr in all_profiles if _pr.active
        )
        _pub_alert = ""
        if _any_running:
            _pub_alert = "<div class='publishing-alert'><span class='pal-dot'></span>&#9889; Bot publicando agora</div>"

        _active_banner = f"""
    <div class="active-project-banner" style="margin-bottom:14px;flex-direction:column;align-items:stretch;gap:10px">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
        <div>
          <div class="active-project-label">Robôs ativos</div>
          <div style="font-size:13px;color:var(--muted);margin-top:2px">{active_count} de {MAX_ACTIVE} ligados ” clique em Iniciar para processar agora</div>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn secondary" style="font-size:13px;padding:7px 14px" type="button" onclick="openWizard()">+ Novo Projeto</button>
        </div>
      </div>
      {_pub_alert}
      <div style="display:flex;flex-wrap:wrap;gap:8px" id="bot-cards-wrap">
        {_bot_cards_html}
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

    {_ph("secao-controle-robo")}"""

    # ── Per-bot control cards ────────────────────────────────────────────────
    _now_ctrl = datetime.utcnow()
    for _bpr in all_profiles:
        _bpr_id    = _bpr.id
        _bpr_name  = html.escape(_bpr.name)
        _bpr_emoji = _safe((_bpr.publish_config_json or {}).get("emoji") or "🤖")
        _bpr_active = _bpr.active

        # per-bot stats
        _b_qd   = int(db.scalar(select(func.count()).select_from(Job).where(Job.profile_id == _bpr_id, Job.status == JobStatus.queued,   Job.run_at <= _now_ctrl)) or 0)
        _b_qs   = int(db.scalar(select(func.count()).select_from(Job).where(Job.profile_id == _bpr_id, Job.status == JobStatus.queued,   Job.run_at >  _now_ctrl)) or 0)
        _b_rj   = int(db.scalar(select(func.count()).select_from(Job).where(Job.profile_id == _bpr_id, Job.status == JobStatus.running)) or 0)
        _b_pp   = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == _bpr_id, Post.status == PostStatus.pending))    or 0)
        _b_proc = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == _bpr_id, Post.status == PostStatus.processing)) or 0)
        _b_fail = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == _bpr_id, Post.status == PostStatus.failed))     or 0)

        _b_lc   = db.scalar(select(JobLog).where(JobLog.profile_id == _bpr_id, JobLog.stage == JOB_COLLECT, JobLog.message == "collect_completed").order_by(JobLog.created_at.desc()).limit(1))
        _b_meta = (_b_lc.meta_json or {}) if _b_lc else {}
        _b_created = int(_b_meta.get("created") or 0)
        _b_skipped = int(_b_meta.get("skipped_duplicate") or _b_meta.get("skipped") or 0)
        _b_ignored = int(_b_meta.get("skipped_non_recipe") or 0) + int(_b_meta.get("skipped_error") or 0)

        if _bpr_active:
            _b_badge  = "<span class='badge-active' style='font-size:11px;padding:3px 9px'><span class='dot-pulse'></span>Ativo</span>"
            _b_iconbg = "linear-gradient(135deg,#10b981,#059669)"
            _b_open   = "open"
        else:
            _b_badge  = "<span class='badge-inactive' style='font-size:11px;padding:3px 9px'><span class='dot-off'></span>Inativo</span>"
            _b_iconbg = "var(--surface2)"
            _b_open   = ""

        if _bpr_active:
            _b_actions = f"""
              <div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:16px">
                <span class="active-project-stat">Coleta: <b>{_b_created}</b> novos / <b>{_b_skipped}</b> rep. / <b>{_b_ignored}</b> ign.</span>
                <span class="active-project-stat">Fila: <b>{_b_qd}</b> prontos / <b>{_b_qs}</b> agend. / <b>{_b_rj}</b> rod.</span>
                <span class="active-project-stat">Posts: <b>{_b_pp}</b> pend. / <b>{_b_proc}</b> proc.</span>
              </div>
              <div class="robot-actions">
                {f'<div class="robot-action-card"><div class="robot-action-icon" style="background:rgba(245,158,11,.15);color:#f59e0b">&#9889;</div><div><div class="robot-action-title">Rodar pendentes agora</div><div class="robot-action-desc muted">{_b_qs} jobs agendados aguardando</div></div><div style="margin-left:auto"><button class="btn secondary ph-run-now-btn" data-bot-id="{_bpr_id}" style="white-space:nowrap">Rodar agora</button></div></div>' if _b_qs > 0 and _b_rj == 0 else ''}
                <div class="robot-action-card">
                  <div class="robot-action-icon" style="background:rgba(99,102,241,.15);color:#6366f1">&#8635;</div>
                  <div>
                    <div class="robot-action-title">Reprocessar IA</div>
                    <div class="robot-action-desc muted">{_b_fail} posts com falha</div>
                    <div style="font-size:11px;color:var(--muted);margin-top:5px;max-width:340px;line-height:1.5">
                      Tenta novamente a gera&#231;&#227;o de conte&#250;do via IA para posts que falharam. Use quando houver erros de API da IA ou timeout.
                    </div>
                  </div>
                  <form method="post" action="/app/robot/retry-ai" style="margin-left:auto">
                    <button class="btn secondary" type="submit" style="white-space:nowrap">&#8635; Reprocessar ({_b_fail})</button>
                  </form>
                </div>
              </div>
              <details style="margin-top:12px">
                <summary style="cursor:pointer;font-size:13px;color:var(--muted);padding:8px 4px;list-style:none;display:flex;align-items:center;gap:6px;border-top:1px solid var(--border)">
                  <span style="font-size:9px">&#9655;</span> A&#231;&#245;es avan&#231;adas
                </summary>
                <div style="display:flex;flex-direction:column;gap:8px;margin-top:10px">
                  <div class="robot-action-card">
                    <div class="robot-action-icon" style="background:rgba(239,68,68,.12);color:#ef4444">&#128465;</div>
                    <div><div class="robot-action-title">Limpar falhas</div><div class="robot-action-desc muted">Remove posts com erro da fila</div></div>
                    <form method="post" action="/app/robot/clear-failures" style="margin-left:auto">
                      <button class="btn secondary" type="submit">Limpar falhas</button>
                    </form>
                  </div>
                  <div class="robot-action-card">
                    <div class="robot-action-icon" style="background:rgba(239,68,68,.12);color:#ef4444">&#128465;</div>
                    <div><div class="robot-action-title">Limpar hist&#243;rico</div><div class="robot-action-desc muted">Remove posts do PostHub (n&#227;o apaga do WP)</div></div>
                    <form method="post" action="/app/robot/clear-posts" style="margin-left:auto">
                      <button class="btn secondary" type="submit">Limpar posts</button>
                    </form>
                  </div>
                </div>
              </details>"""
        else:
            _b_done = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == _bpr_id, Post.status == PostStatus.completed)) or 0)
            _b_actions = f"""
              <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;padding:10px 0">
                <div style="font-size:13px;color:var(--muted)">
                  Este bot est&#225; <b>inativo</b>. Ligue-o na tabela acima para come&#231;ar a publicar.
                </div>
                <div style="display:flex;gap:12px;flex-wrap:wrap">
                  <span class="active-project-stat">&#10003; <b>{_b_done}</b> publicados</span>
                  <span class="active-project-stat">&#9203; <b>{_b_pp}</b> pendentes</span>
                  <span class="active-project-stat">&#10005; <b>{_b_fail}</b> falhas</span>
                </div>
                <a href="/app/profiles/{_bpr_id}" class="btn secondary" style="font-size:12px;padding:6px 14px">&#9881; Configurar</a>
              </div>"""

        body += f"""
    <div class="card" style="margin-bottom:10px">
      <details class="toggle-section" {_b_open}>
        <summary>
          <span class="ts-title" style="display:flex;align-items:center;gap:10px">
            <div style="width:32px;height:32px;border-radius:9px;background:{_b_iconbg};display:flex;align-items:center;justify-content:center;font-size:17px;flex-shrink:0;border:1px solid var(--border)">{_bpr_emoji}</div>
            <span style="font-weight:700">{_bpr_name}</span>
            {_b_badge}
          </span>
          <span class="ts-arrow">&#9655;</span>
        </summary>
        <div class="ts-body">
          {_b_actions}
        </div>
      </details>
      </div>
    </details>"""

    body += f"""

    """
    diag_modal = """
    <div class="diag-overlay" id="diagOverlay" onclick="if(event.target===this)closeDiagModal()">
      <div class="diag-modal">
        <div class="diag-header">
          <div>
            <div style="font-weight:700;font-size:17px">Diagn&#243;stico antes de iniciar</div>
            <div id="diagBotName" style="font-size:12px;color:var(--muted);margin-top:2px"></div>
          </div>
          <button class="diag-close" type="button" onclick="closeDiagModal()">&times;</button>
        </div>
        <div class="diag-body">
        <div id="diagItems" style="display:flex;flex-direction:column;gap:10px;min-height:80px">
          <div style="text-align:center;padding:32px;color:var(--muted)">
            <div style="font-size:28px;margin-bottom:8px">&#9203;</div>
            Verificando configura&#231;&#245;es...
          </div>
        </div>
        </div>
        <div id="diagFooter" class="diag-footer"></div>
      </div>
    </div>
    <form id="diagStartForm" method="post" action="/app/robot/start" style="display:none">
      <input type="hidden" id="diagBotIdInput" name="bot_id" value="">
      <input type="hidden" id="diagAutoReconnectInput" name="auto_reconnect" value="0">
    </form>
    """
    body = body + diag_modal
    return _layout("Robô", body, user=user)


@router.get("/app/bot", include_in_schema=False)
def bot_redirect(user: User = Depends(get_current_user), db=Depends(get_db)):
    bot = _get_or_create_single_bot(db, user=user)
    return RedirectResponse(f"/app/profiles/{bot.id}", status_code=status.HTTP_302_FOUND)


def _wordpress_connection_for_bot(db, *, bot_id: str):
    integ = db.scalar(select(Integration).where(Integration.profile_id == bot_id, Integration.type == IntegrationType.WORDPRESS))
    if not integ:
        return None, None, None, ""
    creds = decrypt_json(integ.credentials_encrypted)
    base_url = str(creds.get("base_url") or "").rstrip("/")
    users = creds.get("users") if isinstance(creds.get("users"), list) else []
    if not users and creds.get("username"):
        users = [{"username": creds["username"], "app_password": creds.get("app_password", "")}]
    active_uname = str(creds.get("active_username") or "")
    active_user = next((u for u in users if u.get("username") == active_uname), users[0] if users else None)
    return integ, creds, active_user, base_url


def _test_wordpress_connection(*, base_url: str, active_user: dict | None, timeout: float = 8.0) -> dict:
    import base64 as _b64
    from urllib.parse import urljoin as _urljoin

    import certifi as _certifi
    import httpx as _httpx

    from app.services.wordpress import WORDPRESS_USER_AGENT

    if not base_url:
        return {"ok": False, "retryable": False, "label": "URL do site nao informada", "detail": "Base URL vazia."}
    if not active_user or not active_user.get("username") or not active_user.get("app_password"):
        return {"ok": False, "retryable": False, "label": "Usuario WordPress sem credenciais", "detail": "Usuario ou App Password vazios."}

    username = str(active_user.get("username") or "")
    app_password = str(active_user.get("app_password") or "")
    token = _b64.b64encode(f"{username}:{app_password}".encode("utf-8")).decode("ascii")
    test_url = _urljoin(base_url.rstrip("/") + "/", "wp-json/wp/v2/users/me?context=edit")
    verify = False if settings.http_insecure_skip_verify else _certifi.where()
    try:
        resp = _httpx.get(
            test_url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {token}",
                "User-Agent": WORDPRESS_USER_AGENT,
            },
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
            verify=verify,
        )
    except Exception as e:
        return {
            "ok": False,
            "retryable": True,
            "status_code": None,
            "label": "WordPress inacessivel",
            "detail": str(e)[:160],
        }

    status_code = int(resp.status_code)
    if status_code == 200:
        try:
            data = resp.json()
        except Exception:
            data = {}
        display_name = data.get("name") or username
        roles = data.get("roles") or []
        return {
            "ok": True,
            "retryable": False,
            "status_code": status_code,
            "display_name": display_name,
            "roles": roles,
            "label": f"WordPress OK - {display_name}",
            "detail": f"Conectado em {base_url}.",
        }

    detail = (resp.text or "").strip().replace("\n", " ")[:180]
    retryable = status_code in {408, 409, 425, 429, 500, 502, 503, 504}
    if status_code == 401:
        label = "Credenciais invalidas"
    elif status_code == 403:
        label = "Acesso bloqueado"
    else:
        label = f"WordPress respondeu {status_code}"
    return {
        "ok": False,
        "retryable": retryable,
        "status_code": status_code,
        "label": label,
        "detail": detail or f"HTTP {status_code}",
    }


def _try_reconnect_wordpress(db, integ: Integration, *, base_url: str, active_user: dict | None) -> dict:
    result = _test_wordpress_connection(base_url=base_url, active_user=active_user)
    if not result.get("ok") and result.get("retryable"):
        result = _test_wordpress_connection(base_url=base_url, active_user=active_user, timeout=12.0)
    integ.status = IntegrationStatus.CONNECTED if result.get("ok") else IntegrationStatus.ERROR
    db.add(integ)
    db.flush()
    return result


def _revive_profile_queue(db, *, profile_id: str, force_now: bool = False) -> int:
    now = datetime.utcnow()
    did = 0
    stuck_cutoff = now - timedelta(minutes=5)
    did += int(db.execute(
        update(Job).where(
            Job.profile_id == profile_id,
            Job.status == JobStatus.running,
            Job.locked_at < stuck_cutoff,
        ).values(status=JobStatus.queued, run_at=now, locked_at=None, locked_by=None, updated_at=now)
    ).rowcount or 0)

    pipeline = [JOB_CLEAN, JOB_AI, JOB_PUBLISH_WP]
    orphan_posts = list(db.scalars(
        select(Post).where(
            Post.profile_id == profile_id,
            Post.status.in_([PostStatus.pending, PostStatus.processing]),
        )
    ))
    for p in orphan_posts:
        active = int(db.scalar(
            select(func.count()).select_from(Job).where(
                Job.post_id == p.id,
                Job.status.in_([JobStatus.queued, JobStatus.running]),
            )
        ) or 0)
        if active:
            continue
        last_ok_type = db.scalar(
            select(Job.type).where(Job.post_id == p.id, Job.status == JobStatus.succeeded)
            .order_by(Job.updated_at.desc()).limit(1)
        )
        if last_ok_type in pipeline:
            idx = pipeline.index(last_ok_type)
            next_job = pipeline[idx + 1] if idx + 1 < len(pipeline) else JOB_PUBLISH_WP
        else:
            next_job = JOB_CLEAN
        run_at = now
        if not force_now and p.scheduled_for and p.scheduled_for > now:
            run_at = p.scheduled_for
        p.status = PostStatus.pending
        p.updated_at = now
        db.add(p)
        enqueue_job(
            db,
            user_id=p.user_id,
            profile_id=p.profile_id,
            post_id=p.id,
            job_type=next_job,
            payload={"collected_content_id": p.collected_content_id},
            run_at=run_at,
        )
        did += 1
    return did


@router.post("/app/robot/start", include_in_schema=False)
def robot_start(
    bot_id: str = Form(default=None),
    auto_reconnect: str = Form("0"),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    if bot_id:
        bot = db.scalar(select(AutomationProfile).where(AutomationProfile.id == bot_id, AutomationProfile.user_id == user.id))
        if not bot:
            return RedirectResponse("/app/robot?msg=Projeto+não+encontrado.", status_code=status.HTTP_302_FOUND)
    else:
        bot = _get_or_create_single_bot(db, user=user)

    try:
        wp_integ, _wp_creds, active_user, base_url = _wordpress_connection_for_bot(db, bot_id=bot.id)
    except Exception:
        wp_integ, active_user, base_url = None, None, ""
    if not wp_integ or not base_url or not active_user or not active_user.get("username") or not active_user.get("app_password"):
        return RedirectResponse(
            f"/app/robot?msg={quote_plus('WordPress não configurado. Vá em Configurar → Integrações → WordPress e adicione a URL do site, usuário e App Password.')}",
            status_code=status.HTTP_302_FOUND,
        )
    wp_test = _try_reconnect_wordpress(db, wp_integ, base_url=base_url, active_user=active_user)
    if not wp_test.get("ok"):
        action = "reconectar" if str(auto_reconnect) == "1" else "conectar"
        detail = str(wp_test.get("detail") or wp_test.get("label") or "")[:140]
        msg = f"Nao foi possivel {action} ao WordPress: {wp_test.get('label') or 'falha de conexao'}"
        if detail:
            msg += f" ({detail})"
        db.commit()
        return RedirectResponse(f"/app/robot?msg={quote_plus(msg)}", status_code=status.HTTP_302_FOUND)

    bot.active = True
    _ensure_publish_config_defaults(db, bot=bot)
    _set_bot_run_stopped(db, bot=bot, stopped=False)
    revived = _revive_profile_queue(db, profile_id=bot.id)
    queued_jobs = int(db.scalar(select(func.count()).select_from(Job).where(Job.profile_id == bot.id, Job.status == JobStatus.queued)) or 0)
    running_jobs = int(db.scalar(select(func.count()).select_from(Job).where(Job.profile_id == bot.id, Job.status == JobStatus.running)) or 0)
    pending_posts = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == bot.id, Post.status == PostStatus.pending)) or 0)
    processing_posts = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == bot.id, Post.status == PostStatus.processing)) or 0)
    if (queued_jobs + running_jobs + pending_posts + processing_posts) > 0:
        db.commit()
        msg = "Fila retomada e em andamento." if revived else "Postagens em andamento. Acompanhe em Posts."
        return RedirectResponse(f"/app/posts?msg={quote_plus(msg)}", status_code=status.HTTP_302_FOUND)
    cfg = bot.schedule_config_json or {}
    limit = int(cfg.get("posts_per_day") or 15)
    respect = int(cfg.get("respect_schedule") or 0) == 1
    interval_minutes = int(cfg.get("interval_minutes") or 0)  # always apply, regardless of respect_schedule
    # Clear all existing posts so the bot counts from zero on this new run.
    # keep_collected=True preserves URL fingerprints so the collector never re-posts the same article.
    _all_post_ids = list(db.scalars(select(Post.id).where(Post.profile_id == bot.id)))
    if _all_post_ids:
        _delete_posts(db, profile_id=bot.id, post_ids=[str(x) for x in _all_post_ids], keep_collected=True)
    enqueue_job(
        db,
        user_id=bot.user_id,
        profile_id=bot.id,
        job_type=JOB_COLLECT,
        payload={"limit": limit, "interval_minutes": interval_minutes, "respect_schedule": 1 if respect else 0},
    )
    db.commit()
    return RedirectResponse("/app/posts", status_code=status.HTTP_302_FOUND)


@router.get("/app/robot/diagnose", include_in_schema=False)
def robot_diagnose(bot_id: str = Query(default=None), user: User = Depends(get_current_user), db=Depends(get_db)):
    """Diagnostico rapido: verifica WP credentials + fontes antes de iniciar."""
    import traceback as _tb
    from fastapi.responses import JSONResponse
    try:
        if bot_id:
            bot = db.scalar(select(AutomationProfile).where(AutomationProfile.id == bot_id, AutomationProfile.user_id == user.id))
            if not bot:
                return JSONResponse({"error": "bot not found", "results": [], "can_start": False}, status_code=404)
        else:
            bot = _get_or_create_single_bot(db, user=user)
    except Exception as _e:
        return JSONResponse({"error": f"init error: {_tb.format_exc()[:800]}", "results": [], "can_start": False}, status_code=500)
    results = []

    # ── 1. WordPress ────────────────────────────────────────────
    wp_can_reconnect = False
    wp_read_error = False
    try:
        wp_integ, _wp_creds, active_user, base_url = _wordpress_connection_for_bot(db, bot_id=bot.id)
    except Exception as e:
        wp_read_error = True
        wp_integ, active_user, base_url = None, None, ""
        results.append({"key": "wordpress", "status": "err", "label": "Erro ao ler credenciais",
                         "desc": str(e)[:120], "fix": "Reconfigure a integração WordPress."})
    if not wp_integ and not wp_read_error:
        results.append({"key": "wordpress", "status": "err", "label": "WordPress não configurado",
                        "desc": "Nenhuma integração WordPress encontrada.",
                        "fix": f"Vá em <a href='/app/profiles/{bot.id}?tab=integracoes'>Integrações → WordPress</a> e adicione URL, usuário e App Password."})
    else:
        if not base_url:
            results.append({"key": "wordpress", "status": "err", "label": "URL do site não informada",
                             "desc": "O campo Base URL está vazio.",
                             "fix": f"Vá em <a href='/app/profiles/{bot.id}?tab=integracoes'>Integrações → WordPress</a> e preencha a URL do site."})
        elif not active_user or not active_user.get("username") or not active_user.get("app_password"):
            results.append({"key": "wordpress", "status": "err", "label": "Usuário WordPress sem credenciais",
                             "desc": "Usuário ou App Password estão vazios.",
                             "fix": f"Vá em <a href='/app/profiles/{bot.id}?tab=integracoes'>Integrações → WordPress</a> e adicione o App Password."})
        else:
            wp_can_reconnect = True
            try:
                wp_test = _try_reconnect_wordpress(db, wp_integ, base_url=base_url, active_user=active_user)
                db.commit()
            except Exception as _wpe:
                import traceback as _tbtmp
                db.rollback()
                wp_test = {"ok": False, "retryable": False, "detail": f"Erro interno: {_tbtmp.format_exc()[-1200:]}"}
            if wp_test.get("ok"):
                display_name = wp_test.get("display_name") or active_user["username"]
                roles = wp_test.get("roles") or []
                if not any(r in roles for r in ("administrator", "editor", "author")):
                    results.append({"key": "wordpress", "status": "warn", "label": f"WordPress conectado ” {display_name}",
                                     "desc": f"Usuário autenticado mas pode não ter permissão para publicar (role: {', '.join(roles) or 'desconhecido'}).",
                                     "fix": "Use um usuário com role <b>Administrator</b> ou <b>Editor</b>."})
                else:
                    results.append({"key": "wordpress", "status": "ok", "label": f"WordPress OK ” {display_name}",
                                     "desc": f"Conectado em <b>{base_url}</b> com role <b>{', '.join(roles)}</b>."})
            elif wp_test.get("status_code") == 401:
                results.append({"key": "wordpress", "status": "err", "label": "Credenciais inválidas",
                                 "desc": f"O WordPress retornou 401 Unauthorized para o usuário <b>{active_user['username']}</b>.",
                                 "fix": f"Gere um novo App Password em <b>{base_url}/wp-admin/profile.php</b> e atualize em <a href='/app/profiles/{bot.id}?tab=integracoes'>Integrações</a>."})
            elif wp_test.get("status_code") == 403:
                results.append({"key": "wordpress", "status": "err", "label": "Acesso bloqueado (403)",
                                 "desc": "O WordPress negou o acesso. A API REST pode estar desativada ou bloqueada por plugin de segurança.",
                                 "fix": "Verifique se a REST API está ativa. Plugins como Wordfence ou iThemes Security podem bloqueá-la."})
            else:
                status_code = wp_test.get("status_code")
                label = wp_test.get("label") or "WordPress inacessível"
                detail = html.escape(str(wp_test.get("detail") or "Resposta inesperada.")[:140])
                if status_code:
                    results.append({"key": "wordpress", "status": "warn", "label": label,
                                     "desc": f"Resposta inesperada de <b>{base_url}</b>: {detail}",
                                     "fix": "Clique em <b>Reconectar e iniciar</b> para testar novamente antes de enfileirar."})
                else:
                    results.append({"key": "wordpress", "status": "err", "label": "WordPress inacessível",
                                     "desc": f"Não foi possível conectar em <b>{base_url}</b>: {detail}",
                                     "fix": "Clique em <b>Reconectar e iniciar</b>. Se falhar de novo, verifique se a URL está correta e se o site está no ar."})

    # ── 2. Fontes ───────────────────────────────────────────────
    sources = list(db.scalars(select(Source).where(Source.profile_id == bot.id, Source.active.is_(True))))
    if not sources:
        results.append({"key": "sources", "status": "err", "label": "Nenhuma fonte configurada",
                         "desc": "O robô precisa de ao menos uma fonte (URL, RSS ou Palavra-chave) para buscar conteúdo.",
                         "fix": f"Vá em <a href='/app/profiles/{bot.id}?tab=fontes'>Configurar → Fontes</a> e adicione uma fonte."})
    else:
        results.append({"key": "sources", "status": "ok", "label": f"{len(sources)} fonte{'s' if len(sources)!=1 else ''} configurada{'s' if len(sources)!=1 else ''}",
                         "desc": ", ".join(f"<b>{html.escape(s.value[:40])}</b>" for s in sources[:3]) + ("..." if len(sources) > 3 else "")})

    # ── 3. IA (Gemini / OpenAI) ──────────────────────────────────
    gem = db.scalar(select(Integration).where(Integration.profile_id == bot.id, Integration.type == IntegrationType.GEMINI))
    oai = db.scalar(select(Integration).where(Integration.profile_id == bot.id, Integration.type == IntegrationType.OPENAI))
    gem_ok = False
    oai_ok = False
    if gem:
        try:
            _gcreds = decrypt_json(gem.credentials_encrypted)
            gem_ok = bool(str(_gcreds.get("api_key") or "").strip())
        except Exception:
            pass
    if oai:
        try:
            _ocreds = decrypt_json(oai.credentials_encrypted)
            oai_ok = bool(str(_ocreds.get("api_key") or "").strip())
        except Exception:
            pass
    if gem_ok and oai_ok:
        results.append({"key": "gemini", "status": "ok",
                         "label": "Gemini + ChatGPT configurados",
                         "desc": "Gemini é a IA principal. Se falhar, o ChatGPT assume automaticamente como fallback."})
    elif gem_ok:
        results.append({"key": "gemini", "status": "ok", "label": "Gemini configurado", "desc": "IA pronta para reescrever os posts."})
    elif oai_ok:
        results.append({"key": "gemini", "status": "ok", "label": "ChatGPT configurado", "desc": "ChatGPT será usado para reescrever os posts."})
    else:
        results.append({"key": "gemini", "status": "warn", "label": "Nenhuma IA configurada",
                         "desc": "Sem IA os posts não serão reescritos.",
                         "fix": f"Vá em <a href='/app/profiles/{bot.id}?tab=integracoes'>Integrações</a> e adicione Gemini ou ChatGPT."})

    # ── 4. Prompt IA ────────────────────────────────────────────
    _ensure_default_recipe_actions(db, bot=bot)
    _site_action_diag = db.scalar(
        select(AiAction)
        .where(AiAction.profile_id == bot.id, AiAction.destination == ActionDestination.WORDPRESS)
        .order_by(AiAction.created_at.asc()).limit(1)
    )
    _fb_action_diag = db.scalar(
        select(AiAction)
        .where(AiAction.profile_id == bot.id, AiAction.destination == ActionDestination.FACEBOOK)
        .order_by(AiAction.created_at.asc()).limit(1)
    )
    _sv1d, _sv2d, _sv3d, _sv4d, _smodd = _parse_prompt_variants(_site_action_diag.prompt_text if _site_action_diag else "")
    _fv1d, _fv2d, _fv3d, _fv4d, _fmodd = _parse_prompt_variants(_fb_action_diag.prompt_text if _fb_action_diag else "")
    _mode_name = {"1": "Prompt 1", "2": "Prompt 2", "3": "Prompt 3", "4": "Prompt 4", "random": "🎲 Aleatório"}
    _site_variants_filled = [v for v in [_sv1d, _sv2d, _sv3d, _sv4d] if v.strip()]
    _fb_variants_filled   = [v for v in [_fv1d, _fv2d, _fv3d, _fv4d] if v.strip()]

    if not any(v.strip() for v in [_sv1d, _sv2d, _sv3d, _sv4d]):
        results.append({"key": "prompt_site", "status": "warn",
                         "label": "Prompt WordPress não configurado",
                         "desc": "O bot vai usar um prompt genérico padrão.",
                         "fix": f"Configure um prompt em <a href='/app/profiles/{bot.id}?tab=ia'>IA → Prompts → WordPress</a>."})
    else:
        if _smodd == "random":
            _pcount = len(_site_variants_filled)
            _preview = html.escape((_site_variants_filled[0])[:90]) + ("…" if len(_site_variants_filled[0]) > 90 else "")
            results.append({"key": "prompt_site", "status": "ok",
                             "label": f"Prompt WordPress: 🎲 Aleatório ({_pcount} variante{'s' if _pcount != 1 else ''})",
                             "desc": f"O bot vai sortear um dos {_pcount} prompts a cada post.<br><small style='opacity:.7'>Ex: «{_preview}»</small>"})
        else:
            _idx = int(_smodd) - 1
            _site_variants_all = [_sv1d, _sv2d, _sv3d, _sv4d]
            _chosen = _site_variants_all[_idx] if 0 <= _idx < len(_site_variants_all) and _site_variants_all[_idx].strip() else (_site_variants_filled[0] if _site_variants_filled else "")
            _preview = html.escape(_chosen[:90]) + ("…" if len(_chosen) > 90 else "")
            results.append({"key": "prompt_site", "status": "ok",
                             "label": f"Prompt WordPress: {_mode_name.get(_smodd, 'Prompt 1')}",
                             "desc": f"«{_preview}»"})

    if not any(v.strip() for v in [_fv1d, _fv2d, _fv3d, _fv4d]):
        results.append({"key": "prompt_fb", "status": "warn",
                         "label": "Prompt Facebook não configurado",
                         "desc": "O bot vai usar um prompt genérico padrão.",
                         "fix": f"Configure em <a href='/app/profiles/{bot.id}?tab=ia'>IA → Prompts → Facebook</a>."})
    else:
        if _fmodd == "random":
            _pcount = len(_fb_variants_filled)
            _preview = html.escape((_fb_variants_filled[0])[:90]) + ("…" if len(_fb_variants_filled[0]) > 90 else "")
            results.append({"key": "prompt_fb", "status": "ok",
                             "label": f"Prompt Facebook: 🎲 Aleatório ({_pcount} variante{'s' if _pcount != 1 else ''})",
                             "desc": f"O bot vai sortear um dos {_pcount} prompts a cada post.<br><small style='opacity:.7'>Ex: «{_preview}»</small>"})
        else:
            _idx = int(_fmodd) - 1
            _fb_variants_all = [_fv1d, _fv2d, _fv3d, _fv4d]
            _chosen = _fb_variants_all[_idx] if 0 <= _idx < len(_fb_variants_all) and _fb_variants_all[_idx].strip() else (_fb_variants_filled[0] if _fb_variants_filled else "")
            _preview = html.escape(_chosen[:90]) + ("…" if len(_chosen) > 90 else "")
            results.append({"key": "prompt_fb", "status": "ok",
                             "label": f"Prompt Facebook: {_mode_name.get(_fmodd, 'Prompt 1')}",
                             "desc": f"«{_preview}»"})

    # ── 5. Facebook ──────────────────────────────────────────────
    publish_cfg_diag = bot.publish_config_json or {}
    fb_enabled_diag = bool(publish_cfg_diag.get("facebook_enabled"))
    fb_integ_diag = db.scalar(select(Integration).where(Integration.profile_id == bot.id, Integration.type == IntegrationType.FACEBOOK))
    fb_pages_diag: list[dict] = []
    if fb_integ_diag:
        try:
            _fbc = decrypt_json(fb_integ_diag.credentials_encrypted)
            fb_pages_diag = [x for x in (_fbc.get("pages") or []) if isinstance(x, dict) and x.get("page_id")]
        except Exception:
            pass
    fb_selected_diag = [str(x) for x in (publish_cfg_diag.get("facebook_page_ids") or []) if str(x).strip()]

    if not fb_enabled_diag:
        results.append({
            "key": "facebook", "status": "info",
            "label": "Facebook desativado",
            "desc": "O bot não vai publicar no Facebook nesta rodada. Ative em <a href='/app/profiles/{bid}?tab=publicacao&ptab=facebook'>Publicação → Facebook</a>.".replace("{bid}", bot.id),
        })
    elif not fb_pages_diag:
        results.append({
            "key": "facebook", "status": "warn",
            "label": "Facebook ativado mas sem páginas",
            "desc": "Nenhuma página do Facebook cadastrada.",
            "fix": f"Vá em <a href='/app/profiles/{bot.id}?tab=integracoes&itab=facebook'>Integrações → Facebook</a> e adicione ao menos uma página.",
        })
    elif not fb_selected_diag:
        results.append({
            "key": "facebook", "status": "warn",
            "label": "Facebook ativado mas nenhuma página selecionada",
            "desc": f"{len(fb_pages_diag)} página(s) cadastrada(s), mas nenhuma selecionada para publicar.",
            "fix": f"Vá em <a href='/app/profiles/{bot.id}?tab=publicacao&ptab=facebook'>Publicação → Facebook</a> e marque as páginas desejadas.",
        })
    else:
        # Quick token validation for selected pages (best-effort, no hard block)
        from app.services.facebook import test_page_token as _test_fb_token
        valid_pages, invalid_pages = [], []
        for _pg in fb_pages_diag:
            _pid = str(_pg.get("page_id") or "")
            _tok = str(_pg.get("access_token") or "")
            if _pid not in fb_selected_diag:
                continue
            if not _tok:
                invalid_pages.append(_pg.get("name") or _pid)
                continue
            _tres = _test_fb_token(page_id=_pid, page_access_token=_tok)
            if _tres.get("ok"):
                valid_pages.append(_tres.get("name") or _pg.get("name") or _pid)
            else:
                invalid_pages.append((_pg.get("name") or _pid) + f" ({_tres.get('error','')[:60]})")
        if invalid_pages and not valid_pages:
            results.append({
                "key": "facebook", "status": "err",
                "label": "Token(s) do Facebook inválido(s)",
                "desc": f"Falha ao validar: {', '.join(invalid_pages[:3])}.",
                "fix": f"Gere novos tokens em <a href='/app/profiles/{bot.id}?tab=integracoes&itab=facebook'>Integrações → Facebook</a>.",
            })
        elif invalid_pages:
            results.append({
                "key": "facebook", "status": "warn",
                "label": f"Facebook parcialmente OK ({len(valid_pages)} de {len(valid_pages)+len(invalid_pages)} páginas)",
                "desc": f"Páginas OK: {', '.join(valid_pages[:2])}. Com erro: {', '.join(invalid_pages[:2])}.",
                "fix": f"Renove os tokens das páginas com erro em <a href='/app/profiles/{bot.id}?tab=integracoes&itab=facebook'>Integrações → Facebook</a>.",
            })
        else:
            fb_image_mode = str(publish_cfg_diag.get("facebook_image") or "link_preview")
            img_label = {"link_preview": "preview do link WP", "direct_photo": "imagem direta", "none": "sem imagem"}.get(fb_image_mode, fb_image_mode)
            results.append({
                "key": "facebook", "status": "ok",
                "label": f"Facebook OK ” {len(valid_pages)} página(s) pronta(s)",
                "desc": f"Páginas: {', '.join(valid_pages[:3])}{'...' if len(valid_pages)>3 else ''}. Imagem: {img_label}.",
            })

    try:
        can_start = all(r["status"] not in ("err",) for r in results)
        can_reconnect_start = bool(
            wp_can_reconnect
            and not can_start
            and not any(r["status"] == "err" and r["key"] != "wordpress" for r in results)
        )
        _scfg = bot.schedule_config_json or {}
        _ppd  = int(_scfg.get("posts_per_day") or 15)
        _imin = int(_scfg.get("interval_minutes") or 0)
        _src_types = list({s.type.value for s in sources}) if sources else []
        _wp_url_diag = ""
        try:
            if wp_integ:
                _wp_url_diag = str(decrypt_json(wp_integ.credentials_encrypted).get("base_url") or "")
        except Exception:
            pass
        summary = {
            "posts_per_day": _ppd,
            "interval_minutes": _imin,
            "sources_count": len(sources),
            "sources_types": _src_types,
            "wp_url": _wp_url_diag,
        }
        return JSONResponse({
            "results": results,
            "can_start": can_start,
            "can_reconnect_start": can_reconnect_start,
            "bot_name": bot.name,
            "summary": summary,
        })
    except Exception as _final_e:
        import traceback as _tb2
        return JSONResponse({
            "error": f"diagnose error: {_tb2.format_exc()[-1200:]}",
            "results": results,
            "can_start": False,
            "can_reconnect_start": False,
        }, status_code=500)


@router.post("/app/robot/stop", include_in_schema=False)
def robot_stop(request: Request, bot_id: str = Form(default=None), user: User = Depends(get_current_user), db=Depends(get_db)):
    if bot_id:
        bot = db.scalar(select(AutomationProfile).where(AutomationProfile.id == bot_id, AutomationProfile.user_id == user.id))
    else:
        bot = _get_or_create_single_bot(db, user=user)
    if not bot:
        _is_ajax = "application/json" in request.headers.get("accept", "")
        if _is_ajax:
            return _JSONResponse({"ok": False, "error": "bot_not_found"}, status_code=404)
        return RedirectResponse("/app/robot", status_code=status.HTTP_302_FOUND)
    _set_bot_run_stopped(db, bot=bot, stopped=True)
    ids = list(db.scalars(select(Post.id).where(
        Post.profile_id == bot.id,
        Post.status.in_([PostStatus.pending, PostStatus.processing]),
    )))
    if ids:
        _cancel_posts(db, profile_id=bot.id, post_ids=[str(x) for x in ids], user=user)
    cancelled_jobs = int(db.scalar(select(func.count()).select_from(Job).where(
        Job.profile_id == bot.id,
        or_(Job.status == JobStatus.queued, Job.status == JobStatus.running),
    )) or 0)
    db.execute(
        update(Job)
        .where(Job.profile_id == bot.id, or_(Job.status == JobStatus.queued, Job.status == JobStatus.running))
        .values(status=JobStatus.failed, last_error="canceled_by_user", locked_at=None, locked_by=None, updated_at=datetime.utcnow())
    )
    db.commit()
    _is_ajax = "application/json" in request.headers.get("accept", "")
    if _is_ajax:
        return _JSONResponse({"ok": True, "active": True, "stopped": True, "cancelled_jobs": cancelled_jobs, "cancelled_posts": len(ids)})
    return RedirectResponse(f"/app/robot?msg={quote_plus('Robô parado com sucesso.')}", status_code=status.HTTP_302_FOUND)


@router.get("/app/robot/status", include_in_schema=False)
def robot_status(user: User = Depends(get_current_user), db=Depends(get_db)):
    """JSON: list of active bots with is_running flag ” used by frontend polling."""
    profiles = list(db.scalars(select(AutomationProfile).where(AutomationProfile.user_id == user.id, AutomationProfile.active.is_(True))))
    result = []
    for p in profiles:
        qj = int(db.scalar(select(func.count()).select_from(Job).where(Job.profile_id == p.id, Job.status.in_([JobStatus.queued, JobStatus.running]))) or 0)
        pp = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == p.id, Post.status.in_([PostStatus.pending, PostStatus.processing]))) or 0)
        result.append({"id": p.id, "name": p.name, "is_running": (qj + pp) > 0})
    return _JSONResponse(result)


@router.get("/app/posts/status-hash", include_in_schema=False)
def posts_status_hash(user: User = Depends(get_current_user), db=Depends(get_db)):
    """Lightweight hash of active job/post state — used by smart polling to avoid unnecessary reloads."""
    import hashlib
    profiles = db.scalars(select(AutomationProfile).where(AutomationProfile.user_id == user.id)).all()
    all_ids = [p.id for p in profiles]
    if not all_ids:
        return _JSONResponse({"hash": "empty", "active": 0})
    active_jobs = int(db.scalar(
        select(func.count()).select_from(Job).where(
            Job.profile_id.in_(all_ids),
            Job.status.in_([JobStatus.queued, JobStatus.running])
        )
    ) or 0)
    active_posts = int(db.scalar(
        select(func.count()).select_from(Post).where(
            Post.profile_id.in_(all_ids),
            Post.status.in_([PostStatus.pending, PostStatus.processing])
        )
    ) or 0)
    # Include the most-recent job update timestamp so the hash changes when a job finishes
    last_ts = db.scalar(
        select(func.max(Job.updated_at)).where(Job.profile_id.in_(all_ids))
    )
    fingerprint = f"{active_jobs}:{active_posts}:{last_ts}"
    h = hashlib.md5(fingerprint.encode()).hexdigest()[:12]
    return _JSONResponse({"hash": h, "active": active_jobs + active_posts})


@router.get("/app/posts/live-jobs", include_in_schema=False)
def posts_live_jobs(user: User = Depends(get_current_user), db=Depends(get_db)):
    """Recent jobs with post title/URL for the live activity log."""
    rows = list(db.execute(
        select(Job, CollectedContent.title, CollectedContent.canonical_url, AutomationProfile.name.label("bot_name"))
        .outerjoin(Post, Post.id == Job.post_id)
        .outerjoin(CollectedContent, CollectedContent.id == Post.collected_content_id)
        .outerjoin(AutomationProfile, AutomationProfile.id == Job.profile_id)
        .where(Job.user_id == user.id)
        .order_by(Job.updated_at.desc())
        .limit(60)
    ).all())
    tz = _user_zoneinfo(user)
    result = []
    for job, title, url, bot_name in rows:
        started = job.created_at
        updated = job.updated_at
        dur = int((updated - started).total_seconds()) if updated and started else 0
        try:
            when = updated.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%H:%M:%S")
        except Exception:
            when = str(updated)[:19]
        result.append({
            "id": job.id,
            "bot": str(bot_name or ""),
            "title": str(title or "")[:80],
            "url": str(url or "")[:120],
            "stage": job.type,
            "status": job.status.value,
            "when": when,
            "dur": dur,
            "error": str(job.last_error or "")[:120],
            "profile_id": job.profile_id or "",
        })
    return _JSONResponse(result)


@router.post("/app/robot/run-now", include_in_schema=False)
def robot_run_now(bot_id: str = Form(default=None), user: User = Depends(get_current_user), db=Depends(get_db)):
    if bot_id:
        bot = db.scalar(select(AutomationProfile).where(AutomationProfile.id == bot_id, AutomationProfile.user_id == user.id))
    else:
        bot = _get_or_create_single_bot(db, user=user)
    if not bot:
        return RedirectResponse("/app/posts", status_code=status.HTTP_302_FOUND)
    bot.active = True
    _set_bot_run_stopped(db, bot=bot, stopped=False)
    now = datetime.utcnow()
    did = 0

    # 1. Libera jobs queued com run_at no futuro
    did += db.execute(
        update(Job).where(Job.profile_id == bot.id, Job.status == JobStatus.queued)
        .values(run_at=now)
    ).rowcount

    # 2. Reseta jobs running travados há mais de 5 min de volta para queued
    stuck_cutoff = now - timedelta(minutes=5)
    did += db.execute(
        update(Job).where(
            Job.profile_id == bot.id,
            Job.status == JobStatus.running,
            Job.locked_at < stuck_cutoff,
        ).values(status=JobStatus.queued, run_at=now, locked_at=None, locked_by=None)
    ).rowcount

    # 3. Posts pending/processing sem nenhum job ativo → re-enfileira o próximo step
    pipeline = [JOB_CLEAN, JOB_AI, JOB_PUBLISH_WP]
    orphan_posts = list(db.scalars(
        select(Post).where(
            Post.profile_id == bot.id,
            Post.status.in_([PostStatus.pending, PostStatus.processing]),
        )
    ))
    for p in orphan_posts:
        active = int(db.scalar(
            select(func.count()).select_from(Job).where(
                Job.post_id == p.id,
                Job.status.in_([JobStatus.queued, JobStatus.running]),
            )
        ) or 0)
        if active:
            continue
        # Determina de onde retomar com base no último job bem-sucedido
        last_ok_type = db.scalar(
            select(Job.type).where(Job.post_id == p.id, Job.status == JobStatus.succeeded)
            .order_by(Job.updated_at.desc()).limit(1)
        )
        if last_ok_type in pipeline:
            idx = pipeline.index(last_ok_type)
            next_job = pipeline[idx + 1] if idx + 1 < len(pipeline) else JOB_PUBLISH_WP
        else:
            next_job = JOB_CLEAN
        p.status = PostStatus.pending
        p.updated_at = now
        db.add(p)
        enqueue_job(db, user_id=p.user_id, profile_id=p.profile_id, post_id=p.id,
                    job_type=next_job, payload={"collected_content_id": p.collected_content_id})
        did += 1

    db.commit()
    if not did:
        return RedirectResponse("/app/posts?msg=Nada+pendente+para+rodar.", status_code=status.HTTP_302_FOUND)
    return RedirectResponse("/app/posts?msg=Fila+liberada+e+retomada.", status_code=status.HTTP_302_FOUND)


@router.post("/app/robot/tick-now", include_in_schema=False)
async def robot_tick_now(
    bot_id: str = Form(default=None),
    force: str = Form("0"),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Prepara a fila, recupera órfãos e processa jobs imediatamente (até 55s). Chamado via AJAX."""
    import asyncio as _asyncio
    import time as _time
    from app.worker import run_worker_tick as _run_tick
    from app.queue import JOB_CLEAN as _JOB_CLEAN, JOB_AI as _JOB_AI, JOB_PUBLISH_WP as _JOB_PUBLISH_WP

    if bot_id:
        bot = db.scalar(select(AutomationProfile).where(AutomationProfile.id == bot_id, AutomationProfile.user_id == user.id))
    else:
        bot = None
        candidates = list(db.scalars(
            select(AutomationProfile)
            .where(AutomationProfile.user_id == user.id)
            .order_by(AutomationProfile.active.desc(), AutomationProfile.created_at.asc())
        ))
        for candidate in candidates:
            active_jobs = int(db.scalar(select(func.count()).select_from(Job).where(
                Job.profile_id == candidate.id,
                Job.status.in_([JobStatus.queued, JobStatus.running]),
            )) or 0)
            active_posts = int(db.scalar(select(func.count()).select_from(Post).where(
                Post.profile_id == candidate.id,
                Post.status.in_([PostStatus.pending, PostStatus.processing]),
            )) or 0)
            if active_jobs + active_posts > 0:
                bot = candidate
                break
        if not bot and candidates:
            bot = candidates[0]

    force_now = str(force or "0") == "1"
    if bot:
        if force_now:
            bot.active = True
            _set_bot_run_stopped(db, bot=bot, stopped=False)
        now = datetime.utcnow()
        # 1. "Rodar agora" libera jobs futuros; processamento automatico respeita a cadencia.
        if force_now:
            db.execute(update(Job).where(Job.profile_id == bot.id, Job.status == JobStatus.queued).values(run_at=now))
        # 2. Reseta jobs running travados há mais de 5 min
        stuck_cutoff = now - timedelta(minutes=5)
        db.execute(
            update(Job).where(
                Job.profile_id == bot.id, Job.status == JobStatus.running, Job.locked_at < stuck_cutoff
            ).values(status=JobStatus.queued, run_at=now, locked_at=None, locked_by=None)
        )
        # 3. Recupera posts órfãos: pending/processing sem nenhum job ativo
        pipeline = [_JOB_CLEAN, _JOB_AI, _JOB_PUBLISH_WP]
        orphan_posts = list(db.scalars(
            select(Post).where(
                Post.profile_id == bot.id,
                Post.status.in_([PostStatus.pending, PostStatus.processing]),
            )
        ))
        for p in orphan_posts:
            active = int(db.scalar(
                select(func.count()).select_from(Job).where(
                    Job.post_id == p.id,
                    Job.status.in_([JobStatus.queued, JobStatus.running]),
                )
            ) or 0)
            if active:
                continue
            last_ok_type = db.scalar(
                select(Job.type).where(Job.post_id == p.id, Job.status == JobStatus.succeeded)
                .order_by(Job.updated_at.desc()).limit(1)
            )
            if last_ok_type in pipeline:
                idx = pipeline.index(last_ok_type)
                next_job = pipeline[idx + 1] if idx + 1 < len(pipeline) else _JOB_PUBLISH_WP
            else:
                next_job = _JOB_CLEAN
            run_at = now
            if not force_now and p.scheduled_for and p.scheduled_for > now:
                run_at = p.scheduled_for
            p.status = PostStatus.pending
            p.updated_at = now
            db.add(p)
            enqueue_job(db, user_id=p.user_id, profile_id=p.profile_id, post_id=p.id,
                        job_type=next_job, payload={"collected_content_id": p.collected_content_id}, run_at=run_at)
        db.commit()

    # Processa jobs em loop por até 55s
    worker_id = f"manual:{user.id}"
    ticks = 0
    deadline = _time.monotonic() + 55
    while _time.monotonic() < deadline:
        try:
            did_work = await _asyncio.to_thread(_run_tick, worker_id=worker_id, user_id=user.id, profile_id=(bot.id if bot else None))
        except Exception:
            break
        if not did_work:
            break
        ticks += 1

    return _JSONResponse({"ok": True, "ticks": ticks})


@router.post("/app/robot/retry-ai", include_in_schema=False)
def robot_retry_ai(bot_id: str = Form(default=None), user: User = Depends(get_current_user), db=Depends(get_db)):
    if bot_id:
        bot = db.scalar(select(AutomationProfile).where(AutomationProfile.id == bot_id, AutomationProfile.user_id == user.id))
    else:
        bot = _get_or_create_single_bot(db, user=user)
    if not bot:
        return RedirectResponse("/app/posts", status_code=status.HTTP_302_FOUND)
    posts = list(
        db.scalars(
            select(Post)
            .where(Post.profile_id == bot.id, Post.status == PostStatus.failed)
            .order_by(Post.updated_at.desc())
            .limit(50)
        )
    )
    if not posts:
        redirect_to = "/app/posts?msg=Nenhuma+falha+para+reprocessar." if bot_id else "/app/robot"
        return RedirectResponse(redirect_to, status_code=status.HTTP_302_FOUND)
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
    redirect_to = "/app/posts?msg=IA+reagendada." if bot_id else "/app/robot"
    return RedirectResponse(redirect_to, status_code=status.HTTP_302_FOUND)


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
    p = AutomationProfile(
        user_id=user.id,
        name=name.strip(),
        active=(active == "1"),
        schedule_config_json={},
        anti_block_config_json={},
        publish_config_json={"facebook_link": "comments", "default_category": "Receitas", "categories": list(DEFAULT_RECIPE_CATEGORIES)},
    )
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
    p = AutomationProfile(
        user_id=user.id,
        name=name.strip(),
        active=True,
        schedule_config_json={},
        anti_block_config_json={},
        publish_config_json={
            "emoji": safe_emoji,
            "facebook_link": "comments",
            "default_category": "Receitas",
            "categories": list(DEFAULT_RECIPE_CATEGORIES),
        },
    )
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
    if _ensure_publish_config_defaults(db, bot=p):
        db.commit()
    tab = (request.query_params.get("tab") or "integracoes").strip().lower()
    tabs = [
        ("integracoes", "Integrações"),
        ("fontes",      "Fontes"),
        ("ia",          "IA"),
        ("publicacao",  "Publicação"),
        ("agendamento", "Agendamento"),
    ]
    _tab_label = dict(tabs).get(tab, tab)
    import random as _rnd_cfg
    _cfg_sleep_msgs = [
        ("&#128564;", "zzZZ... todos os bots em repouso."),
        ("&#127769;", "Modo noturno. Nenhum rob&#244; ativo no momento."),
        ("&#9749;", "Pausa total. Hora do caf&#233; enquanto configura."),
        ("&#127775;", "Silencioso por aqui. Ligue um bot para animar."),
        ("&#128123;", "Fantasma no ar... nenhum bot rodando."),
    ]
    _cfg_sleep_icon, _cfg_sleep_text = _rnd_cfg.choice(_cfg_sleep_msgs)
    _any_active_profiles = list(db.scalars(select(AutomationProfile).where(AutomationProfile.user_id == user.id, AutomationProfile.active == True)))

    if _any_active_profiles:
        _bot_chips = ""
        for _ap in _any_active_profiles:
            if _ap.id == p.id:
                _chip_style = (
                    "display:inline-flex;align-items:center;gap:7px;"
                    "padding:8px 18px;border-radius:22px;"
                    "background:rgba(16,185,129,.22);border:2px solid rgba(16,185,129,.7);"
                    "text-decoration:none;color:var(--text);font-weight:700;font-size:13px;"
                    "box-shadow:0 0 0 3px rgba(16,185,129,.15);transition:background .15s"
                )
            else:
                _chip_style = (
                    "display:inline-flex;align-items:center;gap:7px;"
                    "padding:8px 18px;border-radius:22px;"
                    "background:rgba(16,185,129,.04);border:1px solid rgba(16,185,129,.18);"
                    "text-decoration:none;color:var(--muted);font-weight:500;font-size:13px;"
                    "opacity:.55;transition:background .15s"
                )
            _bot_chips += (
                f"<a href='/app/profiles/{_ap.id}?tab={tab}' style='{_chip_style}'>"
                f"<span class='dot-pulse'></span>{html.escape(_ap.name)}</a>"
            )
        _banner_main = (
            f"<div style='display:flex;flex-direction:column;gap:8px'>"
            f"<span style='font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;color:var(--muted)'>Bots ativos</span>"
            f"<div style='display:flex;flex-wrap:wrap;gap:8px'>{_bot_chips}</div>"
            f"</div>"
        )
    else:
        _banner_main = (
            f"<div style='display:flex;align-items:center;gap:10px'>"
            f"<span style='font-size:22px'>{_cfg_sleep_icon}</span>"
            f"<span style='font-size:14px;color:var(--muted)'>{_cfg_sleep_text}</span>"
            f"</div>"
        )

    body = f"""
    {_ph("banner-projeto-configurar")}
    <div class="active-project-banner" style="margin-bottom:14px">
      <div style="display:flex;align-items:center;gap:12px;flex:1;min-width:0">
        {_banner_main}
      </div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;flex-shrink:0">
        <a href="/app/robot" class="btn secondary" style="font-size:13px;padding:7px 14px">&#8592; Voltar ao Rob&#244;</a>
      </div>
    </div>
    """
    msg = (request.query_params.get("msg") or "").strip()
    if msg:
        body += f"<div class='card' style='border-color: rgba(255,255,255,.08)'><b>{html.escape(msg)}</b></div>"

    if tab == "fontes":
        _all_fontes_profiles = list(db.scalars(
            select(AutomationProfile)
            .where(AutomationProfile.user_id == user.id)
            .order_by(AutomationProfile.active.desc(), AutomationProfile.created_at.asc())
        ))

        def _src_rows(srcs, icon, color, pid):
            if not srcs:
                return "<tr><td colspan='2' style='padding:18px;text-align:center;color:var(--muted);font-size:13px'>Nenhuma cadastrada ainda.</td></tr>"
            out = ""
            for s in srcs:
                out += (
                    f"<tr style='border-top:1px solid var(--border)'>"
                    f"<td style='padding:10px 14px;font-size:13px;word-break:break-all'>"
                    f"<div style='display:flex;align-items:flex-start;gap:10px'>"
                    f"<span style='width:22px;height:22px;border-radius:6px;background:{color};display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:11px;margin-top:1px'>{icon}</span>"
                    f"<span style='font-weight:500;color:var(--text);line-height:1.4'>{html.escape(s.value)}</span>"
                    f"</div></td>"
                    f"<td style='padding:10px 14px;text-align:right;white-space:nowrap'>"
                    f"<form method='post' action='/app/profiles/{pid}/sources/{s.id}/delete' style='margin:0'>"
                    f"<button class='btn flat' type='submit' style='font-size:11px;padding:4px 10px;color:#ef4444;border-color:rgba(239,68,68,.25)' "
                    f"onclick=\"return confirm('Remover esta fonte?')\">&#128465; Remover</button></form></td></tr>"
                )
            return out

        body += f"""
        {_ph("tab-fontes")}
        <div style="display:flex;align-items:flex-start;gap:10px;padding:12px 16px;background:rgba(139,92,246,.08);border:1px solid rgba(139,92,246,.2);border-radius:12px;margin-bottom:16px;font-size:13px;color:var(--muted);line-height:1.6">
          <span style="font-size:18px;flex-shrink:0">&#128278;</span>
          <div>Gerencie as <b>fontes de conte&#250;do</b> de cada bot abaixo. <b>URL</b> = p&#225;gina/site &nbsp;&#124;&nbsp; <b>RSS</b> = feed XML &nbsp;&#124;&nbsp; <b>Palavra-chave</b> = busca por termo.</div>
        </div>
        """
        for _fp in _all_fontes_profiles:
            _fp_sources = list(db.scalars(select(Source).where(Source.profile_id == _fp.id).order_by(Source.created_at.desc())))
            _fp_url = [s for s in _fp_sources if s.type.value == "URL"]
            _fp_rss = [s for s in _fp_sources if s.type.value == "RSS"]
            _fp_kw  = [s for s in _fp_sources if s.type.value == "KEYWORD"]
            _fp_total = len(_fp_sources)
            _fp_open = "open" if (_fp.active or _fp.id == p.id) else ""
            _fp_name_esc = html.escape(_fp.name)
            _fp_id = _fp.id
            _fp_plural = "s" if _fp_total != 1 else ""
            if _fp.active:
                _fp_badge = "<span class='badge-active' style='font-size:10px;padding:2px 7px'><span class='dot-pulse'></span>Ativo</span>"
            else:
                _fp_badge = "<span class='badge-inactive' style='font-size:10px;padding:2px 7px;opacity:.8'><span class='dot-off'></span>Inativo</span>"
            _fp_url_rows = _src_rows(_fp_url, "&#127758;", "rgba(14,165,233,.15)", _fp_id)
            _fp_rss_rows = _src_rows(_fp_rss, "&#128268;", "rgba(245,158,11,.15)", _fp_id)
            _fp_kw_rows  = _src_rows(_fp_kw,  "&#128269;", "rgba(16,185,129,.15)", _fp_id)
            _fp_n_url = len(_fp_url)
            _fp_n_rss = len(_fp_rss)
            _fp_n_kw  = len(_fp_kw)
            _fp_alert_id = "src-alert-" + _fp_id.replace("-", "")
            if _fp.active:
                _fp_form_onsubmit = ""
                _fp_btn_extra = ""
                _fp_alert_block = ""
                _fp_input_required = "required"
            else:
                _fp_input_required = ""
                _fp_form_onsubmit = (
                    "onsubmit=\"event.preventDefault();"
                    "var el=document.getElementById('" + _fp_alert_id + "');"
                    "el.style.display='flex';"
                    "setTimeout(function(){{el.style.display='none';}},4000);\""
                )
                _fp_btn_extra = "opacity:.55;cursor:not-allowed;"
                _fp_alert_block = (
                    "<div id='" + _fp_alert_id + "' style='display:none;align-items:center;gap:9px;"
                    "margin-top:10px;padding:10px 14px;background:rgba(239,68,68,.08);"
                    "border:1px solid rgba(239,68,68,.3);border-radius:8px;font-size:13px;color:#ef4444'>"
                    "&#9888; Bot <b>inativo</b>. Ative o bot para adicionar novas fontes.</div>"
                )
            body += f"""
            <div class="card" style="margin-bottom:14px">
              <details class="toggle-section" {_fp_open}>
                <summary>
                  <span class="ts-title" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
                    <span style="font-weight:700;font-size:14px">{_fp_name_esc}</span>
                    {_fp_badge}
                    <span class="ts-badge" style="color:var(--muted);border-color:var(--border2)">{_fp_total} fonte{_fp_plural}</span>
                  </span>
                  <span class="ts-arrow">&#9655;</span>
                </summary>
                <div class="ts-body">
                  <!-- Add source form -->
                  <div style="padding:14px;border:1px solid var(--border);border-radius:10px;background:var(--surface2);margin-bottom:14px">
                    <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-bottom:10px">Adicionar fonte</div>
                    <form method="post" action="/app/profiles/{_fp_id}/sources/create" {_fp_form_onsubmit}>
                      <div style="display:grid;grid-template-columns:150px 1fr auto;gap:10px;align-items:flex-end">
                        <div>
                          <label style="font-size:11px;font-weight:600;color:var(--muted);margin-bottom:5px;display:block">Tipo</label>
                          <select name="type" style="margin:0">
                            <option value="URL">&#127758; URL</option>
                            <option value="RSS">&#128268; RSS</option>
                            <option value="KEYWORD">&#128269; Palavra-chave</option>
                          </select>
                        </div>
                        <div>
                          <label style="font-size:11px;font-weight:600;color:var(--muted);margin-bottom:5px;display:block">Valor</label>
                          <input name="value" placeholder="https://... ou termo de busca" {_fp_input_required} style="margin:0" />
                        </div>
                        <button class="btn flat" type="submit" style="height:42px;padding:0 20px;font-size:15px;font-weight:700;letter-spacing:0;{_fp_btn_extra}">+ Adicionar</button>
                      </div>
                      {_fp_alert_block}
                    </form>
                  </div>
                  <!-- URL sources -->
                  <details class="toggle-section" open>
                    <summary>
                      <span class="ts-title" style="display:flex;align-items:center;gap:8px">
                        <span style="width:22px;height:22px;border-radius:6px;background:rgba(14,165,233,.15);display:inline-flex;align-items:center;justify-content:center;font-size:11px">&#127758;</span>
                        URLs <span class="ts-badge" style="color:#0ea5e9;border-color:rgba(14,165,233,.3)">{_fp_n_url}</span>
                      </span>
                      <span class="ts-arrow">&#9655;</span>
                    </summary>
                    <div class="ts-body" style="padding:0">
                      <table style="width:100%;border-collapse:collapse">
                        <thead><tr style="background:var(--surface2)">
                          <th style="padding:9px 14px;text-align:left;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">Endere&#231;o</th>
                          <th style="padding:9px 14px;width:110px"></th>
                        </tr></thead>
                        <tbody>{_fp_url_rows}</tbody>
                      </table>
                    </div>
                  </details>
                  <!-- RSS sources -->
                  <details class="toggle-section" style="margin-top:8px">
                    <summary>
                      <span class="ts-title" style="display:flex;align-items:center;gap:8px">
                        <span style="width:22px;height:22px;border-radius:6px;background:rgba(245,158,11,.15);display:inline-flex;align-items:center;justify-content:center;font-size:11px">&#128268;</span>
                        Feeds RSS <span class="ts-badge" style="color:#f59e0b;border-color:rgba(245,158,11,.3)">{_fp_n_rss}</span>
                      </span>
                      <span class="ts-arrow">&#9655;</span>
                    </summary>
                    <div class="ts-body" style="padding:0">
                      <table style="width:100%;border-collapse:collapse">
                        <thead><tr style="background:var(--surface2)">
                          <th style="padding:9px 14px;text-align:left;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">Feed URL</th>
                          <th style="padding:9px 14px;width:110px"></th>
                        </tr></thead>
                        <tbody>{_fp_rss_rows}</tbody>
                      </table>
                    </div>
                  </details>
                  <!-- Keyword sources -->
                  <details class="toggle-section" style="margin-top:8px">
                    <summary>
                      <span class="ts-title" style="display:flex;align-items:center;gap:8px">
                        <span style="width:22px;height:22px;border-radius:6px;background:rgba(16,185,129,.15);display:inline-flex;align-items:center;justify-content:center;font-size:11px">&#128269;</span>
                        Palavras-chave <span class="ts-badge" style="color:#10b981;border-color:rgba(16,185,129,.3)">{_fp_n_kw}</span>
                      </span>
                      <span class="ts-arrow">&#9655;</span>
                    </summary>
                    <div class="ts-body" style="padding:0">
                      <table style="width:100%;border-collapse:collapse">
                        <thead><tr style="background:var(--surface2)">
                          <th style="padding:9px 14px;text-align:left;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">Termo</th>
                          <th style="padding:9px 14px;width:110px"></th>
                        </tr></thead>
                        <tbody>{_fp_kw_rows}</tbody>
                      </table>
                    </div>
                  </details>
                </div>
              </details>
            </div>
            """
    elif tab == "publicacao":
        publish_cfg = dict(p.publish_config_json or {})
        _pub_sched_cfg = dict(p.schedule_config_json or {})
        _pub_posts_per_day = int(_pub_sched_cfg.get("posts_per_day") or 15)
        _pub_interval_min  = int(_pub_sched_cfg.get("interval_minutes") or 0)
        fb_link_place = str(publish_cfg.get("facebook_link", "comments"))
        fb_enabled = bool(publish_cfg.get("facebook_enabled"))
        fb_image_mode = str(publish_cfg.get("facebook_image") or "link_preview")
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
        fast_publish_enabled = bool(publish_cfg.get("fast_publish_enabled"))
        try:
            rss_fallback_after_seconds = int(publish_cfg.get("rss_fallback_after_seconds") or 20)
        except Exception:
            rss_fallback_after_seconds = 20
        rss_fallback_after_seconds = max(5, min(rss_fallback_after_seconds, 180))
        fast_skip_wp_image = bool(publish_cfg.get("fast_skip_wp_image"))
        fast_skip_wp_tags = bool(publish_cfg.get("fast_skip_wp_tags"))
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
        ptab = (request.query_params.get("ptab") or "wordpress").strip().lower()
        _wp_svg = ("<svg width='15' height='15' viewBox='0 0 24 24' fill='currentColor'>"
                   "<path d='M12 2C6.486 2 2 6.486 2 12s4.486 10 10 10 10-4.486 10-10S17.514 2 12 2z"
                   "M3.251 12c0-1.308.265-2.556.741-3.695L7.36 18.658A8.762 8.762 0 0 1 3.251 12z"
                   "m8.749 8.75a8.773 8.773 0 0 1-2.496-.364l2.65-7.695 2.716 7.44a.96.96 0 0 0 .07.136"
                   " 8.764 8.764 0 0 1-2.94.483zm1.211-12.981c.528-.028.999-.084.999-.084"
                   "-.47-.056.415-.748-.056-.72 0 0-1.415.111-2.329.111-.858 0-2.3-.111-2.3-.111"
                   "-.47-.028-.526.692-.055.72 0 0 .444.056.914.084l1.358 3.72-1.908 5.721"
                   "-3.176-8.441c.528-.028 1-.084 1-.084.47-.056.415-.748-.056-.72 0 0"
                   "-1.415.111-2.329.111a12.65 12.65 0 0 1-.31-.005A8.752 8.752 0 0 1 12 3.25"
                   "c2.294 0 4.389.879 5.963 2.315a2.885 2.885 0 0 0-.19-.013"
                   "c-.858 0-1.468.748-1.468 1.551 0 .72.415 1.329.859 2.049"
                   ".332.581.719 1.329.719 2.409 0 .748-.287 1.617-.663 2.825l-.871 2.907"
                   "-3.138-9.534zm3.64 11.791-.012-.025 2.733-7.897c.51-1.274.68-2.293.68-3.199"
                   " 0-.329-.021-.634-.059-.921A8.751 8.751 0 0 1 20.75 12c0 3.216-1.731 6.031-4.319 7.56l.42-1z'/>"
                   "</svg>")
        _fb_svg = ("<svg width='15' height='15' viewBox='0 0 24 24' fill='currentColor'>"
                   "<path d='M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12"
                   "c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43"
                   "c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83"
                   "c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385"
                   "C19.612 23.027 24 18.062 24 12.073z'/></svg>")

        def _ptab_btn(key, icon_svg, label, color, active_color):
            is_a = ptab == key
            if is_a:
                style = f"background:{active_color};color:#fff;border-color:{active_color};box-shadow:0 4px 14px {active_color}44"
            else:
                style = "background:var(--surface);color:var(--muted);border-color:var(--border2)"
            return (f"<a href='?tab=publicacao&ptab={key}' style='display:inline-flex;align-items:center;gap:8px;"
                    f"padding:9px 22px;border-radius:12px;font-size:13px;font-weight:700;"
                    f"border:1.5px solid;text-decoration:none;transition:all .2s;{style}'>"
                    f"{icon_svg} {label}</a>")

        ptab_nav = (f"<div style='display:flex;gap:10px;margin-bottom:22px'>"
                    + _ptab_btn("wordpress", _wp_svg, "WordPress", "#21759b", "#21759b")
                    + _ptab_btn("facebook",  _fb_svg, "Facebook",  "#1877f2", "#1877f2")
                    + "</div>")

        # Build fb pages cards HTML outside f-string to avoid backslash-in-expression issues
        _no_pages_link = f"/app/profiles/{p.id}?tab=integracoes&itab=facebook"
        if fb_pages:
            _fb_cards = []
            _fb_icon_path = "M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z"
            for pg in fb_pages:
                pg_id   = str(pg.get("page_id") or "").strip()
                if not pg_id: continue
                pg_nm   = html.escape(str(pg.get("name") or "") or "P\u00e1gina sem nome")
                pg_id_e = html.escape(pg_id[:20])
                is_sel  = (not fb_selected_ids or pg_id in fb_selected_ids)
                border  = "rgba(24,119,242,.4)" if is_sel else "var(--border2)"
                bg      = "rgba(24,119,242,.06)" if is_sel else "transparent"
                chk     = "checked" if is_sel else ""
                _fb_cards.append(
                    f"<label style='display:flex;align-items:center;gap:10px;padding:10px 14px;"
                    f"border:1.5px solid {border};border-radius:10px;cursor:pointer;background:{bg}'>"
                    f"<input type='checkbox' name='facebook_page_ids' value='{html.escape(pg_id)}' {chk} style='width:16px;height:16px;flex-shrink:0' />"
                    f"<svg width='18' height='18' viewBox='0 0 24 24' fill='#1877f2'><path d='{_fb_icon_path}'/></svg>"
                    f"<div><div style='font-weight:600;font-size:13px'>{pg_nm}</div>"
                    f"<div style='font-size:11px;color:var(--muted)'>ID: {pg_id_e}</div></div></label>"
                )
            _fb_pages_cards_html = "<div style='display:flex;flex-direction:column;gap:8px'>" + "".join(_fb_cards) + "</div>"
        else:
            _fb_pages_cards_html = (
                f"<div style='padding:20px;text-align:center;background:var(--surface2);"
                f"border-radius:10px;border:1px dashed var(--border2)'>"
                f"<div style='font-size:24px;margin-bottom:8px'>&#128441;</div>"
                f"<div style='font-size:13px;color:var(--muted)'>Nenhuma p&#225;gina cadastrada. "
                f"<a href='{_no_pages_link}' style='color:#1877f2;font-weight:600'>"
                f"Adicionar nas Integra&#231;&#245;es &#8594;</a></div></div>"
            )

        if ptab == "facebook":
            ptab_content = f"""
            {_ph("publicacao-facebook")}
            <!-- Facebook header -->
            <div style="display:flex;align-items:center;gap:16px;padding:18px 22px;background:linear-gradient(135deg,#1877f2,#0d65d9);border-radius:14px;margin-bottom:18px;color:#fff">
              <div style="width:48px;height:48px;border-radius:12px;background:rgba(255,255,255,.18);display:flex;align-items:center;justify-content:center;flex-shrink:0">
                <svg width='26' height='26' viewBox='0 0 24 24' fill='#fff'>
                  <path d='M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z'/>
                </svg>
              </div>
              <div>
                <div style="font-weight:800;font-size:16px">Publica&#231;&#227;o no Facebook</div>
                <div style="font-size:12px;opacity:.85;margin-top:2px">Configure suas p&#225;ginas e prefer&#234;ncias de postagem</div>
              </div>
              <div style="margin-left:auto">
                {"<span style='background:rgba(255,255,255,.25);border:1px solid rgba(255,255,255,.4);border-radius:20px;padding:4px 12px;font-size:11px;font-weight:700'>&#9679; Ativo</span>" if fb_enabled else "<span style='background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.2);border-radius:20px;padding:4px 12px;font-size:11px;font-weight:700;opacity:.7'>Inativo</span>"}
              </div>
            </div>
            <form method="post" action="/app/profiles/{p.id}/publish/facebook">
              <!-- Enable toggle -->
              <div class="card" style="margin-bottom:14px;padding:18px 22px">
                <div style="display:flex;align-items:center;justify-content:space-between;gap:16px">
                  <div>
                    <div style="font-weight:700;font-size:14px">Ativar postagem autom&#225;tica</div>
                    <div style="font-size:12px;color:var(--muted);margin-top:3px">Quando ativo, cada artigo publicado no site gera tamb&#233;m um post no Facebook</div>
                  </div>
                  <label style="position:relative;display:inline-block;width:50px;height:26px;flex-shrink:0">
                    <input type="checkbox" name="facebook_enabled" value="1" {"checked" if fb_enabled else ""} style="width:0;height:0;opacity:0;position:absolute" id="fb-toggle-{p.id}" />
                    <span onclick="var cb=document.getElementById('fb-toggle-{p.id}');cb.checked=!cb.checked"
                      style="position:absolute;cursor:pointer;inset:0;border-radius:34px;background:{'#1877f2' if fb_enabled else 'var(--border2)'};transition:.3s">
                      <span style="position:absolute;content:'';height:20px;width:20px;left:{'26px' if fb_enabled else '3px'};bottom:3px;border-radius:50%;background:#fff;transition:.3s"></span>
                    </span>
                  </label>
                </div>
              </div>
              <!-- Pages -->
              <div class="card" style="margin-bottom:14px;padding:18px 22px">
                <div style="font-weight:700;font-size:14px;margin-bottom:4px">P&#225;ginas selecionadas</div>
                <div style="font-size:12px;color:var(--muted);margin-bottom:14px">Escolha em quais p&#225;ginas o conte&#250;do ser&#225; publicado</div>
                {_ph("fb-pages-list")}
                {_fb_pages_cards_html}
              </div>
              <!-- Link placement -->
              <div class="card" style="margin-bottom:18px;padding:18px 22px">
                <div style="font-weight:700;font-size:14px;margin-bottom:4px">Onde inserir o link do artigo</div>
                <div style="font-size:12px;color:var(--muted);margin-bottom:14px">O link redireciona para o artigo no WordPress</div>
                <div style="display:flex;gap:10px;flex-wrap:wrap">
                  <label style="flex:1;min-width:160px;display:flex;align-items:center;gap:10px;padding:12px 16px;border:1.5px solid {'rgba(24,119,242,.5)' if fb_link_place=='comments' else 'var(--border2)'};border-radius:10px;cursor:pointer;background:{'rgba(24,119,242,.06)' if fb_link_place=='comments' else 'transparent'}">
                    <input type="radio" name="facebook_link" value="comments" {"checked" if fb_link_place == "comments" else ""} style="width:16px;height:16px" />
                    <div><div style="font-weight:600;font-size:13px">&#128172; Nos coment&#225;rios</div><div style="font-size:11px;color:var(--muted)">Link no 1&#186; coment&#225;rio do post</div></div>
                  </label>
                  <label style="flex:1;min-width:160px;display:flex;align-items:center;gap:10px;padding:12px 16px;border:1.5px solid {'rgba(24,119,242,.5)' if fb_link_place=='body' else 'var(--border2)'};border-radius:10px;cursor:pointer;background:{'rgba(24,119,242,.06)' if fb_link_place=='body' else 'transparent'}">
                    <input type="radio" name="facebook_link" value="body" {"checked" if fb_link_place == "body" else ""} style="width:16px;height:16px" />
                    <div><div style="font-weight:600;font-size:13px">&#128196; No texto</div><div style="font-size:11px;color:var(--muted)">Link inclu&#237;do no corpo do post</div></div>
                  </label>
                </div>
              </div>
              <!-- Image mode -->
              <div class="card" style="margin-bottom:18px;padding:18px 22px">
                <div style="font-weight:700;font-size:14px;margin-bottom:4px">Imagem no post do Facebook</div>
                <div style="font-size:12px;color:var(--muted);margin-bottom:14px">Como a imagem do artigo deve aparecer no post</div>
                <div style="display:flex;gap:10px;flex-wrap:wrap">
                  <label style="flex:1;min-width:160px;display:flex;align-items:center;gap:10px;padding:12px 16px;border:1.5px solid {'rgba(24,119,242,.5)' if fb_image_mode=='link_preview' else 'var(--border2)'};border-radius:10px;cursor:pointer;background:{'rgba(24,119,242,.06)' if fb_image_mode=='link_preview' else 'transparent'}">
                    <input type="radio" name="facebook_image" value="link_preview" {"checked" if fb_image_mode == "link_preview" else ""} style="width:16px;height:16px" />
                    <div><div style="font-weight:600;font-size:13px">&#128279; Preview do link</div><div style="font-size:11px;color:var(--muted)">Facebook gera o preview automaticamente a partir do link do artigo</div></div>
                  </label>
                  <label style="flex:1;min-width:160px;display:flex;align-items:center;gap:10px;padding:12px 16px;border:1.5px solid {'rgba(24,119,242,.5)' if fb_image_mode=='direct_photo' else 'var(--border2)'};border-radius:10px;cursor:pointer;background:{'rgba(24,119,242,.06)' if fb_image_mode=='direct_photo' else 'transparent'}">
                    <input type="radio" name="facebook_image" value="direct_photo" {"checked" if fb_image_mode == "direct_photo" else ""} style="width:16px;height:16px" />
                    <div><div style="font-weight:600;font-size:13px">&#128247; Imagem direta</div><div style="font-size:11px;color:var(--muted)">Faz upload da imagem do artigo diretamente (foto com legenda)</div></div>
                  </label>
                  <label style="flex:1;min-width:160px;display:flex;align-items:center;gap:10px;padding:12px 16px;border:1.5px solid {'rgba(24,119,242,.5)' if fb_image_mode=='none' else 'var(--border2)'};border-radius:10px;cursor:pointer;background:{'rgba(24,119,242,.06)' if fb_image_mode=='none' else 'transparent'}">
                    <input type="radio" name="facebook_image" value="none" {"checked" if fb_image_mode == "none" else ""} style="width:16px;height:16px" />
                    <div><div style="font-weight:600;font-size:13px">&#128683; Sem imagem</div><div style="font-size:11px;color:var(--muted)">Publica apenas o texto, sem imagem ou preview</div></div>
                  </label>
                </div>
              </div>
              <div style="display:flex;justify-content:flex-end">
                <button class="btn" type="submit" style="background:#1877f2;border-color:#1877f2;padding:11px 28px;font-size:14px">
                  <svg width='14' height='14' viewBox='0 0 24 24' fill='#fff' style='margin-right:6px'><path d='M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z'/></svg>
                  Salvar Facebook
                </button>
              </div>
            </form>"""
        else:
            ptab_content = f"""
            {_ph("publicacao-wordpress")}
            <div style="display:flex;align-items:center;gap:16px;padding:18px 22px;background:linear-gradient(135deg,#21759b,#155f82);border-radius:14px;margin-bottom:18px;color:#fff">
              <div style="width:48px;height:48px;border-radius:12px;background:rgba(255,255,255,.18);display:flex;align-items:center;justify-content:center;flex-shrink:0">
                <svg width='26' height='26' viewBox='0 0 24 24' fill='#fff'>
                  <path d='M12 2C6.486 2 2 6.486 2 12s4.486 10 10 10 10-4.486 10-10S17.514 2 12 2zM3.251 12c0-1.308.265-2.556.741-3.695L7.36 18.658A8.762 8.762 0 0 1 3.251 12zm8.749 8.75a8.773 8.773 0 0 1-2.496-.364l2.65-7.695 2.716 7.44a.96.96 0 0 0 .07.136 8.764 8.764 0 0 1-2.94.483zm1.211-12.981c.528-.028.999-.084.999-.084.47-.056.415-.748-.056-.72 0 0-1.415.111-2.329.111-.858 0-2.3-.111-2.3-.111-.47-.028-.526.692-.055.72 0 0 .444.056.914.084l1.358 3.72-1.908 5.721-3.176-8.441c.528-.028 1-.084 1-.084.47-.056.415-.748-.056-.72 0 0-1.415.111-2.329.111a12.65 12.65 0 0 1-.31-.005A8.752 8.752 0 0 1 12 3.25c2.294 0 4.389.879 5.963 2.315a2.885 2.885 0 0 0-.19-.013c-.858 0-1.468.748-1.468 1.551 0 .72.415 1.329.859 2.049.332.581.719 1.329.719 2.409 0 .748-.287 1.617-.663 2.825l-.871 2.907-3.138-9.534zm3.64 11.791-.012-.025 2.733-7.897c.51-1.274.68-2.293.68-3.199 0-.329-.021-.634-.059-.921A8.751 8.751 0 0 1 20.75 12c0 3.216-1.731 6.031-4.319 7.56l.42-1z'/>
                </svg>
              </div>
              <div>
                <div style="font-weight:800;font-size:16px">Publica&#231;&#227;o no WordPress</div>
                <div style="font-size:12px;opacity:.85;margin-top:2px">Configure categorias e prefer&#234;ncias de conte&#250;do</div>
              </div>
            </div>
            <form method="post" action="/app/profiles/{p.id}/publish/wordpress">
              <div class="card" style="margin-bottom:14px;padding:18px 22px">
                <div style="font-weight:700;font-size:14px;margin-bottom:4px">Categoria padr&#227;o</div>
                <div style="font-size:12px;color:var(--muted);margin-bottom:12px">Usada quando a IA n&#227;o consegue identificar a categoria correta</div>
                <input name="default_category" value="{html.escape(default_cat)}" required placeholder="Ex: Not&#237;cias" />
              </div>
              <div class="card" style="margin-bottom:18px;padding:18px 22px">
                <div style="font-weight:700;font-size:14px;margin-bottom:4px">Categorias do site</div>
                <div style="font-size:12px;color:var(--muted);margin-bottom:12px">Liste <b>exatamente</b> como aparecem no WordPress ” uma por linha. A IA escolhe 1 dessa lista.</div>
                <textarea name="categories" placeholder="Receitas&#10;Viagens&#10;Tecnologia&#10;Sa&#250;de" style="min-height:180px;font-size:13px">{html.escape(cats_lines)}</textarea>
                <div style="margin-top:8px;font-size:11px;color:var(--muted)">Categorias com nomes diferentes do WordPress causam erros de classifica&#231;&#227;o.</div>
              </div>
              <div class="card" style="margin-bottom:18px;padding:18px 22px">
                <div style="font-weight:700;font-size:14px;margin-bottom:4px">Modo r&#225;pido</div>
                <div style="font-size:12px;color:var(--muted);margin-bottom:14px">Acelera a coleta e a reescrita. Se a busca normal travar, o bot tenta o RSS do pr&#243;prio site depois do limite definido.</div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
                  <label style="display:flex;align-items:flex-start;gap:10px;padding:12px 14px;border:1px solid var(--border2);border-radius:10px">
                    <input type="checkbox" name="fast_publish_enabled" value="1" {"checked" if fast_publish_enabled else ""} style="width:16px;height:16px;margin-top:2px" />
                    <span>
                      <span style="display:block;font-weight:700;font-size:13px">Ativar modo r&#225;pido</span>
                      <span style="display:block;font-size:11px;color:var(--muted);margin-top:4px">Mant&#233;m a estrutura do post, mas reduz o caminho lento quando a fonte &#233; homepage ou listagem.</span>
                    </span>
                  </label>
                  <div>
                    <label>Trocar para RSS ap&#243;s (seg)</label>
                    <input name="rss_fallback_after_seconds" type="number" min="5" max="180" step="1" value="{rss_fallback_after_seconds}" />
                    <div class="muted" style="margin-top:5px;font-size:12px">Quando a coleta da URL passar desse tempo, tenta o feed RSS do mesmo site.</div>
                  </div>
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px">
                  <label style="display:flex;align-items:flex-start;gap:10px;padding:12px 14px;border:1px solid var(--border2);border-radius:10px">
                    <input type="checkbox" name="fast_skip_wp_image" value="1" {"checked" if fast_skip_wp_image else ""} style="width:16px;height:16px;margin-top:2px" />
                    <span>
                      <span style="display:block;font-weight:700;font-size:13px">Pular imagem destacada</span>
                      <span style="display:block;font-size:11px;color:var(--muted);margin-top:4px">Corta o upload para o WordPress, que costuma ser uma das partes mais lentas.</span>
                    </span>
                  </label>
                  <label style="display:flex;align-items:flex-start;gap:10px;padding:12px 14px;border:1px solid var(--border2);border-radius:10px">
                    <input type="checkbox" name="fast_skip_wp_tags" value="1" {"checked" if fast_skip_wp_tags else ""} style="width:16px;height:16px;margin-top:2px" />
                    <span>
                      <span style="display:block;font-weight:700;font-size:13px">Pular cria&#231;&#227;o de tags</span>
                      <span style="display:block;font-size:11px;color:var(--muted);margin-top:4px">Evita chamadas extras na API do WordPress quando a prioridade for velocidade.</span>
                    </span>
                  </label>
                </div>
                <div style="margin-top:10px;font-size:11px;color:var(--muted)">Para tirar o melhor desse modo, use o Prompt 4 na aba IA para uma vers&#227;o mais enxuta da receita.</div>
              </div>
              <div style="display:flex;justify-content:flex-end">
                <button class="btn" type="submit" style="background:#21759b;border-color:#21759b;padding:11px 28px;font-size:14px">
                  <svg width='14' height='14' viewBox='0 0 24 24' fill='#fff' style='margin-right:6px'><path d='M12 2C6.486 2 2 6.486 2 12s4.486 10 10 10 10-4.486 10-10S17.514 2 12 2z'/></svg>
                  Salvar WordPress
                </button>
              </div>
            </form>"""

        body += f"""
        {_ph("tab-publicacao")}
        <!-- Cadência de Publicação -->
        <div class="card" style="margin-bottom:14px">
          <details class="toggle-section" open>
            <summary>
              <span class="ts-title" style="display:flex;align-items:center;gap:8px">
                <span style="width:26px;height:26px;border-radius:7px;background:rgba(139,92,246,.15);display:inline-flex;align-items:center;justify-content:center;font-size:13px">&#128203;</span>
                Cad&#234;ncia de Publica&#231;&#227;o
              </span>
              <span class="ts-arrow">&#9655;</span>
            </summary>
            <div class="ts-body">
              <p class="muted" style="margin-bottom:14px">Define quantos posts o bot vai publicar por ciclo e o espa&#231;amento entre cada um.</p>
              <form method="post" action="/app/profiles/{p.id}/schedule">
                <input type="hidden" name="next_tab" value="publicacao" />
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
                  <div>
                    <label>Posts por sess&#227;o</label>
                    <input name="posts_per_day" type="number" min="1" step="1" value="{_pub_posts_per_day}" />
                    <div class="muted" style="margin-top:5px;font-size:12px">Quantidade m&#225;xima de posts publicados por ciclo do bot</div>
                  </div>
                  <div>
                    <label>Intervalo entre postagens (min)</label>
                    <input name="interval_minutes" type="number" min="0" step="1" value="{_pub_interval_min}" />
                    <div class="muted" style="margin-top:5px;font-size:12px">0 = publica tudo seguido sem pausa entre os posts</div>
                  </div>
                </div>
                <div style="margin-top:14px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px">
                  <div style="font-size:12px;color:var(--muted)">
                    &#128161; Configura&#231;&#245;es de data/hora e agendamento avan&#231;ado est&#227;o na aba <b>Agendamento</b>.
                  </div>
                  <button class="btn flat" type="submit" style="gap:6px;padding:9px 22px">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
                    Salvar cad&#234;ncia
                  </button>
                </div>
              </form>
            </div>
          </details>
        </div>
        {ptab_nav}
        {ptab_content}
        """
    elif tab == "integracoes":
        integrations = list(db.scalars(select(Integration).where(Integration.profile_id == p.id).order_by(Integration.created_at.desc())))
        fb_pages: list[dict] = []
        fb_integ = db.scalar(select(Integration).where(Integration.profile_id == p.id, Integration.type == IntegrationType.FACEBOOK))
        fb_oauth_app_id = ""
        fb_oauth_app_secret_saved = False
        if fb_integ:
            try:
                fb_creds = decrypt_json(fb_integ.credentials_encrypted)
                pages_val = fb_creds.get("pages") if isinstance(fb_creds, dict) else None
                if isinstance(pages_val, list):
                    fb_pages = [x for x in pages_val if isinstance(x, dict)]
                fb_oauth_app_id = str(fb_creds.get("oauth_app_id") or "").strip()
                fb_oauth_app_secret_saved = bool(str(fb_creds.get("oauth_app_secret") or "").strip())
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
            _fb_test_btn_id = f"fb-test-{p.id}-{pid[:8]}"
            _fb_test_btn = (
                f"<button type='button' id='{_fb_test_btn_id}' "
                f"onclick=\"(function(){{var btn=document.getElementById('{_fb_test_btn_id}');"
                f"btn.disabled=true;btn.textContent='Testando...';"
                f"fetch('/app/profiles/{p.id}/integrations/facebook/pages/test',{{method:'POST',"
                f"headers:{{'Content-Type':'application/x-www-form-urlencoded'}},"
                f"body:'page_id={html.escape(pid, quote=True)}'}}).then(function(r){{return r.json();}})"
                f".then(function(d){{var s=document.getElementById('{_fb_test_btn_id}-status');"
                f"if(d.ok){{s.innerHTML='<span style=\\'color:#10b981;font-weight:700\\'>&#10003; '+d.name+'</span>';}}"
                f"else{{s.innerHTML='<span style=\\'color:#ef4444;font-weight:700\\'>&#10007; '+(d.error||'erro')+'</span>';}}"
                f"btn.textContent='Testar';btn.disabled=false;}})"
                f".catch(function(){{var s=document.getElementById('{_fb_test_btn_id}-status');"
                f"s.innerHTML='<span style=\\'color:#ef4444\\'>Erro de rede</span>';"
                f"btn.textContent='Testar';btn.disabled=false;}});}})()\""
                f" style='font-size:12px;padding:5px 12px' class='btn flat'>Testar</button>"
            )
            fb_rows += (
                f"<tr style='border-top:1px solid var(--border)'>"
                f"<td style='padding:12px 18px;font-size:13px;font-weight:600'>{html.escape(nm) or '”'}</td>"
                f"<td style='padding:12px 18px;font-size:13px;color:var(--muted);font-family:monospace'>{html.escape(pid)}</td>"
                f"<td style='padding:12px 18px'><span class='pill'>{html.escape(token_state)}</span></td>"
                f"<td id='{_fb_test_btn_id}-status' style='padding:12px 18px;font-size:12px;color:var(--muted)'>”</td>"
                f"<td style='padding:12px 18px;text-align:right'><div style='display:flex;gap:6px;justify-content:flex-end;align-items:center'>"
                f"{_fb_test_btn}"
                f"<form method='post' action='/app/profiles/{p.id}/integrations/facebook/pages/remove' style='margin:0'>"
                f"<input type='hidden' name='page_id' value='{html.escape(pid)}' />"
                f"<button class='btn secondary' type='submit' style='font-size:12px;padding:5px 12px;color:#ef4444'>Remover</button></form>"
                f"</div></td></tr>"
            )
        if not fb_rows:
            fb_rows = "<tr><td colspan='5' style='padding:20px 18px;text-align:center;color:var(--muted);font-size:13px'>Nenhuma p\u00e1gina cadastrada.</td></tr>"
        # Monta linhas da tabela Conexões com URL extraída dos dados cifrados
        conn_rows = ""
        for i in integrations:
            try:
                icreds = decrypt_json(i.credentials_encrypted)
            except Exception:
                icreds = {}
            if i.type == IntegrationType.WORDPRESS:
                conn_url = str(icreds.get("base_url") or "”")
            elif i.type == IntegrationType.FACEBOOK:
                pages_list = icreds.get("pages") or []
                conn_url = f"{len(pages_list)} página(s)" if pages_list else "”"
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
        _svg_edit = "<svg width='13' height='13' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7'/><path d='M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z'/></svg>"
        _svg_trash = "<svg width='13' height='13' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><polyline points='3 6 5 6 21 6'/><path d='M19 6l-1 14H6L5 6'/><path d='M10 11v6'/><path d='M14 11v6'/><path d='M9 6V4h6v2'/></svg>"
        _svg_save  = "<svg width='13' height='13' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z'/><polyline points='17 21 17 13 7 13 7 21'/><polyline points='7 3 7 8 15 8'/></svg>"
        for _wu_idx, wu in enumerate(wp_users):
            uname = html.escape(str(wu.get("username") or ""))
            raw_pass = html.escape(str(wu.get("app_password") or ""), quote=True)
            is_active_wu = (wu.get("username") == wp_active_username)
            _pid = f"wpp-{p.id}-{_wu_idx}"
            _edit_row_id = "wp-edit-" + _pid
            status_badge = "<span class='badge-active' style='font-size:11px;padding:3px 8px'><span class='dot-pulse'></span>Em uso</span>" if is_active_wu else "<span class='badge-inactive' style='font-size:11px;padding:3px 8px'><span class='dot-off'></span>Inativo</span>"
            usar_btn = (
                "<span style='font-size:11px;color:var(--muted)'>”</span>"
                if is_active_wu else
                f"<form method='post' action='/app/profiles/{p.id}/integrations/wordpress/set-active-user' style='margin:0'>"
                f"<input type='hidden' name='username' value='{uname}' />"
                f"<button class='btn flat' style='font-size:12px;padding:5px 12px' type='submit'>Usar</button></form>"
            )
            edit_btn = (
                "<button type='button' class='btn flat' style='font-size:12px;padding:5px 12px;gap:5px' "
                "onclick=\"var r=document.getElementById('" + _edit_row_id + "');"
                "r.style.display=r.style.display==='none'?'table-row':'none'\">"
                + _svg_edit + "Editar</button>"
            )
            del_btn = (
                f"<form method='post' action='/app/profiles/{p.id}/integrations/wordpress/remove-user' style='margin:0'>"
                f"<input type='hidden' name='username' value='{uname}' />"
                f"<button class='btn flat' type='submit' style='font-size:12px;padding:5px 12px;color:#ef4444;border-color:rgba(239,68,68,.25);gap:5px' "
                f"onclick=\"return confirm('Remover este usu\u00e1rio?')\">"
                + _svg_trash + f"Remover</button></form>"
            )
            pass_cell = (
                f"<div style='display:flex;align-items:center;gap:5px'>"
                f"<span id='{_pid}' data-pass='{raw_pass}' data-shown='0' "
                f"style='font-family:monospace;font-size:12px;color:var(--muted);letter-spacing:1px'>&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;</span>"
                f"<button type='button' id='{_pid}-btn' "
                f"onclick=\"var s=document.getElementById('{_pid}');var shown=s.dataset.shown==='1';"
                f"s.textContent=shown?'\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022':s.dataset.pass;s.dataset.shown=shown?'0':'1';\" "
                f"style='background:none;border:none;cursor:pointer;color:var(--muted);padding:2px;display:flex;align-items:center'>"
                f"<svg width='13' height='13' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2'>"
                f"<path d='M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z'/><circle cx='12' cy='12' r='3'/></svg>"
                f"</button></div>"
            )
            edit_row = (
                f"<tr id='{_edit_row_id}' style='display:none;background:var(--surface2);border-top:1px solid var(--border)'>"
                f"<td colspan='4' style='padding:16px 18px'>"
                f"<form method='post' action='/app/profiles/{p.id}/integrations/wordpress/edit-user'>"
                f"<input type='hidden' name='old_username' value='{uname}' />"
                f"<div style='display:grid;grid-template-columns:1fr 1fr auto;gap:12px;align-items:flex-end'>"
                f"<div><label style='font-size:11px;font-weight:600;color:var(--muted);margin-bottom:5px;display:block'>Usu\u00e1rio</label>"
                f"<input name='new_username' value='{uname}' required style='margin:0' /></div>"
                f"<div><label style='font-size:11px;font-weight:600;color:var(--muted);margin-bottom:5px;display:block'>App Password</label>"
                f"<input name='new_app_password' type='password' placeholder='Nova senha (deixe vazio para manter)' style='margin:0' /></div>"
                f"<button class='btn flat' type='submit' style='height:42px;padding:0 18px;gap:6px'>"
                + _svg_save + f"Salvar</button>"
                f"</div></form></td></tr>"
            )
            wp_user_rows += (
                f"<tr style='border-top:1px solid var(--border)'>"
                f"<td style='padding:13px 18px'><span style='font-size:14px;font-weight:600'>{uname}</span></td>"
                f"<td style='padding:13px 18px'>{status_badge}</td>"
                f"<td style='padding:13px 18px'>{pass_cell}</td>"
                f"<td style='padding:13px 18px;text-align:right'><div style='display:flex;gap:8px;align-items:center;justify-content:flex-end'>{usar_btn}{edit_btn}{del_btn}</div></td>"
                f"</tr>"
                + edit_row
            )
        if not wp_user_rows:
            wp_user_rows = "<tr><td colspan='4' style='padding:20px 18px;text-align:center;color:var(--muted);font-size:13px'>Nenhum usu\u00e1rio cadastrado.</td></tr>"

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

        oai_integ = db.scalar(select(Integration).where(Integration.profile_id == p.id, Integration.type == IntegrationType.OPENAI))
        oai_current_model = "gpt-4o-mini"
        oai_configured = False
        if oai_integ:
            try:
                oai_creds = decrypt_json(oai_integ.credentials_encrypted)
                oai_current_model = str(oai_creds.get("model") or "gpt-4o-mini").strip() or "gpt-4o-mini"
                oai_configured = bool(oai_creds.get("api_key"))
            except Exception:
                pass

        # Aba ativa dentro de integrações (via query param itab)
        itab = (request.query_params.get("itab") or "wordpress").strip().lower()

        _itab_icons = {
            "wordpress": "<svg width='16' height='16' viewBox='0 0 24 24' fill='currentColor'><path d='M12 2C6.486 2 2 6.486 2 12s4.486 10 10 10 10-4.486 10-10S17.514 2 12 2zM3.251 12c0-1.308.265-2.556.741-3.695L7.36 18.658A8.762 8.762 0 0 1 3.251 12zm8.749 8.75a8.773 8.773 0 0 1-2.496-.364l2.65-7.695 2.716 7.44a.96.96 0 0 0 .07.136 8.764 8.764 0 0 1-2.94.483zm1.211-12.981c.528-.028.999-.084.999-.084.47-.056.415-.748-.056-.72 0 0-1.415.111-2.329.111-.858 0-2.3-.111-2.3-.111-.47-.028-.526.692-.055.72 0 0 .444.056.914.084l1.358 3.72-1.908 5.721-3.176-8.441c.528-.028 1-.084 1-.084.47-.056.415-.748-.056-.72 0 0-1.415.111-2.329.111a12.65 12.65 0 0 1-.31-.005A8.752 8.752 0 0 1 12 3.25c2.294 0 4.389.879 5.963 2.315a2.885 2.885 0 0 0-.19-.013c-.858 0-1.468.748-1.468 1.551 0 .72.415 1.329.859 2.049.332.581.719 1.329.719 2.409 0 .748-.287 1.617-.663 2.825l-.871 2.907-3.138-9.534zm3.64 11.791-.012-.025 2.733-7.897c.51-1.274.68-2.293.68-3.199 0-.329-.021-.634-.059-.921A8.751 8.751 0 0 1 20.75 12c0 3.216-1.731 6.031-4.319 7.56l.42-1z'/></svg>",
            "gemini": "<svg width='16' height='16' viewBox='0 0 24 24' fill='currentColor'><path d='M12 2l2.4 7.4H22l-6.3 4.6 2.4 7.4L12 17l-6.1 4.4 2.4-7.4L2 9.4h7.6z' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linejoin='round'/><path d='M12 2C10.5 7 8 9.5 2 12c6 2.5 8.5 5 10 10 1.5-5 4-7.5 10-10-6-2.5-8.5-5-10-10z'/></svg>",
            "openai": "<svg width='16' height='16' viewBox='0 0 24 24' fill='currentColor'><path d='M22.28 9.95a5.4 5.4 0 0 0-.46-4.43 5.47 5.47 0 0 0-5.88-2.62 5.4 5.4 0 0 0-4.07-1.82 5.47 5.47 0 0 0-5.21 3.79 5.4 5.4 0 0 0-3.61 2.62 5.47 5.47 0 0 0 .67 6.41 5.4 5.4 0 0 0 .46 4.43 5.47 5.47 0 0 0 5.88 2.62 5.4 5.4 0 0 0 4.07 1.82 5.47 5.47 0 0 0 5.22-3.8 5.4 5.4 0 0 0 3.6-2.62 5.47 5.47 0 0 0-.67-6.42zm-8.15 11.42a4.05 4.05 0 0 1-2.6-.94l.13-.07 4.31-2.49a.71.71 0 0 0 .36-.62v-6.08l1.82 1.05a.07.07 0 0 1 .04.05v5.03a4.07 4.07 0 0 1-4.06 4.07zm-8.73-3.74a4.05 4.05 0 0 1-.48-2.72l.13.08 4.31 2.49a.71.71 0 0 0 .71 0l5.27-3.04v2.1a.07.07 0 0 1-.03.06l-4.36 2.52a4.07 4.07 0 0 1-5.55-1.49zm-1.14-9.43a4.05 4.05 0 0 1 2.12-1.79v5.1a.71.71 0 0 0 .36.62l5.26 3.04-1.82 1.05a.07.07 0 0 1-.07 0L5.7 13.7a4.07 4.07 0 0 1-.44-5.5zm14.97 3.49-5.27-3.04 1.82-1.05a.07.07 0 0 1 .07 0l4.37 2.52a4.07 4.07 0 0 1-.63 6.55v-5.1a.71.71 0 0 0-.36-.88zm1.81-2.73-.13-.08-4.3-2.5a.71.71 0 0 0-.72 0L10.63 9.4V7.3a.07.07 0 0 1 .03-.06l4.36-2.52a4.07 4.07 0 0 1 6.04 4.24zm-11.4 3.75-1.82-1.05a.07.07 0 0 1-.04-.05V6.58a4.07 4.07 0 0 1 6.68-3.13l-.13.07-4.3 2.49a.71.71 0 0 0-.36.62l-.03 6.09zm.99-2.13 2.35-1.35 2.34 1.35v2.7l-2.34 1.35-2.35-1.35v-2.7z'/></svg>",
            "facebook": "<svg width='16' height='16' viewBox='0 0 24 24' fill='currentColor'><path d='M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z'/></svg>",
            "conexoes": "<svg width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71'/><path d='M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71'/></svg>",
        }
        itabs = [("wordpress", "WordPress"), ("gemini", "Gemini"), ("openai", "ChatGPT"), ("facebook", "Facebook"), ("conexoes", "Conexões")]

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
                ("gemini-2.0-flash",       "gemini-2.0-flash ” Rápido, mais recente ⚡"),
                ("gemini-2.0-flash-lite",  "gemini-2.0-flash-lite ” Leve e econômico 🪶"),
                ("gemini-1.5-flash-latest","gemini-1.5-flash-latest ” Padrão recomendado ✅"),
                ("gemini-1.5-flash-8b",    "gemini-1.5-flash-8b ” Ultra rápido, menor 🏎"),
                ("gemini-1.5-pro-latest",  "gemini-1.5-pro-latest ” Mais inteligente, mais lento 🧠"),
                ("gemini-2.0-pro-exp",     "gemini-2.0-pro-exp ” Experimental, Pro 2.0 🔬"),
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
                        class="btn secondary" style="flex:1;justify-content:center">✏ï¸ Editar</button>
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
                _gem_form_label = "✏ï¸ Editar chave / modelo"
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
                    <div style="margin-top:6px;font-size:11px;color:var(--muted)">Dúvida? Deixe <b>gemini-1.5-flash-latest</b> ” bom para a maioria dos casos.</div>
                  </div>
                </div>
                <div style="margin-top:20px;display:flex;gap:10px">
                  <button class="btn" type="submit">Salvar</button>
                  {"<button type='button' class='btn secondary' onclick=\"document.getElementById('gem-edit-form').style.display='none'\">Cancelar</button>" if gem_configured else ""}
                </div>
              </form>
            </div>"""
        elif itab == "openai":
            _oai_models = [
                ("gpt-4o-mini",   "gpt-4o-mini ” Rápido e econômico ⚡"),
                ("gpt-4o",        "gpt-4o ” Mais inteligente, multimodal 🧠"),
                ("gpt-4-turbo",   "gpt-4-turbo ” GPT-4 otimizado 🚀"),
                ("gpt-3.5-turbo", "gpt-3.5-turbo ” Leve e rápido 🪶"),
            ]
            _oai_model_opts = "".join(
                f"<option value='{v}' {'selected' if v == oai_current_model else ''}>{html.escape(l)}</option>"
                for v, l in _oai_models
            )
            if oai_configured and oai_integ:
                try:
                    _oai_raw_key = decrypt_json(oai_integ.credentials_encrypted).get("api_key", "")
                    _oai_masked = (_oai_raw_key[:8] + "•" * min(16, max(4, len(_oai_raw_key) - 8))) if len(_oai_raw_key) > 8 else "••••••••"
                except Exception:
                    _oai_masked = "••••••••"
                _oai_integ_id = str(oai_integ.id)
                _oai_status = str(oai_integ.status.value) if oai_integ.status else "UNKNOWN"
                _oai_status_color = "#10b981" if _oai_status == "CONNECTED" else "#f59e0b"
                _oai_list = f"""
                <div style="border:1px solid var(--border2);border-radius:14px;overflow:hidden;margin-bottom:24px">
                  <div style="background:var(--surface2);padding:12px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
                    <div style="display:flex;align-items:center;gap:10px">
                      <span style="font-size:20px">🤖</span>
                      <div>
                        <div style="font-size:13px;font-weight:700;color:var(--text)">ChatGPT (OpenAI)</div>
                        <div style="font-size:11px;color:var(--muted)">Integração configurada</div>
                      </div>
                    </div>
                    <span style="color:{_oai_status_color};font-size:12px;font-weight:700;background:{'rgba(16,185,129,.1)' if _oai_status=='CONNECTED' else 'rgba(245,158,11,.1)'};border:1px solid {'rgba(16,185,129,.3)' if _oai_status=='CONNECTED' else 'rgba(245,158,11,.3)'};padding:3px 10px;border-radius:20px">{html.escape(_oai_status)}</span>
                  </div>
                  <div style="padding:18px 20px;display:grid;grid-template-columns:1fr 1fr;gap:16px">
                    <div>
                      <div style="font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted);margin-bottom:5px">Chave API</div>
                      <div style="font-family:monospace;font-size:13px;color:var(--text);background:var(--input-bg);border:1px solid var(--border);border-radius:8px;padding:7px 12px;letter-spacing:.5px">{html.escape(_oai_masked)}</div>
                    </div>
                    <div>
                      <div style="font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted);margin-bottom:5px">Modelo</div>
                      <div style="font-size:13px;font-weight:600;background:var(--input-bg);border:1px solid var(--border);border-radius:8px;padding:7px 12px">{html.escape(oai_current_model)}</div>
                    </div>
                    <div>
                      <div style="font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted);margin-bottom:5px">Bot</div>
                      <div style="font-size:13px;font-weight:600;padding:7px 0">{html.escape(p.name)}</div>
                    </div>
                    <div style="display:flex;align-items:flex-end;gap:8px;padding-bottom:2px">
                      <button type="button"
                        onclick="var f=document.getElementById('oai-edit-form');f.style.display=f.style.display==='none'?'block':'none'"
                        class="btn secondary" style="flex:1;justify-content:center">✏ï¸ Editar</button>
                      <form method="post" action="/app/profiles/{p.id}/integrations/{_oai_integ_id}/delete" style="margin:0;flex:1">
                        <button class="btn secondary" type="submit" style="width:100%;justify-content:center;color:#ef4444"
                          onclick="return confirm('Remover integração ChatGPT?')">
                          <svg width='13' height='13' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' style='margin-right:4px'>
                            <polyline points='3 6 5 6 21 6'/><path d='M19 6l-1 14H6L5 6'/><path d='M10 11v6'/><path d='M14 11v6'/><path d='M9 6V4h6v2'/>
                          </svg> Excluir
                        </button>
                      </form>
                    </div>
                  </div>
                </div>"""
                _oai_form_display = "display:none"
                _oai_form_label = "✏ï¸ Editar chave / modelo"
            else:
                _oai_list = ""
                _oai_form_display = "display:block"
                _oai_form_label = "Configurar ChatGPT"

            itab_content = f"""
            {_oai_list}
            <div id="oai-edit-form" style="{_oai_form_display};background:var(--surface2);border:1px solid var(--border);border-radius:14px;padding:24px">
              <div style="font-size:15px;font-weight:700;color:var(--text);margin-bottom:16px">{_oai_form_label}</div>
              <div style="background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:20px;font-size:13px;color:var(--muted);line-height:1.9">
                <div style="font-weight:700;color:var(--text);margin-bottom:4px">Como obter a API Key</div>
                <div>1. Acesse &nbsp;<a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener"
                  style="color:var(--primary);font-weight:600;text-decoration:underline">platform.openai.com/api-keys</a></div>
                <div>2. Faça login com sua conta OpenAI</div>
                <div>3. Clique em <b>Create new secret key</b></div>
                <div>4. Copie a chave e cole no campo abaixo</div>
                <div style="margin-top:8px;color:#f59e0b;font-size:12px">⚠ï¸ A API OpenAI requer créditos pagos. Com ChatGPT Plus você tem acesso à API separado ” verifique em platform.openai.com/usage.</div>
              </div>
              <form method="post" action="/app/profiles/{p.id}/integrations/openai">
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
                  <div style="grid-column:1/-1">
                    <label>OpenAI API Key</label>
                    <input name="api_key" type="password" placeholder="sk-..." required
                      style="font-family:monospace;letter-spacing:.5px" />
                  </div>
                  <div style="grid-column:1/-1">
                    <label>Modelo</label>
                    <select name="model" style="width:100%;padding:10px 13px;border-radius:9px;border:1px solid var(--border);background:var(--input-bg);color:var(--text);font-size:13px;cursor:pointer">
                      {_oai_model_opts}
                    </select>
                    <div style="margin-top:6px;font-size:11px;color:var(--muted)">Recomendado: <b>gpt-4o-mini</b> ” mais barato, suficiente para reescrever posts.</div>
                  </div>
                </div>
                <div style="margin-top:20px;display:flex;gap:10px">
                  <button class="btn" type="submit">Salvar</button>
                  {"<button type='button' class='btn secondary' onclick=\"document.getElementById('oai-edit-form').style.display='none'\">Cancelar</button>" if oai_configured else ""}
                </div>
              </form>
            </div>"""
        elif itab == "facebook":
            _fb_step_s = "display:flex;align-items:flex-start;gap:14px;padding:14px 0;border-bottom:1px solid var(--border)"
            _fb_num_s  = "width:28px;height:28px;border-radius:50%;background:var(--primary);color:#fff;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0;margin-top:1px"
            itab_content = f"""
            <!-- ══ Passo a passo ══ -->
            <details style="margin-bottom:20px" open>
              <summary style="cursor:pointer;font-size:14px;font-weight:700;color:var(--text);padding:12px 0;list-style:none;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)">
                <span style="width:26px;height:26px;border-radius:7px;background:rgba(24,119,242,.15);display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:13px">&#128214;</span>
                Como conectar sua p&#225;gina ao Facebook
                <span style="font-size:11px;color:var(--muted);font-weight:400;margin-left:4px">(clique para recolher)</span>
              </summary>
              <div style="padding:16px 0 4px">
                <div style="background:rgba(24,119,242,.07);border:1px solid rgba(24,119,242,.2);border-radius:12px;padding:14px 18px;margin-bottom:16px;font-size:13px;color:var(--muted);line-height:1.6">
                  O PostHub precisa de um <b style="color:var(--text)">Page Access Token</b> para publicar em nome da sua p&#225;gina. Siga os passos abaixo para obt&#234;-lo sem expor sua senha pessoal.
                </div>

                <div style="{_fb_step_s}">
                  <span style="{_fb_num_s}">1</span>
                  <div>
                    <div style="font-weight:600;font-size:13px;margin-bottom:4px">Acesse o Graph API Explorer</div>
                    <div style="font-size:12px;color:var(--muted);line-height:1.65">
                      Abra <b>developers.facebook.com/tools/explorer</b> no navegador.<br>
                      No canto superior direito, fa&#231;a login com sua conta Meta (a mesma que administra a p&#225;gina).
                    </div>
                    <a href="https://developers.facebook.com/tools/explorer" target="_blank" rel="noopener"
                       style="display:inline-flex;align-items:center;gap:5px;margin-top:8px;font-size:12px;padding:5px 12px;background:rgba(24,119,242,.12);border:1px solid rgba(24,119,242,.3);border-radius:8px;color:#1877f2;text-decoration:none;font-weight:600">
                      &#8599; Abrir Graph API Explorer
                    </a>
                  </div>
                </div>

                <div style="{_fb_step_s}">
                  <span style="{_fb_num_s}">2</span>
                  <div>
                    <div style="font-weight:600;font-size:13px;margin-bottom:4px">Selecione seu App Meta e gere um User Token</div>
                    <div style="font-size:12px;color:var(--muted);line-height:1.65">
                      No Explorer, clique em <b>Meta App</b> e selecione seu app (ou crie um em developers.facebook.com).<br>
                      Clique em <b>Gerar Token de Acesso (Generate Access Token)</b>.<br>
                      Na janela de permiss&#245;es, marque obrigatoriamente:<br>
                      <span style="font-family:monospace;background:var(--surface2);padding:2px 6px;border-radius:4px;font-size:11px;margin:4px 4px 0 0;display:inline-block">pages_manage_posts</span>
                      <span style="font-family:monospace;background:var(--surface2);padding:2px 6px;border-radius:4px;font-size:11px;margin:4px 4px 0 0;display:inline-block">pages_read_engagement</span>
                      <span style="font-family:monospace;background:var(--surface2);padding:2px 6px;border-radius:4px;font-size:11px;margin:4px 4px 0 0;display:inline-block">pages_show_list</span>
                    </div>
                  </div>
                </div>

                <div style="{_fb_step_s}">
                  <span style="{_fb_num_s}">3</span>
                  <div>
                    <div style="font-weight:600;font-size:13px;margin-bottom:4px">Obtenha o Page Access Token da sua p&#225;gina</div>
                    <div style="font-size:12px;color:var(--muted);line-height:1.65">
                      Com o User Token gerado no Passo 2, fa&#231;a a seguinte chamada no Explorer:<br>
                      <code style="display:block;background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:8px 12px;margin:8px 0;font-size:12px;word-break:break-all">GET /me/accounts?fields=id,name,access_token</code>
                      A resposta lista todas as suas p&#225;ginas. Copie o <b>access_token</b> e o <b>id</b> da p&#225;gina desejada.
                    </div>
                  </div>
                </div>

                <div style="{_fb_step_s};border-bottom:none">
                  <span style="{_fb_num_s}">4</span>
                  <div>
                    <div style="font-weight:600;font-size:13px;margin-bottom:4px">Cole as informa&#231;&#245;es no formul&#225;rio abaixo</div>
                    <div style="font-size:12px;color:var(--muted);line-height:1.65">
                      Preencha o <b>Page ID</b> (o campo <code style="font-size:11px;background:var(--surface2);padding:1px 5px;border-radius:3px">id</code>) e o <b>Page Access Token</b> (o campo <code style="font-size:11px;background:var(--surface2);padding:1px 5px;border-radius:3px">access_token</code>).<br>
                      Voc&#234; pode adicionar quantas p&#225;ginas quiser ” cada uma &#233; configurada de forma independente para este bot.
                    </div>
                  </div>
                </div>
              </div>
            </details>

            <!-- ══ OAuth automático ══ -->
            <div style="background:linear-gradient(135deg,rgba(24,119,242,.08),rgba(24,119,242,.03));border:1.5px solid rgba(24,119,242,.25);border-radius:14px;padding:22px;margin-bottom:20px">
              <div style="font-size:14px;font-weight:700;color:var(--text);margin-bottom:6px;display:flex;align-items:center;gap:8px">
                <svg width='18' height='18' viewBox='0 0 24 24' fill='#1877f2'><path d='M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z'/></svg>
                Conectar automaticamente via OAuth
              </div>
              <div style="font-size:12px;color:var(--muted);margin-bottom:16px">Configure o App ID e Secret do seu app Meta uma vez ” depois basta clicar em Conectar para importar todas as suas páginas automaticamente.</div>
              <form method="post" action="/app/profiles/{p.id}/integrations/facebook/oauth-config" style="margin-bottom:14px">
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px">
                  <div>
                    <label style="font-size:11px;font-weight:600;color:var(--muted);margin-bottom:5px;display:block">App ID</label>
                    <input name="oauth_app_id" value="{html.escape(fb_oauth_app_id)}" placeholder="Ex: 1429193945189041" style="margin:0" />
                  </div>
                  <div>
                    <label style="font-size:11px;font-weight:600;color:var(--muted);margin-bottom:5px;display:block">App Secret {"&#10003; salvo" if fb_oauth_app_secret_saved else ""}</label>
                    <input name="oauth_app_secret" type="password" placeholder="{"Deixe vazio para manter" if fb_oauth_app_secret_saved else "Cole a chave secreta do app"}" style="margin:0" />
                  </div>
                </div>
                <div style="font-size:11px;color:var(--muted);margin-bottom:12px;padding:8px 12px;background:var(--surface2);border-radius:8px;border:1px solid var(--border)">
                  &#128279; Adicione esta URL como <b>URI de redirecionamento OAuth</b> no seu app Meta:<br>
                  <code style="font-size:11px;word-break:break-all;color:var(--primary)">/app/oauth/facebook/callback</code>
                  (ex: <code style="font-size:11px">http://localhost:8000/app/oauth/facebook/callback</code>)
                </div>
                <div style="display:flex;gap:10px;align-items:center">
                  <button class="btn flat" type="submit" style="font-size:13px;padding:9px 18px">Salvar credenciais</button>
                  {"<a href='/app/oauth/facebook/start?profile_id=" + p.id + "' class='btn' style='background:#1877f2;border-color:#1877f2;font-size:13px;padding:9px 20px;text-decoration:none;display:inline-flex;align-items:center;gap:6px'><svg width=14 height=14 viewBox=0 0 24 24 fill=#fff><path d=M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z /></svg> Conectar com Facebook</a>" if fb_oauth_app_id and fb_oauth_app_secret_saved else "<span style='font-size:12px;color:var(--muted)'>Salve as credenciais para habilitar o botão Conectar</span>"}
                </div>
              </form>
            </div>

            <!-- ══ Formulário de adição ══ -->
            <div style="background:var(--surface2);border:1px solid var(--border);border-radius:14px;padding:22px;margin-bottom:24px">
              <div style="font-size:14px;font-weight:700;color:var(--text);margin-bottom:16px;display:flex;align-items:center;gap:8px">
                <span style="font-size:18px">&#10133;</span> Adicionar p&#225;gina manualmente
              </div>
              <form method="post" action="/app/profiles/{p.id}/integrations/facebook/pages/add">
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
                  <div>
                    <label>Nome da p&#225;gina <span style="color:var(--muted);font-weight:400">(opcional)</span></label>
                    <input name="name" placeholder="Ex: Receitas da Mari" />
                  </div>
                  <div>
                    <label>Page ID <span style="color:#ef4444">*</span></label>
                    <input name="page_id" placeholder="Ex: 123456789012345" required />
                  </div>
                  <div style="grid-column:1/-1">
                    <label>Page Access Token <span style="color:#ef4444">*</span></label>
                    <div style="position:relative">
                      <input name="access_token" id="fb-token-input-{p.id}" type="password" placeholder="Cole o token obtido em /me/accounts" required style="padding-right:80px" />
                      <button type="button" onclick="var i=document.getElementById('fb-token-input-{p.id}');i.type=i.type==='password'?'text':'password';this.textContent=i.type==='password'?'Mostrar':'Ocultar'"
                        style="position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;cursor:pointer;font-size:12px;color:var(--muted);padding:4px 6px">Mostrar</button>
                    </div>
                  </div>
                </div>
                <div style="margin-top:16px;display:flex;gap:10px">
                  <button class="btn" type="submit">&#10133; Adicionar p&#225;gina</button>
                </div>
              </form>
            </div>

            <!-- ══ Páginas cadastradas ══ -->
            <div style="font-size:12px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;color:var(--muted);margin-bottom:10px">
              P&#225;ginas cadastradas neste bot
            </div>
            <div style="border:1px solid var(--border);border-radius:14px;overflow:hidden">
              <table style="width:100%;border-collapse:collapse">
                <thead>
                  <tr style="background:var(--surface2)">
                    <th style="padding:10px 16px;text-align:left;font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">P&#225;gina</th>
                    <th style="padding:10px 16px;text-align:left;font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">Page ID</th>
                    <th style="padding:10px 16px;text-align:left;font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">Token</th>
                    <th style="padding:10px 16px;text-align:left;font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">Status</th>
                    <th style="padding:10px 16px;text-align:right;font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">A&#231;&#245;es</th>
                  </tr>
                </thead>
                <tbody>{fb_rows}</tbody>
              </table>
            </div>"""
        else:  # conexoes ” per-bot view across all profiles
            _all_conn_profiles = list(db.scalars(select(AutomationProfile).where(AutomationProfile.user_id == user.id).order_by(AutomationProfile.active.desc(), AutomationProfile.created_at.asc())))
            _conn_sections = ""
            for _cp in _all_conn_profiles:
                _cp_integ = list(db.scalars(select(Integration).where(Integration.profile_id == _cp.id).order_by(Integration.type, Integration.created_at.desc())))
                _cp_emoji = (_cp.publish_config_json or {}).get("emoji") or "&#129302;"
                _cp_name  = html.escape(_cp.name)
                _cp_active_badge = ("<span class='badge-active' style='font-size:10px;padding:2px 8px'><span class='dot-pulse'></span>Ativo</span>"
                                    if _cp.active else
                                    "<span class='badge-inactive' style='font-size:10px;padding:2px 8px'><span class='dot-off'></span>Inativo</span>")
                _cp_rows = ""
                for _ci in _cp_integ:
                    try:
                        _ci_creds = decrypt_json(_ci.credentials_encrypted)
                    except Exception:
                        _ci_creds = {}
                    if _ci.type == IntegrationType.WORDPRESS:
                        _ci_url = str(_ci_creds.get("base_url") or "”")
                    elif _ci.type == IntegrationType.FACEBOOK:
                        _ci_pages = _ci_creds.get("pages") or []
                        _ci_url = f"{len(_ci_pages)} p&#225;gina(s)" if _ci_pages else "”"
                    elif _ci.type == IntegrationType.GEMINI:
                        _ci_url = str(_ci_creds.get("model") or "”")
                    else:
                        _ci_url = html.escape(_ci.name)
                    _ci_connected = _ci.status.value == "CONNECTED"
                    if _ci_connected:
                        _ci_status_html = ("<span style='display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;color:#10b981'>"
                                           "<span class='dot-pulse' style='width:8px;height:8px;border-radius:50%;background:#10b981;flex-shrink:0'></span>"
                                           "Conectado</span>")
                    else:
                        _ci_status_html = ("<span style='display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;color:#ef4444'>"
                                           "<span style='width:8px;height:8px;border-radius:50%;background:#ef4444;flex-shrink:0;opacity:.7'></span>"
                                           "Desconectado</span>")
                    _cp_rows += (
                        f"<tr style='border-top:1px solid var(--border)'>"
                        f"<td style='padding:11px 16px'><span class='pill'>{html.escape(_ci.type.value)}</span></td>"
                        f"<td style='padding:11px 16px;font-size:13px;word-break:break-all;max-width:240px'>{html.escape(_ci_url)}</td>"
                        f"<td style='padding:11px 16px'>{_ci_status_html}</td>"
                        f"<td style='padding:11px 16px;text-align:right'>"
                        f"<form method='post' action='/app/profiles/{_cp.id}/integrations/{_ci.id}/delete' style='margin:0'>"
                        f"<button type='submit' style='display:inline-flex;align-items:center;gap:5px;background:none;border:none;cursor:pointer;"
                        f"font-size:12px;color:#ef4444;padding:4px 8px;border-radius:6px;font-family:inherit;transition:background .15s' "
                        f"onmouseover=\"this.style.background='rgba(239,68,68,.1)'\" onmouseout=\"this.style.background='none'\" "
                        f"onclick=\"return confirm('Remover esta integra&#231;&#227;o?')\">"
                        f"<svg width='13' height='13' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
                        f"<polyline points='3 6 5 6 21 6'/><path d='M19 6l-1 14H6L5 6'/><path d='M10 11v6'/><path d='M14 11v6'/><path d='M9 6V4h6v2'/>"
                        f"</svg>Remover</button></form></td></tr>"
                    )
                if not _cp_rows:
                    _cp_rows = f"<tr><td colspan='4' style='padding:18px;text-align:center;color:var(--muted);font-size:13px'>Nenhuma integra&#231;&#227;o cadastrada.</td></tr>"
                _conn_sections += f"""
                <div style="margin-bottom:14px">
                  <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
                    <span style="font-size:17px">{_cp_emoji}</span>
                    <span style="font-weight:700;font-size:14px">{_cp_name}</span>
                    {_cp_active_badge}
                    <a href="/app/profiles/{_cp.id}?tab=integracoes" style="margin-left:auto;font-size:12px;color:var(--primary);text-decoration:none;font-weight:600">Gerenciar &#8594;</a>
                  </div>
                  <div style="border:1px solid var(--border);border-radius:12px;overflow:hidden">
                    <table style="width:100%;border-collapse:collapse">
                      <thead><tr style="background:var(--surface2)">
                        <th style="padding:9px 16px;text-align:left;font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">Tipo</th>
                        <th style="padding:9px 16px;text-align:left;font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">Detalhe</th>
                        <th style="padding:9px 16px;text-align:left;font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">Status</th>
                        <th style="padding:9px 16px;text-align:right;font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)">A&#231;&#245;es</th>
                      </tr></thead>
                      <tbody>{_cp_rows}</tbody>
                    </table>
                  </div>
                </div>"""
            if not _conn_sections:
                _conn_sections = "<div style='padding:20px;text-align:center;color:var(--muted);font-size:13px'>Nenhum projeto criado ainda.</div>"
            itab_content = _conn_sections

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
        respect = int(cfg.get("respect_schedule") or 0) == 1
        start_at_utc = str(cfg.get("start_at_utc") or "").strip()
        start_local_value = ""
        _sched_base_local = None
        _user_tz = _user_zoneinfo(user)
        if start_at_utc:
            try:
                dt = datetime.fromisoformat(start_at_utc.replace("Z", "+00:00"))
                _sched_base_local = dt.astimezone(_user_tz).replace(tzinfo=None)
                start_local_value = _sched_base_local.strftime("%Y-%m-%dT%H:%M")
            except Exception:
                start_local_value = ""

        # ── queued/processing posts ────────────────────────────────
        # Busca posts pending ou processing com qualquer job na fila/rodando
        _sched_posts_raw = db.execute(
            select(Post, Job, CollectedContent)
            .join(
                Job,
                (Job.post_id == Post.id)
                & (Job.status.in_([JobStatus.queued, JobStatus.running]))
            )
            .outerjoin(CollectedContent, CollectedContent.id == Post.collected_content_id)
            .where(
                Post.profile_id == p.id,
                Post.status.in_([PostStatus.pending, PostStatus.processing]),
            )
            .order_by(Job.run_at.asc())
        ).all()
        # deduplica: um post pode ter múltiplos jobs, manter só o primeiro (run_at mais cedo)
        _seen_ids: set = set()
        _sched_posts = []
        for _row in _sched_posts_raw:
            if _row[0].id not in _seen_ids:
                _seen_ids.add(_row[0].id)
                _sched_posts.append((_row[0], _row[1], _row[2]))

        # Posts recentemente concluídos (últimas 48h) com URL do WP
        _completed_posts_raw = db.execute(
            select(Post, CollectedContent)
            .outerjoin(CollectedContent, CollectedContent.id == Post.collected_content_id)
            .where(
                Post.profile_id == p.id,
                Post.status == PostStatus.completed,
                Post.wp_url.isnot(None),
                Post.published_at >= datetime.utcnow() - timedelta(hours=48),
            )
            .order_by(Post.published_at.desc())
            .limit(5)
        ).all()
        _completed_posts = list(_completed_posts_raw)

        # active queue counts (all queued/running jobs for this profile)
        _sq_jobs = int(db.scalar(
            select(func.count()).select_from(Job).where(
                Job.profile_id == p.id,
                Job.status.in_([JobStatus.queued, JobStatus.running])
            )
        ) or 0)
        _sq_posts_proc = int(db.scalar(
            select(func.count()).select_from(Post).where(
                Post.profile_id == p.id,
                Post.status == PostStatus.processing
            )
        ) or 0)
        _has_active = (_sq_jobs + len(_sched_posts) + _sq_posts_proc) > 0

        # ── helpers ───────────────────────────────────────────────
        _now_local = _to_user_local(datetime.utcnow(), user=user) or datetime.now()
        _interval_txt = f"{interval_minutes} min entre posts" if interval_minutes > 0 else "sem intervalo (tudo seguido)"
        _start_txt = (_sched_base_local.strftime("%d/%m/%Y às %H:%M") if (respect and _sched_base_local) else "agora")

        def _fmt_when(dt_obj):
            diff = int((dt_obj - _now_local).total_seconds() / 60)
            if dt_obj < _now_local:
                return "<span style='color:var(--muted);font-size:11px'>em processamento</span>"
            elif diff < 60:
                return f"<span style='color:#10b981;font-size:11px'>em {diff} min</span>"
            elif diff < 1440:
                return f"<span style='font-size:11px;color:var(--muted)'>em {diff//60}h{diff%60:02d}</span>"
            else:
                return f"<span style='font-size:11px;color:var(--muted)'>em {diff//1440}d</span>"

        # ── helpers de SVG ───────────────────────────────────────
        _ico_edit_sm  = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>'
        _ico_del_sm   = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4h6v2"/></svg>'

        # ── CSS de tabela ─────────────────────────────────────────
        _tbl_th = ("font-size:10px;font-weight:600;color:var(--muted);text-transform:uppercase;"
                   "letter-spacing:.06em;padding:5px 6px 5px 0;border-bottom:2px solid var(--border);"
                   "white-space:nowrap;user-select:none")
        _tbl_td = "padding:6px 6px 6px 0;vertical-align:middle;font-size:12px"

        # Botão ação em lote (aparece quando há selecionados)
        _bulk_bar = f"""
        <div id="schedBulkBar-{p.id}" style="display:none;align-items:center;gap:10px;
          padding:8px 12px;border-radius:10px;background:rgba(139,92,246,.1);
          border:1px solid rgba(139,92,246,.25);margin-bottom:10px;flex-wrap:wrap">
          <span id="schedBulkCount-{p.id}" style="font-size:13px;font-weight:600;color:var(--text)">
            0 selecionados
          </span>
          <button type="button" onclick="schedBulkEdit('{p.id}')"
            class="btn secondary" style="font-size:12px;padding:5px 12px;gap:5px">
            {_ico_edit_sm} Editar selecionados
          </button>
          <button type="button" onclick="schedBulkDelete('{p.id}')"
            style="background:none;border:1px solid rgba(239,68,68,.4);color:#ef4444;
              border-radius:8px;padding:5px 12px;font-size:12px;cursor:pointer;
              display:inline-flex;align-items:center;gap:5px">
            {_ico_del_sm} Excluir selecionados
          </button>
          <button type="button" onclick="schedClearSel('{p.id}')"
            style="background:none;border:none;color:var(--muted);font-size:12px;
              cursor:pointer;padding:4px 8px;border-radius:6px;margin-left:auto">
            Limpar seleção ✕
          </button>
        </div>"""

        # Modal de edição em lote
        _bulk_edit_modal = f"""
        <div id="schedBulkEditModal-{p.id}" style="display:none;margin-bottom:10px;
          padding:14px 16px;border-radius:12px;background:rgba(139,92,246,.07);
          border:1px solid rgba(139,92,246,.2)">
          <div style="font-size:13px;font-weight:600;margin-bottom:10px">
            Editar hor&#225;rio dos selecionados
          </div>
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <label style="font-size:12px;color:var(--muted)">Nova data/hora:</label>
            <input type="datetime-local" id="schedBulkDateTime-{p.id}"
              style="font-size:13px;padding:6px 10px;border-radius:8px;
                border:1px solid var(--border);background:var(--surface);color:var(--text)" />
            <button type="button" onclick="schedBulkEditConfirm('{p.id}')"
              class="btn flat" style="font-size:12px;padding:6px 14px">Aplicar</button>
            <button type="button"
              onclick="document.getElementById('schedBulkEditModal-{p.id}').style.display='none'"
              class="btn secondary" style="font-size:12px;padding:6px 14px">Cancelar</button>
          </div>
          <div style="font-size:11px;color:var(--muted);margin-top:7px">
            O intervalo entre posts ser&#225; recalculado a partir deste hor&#225;rio.
          </div>
        </div>"""

        # ── cabeçalho da tabela ───────────────────────────────────
        _tbl_head = f"""
        <table id="schedTable-{p.id}" style="width:100%;border-collapse:collapse;table-layout:fixed">
          <colgroup>
            <col style="width:26px">
            <col style="width:24px">
            <col>
            <col style="width:92px">
            <col style="width:62px">
            <col style="width:96px">
            <col style="width:36px">
            <col style="width:36px">
          </colgroup>
          <thead>
            <tr>
              <th style="{_tbl_th};padding-left:0">
                <input type="checkbox" id="schedSelAll-{p.id}" title="Selecionar todos"
                  onchange="schedToggleAll('{p.id}',this.checked)"
                  style="width:14px;height:14px;cursor:pointer;accent-color:rgba(139,92,246,.9)">
              </th>
              <th style="{_tbl_th}">#</th>
              <th style="{_tbl_th}">T&#237;tulo</th>
              <th style="{_tbl_th}">Data</th>
              <th style="{_tbl_th}">Hora</th>
              <th style="{_tbl_th}">Status</th>
              <th style="{_tbl_th};text-align:center">Editar</th>
              <th style="{_tbl_th};text-align:center">Excluir</th>
            </tr>
          </thead>
          <tbody id="schedTbody-{p.id}">"""

        # ── "O que será feito" rows ───────────────────────────────
        _preview_rows = _bulk_bar + _bulk_edit_modal + _tbl_head
        _tbody_rows = ""

        if _sched_posts:
            # Real scheduled posts
            for _idx, (_sp, _sj, _sc) in enumerate(_sched_posts):
                _outs = _sp.outputs_json if isinstance(_sp.outputs_json, dict) else {}
                _title_raw = str((_outs.get("recipe") or {}).get("title") or "").strip()
                if not _title_raw and _sc:
                    _title_raw = str(_sc.title or "").strip()
                _title_disp = html.escape(_title_raw[:60]) if _title_raw else f"Post {_idx+1} ” t&#237;tulo pendente"
                _run_at = _sj.run_at or _now_local
                _run_local = _to_user_local(_run_at, user=user) or _now_local
                _date_str = _run_local.strftime("%d/%m/%Y")
                _time_str = _run_local.strftime("%H:%M")
                _when_str = _fmt_when(_run_local)
                _run_at_input = _run_local.strftime("%Y-%m-%dT%H:%M")
                _sid = html.escape(_sp.id)
                _tbody_rows += f"""
            <tr id="srow-{_sid}" class="sched-row" data-id="{_sid}" data-type="real"
              style="border-bottom:1px solid var(--border)">
              <td style="{_tbl_td};padding-left:0">
                <input type="checkbox" class="sched-cb" data-profile="{p.id}"
                  value="{_sid}" onchange="schedOnCheck('{p.id}')"
                  style="width:14px;height:14px;cursor:pointer;accent-color:rgba(139,92,246,.9)">
              </td>
              <td style="{_tbl_td}">
                <span style="width:22px;height:22px;border-radius:50%;background:rgba(139,92,246,.15);
                  display:inline-flex;align-items:center;justify-content:center;
                  font-size:10px;font-weight:700;color:rgba(139,92,246,.9)">{_idx+1}</span>
              </td>
              <td style="{_tbl_td};font-weight:600;line-height:1.35">{_title_disp}</td>
              <td style="{_tbl_td};color:var(--muted);white-space:nowrap">{_date_str}</td>
              <td style="{_tbl_td};font-weight:700;white-space:nowrap">{_time_str}</td>
              <td style="{_tbl_td}">{_when_str}</td>
              <td style="{_tbl_td};text-align:center">
                <button type="button" onclick="toggleSchedEdit('{_sid}')"
                  title="Editar hor&#225;rio"
                  style="background:none;border:none;cursor:pointer;color:var(--muted);
                    padding:5px;border-radius:6px;display:inline-flex;align-items:center"
                  onmouseover="this.style.color='var(--text)'" onmouseout="this.style.color='var(--muted)'">
                  {_ico_edit_sm}
                </button>
              </td>
              <td style="{_tbl_td};text-align:center">
                <button type="button" onclick="schedDeletePost('{p.id}','{_sid}')" title="Excluir"
                  style="background:none;border:none;cursor:pointer;color:var(--muted);
                    padding:5px;border-radius:6px;display:inline-flex;align-items:center"
                  onmouseover="this.style.color='#ef4444'" onmouseout="this.style.color='var(--muted)'">
                  {_ico_del_sm}
                </button>
              </td>
            </tr>
            <tr id="sedit-row-{_sid}" style="display:none">
              <td colspan="8" style="padding:0 0 12px 32px">
                <form method="post" action="/app/profiles/{p.id}/schedule/posts/{_sid}/reschedule"
                  style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 0">
                  <label style="font-size:12px;color:var(--muted)">Novo hor&#225;rio:</label>
                  <input type="datetime-local" name="run_at" value="{_run_at_input}"
                    style="font-size:13px;padding:6px 10px;border-radius:8px;
                      border:1px solid var(--border);background:var(--surface);color:var(--text)" />
                  <button class="btn flat" type="submit" style="font-size:12px;padding:6px 14px">Salvar</button>
                  <button type="button" onclick="toggleSchedEdit('{_sid}')"
                    class="btn secondary" style="font-size:12px;padding:6px 14px">Cancelar</button>
                </form>
              </td>
            </tr>"""

        # ── Posts recentemente concluídos (últimas 48h) ──────────
        if _completed_posts:
            if _sched_posts:
                _tbody_rows += f"""
            <tr><td colspan="8" style="padding:2px 0;border-bottom:1px dashed rgba(16,185,129,.3)"></td></tr>"""
            for _cp, _cc in _completed_posts:
                _cp_outs = _cp.outputs_json if isinstance(_cp.outputs_json, dict) else {}
                _cp_title = str((_cp_outs.get("recipe") or {}).get("title") or "").strip()
                if not _cp_title and _cc:
                    _cp_title = str(_cc.title or "").strip()
                _cp_title_disp = html.escape(_cp_title[:60]) if _cp_title else "Post publicado"
                _cp_pub = _cp.published_at
                try:
                    _cp_local = _to_user_local(_cp_pub, user=user) or _now_local
                except Exception:
                    _cp_local = _now_local
                _cp_date = _cp_local.strftime("%d/%m/%Y")
                _cp_time = _cp_local.strftime("%H:%M")
                _cp_url = html.escape(str(_cp.wp_url or ""))
                _cp_link = f'<a href="{_cp_url}" target="_blank" rel="noopener" style="color:#10b981;font-size:11px;display:inline-flex;align-items:center;gap:3px;text-decoration:none;white-space:nowrap" title="Ver post no WordPress">&#128279; ver post</a>' if _cp_url else ""
                _tbody_rows += f"""
            <tr style="border-bottom:1px solid var(--border);background:rgba(16,185,129,.03)">
              <td style="{_tbl_td};padding-left:0"></td>
              <td style="{_tbl_td}">
                <span style="font-size:14px" title="Publicado">&#9989;</span>
              </td>
              <td style="{_tbl_td};font-weight:600;line-height:1.3">{_cp_title_disp}</td>
              <td style="{_tbl_td};color:var(--muted);white-space:nowrap">{_cp_date}</td>
              <td style="{_tbl_td};font-weight:700;white-space:nowrap">{_cp_time}</td>
              <td style="{_tbl_td}">
                <span style="font-size:11px;font-weight:600;color:#10b981;background:rgba(16,185,129,.1);
                  border-radius:5px;padding:2px 6px;white-space:nowrap">&#10003; Conclu&#237;do</span>
              </td>
              <td style="{_tbl_td};text-align:center" colspan="2">{_cp_link}</td>
            </tr>"""

        if not _sched_posts:
            # Projected timeline
            _preview_base = _sched_base_local if (respect and _sched_base_local) else _now_local
            _show_n = min(posts_per_day, 8)
            for _i in range(_show_n):
                _pt = _preview_base + timedelta(minutes=interval_minutes * _i)
                _is_past = _pt < _now_local
                _date_str = _pt.strftime("%d/%m/%Y")
                _time_str = _pt.strftime("%H:%M")
                _run_at_input = _pt.strftime("%Y-%m-%dT%H:%M")
                _slot_id = f"proj-{_i}"
                _status_label = "<span style='font-size:11px;color:#ef4444'>atrasado</span>" if _is_past else "<span style='font-size:11px;color:var(--muted)'>projeção</span>"
                _tbody_rows += f"""
            <tr id="srow-{_slot_id}" class="sched-row" data-id="{_slot_id}" data-type="proj"
              style="border-bottom:1px solid var(--border)">
              <td style="{_tbl_td};padding-left:0">
                <input type="checkbox" class="sched-cb" data-profile="{p.id}"
                  value="{_slot_id}" onchange="schedOnCheck('{p.id}')"
                  style="width:14px;height:14px;cursor:pointer;accent-color:rgba(139,92,246,.9)">
              </td>
              <td style="{_tbl_td}">
                <span style="width:22px;height:22px;border-radius:50%;background:rgba(139,92,246,.1);
                  display:inline-flex;align-items:center;justify-content:center;
                  font-size:10px;font-weight:700;color:rgba(139,92,246,.7)">{_i+1}</span>
              </td>
              <td style="{_tbl_td};color:var(--muted);font-style:italic">
                Post {_i+1} ” t&#237;tulo gerado ao ativar
              </td>
              <td style="{_tbl_td};color:var(--muted);white-space:nowrap">{_date_str}</td>
              <td style="{_tbl_td};font-weight:700;white-space:nowrap">{_time_str}</td>
              <td style="{_tbl_td}">{_status_label}</td>
              <td style="{_tbl_td};text-align:center">
                <button type="button" onclick="toggleSchedEdit('{_slot_id}')"
                  title="Editar hor&#225;rio previsto"
                  style="background:none;border:none;cursor:pointer;color:var(--muted);
                    padding:5px;border-radius:6px;display:inline-flex;align-items:center"
                  onmouseover="this.style.color='var(--text)'" onmouseout="this.style.color='var(--muted)'">
                  {_ico_edit_sm}
                </button>
              </td>
              <td style="{_tbl_td};text-align:center">
                <button type="button" onclick="removeSchedSlot('{_slot_id}')"
                  title="Remover slot"
                  style="background:none;border:none;cursor:pointer;color:var(--muted);
                    padding:5px;border-radius:6px;display:inline-flex;align-items:center"
                  onmouseover="this.style.color='#ef4444'" onmouseout="this.style.color='var(--muted)'">
                  {_ico_del_sm}
                </button>
              </td>
            </tr>
            <tr id="sedit-row-{_slot_id}" style="display:none">
              <td colspan="8" style="padding:0 0 12px 32px">
                <form method="post" action="/app/profiles/{p.id}/schedule"
                  style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 0">
                  <input type="hidden" name="posts_per_day" value="{posts_per_day}" />
                  <input type="hidden" name="interval_minutes" value="{interval_minutes}" />
                  <input type="hidden" name="respect_schedule" value="{'1' if respect else '0'}" />
                  <label style="font-size:12px;color:var(--muted)">Novo in&#237;cio (slot {_i+1}):</label>
                  <input type="datetime-local" name="start_at" value="{_run_at_input}"
                    style="font-size:13px;padding:6px 10px;border-radius:8px;
                      border:1px solid var(--border);background:var(--surface);color:var(--text)" />
                  <button class="btn flat" type="submit" style="font-size:12px;padding:6px 14px">Salvar</button>
                  <button type="button" onclick="toggleSchedEdit('{_slot_id}')"
                    class="btn secondary" style="font-size:12px;padding:6px 14px">Cancelar</button>
                </form>
              </td>
            </tr>"""
            _more = posts_per_day - _show_n
            if _more > 0:
                _tbody_rows += f"""
            <tr><td colspan="8" style="padding:8px 0;color:var(--muted);font-size:12px;text-align:center">
              + {_more} post{'s' if _more!=1 else ''} a mais...
            </td></tr>"""

        # fecha tabela + rodapé com "Excluir todos"
        # Se há posts reais: POST para cancelar no banco.
        # Se é projeção: botão JS que remove todas as linhas da tabela.
        if _sched_posts:
            _del_all_btn = f"""
          <button type="button" onclick="schedDeleteAll('{p.id}', {len(_sched_posts)})"
            style="background:none;border:none;cursor:pointer;color:#ef4444;
              font-size:12px;display:inline-flex;align-items:center;gap:5px;
              padding:5px 2px;text-decoration:underline;text-underline-offset:2px">
            {_ico_del_sm} Excluir todos os agendados ({len(_sched_posts)})
          </button>"""
        else:
            _del_all_btn = f"""
          <button type="button"
            onclick="schedRemoveAllSlots('{p.id}')"
            style="background:none;border:none;cursor:pointer;color:#ef4444;
              font-size:12px;display:inline-flex;align-items:center;gap:5px;
              padding:5px 2px;text-decoration:underline;text-underline-offset:2px">
            {_ico_del_sm} Excluir todos os agendados
          </button>"""

        _preview_rows += _tbody_rows + f"""
          </tbody>
        </table>
        <div style="padding:14px 0 2px;display:flex;align-items:center;justify-content:flex-end">
          {_del_all_btn}
        </div>"""

        _posts_label = f"{len(_sched_posts)} agendado{'s' if len(_sched_posts)!=1 else ''}" if _sched_posts else "Nenhum agendado"
        _done_label = f" &bull; {len(_completed_posts)} conclu&#237;do{'s' if len(_completed_posts)!=1 else ''}" if _completed_posts else ""
        _preview_title = f"Agendamento &mdash; {_posts_label}{_done_label}" if (_sched_posts or _completed_posts) else "Agendamento (projeção)"

        body += f"""
        {_ph("tab-agendamento")}

        <!-- ── Fila ativa ─────────────────────────────────────── -->
        {_ph("agendamento-fila-ativa")}
        {"" if not _has_active else f'''
        <div class="card" style="margin-bottom:14px;border:1px solid rgba(245,158,11,.35);background:rgba(245,158,11,.05)">
          <details class="toggle-section" open>
            <summary>
              <span class="ts-title" style="display:flex;align-items:center;gap:8px">
                <span style="width:8px;height:8px;border-radius:50%;background:#f59e0b;display:inline-block;box-shadow:0 0 0 3px rgba(245,158,11,.22)"></span>
                Fila ativa &mdash; {_sq_jobs} job{"s" if _sq_jobs!=1 else ""} na fila, {len(_sched_posts)} pendente{"s" if len(_sched_posts)!=1 else ""}{"" if _sq_posts_proc==0 else f", {_sq_posts_proc} processando"}
              </span>
              <span class="ts-arrow">&#9655;</span>
            </summary>
            <div class="ts-body">
              <p class="muted" style="margin-bottom:12px">
                H&#225; postagens em andamento. Cancele tudo para reconfigurar.
              </p>
              <button class="btn" type="button"
                onclick="schedDeleteAll('{p.id}', {_sq_jobs + len(_sched_posts)})"
                style="background:#ef4444;border-color:#ef4444;color:#fff;gap:7px">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                Cancelar tudo
              </button>
            </div>
          </details>
        </div>'''}

        <!-- ── Configurar ─────────────────────────────────────── -->
        {_ph("agendamento-config")}
        <div class="card" style="margin-bottom:14px">
          <details class="toggle-section" open>
            <summary>
              <span class="ts-title" style="display:flex;align-items:center;gap:8px">
                <span style="width:26px;height:26px;border-radius:7px;background:rgba(139,92,246,.15);
                  display:inline-flex;align-items:center;justify-content:center;font-size:13px">&#9881;</span>
                Configurar agendamento
              </span>
              <span class="ts-arrow">&#9655;</span>
            </summary>
            <div class="ts-body">
              {_ph("agendamento-form")}
              <p class="muted" style="margin-bottom:14px">
                Define quantidade, intervalo e data/hora de in&#237;cio.
                <b>Salvar</b> guarda as configura&#231;&#245;es. <b>Ativar</b> roda o diagn&#243;stico e inicia.
              </p>
              <form method="post" action="/app/profiles/{p.id}/schedule">
                <div class="row">
                  <div class="col">
                    <label>Quantidade de posts</label>
                    <input name="posts_per_day" type="number" min="1" step="1" value="{posts_per_day}" />
                  </div>
                  <div class="col">
                    <label>Intervalo entre posts (min)</label>
                    <input name="interval_minutes" type="number" min="0" value="{interval_minutes}" />
                    <div class="muted" style="margin-top:6px">0 = publica tudo seguido</div>
                  </div>
                  <div class="col">
                    <label>Data/hora de in&#237;cio</label>
                    <input name="start_at" type="datetime-local" value="{html.escape(start_local_value)}" />
                    <div class="muted" style="margin-top:6px">Vazio = come&#231;ar agora ao ativar</div>
                  </div>
                </div>
                <div class="row" style="margin-top:12px">
                  <div class="col">
                    <label>Respeitar data/hora de in&#237;cio</label>
                    <select name="respect_schedule">
                      <option value="0" {"selected" if not respect else ""}>N&#227;o (usar intervalo, ignorar data/hora)</option>
                      <option value="1" {"selected" if respect else ""}>Sim (aguardar data/hora antes de publicar)</option>
                    </select>
                  </div>
                </div>
                <div style="margin-top:16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
                  <button class="btn flat" type="submit" style="gap:6px">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
                    Salvar configura&#231;&#227;o
                  </button>
                  <button type="button" class="btn" onclick="openSchedDiag('{p.id}')"
                    style="gap:7px;background:#10b981;border-color:#10b981;color:#fff"
                    {"disabled title='Cancele a fila ativa antes de reativar'" if _has_active else ""}>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                    Ativar agendamento
                  </button>
                </div>
              </form>
            </div>
          </details>
        </div>

        <!-- ── O que será feito ───────────────────────────────── -->
        {_ph("agendamento-preview")}
        <div class="card" style="margin-bottom:14px">
          <details class="toggle-section" open>
            <summary>
              <span class="ts-title" style="display:flex;align-items:center;gap:8px">
                <span style="width:26px;height:26px;border-radius:7px;background:rgba(16,185,129,.15);
                  display:inline-flex;align-items:center;justify-content:center;font-size:14px">&#128640;</span>
                &#128197; {_preview_title}
              </span>
              <span class="ts-arrow">&#9655;</span>
            </summary>
            <div class="ts-body">
              {_ph("agendamento-preview-header")}
              <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:14px;font-size:13px;
                padding:10px 14px;background:rgba(139,92,246,.07);border-radius:10px;
                border:1px solid rgba(139,92,246,.12)">
                <div>&#128197; <span class="muted">In&#237;cio:</span> <b>{_start_txt}</b></div>
                <div>&#128221; <span class="muted">Posts:</span> <b>{posts_per_day}</b></div>
                <div>&#9200; <span class="muted">Intervalo:</span> <b>{_interval_txt}</b></div>
              </div>
              {_ph("agendamento-lista-posts")}
              <div style="overflow-x:auto">
                {_preview_rows}
              </div>
              <p class="muted" style="margin-top:10px;font-size:12px">
                &#9432; {"T&#237;tulos reais ap&#243;s coleta. Hor&#225;rios podem variar conforme tempo de processamento." if _sched_posts else "Projeção estimada. T&#237;tulos reais aparecem ap&#243;s ativar o agendamento."}
              </p>
            </div>
          </details>
        </div>

        <!-- ── Custom confirm modal (global) ────────────────────── -->
        <div class="diag-overlay" id="phConfirmOverlay" onclick="if(event.target===this)phConfirmReply(false)">
          <div class="diag-modal" style="padding:28px 28px 24px;max-width:380px;text-align:center">
            <div style="font-size:32px;margin-bottom:10px">&#9888;&#65039;</div>
            <div id="phConfirmTitle" style="font-weight:700;font-size:16px;margin-bottom:8px;color:var(--text)">Confirmar</div>
            <div id="phConfirmMsg" style="font-size:13px;color:var(--muted);margin-bottom:24px;line-height:1.55"></div>
            <div style="display:flex;gap:10px;justify-content:center">
              <button type="button" onclick="phConfirmReply(false)"
                class="btn secondary" style="min-width:90px;padding:8px 18px">Cancelar</button>
              <button type="button" id="phConfirmOkBtn" onclick="phConfirmReply(true)"
                style="min-width:90px;padding:8px 18px;background:#ef4444;border:none;
                  border-radius:10px;color:#fff;font-size:13px;font-weight:600;cursor:pointer;
                  transition:opacity .15s" onmouseover="this.style.opacity='.85'" onmouseout="this.style.opacity='1'">
                Confirmar
              </button>
            </div>
          </div>
        </div>

        <!-- ── Diagnostic modal ───────────────────────────────── -->
        <div class="diag-overlay" id="schedDiagOverlay" onclick="if(event.target===this)closeSchedDiag()">
          <div class="diag-modal">
            <div class="diag-header">
              <div>
                <div style="font-weight:700;font-size:17px">Diagn&#243;stico antes de ativar</div>
                <div id="schedDiagBotName" style="font-size:12px;color:var(--muted);margin-top:2px"></div>
              </div>
              <button class="diag-close" type="button" onclick="closeSchedDiag()">&times;</button>
            </div>
            <div class="diag-body">
            <div id="schedDiagItems" style="display:flex;flex-direction:column;gap:10px;min-height:80px">
              <div style="text-align:center;padding:32px;color:var(--muted)">
                <div style="font-size:28px;margin-bottom:8px">&#9203;</div>Verificando...
              </div>
            </div>
            </div>
            <div id="schedDiagFooter" class="diag-footer"></div>
          </div>
        </div>
        <form id="schedActivateForm" method="post" action="/app/profiles/{p.id}/schedule/activate" style="display:none">
          <input type="hidden" id="schedAutoReconnectInput" name="auto_reconnect" value="0">
        </form>
        <script>
        function toggleSchedEdit(id) {{
          var el = document.getElementById('sedit-row-' + id) || document.getElementById('sedit-' + id);
          if (!el) return;
          var visible = el.style.display !== 'none' && el.style.display !== '';
          el.style.display = visible ? 'none' : 'table-row';
        }}
        function removeSchedSlot(id) {{
          var row = document.getElementById('srow-' + id);
          var edit = document.getElementById('sedit-row-' + id);
          [row, edit].forEach(function(r) {{
            if (r) {{ r.style.transition='opacity .2s'; r.style.opacity='0'; setTimeout(function(){{r.style.display='none';}},200); }}
          }});
        }}
        /* ── Checkbox em lote ── */
        function schedOnCheck(pid) {{
          var cbs = document.querySelectorAll('.sched-cb[data-profile="'+pid+'"]:checked');
          var bar = document.getElementById('schedBulkBar-'+pid);
          var cnt = document.getElementById('schedBulkCount-'+pid);
          var selAll = document.getElementById('schedSelAll-'+pid);
          var total = document.querySelectorAll('.sched-cb[data-profile="'+pid+'"]').length;
          if (bar) bar.style.display = cbs.length > 0 ? 'flex' : 'none';
          if (cnt) cnt.textContent = cbs.length + (cbs.length===1?' selecionado':' selecionados');
          if (selAll) selAll.indeterminate = cbs.length > 0 && cbs.length < total;
          if (selAll) selAll.checked = cbs.length === total && total > 0;
        }}
        function schedToggleAll(pid, checked) {{
          document.querySelectorAll('.sched-cb[data-profile="'+pid+'"]').forEach(function(cb){{ cb.checked=checked; }});
          schedOnCheck(pid);
        }}
        function schedClearSel(pid) {{
          document.querySelectorAll('.sched-cb[data-profile="'+pid+'"]').forEach(function(cb){{ cb.checked=false; }});
          schedOnCheck(pid);
        }}
        function _schedSelected(pid) {{
          return Array.from(document.querySelectorAll('.sched-cb[data-profile="'+pid+'"]:checked')).map(function(cb){{return cb.value;}});
        }}
        /* ── Custom confirm modal ── */
        var _phConfirmResolve = null;
        function phConfirm(title, msg, okLabel) {{
          return new Promise(function(resolve) {{
            _phConfirmResolve = resolve;
            var el = document.getElementById('phConfirmOverlay');
            if (!el) {{ resolve(window.confirm(msg)); return; }}
            var titleEl = document.getElementById('phConfirmTitle');
            var msgEl   = document.getElementById('phConfirmMsg');
            var okEl    = document.getElementById('phConfirmOkBtn');
            if (titleEl) titleEl.textContent = title  || 'Confirmar';
            if (msgEl)   msgEl.textContent   = msg    || '';
            if (okEl)    okEl.textContent    = okLabel|| 'Confirmar';
            el.classList.add('open');
          }});
        }}
        function phConfirmReply(result) {{
          var el = document.getElementById('phConfirmOverlay');
          if (el) el.classList.remove('open');
          if (_phConfirmResolve) {{ _phConfirmResolve(result); _phConfirmResolve = null; }}
        }}
        document.addEventListener('keydown', function(e) {{
          if (e.key === 'Escape') {{
            var el = document.getElementById('phConfirmOverlay');
            if (el && el.classList.contains('open')) phConfirmReply(false);
          }}
        }});
        /* ── Excluir post individual ── */
        function schedDeletePost(pid, postId) {{
          phConfirm('Excluir post agendado', 'Este post ser\u00e1 cancelado e removido da fila.', 'Excluir').then(function(ok) {{
            if (!ok) return;
            var f = document.createElement('form');
            f.method='post'; f.action='/app/profiles/'+pid+'/schedule/posts/'+postId+'/cancel';
            document.body.appendChild(f); f.submit();
          }});
        }}
        /* ── Excluir todos agendados ── */
        function schedDeleteAll(pid, count) {{
          phConfirm('Excluir todos os agendados', 'Cancelar todos os ' + count + ' post(s) agendado(s)?', 'Excluir todos').then(function(ok) {{
            if (!ok) return;
            var f = document.createElement('form');
            f.method='post'; f.action='/app/profiles/'+pid+'/schedule/cancel';
            document.body.appendChild(f); f.submit();
          }});
        }}
        function schedBulkDelete(pid) {{
          var ids = _schedSelected(pid);
          if (!ids.length) return;
          var realIds = ids.filter(function(id) {{
            var row = document.querySelector('.sched-row[data-id="'+id+'"]');
            return row && row.dataset.type === 'real';
          }});
          var projIds = ids.filter(function(id) {{
            var row = document.querySelector('.sched-row[data-id="'+id+'"]');
            return !row || row.dataset.type !== 'real';
          }});
          phConfirm('Excluir selecionados', 'Cancelar e remover ' + ids.length + ' post(s) da fila?', 'Excluir').then(function(ok) {{
            if (!ok) return;
            // Projeções: remove da tela
            projIds.forEach(function(id) {{ removeSchedSlot(id); }});
            if (realIds.length === 0) {{ schedClearSel(pid); return; }}
            // Posts reais: cancela via fetch (evita navegar múltiplas vezes)
            var body = new URLSearchParams();
            realIds.forEach(function(id) {{ body.append('post_ids', id); }});
            fetch('/app/profiles/'+pid+'/schedule/posts/bulk-cancel', {{
              method:'POST',
              credentials:'same-origin',
              headers: {{'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'}},
              body: body.toString()
            }}).then(function() {{
              window.location.href = '/app/profiles/'+pid+'?tab=agendamento';
            }}).catch(function() {{
              window.location.href = '/app/profiles/'+pid+'?tab=agendamento';
            }});
          }});
        }}
        function schedBulkEdit(pid) {{
          var ids = _schedSelected(pid);
          if (!ids.length) return;
          document.getElementById('schedBulkEditModal-'+pid).style.display = 'block';
        }}
        function schedRemoveAllSlots(pid) {{
          phConfirm('Remover todos os slots', 'Remover todos os slots da proje\u00e7\u00e3o da tela?', 'Remover').then(function(ok) {{
            if (!ok) return;
            document.querySelectorAll('#schedTbody-'+pid+' tr').forEach(function(r) {{
              r.style.transition='opacity .15s'; r.style.opacity='0';
              setTimeout(function(){{ r.style.display='none'; }}, 160);
            }});
            var bar = document.getElementById('schedBulkBar-'+pid);
            if (bar) bar.style.display='none';
            schedClearSel(pid);
          }});
        }}
        function schedBulkEditConfirm(pid) {{
          var dt = document.getElementById('schedBulkDateTime-'+pid).value;
          if (!dt) {{ alert('Escolha uma data/hora.'); return; }}
          var ids = _schedSelected(pid);
          if (!ids.length) return;
          // Para posts reais, reagendar o primeiro e recalcular os demais com offset
          // Para projeção, atualiza visualmente a data/hora nas cells
          var date = dt.split('T')[0].split('-').reverse().join('/');
          var time = dt.split('T')[1] || '';
          ids.forEach(function(id) {{
            var row = document.querySelector('.sched-row[data-id="'+id+'"]');
            if (!row) return;
            if (row.dataset.type === 'real') {{
              var f = document.createElement('form');
              f.method='post'; f.action='/app/profiles/'+pid+'/schedule/posts/'+id+'/reschedule';
              var inp = document.createElement('input'); inp.type='hidden'; inp.name='run_at'; inp.value=dt;
              f.appendChild(inp); document.body.appendChild(f); f.submit();
            }} else {{
              var cells = row.querySelectorAll('td');
              if (cells[3]) cells[3].textContent = date;
              if (cells[4]) {{ cells[4].innerHTML = '<b>'+time+'</b>'; }}
            }}
          }});
          document.getElementById('schedBulkEditModal-'+pid).style.display = 'none';
        }}
        function openSchedDiag(botId) {{
          document.getElementById('schedDiagOverlay').classList.add('open');
          if (typeof window._phPlayInfoSound === 'function') window._phPlayInfoSound();
          document.getElementById('schedDiagItems').innerHTML = '<div style="text-align:center;padding:32px;color:var(--muted)"><div style="font-size:28px;margin-bottom:8px">&#9203;</div>Verificando configura&#231;&#245;es...</div>';
          document.getElementById('schedDiagFooter').innerHTML = '';
          fetch('/app/robot/diagnose?bot_id=' + encodeURIComponent(botId), {{credentials:'same-origin'}})
            .then(function(r) {{ if (!r.ok) throw new Error('HTTP '+r.status); return r.json(); }})
            .then(function(data) {{
              var nameEl = document.getElementById('schedDiagBotName');
              if (nameEl && data.bot_name) nameEl.textContent = 'Bot: ' + data.bot_name;
              var icons = {{ok:'&#9989;',warn:'&#9888;&#65039;',err:'&#10060;'}};
              var out = '';
              (data.results||[]).forEach(function(item) {{
                out += phRenderDiagItem(item, icons);
              }});
              var snd = (typeof window._phSoundLabel === 'function') ? window._phSoundLabel() : '&#129534; Caixa';
              document.getElementById('schedDiagItems').innerHTML = (out || '<div style="padding:20px;text-align:center;color:var(--muted)">Nenhum resultado.</div>') + phRenderDiagSummary(data.summary, snd, 'Site');
              document.getElementById('schedDiagFooter').innerHTML = phRenderDiagFooter(
                'closeSchedDiag',
                'confirmSchedActivate',
                !!data.can_start,
                !!data.can_reconnect_start,
                'Ativar agendamento',
                'Reconectar e ativar'
              );
            }})
            .catch(function(err) {{
              document.getElementById('schedDiagItems').innerHTML = '<div style="text-align:center;padding:24px;color:#ef4444">&#10060; Erro: '+err.message+'</div>';
              document.getElementById('schedDiagFooter').innerHTML = '<button type="button" class="btn secondary" onclick="closeSchedDiag()">Fechar</button>';
            }});
        }}
        function closeSchedDiag() {{
          document.getElementById('schedDiagOverlay').classList.remove('open');
        }}
        function confirmSchedActivate(autoReconnect) {{
          document.getElementById('schedAutoReconnectInput').value = autoReconnect ? '1' : '0';
          closeSchedDiag();
          document.getElementById('schedActivateForm').submit();
        }}
        </script>
        """
    elif tab == "posts":
        return RedirectResponse("/app/posts", status_code=status.HTTP_302_FOUND)
    elif tab == "ia":
        _wp_icon_ia = ("<svg width='18' height='18' viewBox='0 0 24 24' fill='currentColor' style='flex-shrink:0'>"
                       "<path d='M12 2C6.486 2 2 6.486 2 12s4.486 10 10 10 10-4.486 10-10S17.514 2 12 2z"
                       "M3.251 12c0-1.308.265-2.556.741-3.695L7.36 18.658A8.762 8.762 0 0 1 3.251 12z"
                       "m8.749 8.75a8.773 8.773 0 0 1-2.496-.364l2.65-7.695 2.716 7.44a.96.96 0 0 0 .07.136"
                       " 8.764 8.764 0 0 1-2.94.483zm1.211-12.981c.528-.028.999-.084.999-.084"
                       " .47-.056.415-.748-.056-.72 0 0-1.415.111-2.329.111-.858 0-2.3-.111-2.3-.111"
                       "-.47-.028-.526.692-.055.72 0 0 .444.056.914.084l1.358 3.72-1.908 5.721"
                       "-3.176-8.441c.528-.028 1-.084 1-.084.47-.056.415-.748-.056-.72 0 0"
                       "-1.415.111-2.329.111a12.65 12.65 0 0 1-.31-.005A8.752 8.752 0 0 1 12 3.25"
                       "c2.294 0 4.389.879 5.963 2.315a2.885 2.885 0 0 0-.19-.013"
                       "c-.858 0-1.468.748-1.468 1.551 0 .72.415 1.329.859 2.049"
                       ".332.581.719 1.329.719 2.409 0 .748-.287 1.617-.663 2.825l-.871 2.907"
                       "-3.138-9.534zm3.64 11.791-.012-.025 2.733-7.897c.51-1.274.68-2.293.68-3.199"
                       " 0-.329-.021-.634-.059-.921A8.751 8.751 0 0 1 20.75 12c0 3.216-1.731 6.031-4.319 7.56l.42-1z'/>"
                       "</svg>")
        _fb_icon_ia = ("<svg width='18' height='18' viewBox='0 0 24 24' fill='currentColor' style='flex-shrink:0'>"
                       "<path d='M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12"
                       "c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43"
                       "c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83"
                       "c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385"
                       "C19.612 23.027 24 18.062 24 12.073z'/></svg>")
        _all_ia_profiles = list(db.scalars(
            select(AutomationProfile)
            .where(AutomationProfile.user_id == user.id)
            .order_by(AutomationProfile.active.desc(), AutomationProfile.created_at.asc())
        ))
        body += f"""
        {_ph("tab-ia-comandos")}
        <div style="display:flex;align-items:flex-start;gap:10px;padding:12px 16px;background:rgba(139,92,246,.08);border:1px solid rgba(139,92,246,.2);border-radius:12px;margin-bottom:16px;font-size:13px;color:var(--muted);line-height:1.6">
          <span style="font-size:18px;flex-shrink:0">&#9889;</span>
          <div>O <b>prompt da IA</b> define como o conte&#250;do ser&#225; reescrito para cada destino.
          Escreva instru&#231;&#245;es claras ” tom de voz, formato, tamanho, hashtags, etc.
          Cada bot tem seu pr&#243;prio conjunto de prompts independente.</div>
        </div>
        """
        for _ip in _all_ia_profiles:
            _ip_site_action = db.scalar(
                select(AiAction)
                .where(AiAction.profile_id == _ip.id, AiAction.destination == ActionDestination.WORDPRESS)
                .order_by(AiAction.created_at.asc()).limit(1)
            )
            _ip_fb_action = db.scalar(
                select(AiAction)
                .where(AiAction.profile_id == _ip.id, AiAction.destination == ActionDestination.FACEBOOK)
                .order_by(AiAction.created_at.asc()).limit(1)
            )
            _ip_site_prompt = (_ip_site_action.prompt_text if _ip_site_action else "").strip()
            _ip_fb_prompt   = (_ip_fb_action.prompt_text   if _ip_fb_action   else "").strip()
            _ip_sv1, _ip_sv2, _ip_sv3, _ip_sv4, _ip_smode = _parse_prompt_variants(_ip_site_prompt)
            _ip_fv1, _ip_fv2, _ip_fv3, _ip_fv4, _ip_fmode = _parse_prompt_variants(_ip_fb_prompt)
            _ip_open = "open" if (_ip.active or _ip.id == p.id) else ""
            _ip_name_esc = html.escape(_ip.name)
            _ip_id = _ip.id
            if _ip.active:
                _ip_badge = "<span class='badge-active' style='font-size:10px;padding:2px 7px'><span class='dot-pulse'></span>Ativo</span>"
            else:
                _ip_badge = "<span class='badge-inactive' style='font-size:10px;padding:2px 7px;opacity:.8'><span class='dot-off'></span>Inativo</span>"
            _ip_wp_status = ("<span style='margin-left:auto;font-size:10px;font-weight:700;color:#10b981;background:rgba(16,185,129,.12);border:1px solid rgba(16,185,129,.3);padding:2px 8px;border-radius:20px'>&#9679; Configurado</span>"
                             if any([_ip_sv1, _ip_sv2, _ip_sv3, _ip_sv4]) else
                             "<span style='margin-left:auto;font-size:10px;font-weight:700;color:#f59e0b;background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.3);padding:2px 8px;border-radius:20px'>Vazio</span>")
            _ip_fb_status  = ("<span style='margin-left:auto;font-size:10px;font-weight:700;color:#10b981;background:rgba(16,185,129,.12);border:1px solid rgba(16,185,129,.3);padding:2px 8px;border-radius:20px'>&#9679; Configurado</span>"
                              if any([_ip_fv1, _ip_fv2, _ip_fv3, _ip_fv4]) else
                              "<span style='margin-left:auto;font-size:10px;font-weight:700;color:#f59e0b;background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.3);padding:2px 8px;border-radius:20px'>Vazio</span>")
            # mode badge labels
            _mode_labels = {"1": "Prompt 1", "2": "Prompt 2", "3": "Prompt 3", "4": "Prompt 4", "random": "Aleat&#243;rio"}
            _ip_smode_sel = {k: ("selected" if k == _ip_smode else "") for k in ("1","2","3","4","random")}
            _ip_fmode_sel = {k: ("selected" if k == _ip_fmode else "") for k in ("1","2","3","4","random")}
            # prompt textarea escapes
            _e = lambda s: html.escape(s or "")
            _sv1e, _sv2e, _sv3e, _sv4e = _e(_ip_sv1), _e(_ip_sv2), _e(_ip_sv3), _e(_ip_sv4)
            _fv1e, _fv2e, _fv3e, _fv4e = _e(_ip_fv1), _e(_ip_fv2), _e(_ip_fv3), _e(_ip_fv4)
            _iid = _ip_id.replace("-", "")  # safe JS identifier
            body += f"""
            <div class="card" style="margin-bottom:14px">
              <details class="toggle-section" {_ip_open}>
                <summary>
                  <span class="ts-title" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
                    <span style="font-weight:700;font-size:14px">{_ip_name_esc}</span>
                    {_ip_badge}
                  </span>
                  <span class="ts-arrow">&#9655;</span>
                </summary>
                <div class="ts-body">
                  <form method="post" action="/app/profiles/{_ip_id}/ai-prompts">
                    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:stretch;margin-bottom:16px">
                      <!-- WordPress prompt -->
                      <div class="card" style="padding:20px;border-top:3px solid #21759b;display:flex;flex-direction:column;height:100%;box-sizing:border-box">
                        <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
                          <div style="width:36px;height:36px;border-radius:9px;background:#21759b;display:flex;align-items:center;justify-content:center;color:#fff;flex-shrink:0">
                            {_wp_icon_ia}
                          </div>
                          <div>
                            <div style="font-weight:700;font-size:14px">WordPress</div>
                            <div style="font-size:11px;color:var(--muted)">Prompt para artigos do site</div>
                          </div>
                          {_ip_wp_status}
                        </div>
                        <!-- Prompt tabs -->
                        <div style="display:flex;gap:4px;margin-bottom:10px">
                          <button type="button" id="swptab_{_iid}_1" onclick="phSwitchTab('{_iid}','wp',1)"
                            style="flex:1;padding:5px 0;font-size:12px;font-weight:600;border-radius:7px;border:1px solid rgba(139,92,246,.35);cursor:pointer;background:rgba(139,92,246,.15);color:var(--fg);transition:all .15s">Prompt 1</button>
                          <button type="button" id="swptab_{_iid}_2" onclick="phSwitchTab('{_iid}','wp',2)"
                            style="flex:1;padding:5px 0;font-size:12px;font-weight:600;border-radius:7px;border:1px solid rgba(139,92,246,.15);cursor:pointer;background:transparent;color:var(--muted);transition:all .15s">Prompt 2</button>
                          <button type="button" id="swptab_{_iid}_3" onclick="phSwitchTab('{_iid}','wp',3)"
                            style="flex:1;padding:5px 0;font-size:12px;font-weight:600;border-radius:7px;border:1px solid rgba(139,92,246,.15);cursor:pointer;background:transparent;color:var(--muted);transition:all .15s">Prompt 3</button>
                          <button type="button" id="swptab_{_iid}_4" onclick="phSwitchTab('{_iid}','wp',4)"
                            style="flex:1;padding:5px 0;font-size:12px;font-weight:600;border-radius:7px;border:1px solid rgba(139,92,246,.15);cursor:pointer;background:transparent;color:var(--muted);transition:all .15s">Prompt 4</button>
                        </div>
                        <div id="swp_{_iid}_1" style="display:block">
                          <textarea name="site_prompt_1" placeholder="Prompt 1: Reescreva a receita em portugu&#234;s com tom acolhedor e SEO completo..." style="height:190px;font-size:13px;resize:none;width:100%;box-sizing:border-box">{_sv1e}</textarea>
                        </div>
                        <div id="swp_{_iid}_2" style="display:none">
                          <textarea name="site_prompt_2" placeholder="Prompt 2: Reescreva com tom mais t&#233;cnico e detalhado, focando em dicas de nutri&#231;&#227;o..." style="height:190px;font-size:13px;resize:none;width:100%;box-sizing:border-box">{_sv2e}</textarea>
                        </div>
                        <div id="swp_{_iid}_3" style="display:none">
                          <textarea name="site_prompt_3" placeholder="Prompt 3: Reescreva com linguagem descontra&#237;da, voltada para p&#250;blico jovem..." style="height:190px;font-size:13px;resize:none;width:100%;box-sizing:border-box">{_sv3e}</textarea>
                        </div>
                        <div id="swp_{_iid}_4" style="display:none">
                          <textarea name="site_prompt_4" placeholder="Prompt 4: Reescreva no modo r&#225;pido, com a mesma estrutura, introdu&#231;&#227;o curta e texto objetivo para publicar mais cedo..." style="height:190px;font-size:13px;resize:none;width:100%;box-sizing:border-box">{_sv4e}</textarea>
                        </div>
                        <div style="display:flex;align-items:center;gap:8px;margin-top:10px">
                          <span style="font-size:12px;font-weight:600;color:var(--muted);white-space:nowrap">Bot usa:</span>
                          <select name="site_prompt_mode" style="flex:1;font-size:12px;padding:5px 8px;border-radius:7px;border:1px solid var(--border);background:var(--card);color:var(--fg)">
                            <option value="1" {_ip_smode_sel["1"]}>Prompt 1</option>
                            <option value="2" {_ip_smode_sel["2"]}>Prompt 2</option>
                            <option value="3" {_ip_smode_sel["3"]}>Prompt 3</option>
                            <option value="4" {_ip_smode_sel["4"]}>Prompt 4</option>
                            <option value="random" {_ip_smode_sel["random"]}>&#127922; Aleat&#243;rio</option>
                          </select>
                        </div>
                        <div style="margin-top:6px;font-size:11px;color:var(--muted)">Dica: cada prompt pode ter um estilo diferente ” o bot usa o selecionado (ou sorteia no Aleat&#243;rio).</div>
                      </div>
                      <!-- Facebook prompt -->
                      <div class="card" style="padding:20px;border-top:3px solid #1877f2;display:flex;flex-direction:column;height:100%;box-sizing:border-box;margin-top:0">
                        <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
                          <div style="width:36px;height:36px;border-radius:9px;background:#1877f2;display:flex;align-items:center;justify-content:center;color:#fff;flex-shrink:0">
                            {_fb_icon_ia}
                          </div>
                          <div>
                            <div style="font-weight:700;font-size:14px">Facebook</div>
                            <div style="font-size:11px;color:var(--muted)">Prompt para posts sociais</div>
                          </div>
                          {_ip_fb_status}
                        </div>
                        <!-- Prompt tabs -->
                        <div style="display:flex;gap:4px;margin-bottom:10px">
                          <button type="button" id="sfbtab_{_iid}_1" onclick="phSwitchTab('{_iid}','fb',1)"
                            style="flex:1;padding:5px 0;font-size:12px;font-weight:600;border-radius:7px;border:1px solid rgba(24,119,242,.35);cursor:pointer;background:rgba(24,119,242,.12);color:var(--fg);transition:all .15s">Prompt 1</button>
                          <button type="button" id="sfbtab_{_iid}_2" onclick="phSwitchTab('{_iid}','fb',2)"
                            style="flex:1;padding:5px 0;font-size:12px;font-weight:600;border-radius:7px;border:1px solid rgba(24,119,242,.15);cursor:pointer;background:transparent;color:var(--muted);transition:all .15s">Prompt 2</button>
                          <button type="button" id="sfbtab_{_iid}_3" onclick="phSwitchTab('{_iid}','fb',3)"
                            style="flex:1;padding:5px 0;font-size:12px;font-weight:600;border-radius:7px;border:1px solid rgba(24,119,242,.15);cursor:pointer;background:transparent;color:var(--muted);transition:all .15s">Prompt 3</button>
                          <button type="button" id="sfbtab_{_iid}_4" onclick="phSwitchTab('{_iid}','fb',4)"
                            style="flex:1;padding:5px 0;font-size:12px;font-weight:600;border-radius:7px;border:1px solid rgba(24,119,242,.15);cursor:pointer;background:transparent;color:var(--muted);transition:all .15s">Prompt 4</button>
                        </div>
                        <div id="sfb_{_iid}_1" style="display:block">
                          <textarea name="facebook_prompt_1" placeholder="Prompt 1: Crie um post curto e envolvente com emojis e CTA..." style="height:190px;font-size:13px;resize:none;width:100%;box-sizing:border-box">{_fv1e}</textarea>
                        </div>
                        <div id="sfb_{_iid}_2" style="display:none">
                          <textarea name="facebook_prompt_2" placeholder="Prompt 2: Post mais informativo, destacando benef&#237;cios da receita..." style="height:190px;font-size:13px;resize:none;width:100%;box-sizing:border-box">{_fv2e}</textarea>
                        </div>
                        <div id="sfb_{_iid}_3" style="display:none">
                          <textarea name="facebook_prompt_3" placeholder="Prompt 3: Post descontra&#237;do com humor leve e emojis..." style="height:190px;font-size:13px;resize:none;width:100%;box-sizing:border-box">{_fv3e}</textarea>
                        </div>
                        <div id="sfb_{_iid}_4" style="display:none">
                          <textarea name="facebook_prompt_4" placeholder="Prompt 4: Post ultra curto para modo r&#225;pido, com CTA cedo e texto direto..." style="height:190px;font-size:13px;resize:none;width:100%;box-sizing:border-box">{_fv4e}</textarea>
                        </div>
                        <div style="display:flex;align-items:center;gap:8px;margin-top:10px">
                          <span style="font-size:12px;font-weight:600;color:var(--muted);white-space:nowrap">Bot usa:</span>
                          <select name="facebook_prompt_mode" style="flex:1;font-size:12px;padding:5px 8px;border-radius:7px;border:1px solid var(--border);background:var(--card);color:var(--fg)">
                            <option value="1" {_ip_fmode_sel["1"]}>Prompt 1</option>
                            <option value="2" {_ip_fmode_sel["2"]}>Prompt 2</option>
                            <option value="3" {_ip_fmode_sel["3"]}>Prompt 3</option>
                            <option value="4" {_ip_fmode_sel["4"]}>Prompt 4</option>
                            <option value="random" {_ip_fmode_sel["random"]}>&#127922; Aleat&#243;rio</option>
                          </select>
                        </div>
                        <div style="margin-top:6px;font-size:11px;color:var(--muted)">Dica: posts curtos funcionam melhor. Use emojis e chamadas para a&#231;&#227;o.</div>
                      </div>
                    </div>
                    <div style="display:flex;justify-content:flex-end">
                      <button class="btn flat" type="submit" style="padding:10px 26px;font-size:14px;gap:7px">
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
                        Salvar prompts
                      </button>
                    </div>
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
    next_tab: str = Form("agendamento"),
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
            utc = _local_input_to_utc_naive(s, user=user).replace(tzinfo=timezone.utc)
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
    _safe_next = next_tab if next_tab in ("agendamento", "publicacao") else "agendamento"
    return RedirectResponse(f"/app/profiles/{p.id}?tab={_safe_next}&msg={quote_plus('Cadência salva.')}", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/schedule/activate", include_in_schema=False)
def profile_schedule_activate(
    profile_id: str,
    auto_reconnect: str = Form("0"),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Ativa o agendamento: roda como Iniciar mas respeitando a config salva (start_at_utc + intervalo)."""
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    # Validate WP connection first (same as robot_start)
    try:
        wp_integ, _wp_creds, active_user, base_url = _wordpress_connection_for_bot(db, bot_id=p.id)
    except Exception:
        wp_integ, active_user, base_url = None, None, ""
    if not wp_integ or not base_url or not active_user or not active_user.get("username") or not active_user.get("app_password"):
        return RedirectResponse(
            f"/app/profiles/{p.id}?tab=agendamento&msg={quote_plus('WordPress não configurado. Configure antes de ativar.')}",
            status_code=status.HTTP_302_FOUND,
        )
    if str(auto_reconnect) == "1":
        _try_reconnect_wordpress(db, wp_integ, base_url=base_url, active_user=active_user)
        db.commit()
    # Check no active queue
    p.active = True
    _ensure_publish_config_defaults(db, bot=p)
    _set_bot_run_stopped(db, bot=p, stopped=False)
    revived = _revive_profile_queue(db, profile_id=p.id)
    _active = int(db.scalar(
        select(func.count()).select_from(Job).where(
            Job.profile_id == p.id,
            Job.status.in_([JobStatus.queued, JobStatus.running])
        )
    ) or 0)
    _active_posts = int(db.scalar(
        select(func.count()).select_from(Post).where(
            Post.profile_id == p.id,
            Post.status.in_([PostStatus.pending, PostStatus.processing]),
        )
    ) or 0)
    if (_active + _active_posts) > 0:
        db.commit()
        msg = "Fila retomada e em andamento." if revived else "Ja existe uma fila ativa. Acompanhe em Posts."
        return RedirectResponse(f"/app/posts?msg={quote_plus(msg)}", status_code=status.HTTP_302_FOUND)
    cfg = p.schedule_config_json or {}
    limit = int(cfg.get("posts_per_day") or 15)
    respect = int(cfg.get("respect_schedule") or 0) == 1
    interval_minutes = int(cfg.get("interval_minutes") or 0)
    # Clear all existing posts so the bot counts from zero on this new run.
    # keep_collected=True preserves URL fingerprints so the collector never re-posts the same article.
    _all_post_ids = list(db.scalars(select(Post.id).where(Post.profile_id == p.id)))
    if _all_post_ids:
        _delete_posts(db, profile_id=p.id, post_ids=[str(x) for x in _all_post_ids], keep_collected=True)
    enqueue_job(
        db,
        user_id=p.user_id,
        profile_id=p.id,
        job_type=JOB_COLLECT,
        payload={"limit": limit, "interval_minutes": interval_minutes, "respect_schedule": 1 if respect else 0},
    )
    db.commit()
    return RedirectResponse(
        f"/app/posts?msg={quote_plus('Agendamento ativado! Posts sendo enfileirados.')}",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/app/profiles/{profile_id}/schedule/posts/{post_id}/cancel", include_in_schema=False)
def profile_schedule_post_cancel(
    profile_id: str,
    post_id: str,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Cancela um post agendado específico e seus jobs pendentes."""
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    post = db.scalar(select(Post).where(Post.id == post_id, Post.profile_id == p.id))
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    _cancel_posts(db, profile_id=p.id, post_ids=[post.id], user=user)
    db.commit()
    return RedirectResponse(
        f"/app/profiles/{p.id}?tab=agendamento&msg={quote_plus('Post cancelado.')}",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/app/profiles/{profile_id}/schedule/posts/bulk-cancel", include_in_schema=False)
def profile_schedule_posts_bulk_cancel(
    profile_id: str,
    post_ids: list[str] = Form(default=[]),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ids = [str(x).strip() for x in post_ids if str(x).strip()]
    if ids:
        ids = list(db.scalars(select(Post.id).where(
            Post.profile_id == p.id,
            Post.id.in_(ids),
            Post.status.in_([PostStatus.pending, PostStatus.processing]),
        )))
        _cancel_posts(db, profile_id=p.id, post_ids=[str(x) for x in ids], user=user)
    db.commit()
    return RedirectResponse(
        f"/app/profiles/{p.id}?tab=agendamento&msg={quote_plus(f'{len(ids)} post(s) cancelado(s).')}",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/app/profiles/{profile_id}/schedule/posts/{post_id}/reschedule", include_in_schema=False)
def profile_schedule_post_reschedule(
    profile_id: str,
    post_id: str,
    run_at: str = Form(""),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Reagenda um post específico para nova data/hora."""
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    post = db.scalar(select(Post).where(Post.id == post_id, Post.profile_id == p.id))
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not run_at:
        return RedirectResponse(
            f"/app/profiles/{p.id}?tab=agendamento&err={quote_plus('Data/hora inválida.')}",
            status_code=status.HTTP_302_FOUND,
        )
    try:
        new_dt = _local_input_to_utc_naive(run_at, user=user)
    except Exception:
        return RedirectResponse(
            f"/app/profiles/{p.id}?tab=agendamento&err={quote_plus('Formato de data inválido.')}",
            status_code=status.HTTP_302_FOUND,
        )
    from sqlalchemy import update as _upd
    db.execute(
        _upd(Job).where(
            Job.post_id == post.id,
            Job.status == JobStatus.queued,
        ).values(run_at=new_dt)
    )
    db.commit()
    return RedirectResponse(
        f"/app/profiles/{p.id}?tab=agendamento&msg={quote_plus('Post reagendado com sucesso.')}",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/app/profiles/{profile_id}/schedule/cancel", include_in_schema=False)
def profile_schedule_cancel(
    profile_id: str,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Cancela todos os jobs e posts pendentes do perfil."""
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ids = list(db.scalars(select(Post.id).where(
        Post.profile_id == p.id,
        Post.status.in_([PostStatus.pending, PostStatus.processing]),
    )))
    cancelled_posts = len(ids)
    before_jobs = int(db.scalar(select(func.count()).select_from(Job).where(
        Job.profile_id == p.id,
        Job.status.in_([JobStatus.queued, JobStatus.running]),
    )) or 0)
    _cancel_posts(db, profile_id=p.id, post_ids=[str(x) for x in ids], user=user)
    db.execute(
        update(Job)
        .where(Job.profile_id == p.id, Job.status.in_([JobStatus.queued, JobStatus.running]))
        .values(status=JobStatus.failed, last_error="canceled_by_user", locked_at=None, locked_by=None, updated_at=datetime.utcnow())
    )
    cancelled_jobs = before_jobs
    db.commit()
    msg = f"Cancelado: {cancelled_jobs} job(s) e {cancelled_posts} post(s) pendente(s)."
    return RedirectResponse(
        f"/app/profiles/{p.id}?tab=agendamento&msg={quote_plus(msg)}",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/app/profiles/{profile_id}/ai-prompts", include_in_schema=False)
def profile_ai_prompts_save(
    profile_id: str,
    site_prompt_1: str = Form(""),
    site_prompt_2: str = Form(""),
    site_prompt_3: str = Form(""),
    site_prompt_4: str = Form(""),
    site_prompt_mode: str = Form("1"),
    facebook_prompt_1: str = Form(""),
    facebook_prompt_2: str = Form(""),
    facebook_prompt_3: str = Form(""),
    facebook_prompt_4: str = Form(""),
    facebook_prompt_mode: str = Form("1"),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    _ensure_default_recipe_actions(db, bot=p)

    def _build_prompt_json(v1: str, v2: str, v3: str, v4: str, mode: str) -> str:
        v1 = (v1 or "").strip()
        v2 = (v2 or "").strip()
        v3 = (v3 or "").strip()
        v4 = (v4 or "").strip()
        mode = (mode or "1").strip()
        if mode not in ("1", "2", "3", "4", "random"):
            mode = "1"
        return json.dumps({"mode": mode, "v": [v1, v2, v3, v4]}, ensure_ascii=False)

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
    sp = _build_prompt_json(site_prompt_1, site_prompt_2, site_prompt_3, site_prompt_4, site_prompt_mode)
    fp = _build_prompt_json(facebook_prompt_1, facebook_prompt_2, facebook_prompt_3, facebook_prompt_4, facebook_prompt_mode)
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
            .where(Job.profile_id == profile_id, Job.post_id.in_(post_ids), Job.status.in_([JobStatus.queued, JobStatus.running]))
            .values(status=JobStatus.failed, last_error="canceled_by_user", locked_at=None, locked_by=None, updated_at=now)
        )


def _cancel_collect_jobs(db, *, profile_id: str) -> int:
    now = datetime.utcnow()
    result = db.execute(
        update(Job)
        .where(
            Job.profile_id == profile_id,
            Job.type == JOB_COLLECT,
            Job.status.in_([JobStatus.queued, JobStatus.running]),
        )
        .values(status=JobStatus.failed, last_error="canceled_by_user", locked_at=None, locked_by=None, updated_at=now)
    )
    return int(result.rowcount or 0)


def _delete_posts(db, *, profile_id: str, post_ids: list[str], keep_collected: bool = False):
    """Delete posts and associated jobs/logs.
    keep_collected=True: preserve CollectedContent records (keeps URL fingerprints for deduplication).
    keep_collected=False (default): also deletes CollectedContent records.
    """
    posts = list(db.scalars(select(Post).where(Post.profile_id == profile_id, Post.id.in_(post_ids))))
    content_ids = [p.collected_content_id for p in posts if p.collected_content_id]
    if post_ids:
        db.query(JobLog).filter(JobLog.profile_id == profile_id, JobLog.post_id.in_(post_ids)).delete(synchronize_session=False)
        db.query(Job).filter(Job.profile_id == profile_id, Job.post_id.in_(post_ids)).delete(synchronize_session=False)
        db.query(Post).filter(Post.profile_id == profile_id, Post.id.in_(post_ids)).delete(synchronize_session=False)
    if content_ids and not keep_collected:
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


@router.post("/app/profiles/{profile_id}/posts/{post_id}/correct", include_in_schema=False)
def profile_post_correct(profile_id: str, post_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    post = db.scalar(select(Post).where(Post.profile_id == p.id, Post.id == post_id))
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if post.status != PostStatus.completed:
        return RedirectResponse("/app/posts?msg=Somente+posts+publicados+podem+ser+corrigidos.", status_code=status.HTTP_302_FOUND)
    active_job = db.scalar(
        select(Job.id)
        .where(Job.post_id == post.id, Job.status.in_([JobStatus.queued, JobStatus.running]))
        .limit(1)
    )
    if active_job:
        return RedirectResponse("/app/posts?msg=Este+post+j%C3%A1+est%C3%A1+em+corre%C3%A7%C3%A3o.", status_code=status.HTTP_302_FOUND)
    outputs = dict(post.outputs_json or {})
    # Clear all previously generated content so the AI produces a fresh rewrite
    outputs.pop("recipe", None)
    outputs.pop("image", None)
    for k in list(outputs.keys()):
        if isinstance(k, str) and (k.startswith("wordpress:") or k.startswith("facebook:")):
            outputs.pop(k, None)
    outputs["correction_requested"] = True
    post.outputs_json = outputs
    post.status = PostStatus.processing
    post.updated_at = datetime.utcnow()
    db.add(post)
    enqueue_job(
        db,
        user_id=post.user_id,
        profile_id=post.profile_id,
        post_id=post.id,
        job_type=JOB_AI,
        payload={"collected_content_id": post.collected_content_id},
    )
    db.commit()
    return RedirectResponse("/app/posts?msg=Corre%C3%A7%C3%A3o+reagendada.", status_code=status.HTTP_302_FOUND)


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
        return RedirectResponse("/app/posts", status_code=status.HTTP_302_FOUND)
    if mode == "cancel":
        _cancel_posts(db, profile_id=p.id, post_ids=ids, user=user)
        db.commit()
        return RedirectResponse("/app/posts", status_code=status.HTTP_302_FOUND)
    if mode == "delete_wp":
        try:
            creds = _get_wordpress_creds_for_profile(db, profile_id=p.id, user_id=user.id)
        except WordPressError as e:
            return RedirectResponse(f"/app/posts?msg={quote_plus(str(e))}", status_code=status.HTTP_302_FOUND)
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
        return RedirectResponse(f"/app/posts?msg={quote_plus(msg)}", status_code=status.HTTP_302_FOUND)
    _delete_posts(db, profile_id=p.id, post_ids=ids)
    db.commit()
    return RedirectResponse("/app/posts", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/posts/cancel-all", include_in_schema=False)
def profile_posts_cancel_all(profile_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ids = list(
        db.scalars(select(Post.id).where(Post.profile_id == p.id, Post.status.in_([PostStatus.pending, PostStatus.processing])))
    )
    canceled_collect = _cancel_collect_jobs(db, profile_id=p.id)
    if not ids and not canceled_collect:
        return RedirectResponse("/app/posts?msg=Nenhum+post+pendente+para+cancelar.", status_code=status.HTTP_302_FOUND)
    if ids:
        _cancel_posts(db, profile_id=p.id, post_ids=[str(x) for x in ids], user=user)
    db.commit()
    return RedirectResponse("/app/posts", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/posts/delete-completed", include_in_schema=False)
def profile_posts_delete_completed(profile_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ids = list(db.scalars(select(Post.id).where(Post.profile_id == p.id, Post.status == PostStatus.completed)))
    if not ids:
        return RedirectResponse("/app/posts?msg=Nenhum+post+publicado+para+apagar.", status_code=status.HTTP_302_FOUND)
    _delete_posts(db, profile_id=p.id, post_ids=[str(x) for x in ids])
    db.commit()
    return RedirectResponse("/app/posts", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/posts/delete-failed", include_in_schema=False)
def profile_posts_delete_failed(profile_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ids = list(db.scalars(select(Post.id).where(Post.profile_id == p.id, Post.status == PostStatus.failed)))
    if not ids:
        return RedirectResponse("/app/posts?msg=Nenhuma+falha+para+excluir.", status_code=status.HTTP_302_FOUND)
    _delete_posts(db, profile_id=p.id, post_ids=[str(x) for x in ids])
    db.commit()
    return RedirectResponse("/app/posts", status_code=status.HTTP_302_FOUND)


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
    return RedirectResponse(f"/app/profiles/{p.id}?tab=fontes", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/run", include_in_schema=False)
def profile_run(profile_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    p.active = True
    _set_bot_run_stopped(db, bot=p, stopped=False)
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
        # Atualiza integração existente ” adiciona ou atualiza o usuário na lista
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
    return RedirectResponse(f"/app/profiles/{p.id}?tab=integracoes&itab=wordpress&msg={quote_plus('Usuário removido.')}", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/integrations/wordpress/edit-user", include_in_schema=False)
def profile_wp_edit_user(
    profile_id: str,
    old_username: str = Form(...),
    new_username: str = Form(...),
    new_app_password: str = Form(""),
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
    old = old_username.strip()
    new_u = new_username.strip()
    new_p = new_app_password.strip()
    users = creds.get("users") or []
    for u_entry in users:
        if u_entry.get("username") == old:
            u_entry["username"] = new_u
            if new_p:
                u_entry["app_password"] = new_p
    if creds.get("active_username") == old:
        creds["active_username"] = new_u
    creds["users"] = users
    integ.credentials_encrypted = encrypt_json(creds)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}?tab=integracoes&itab=wordpress&msg={quote_plus('Usuário atualizado.')}", status_code=status.HTTP_302_FOUND)


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
    return RedirectResponse(f"/app/profiles/{p.id}?tab=integracoes&itab=gemini", status_code=status.HTTP_302_FOUND)


@router.post("/app/profiles/{profile_id}/integrations/openai", include_in_schema=False)
def profile_openai_integration_create(
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
    existing = db.scalar(select(Integration).where(Integration.profile_id == p.id, Integration.type == IntegrationType.OPENAI))
    if existing:
        existing.credentials_encrypted = encrypted
        existing.name = "ChatGPT"
        db.add(existing)
    else:
        integ = Integration(
            user_id=p.user_id,
            profile_id=p.id,
            type=IntegrationType.OPENAI,
            name="ChatGPT",
            credentials_encrypted=encrypted,
        )
        db.add(integ)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}?tab=integracoes&itab=openai", status_code=status.HTTP_302_FOUND)


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
    facebook_image: str = Form("link_preview"),
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
    _img = str(facebook_image or "link_preview").strip()
    cfg["facebook_image"] = _img if _img in ("link_preview", "direct_photo", "none") else "link_preview"
    p.publish_config_json = cfg
    db.add(p)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}?tab=publicacao&ptab=facebook&msg={quote_plus('Configuração do Facebook salva.')}", status_code=status.HTTP_302_FOUND)


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


@router.post("/app/profiles/{profile_id}/integrations/facebook/pages/test", include_in_schema=False)
def profile_facebook_pages_test(
    profile_id: str,
    page_id: str = Form(...),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    from fastapi.responses import JSONResponse
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    pid = str(page_id or "").strip()
    if not pid:
        return JSONResponse({"ok": False, "error": "missing_page_id"}, status_code=400)
    integ = db.scalar(select(Integration).where(Integration.profile_id == p.id, Integration.type == IntegrationType.FACEBOOK))
    if not integ:
        return JSONResponse({"ok": False, "error": "Integração do Facebook não configurada."})
    try:
        creds = decrypt_json(integ.credentials_encrypted)
    except Exception:
        return JSONResponse({"ok": False, "error": "Erro ao ler credenciais."})
    pages_val = creds.get("pages") if isinstance(creds, dict) else None
    pages = [x for x in pages_val if isinstance(x, dict)] if isinstance(pages_val, list) else []
    token = ""
    for pg in pages:
        if str(pg.get("page_id") or "").strip() == pid:
            token = str(pg.get("access_token") or "").strip()
            break
    if not token:
        return JSONResponse({"ok": False, "error": "Token não encontrado para esta página."})
    from app.services.facebook import test_page_token as _test_fb_token
    result = _test_fb_token(page_id=pid, page_access_token=token)
    return JSONResponse(result)


@router.get("/app/oauth/facebook/start", include_in_schema=False)
def facebook_oauth_start(
    profile_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    integ = db.scalar(select(Integration).where(Integration.profile_id == p.id, Integration.type == IntegrationType.FACEBOOK))
    app_id = ""
    if integ:
        try:
            creds = decrypt_json(integ.credentials_encrypted)
            app_id = str(creds.get("oauth_app_id") or "").strip()
        except Exception:
            pass
    if not app_id:
        return RedirectResponse(f"/app/profiles/{profile_id}?tab=integracoes&itab=facebook&msg={quote_plus('Configure o App ID primeiro.')}", status_code=302)
    state = f"{profile_id}:{secrets.token_hex(16)}"
    request.session["fb_oauth_state"] = state
    base = str(request.base_url).rstrip("/")
    callback_url = f"{base}/app/oauth/facebook/callback"
    redirect_url = (
        f"https://www.facebook.com/v19.0/dialog/oauth"
        f"?client_id={app_id}"
        f"&redirect_uri={quote_plus(callback_url)}"
        f"&scope=pages_manage_posts%2Cpages_show_list"
        f"&response_type=code"
        f"&state={state}"
    )
    return RedirectResponse(redirect_url, status_code=302)


@router.get("/app/oauth/facebook/callback", include_in_schema=False)
def facebook_oauth_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    import httpx as _httpx
    if error:
        return RedirectResponse(f"/app?msg={quote_plus('Facebook OAuth cancelado.')}", status_code=302)
    saved_state = request.session.get("fb_oauth_state", "")
    if not saved_state or saved_state != state:
        raise HTTPException(status_code=400, detail="invalid_state")
    profile_id = (state or "").split(":")[0]
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=404)
    integ = db.scalar(select(Integration).where(Integration.profile_id == p.id, Integration.type == IntegrationType.FACEBOOK))
    if not integ:
        return RedirectResponse(f"/app/profiles/{profile_id}?tab=integracoes&itab=facebook&msg={quote_plus('Integração não configurada.')}", status_code=302)
    try:
        creds = decrypt_json(integ.credentials_encrypted)
    except Exception:
        creds = {}
    app_id = str(creds.get("oauth_app_id") or "").strip()
    app_secret = str(creds.get("oauth_app_secret") or "").strip()
    if not app_id or not app_secret:
        return RedirectResponse(f"/app/profiles/{profile_id}?tab=integracoes&itab=facebook&msg={quote_plus('App ID ou Secret não configurados.')}", status_code=302)
    base = str(request.base_url).rstrip("/")
    callback_url = f"{base}/app/oauth/facebook/callback"
    try:
        r = _httpx.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
            "client_id": app_id, "client_secret": app_secret,
            "redirect_uri": callback_url, "code": code,
        }, timeout=15)
        token_data = r.json()
        user_token = str(token_data.get("access_token") or "").strip()
        if not user_token:
            raise ValueError(str(token_data))
        r2 = _httpx.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": app_id,
            "client_secret": app_secret, "fb_exchange_token": user_token,
        }, timeout=15)
        ll_token = str(r2.json().get("access_token") or user_token).strip()
        r3 = _httpx.get("https://graph.facebook.com/v19.0/me/accounts", params={
            "access_token": ll_token, "fields": "id,name,access_token",
        }, timeout=15)
        pages_data = r3.json().get("data") or []
    except Exception as exc:
        return RedirectResponse(f"/app/profiles/{profile_id}?tab=integracoes&itab=facebook&msg={quote_plus(f'Erro OAuth: {str(exc)[:120]}')}", status_code=302)
    existing_pages = [x for x in (creds.get("pages") or []) if isinstance(x, dict)]
    existing_ids = {str(x.get("page_id") or "") for x in existing_pages}
    added = 0
    for pg in pages_data:
        pid = str(pg.get("id") or "").strip()
        ptoken = str(pg.get("access_token") or "").strip()
        pname = str(pg.get("name") or "").strip()
        if not pid or not ptoken:
            continue
        if pid not in existing_ids:
            existing_pages.append({"page_id": pid, "name": pname, "access_token": ptoken})
            existing_ids.add(pid)
            added += 1
        else:
            for ep in existing_pages:
                if str(ep.get("page_id") or "") == pid:
                    ep["access_token"] = ptoken
                    if pname:
                        ep["name"] = pname
    new_creds = dict(creds)
    new_creds["pages"] = existing_pages
    integ.credentials_encrypted = encrypt_json(new_creds)
    integ.name = "Facebook"
    db.add(integ)
    db.commit()
    msg = f"{added} página(s) adicionada(s) via OAuth." if added else "Tokens das páginas atualizados via OAuth."
    return RedirectResponse(f"/app/profiles/{profile_id}?tab=integracoes&itab=facebook&msg={quote_plus(msg)}", status_code=302)


@router.post("/app/profiles/{profile_id}/integrations/facebook/oauth-config", include_in_schema=False)
def profile_facebook_oauth_config(
    profile_id: str,
    oauth_app_id: str = Form(""),
    oauth_app_secret: str = Form(""),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    p = _get_profile_for_user(db, profile_id=profile_id, user=user)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    existing = db.scalar(select(Integration).where(Integration.profile_id == p.id, Integration.type == IntegrationType.FACEBOOK))
    if existing:
        try:
            creds = decrypt_json(existing.credentials_encrypted)
        except Exception:
            creds = {}
        creds = dict(creds)
    else:
        creds = {"pages": []}
        existing = Integration(
            id=str(__import__("uuid").uuid4()),
            user_id=user.id,
            profile_id=p.id,
            type=IntegrationType.FACEBOOK,
            name="Facebook",
            status=__import__("app.models", fromlist=["IntegrationStatus"]).IntegrationStatus.CONNECTED,
        )
    creds["oauth_app_id"] = str(oauth_app_id or "").strip()
    creds["oauth_app_secret"] = str(oauth_app_secret or "").strip()
    existing.credentials_encrypted = encrypt_json(creds)
    db.add(existing)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}?tab=integracoes&itab=facebook&msg={quote_plus('App OAuth salvo.')}", status_code=302)


@router.post("/app/profiles/{profile_id}/publish/wordpress", include_in_schema=False)
def profile_publish_wordpress(
    profile_id: str,
    default_category: str = Form("Receitas"),
    categories: str = Form(""),
    fast_publish_enabled: str = Form(""),
    rss_fallback_after_seconds: str = Form("20"),
    fast_skip_wp_image: str = Form(""),
    fast_skip_wp_tags: str = Form(""),
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
    cfg["fast_publish_enabled"] = str(fast_publish_enabled or "").strip() == "1"
    try:
        cfg["rss_fallback_after_seconds"] = max(5, min(int(rss_fallback_after_seconds or 20), 180))
    except Exception:
        cfg["rss_fallback_after_seconds"] = 20
    cfg["fast_skip_wp_image"] = str(fast_skip_wp_image or "").strip() == "1"
    cfg["fast_skip_wp_tags"] = str(fast_skip_wp_tags or "").strip() == "1"
    p.publish_config_json = cfg
    db.add(p)
    db.commit()
    return RedirectResponse(f"/app/profiles/{p.id}?tab=publicacao&ptab=wordpress&msg={quote_plus('Configuração do WordPress salva.')}", status_code=status.HTTP_302_FOUND)


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


def _job_payload_limit(job: Job) -> int:
    payload = job.payload_json if isinstance(job.payload_json, dict) else {}
    try:
        limit = int(payload.get("limit") or 0)
    except Exception:
        limit = 0
    return max(0, limit)


def _collect_job_target(job: Job) -> int:
    payload = job.payload_json if isinstance(job.payload_json, dict) else {}
    try:
        start = int(payload.get("schedule_index_start") or 0)
    except Exception:
        start = 0
    return max(0, start + _job_payload_limit(job))


def _active_collect_plan(db, *, profile_id: str) -> dict:
    jobs = list(db.scalars(
        select(Job).where(
            Job.profile_id == profile_id,
            Job.type == JOB_COLLECT,
            Job.status.in_([JobStatus.queued, JobStatus.running]),
        )
    ))
    if not jobs:
        return {"missing": 0, "requested": 0, "materialized": 0, "running": False, "next_run": None, "interval_minutes": 0}
    anchor = max(
        jobs,
        key=lambda j: (_collect_job_target(j), j.created_at or datetime.utcnow(), j.run_at or datetime.utcnow()),
    )
    total_requested = _collect_job_target(anchor)
    if total_requested <= 0:
        return {"missing": 0, "requested": 0, "materialized": 0, "running": False, "next_run": None, "interval_minutes": 0}
    first_created = anchor.created_at
    materialized = 0
    if first_created:
        materialized = int(db.scalar(
            select(func.count()).select_from(Post).where(
                Post.profile_id == profile_id,
                Post.created_at >= first_created,
            )
        ) or 0)
    missing = max(0, total_requested - materialized)
    next_run = anchor.run_at
    running = anchor.status == JobStatus.running
    first_payload = anchor.payload_json if isinstance(anchor.payload_json, dict) else {}
    try:
        interval_minutes = int(first_payload.get("interval_minutes") or 0)
    except Exception:
        interval_minutes = 0
    try:
        schedule_index_start = int(first_payload.get("schedule_index_start") or 0)
    except Exception:
        schedule_index_start = 0
    return {
        "missing": missing,
        "requested": total_requested,
        "materialized": materialized,
        "running": running,
        "next_run": next_run,
        "interval_minutes": interval_minutes,
        "schedule_index_start": schedule_index_start,
    }


@router.get("/app/posts", include_in_schema=False)
def posts_page(request: Request, user: User = Depends(get_current_user), db=Depends(get_db)):
    all_profiles = list(db.scalars(
        select(AutomationProfile)
        .where(AutomationProfile.user_id == user.id)
        .order_by(AutomationProfile.active.desc(), AutomationProfile.created_at.asc())
    ))
    revived = 0
    for profile in all_profiles:
        profile_cfg = dict(profile.publish_config_json or {})
        if profile.active and not profile_cfg.get("run_stopped_at"):
            revived += _revive_profile_queue(db, profile_id=profile.id)
    if revived:
        db.commit()
    collect_plan_by_profile = {p.id: _active_collect_plan(db, profile_id=p.id) for p in all_profiles}

    # ── Global totals ────────────────────────────────────────────────────────
    all_ids = [p.id for p in all_profiles]
    total_pub  = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id.in_(all_ids), Post.status == PostStatus.completed)) or 0) if all_ids else 0
    total_pend_real = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id.in_(all_ids), Post.status.in_([PostStatus.pending, PostStatus.processing]))) or 0) if all_ids else 0
    total_pend_planned = sum(int(plan.get("missing") or 0) for plan in collect_plan_by_profile.values())
    total_pend = total_pend_real + total_pend_planned
    total_fail = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id.in_(all_ids), Post.status == PostStatus.failed)) or 0) if all_ids else 0
    worker_inline_enabled = os.getenv("POSTHUB_INLINE_WORKER", "1") != "0"
    cron_secret_configured = bool((os.getenv("CRON_SECRET") or "").strip())
    worker_tick_url = str(request.base_url).rstrip("/") + "/api/worker/tick"
    worker_curl = f'curl -X GET "{worker_tick_url}" -H "Authorization: Bearer <CRON_SECRET>"'
    worker_fetch = (
        "fetch("
        + json.dumps(worker_tick_url)
        + ", {headers:{Authorization:'Bearer ' + process.env.CRON_SECRET}})"
    )

    # ── Summary bar ─────────────────────────────────────────────────────────
    summary_bar = f"""
    <div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px">
      <div style="flex:1;min-width:140px;padding:16px 20px;background:var(--surface);border:1px solid rgba(16,185,129,.25);border-radius:14px;display:flex;align-items:center;gap:12px">
        <div style="width:40px;height:40px;border-radius:10px;background:rgba(16,185,129,.15);display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0">✓</div>
        <div><div style="font-size:26px;font-weight:800;color:#10b981;line-height:1">{total_pub}</div><div style="font-size:11px;color:var(--muted);margin-top:2px;text-transform:uppercase;letter-spacing:.6px">Publicados</div></div>
      </div>
      <div style="flex:1;min-width:140px;padding:16px 20px;background:var(--surface);border:1px solid rgba(245,158,11,.25);border-radius:14px;display:flex;align-items:center;gap:12px">
        <div style="width:40px;height:40px;border-radius:10px;background:rgba(245,158,11,.15);display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0">⏳</div>
        <div><div style="font-size:26px;font-weight:800;color:#f59e0b;line-height:1">{total_pend}</div><div style="font-size:11px;color:var(--muted);margin-top:2px;text-transform:uppercase;letter-spacing:.6px">Pendentes</div></div>
      </div>
      <div style="flex:1;min-width:140px;padding:16px 20px;background:var(--surface);border:1px solid rgba(239,68,68,.25);border-radius:14px;display:flex;align-items:center;gap:12px">
        <div style="width:40px;height:40px;border-radius:10px;background:rgba(239,68,68,.15);display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0">✕</div>
        <div><div style="font-size:26px;font-weight:800;color:#ef4444;line-height:1">{total_fail}</div><div style="font-size:11px;color:var(--muted);margin-top:2px;text-transform:uppercase;letter-spacing:.6px">Falhas</div></div>
      </div>
      <div style="flex:1;min-width:140px;padding:16px 20px;background:var(--surface);border:1px solid var(--border);border-radius:14px;display:flex;align-items:center;gap:12px">
        <div style="width:40px;height:40px;border-radius:10px;background:var(--surface2);display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0">🤖</div>
        <div><div style="font-size:26px;font-weight:800;color:var(--text);line-height:1">{len(all_profiles)}</div><div style="font-size:11px;color:var(--muted);margin-top:2px;text-transform:uppercase;letter-spacing:.6px">Projetos</div></div>
      </div>
    </div>"""

    # ── Flash message ────────────────────────────────────────────────────────
    flash_msg = (request.query_params.get("msg") or "").strip()
    flash_html = f"<div class='card' style='border-color:rgba(99,102,241,.4);margin-bottom:14px'><b>{html.escape(flash_msg)}</b></div>" if flash_msg else ""
    now_utc = datetime.utcnow()
    worker_notice = ""
    if not worker_inline_enabled:
        worker_notice = """
        <div class="card" style="margin-bottom:14px;border-color:rgba(245,158,11,.45);background:rgba(245,158,11,.06)">
          <div style="display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap">
            <div style="width:38px;height:38px;border-radius:10px;background:rgba(245,158,11,.16);display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0">&#9888;</div>
            <div style="flex:1;min-width:260px">
              <div style="font-size:14px;font-weight:800;color:#f59e0b">Fila em modo serverless</div>
              <div style="font-size:12px;color:var(--muted);margin-top:4px;line-height:1.6">
                Neste deploy o worker inline est&#225; desligado. Sem cron externo, a fila anda melhor com esta tela aberta ou usando o bot&#227;o <b>Rodar agora</b>.
                No plano Hobby da Vercel, o cron nativo n&#227;o pode rodar a cada minuto.
              </div>
            </div>
          </div>
        </div>"""
    cron_helper = f"""
    <details class="card toggle-section" data-persist-toggle="worker-cron-help" style="margin-bottom:14px;padding:0;overflow:hidden">
      <summary style="padding:14px 18px">
        <span class="ts-title" style="display:flex;align-items:center;gap:8px">
          <span style="width:24px;height:24px;border-radius:6px;background:rgba(16,185,129,.12);display:inline-flex;align-items:center;justify-content:center">&#9201;</span>
          Cron externo do worker
          <span class="ts-badge" style="color:{'#10b981' if cron_secret_configured else '#ef4444'};border-color:{'rgba(16,185,129,.3)' if cron_secret_configured else 'rgba(239,68,68,.3)'}">
            {'CRON_SECRET ok' if cron_secret_configured else 'CRON_SECRET faltando'}
          </span>
        </span>
        <span class="ts-arrow">&#9658;</span>
      </summary>
      <div class="ts-body" style="padding:14px 18px 16px">
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px">
          <div>
            <div style="font-size:13px;font-weight:700;margin-bottom:6px">URL para agendar</div>
            <textarea readonly style="min-height:68px;font-size:12px;background:var(--surface2)">{html.escape(worker_tick_url)}</textarea>
            <div style="margin-top:8px;font-size:11px;color:var(--muted)">Agende uma chamada GET a cada 1 minuto em um provedor externo como cron-job.org, EasyCron, Pipedream ou Uptime Kuma. Neste repositório tamb&#233;m existe um workflow pronto do GitHub Actions para rodar a cada 5 minutos.</div>
          </div>
          <div>
            <div style="font-size:13px;font-weight:700;margin-bottom:6px">Header obrigat&#243;rio</div>
            <textarea readonly style="min-height:68px;font-size:12px;background:var(--surface2)">Authorization: Bearer &lt;CRON_SECRET&gt;</textarea>
            <div style="margin-top:8px;font-size:11px;color:var(--muted)">Use o valor salvo em <code>CRON_SECRET</code> no Vercel. Quando essa vari&#225;vel existe, o endpoint exige o token no header.</div>
          </div>
          <div>
            <div style="font-size:13px;font-weight:700;margin-bottom:6px">Exemplo cURL</div>
            <textarea readonly style="min-height:90px;font-size:12px;background:var(--surface2)">{html.escape(worker_curl)}</textarea>
          </div>
          <div>
            <div style="font-size:13px;font-weight:700;margin-bottom:6px">Exemplo JavaScript</div>
            <textarea readonly style="min-height:90px;font-size:12px;background:var(--surface2)">{html.escape(worker_fetch)}</textarea>
          </div>
        </div>
        <div style="margin-top:10px;font-size:11px;color:var(--muted);line-height:1.6">
          Cada chamada do worker processa a fila em loop por at&#233; 50 segundos. Nos novos posts, o pipeline tamb&#233;m ficou mais curto e n&#227;o cria mais a etapa separada de m&#237;dia. Para usar o workflow do GitHub Actions, defina os secrets <code>POSTHUB_WORKER_TICK_URL</code> e <code>POSTHUB_CRON_SECRET</code>.
        </div>
      </div>
    </details>
    """
    help_menu = f"""
    <details class="card toggle-section" data-persist-toggle="help-menu-posts" style="margin-bottom:14px;padding:0;overflow:hidden">
      <summary style="padding:14px 18px">
        <span class="ts-title" style="display:flex;align-items:center;gap:8px">
          <span style="width:24px;height:24px;border-radius:6px;background:rgba(99,102,241,.12);display:inline-flex;align-items:center;justify-content:center">&#8505;</span>
          Ajuda do menu Posts
        </span>
        <span class="ts-arrow">&#9658;</span>
      </summary>
      <div class="ts-body" style="padding:14px 18px 16px">
        {_ph("help-menu-posts")}
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px;font-size:12px;color:var(--muted);line-height:1.55">
          <div><b style="color:var(--text)">Abrir bot</b><br>O cabeçalho de cada bot é um toggle. Clique nele para mostrar ou ocultar as ações e listas.</div>
          <div><b style="color:var(--text)">Rodar agora</b><br>Libera jobs pendentes/agendados. Fica desativado quando não existe fila para rodar.</div>
          <div><b style="color:var(--text)">Reprocessar IA</b><br>Reagenda posts com falha para a IA tentar novamente. Só ativa quando há falhas.</div>
          <div><b style="color:var(--text)">Cancelar pendentes</b><br>Cancela posts pendentes ou em processamento do bot. Só ativa quando há pendências.</div>
          <div><b style="color:var(--text)">Apagar publicados</b><br>Remove publicados do PostHub. Use "Apagar do WordPress" para remover também do site.</div>
          <div><b style="color:var(--text)">Corrigir</b><br>Reprocessa o texto de um post publicado e atualiza o artigo existente no WordPress.</div>
          <div><b style="color:var(--text)">Logs</b><br>Fica oculto por padrão. Abra apenas quando quiser acompanhar etapas e erros.</div>
        </div>
      </div>
    </details>
    """

    # ── Table helper ─────────────────────────────────────────────────────────
    def _ptable(tid, rows, empty_msg, last_col="Link", extra_col: str | None = None, extra_col2: str | None = None):
        th_style = "padding:10px 14px;text-align:left;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)"
        thead = (
            "<tr style='background:var(--surface2)'>"
            f"<th style='{th_style};width:36px'><input type='checkbox' style='width:14px;height:14px;cursor:pointer' onclick=\"var _h=this;var _t=document.getElementById('" + tid + "');_t.querySelectorAll('tbody input[name=post_id]').forEach(function(c){c.checked=_h.checked;});_phUpdateCount('" + tid + "');\"></th>"
            f"<th style='{th_style}'>T\u00edtulo</th>"
            f"<th style='{th_style}'>Status</th>"
            f"<th style='{th_style}'>Quando</th>"
            f"<th style='{th_style}'>{html.escape(last_col)}</th>"
            + (f"<th style='{th_style}'>{html.escape(extra_col)}</th>" if extra_col else "")
            + (f"<th style='{th_style}'>{html.escape(extra_col2)}</th>" if extra_col2 else "")
            + "</tr>"
        )
        colspan = 5 + (1 if extra_col else 0) + (1 if extra_col2 else 0)
        body_rows = rows or f"<tr><td colspan='{colspan}' style='padding:20px;text-align:center;color:var(--muted);font-size:13px'>{empty_msg}</td></tr>"
        # Add onchange to each checkbox in the rows
        if rows:
            body_rows = body_rows.replace(
                "name='post_id'",
                f"name='post_id' onchange=\"_phUpdateCount('{tid}')\""
            )
        return (f"<div style='border:1px solid var(--border);border-radius:12px;overflow:hidden'>"
                f"<div style='overflow-x:auto;-webkit-overflow-scrolling:touch'>"
                f"<table id='{tid}' style='width:100%;border-collapse:collapse;min-width:520px'>"
                f"<thead>{thead}</thead><tbody>{body_rows}</tbody></table></div></div>"
                f"<div id='cnt-{tid}' style='font-size:11px;color:var(--muted);margin-top:5px;min-height:16px'></div>")

    # ── Per-bot sections ─────────────────────────────────────────────────────
    bot_sections = ""
    for pr in all_profiles:
        pr_emoji = _safe((pr.publish_config_json or {}).get("emoji") or "🤖")
        pr_name  = html.escape(pr.name)
        pr_id    = html.escape(pr.id)

        # WP domain
        wp_url = ""
        wp_integ = db.scalar(select(Integration).where(Integration.profile_id == pr.id, Integration.type == IntegrationType.WORDPRESS))
        if wp_integ:
            try:
                creds_d = decrypt_json(wp_integ.credentials_encrypted)
                wp_url = (creds_d.get("base_url") or "") if isinstance(creds_d, dict) else ""
            except Exception:
                pass
        wp_domain = wp_url.replace("https://","").replace("http://","").rstrip("/") if wp_url else ""

        # counts
        c_pub  = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == pr.id, Post.status == PostStatus.completed)) or 0)
        c_pend_real = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == pr.id, Post.status.in_([PostStatus.pending, PostStatus.processing]))) or 0)
        collect_plan = collect_plan_by_profile.get(pr.id) or {}
        c_pend_planned = int(collect_plan.get("missing") or 0)
        c_pend = c_pend_real + c_pend_planned
        c_fail = int(db.scalar(select(func.count()).select_from(Post).where(Post.profile_id == pr.id, Post.status == PostStatus.failed)) or 0)
        _b_qd = int(db.scalar(select(func.count()).select_from(Job).where(
            Job.profile_id == pr.id, Job.status == JobStatus.queued)) or 0)
        _b_rj = int(db.scalar(select(func.count()).select_from(Job).where(
            Job.profile_id == pr.id, Job.status == JobStatus.running)) or 0)

        # load posts per category
        def _load(statuses, limit=200):
            return list(db.execute(
                select(Post, CollectedContent.title, CollectedContent.canonical_url)
                .join(CollectedContent, CollectedContent.id == Post.collected_content_id)
                .where(Post.profile_id == pr.id, Post.status.in_(statuses))
                .order_by(Post.published_at.desc().nullslast(), Post.created_at.desc())
                .limit(limit)
            ).all())

        pub_posts   = _load([PostStatus.completed])
        pend_posts  = _load([PostStatus.pending, PostStatus.processing])
        fail_posts  = _load([PostStatus.failed])

        def _pending_timer_html(p_obj: Post) -> str:
            stage_labels = {
                JOB_COLLECT: "coleta",
                JOB_CLEAN: "limpeza",
                JOB_AI: "IA",
                JOB_MEDIA: "mídia",
                JOB_PUBLISH_WP: "publicação",
            }
            elapsed_ms = int(p_obj.created_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
            elapsed_row = (
                "<div style='font-size:10px;color:var(--muted);margin-top:3px;white-space:nowrap'>"
                f"há <span data-ph-elapsed-since='{elapsed_ms}'>...</span></div>"
            )
            running_job = db.scalar(
                select(Job)
                .where(Job.post_id == p_obj.id, Job.status == JobStatus.running)
                .order_by(Job.updated_at.desc())
                .limit(1)
            )
            if running_job:
                stage = html.escape(stage_labels.get(running_job.type, running_job.type))
                main = (
                    "<span style='display:inline-flex;align-items:center;gap:6px;color:#6366f1;font-size:11px;font-weight:700;"
                    "background:rgba(99,102,241,.12);padding:3px 8px;border-radius:20px;white-space:nowrap'>"
                    f"<span class='dot-pulse'></span>{stage} agora</span>"
                )
                return f"<div>{main}{elapsed_row}</div>"
            queued_job = db.scalar(
                select(Job)
                .where(Job.post_id == p_obj.id, Job.status == JobStatus.queued)
                .order_by(Job.run_at.asc())
                .limit(1)
            )
            if queued_job:
                stage = html.escape(stage_labels.get(queued_job.type, queued_job.type))
                run_at = queued_job.run_at or now_utc
                if run_at <= now_utc:
                    main = (
                        "<span style='display:inline-flex;align-items:center;gap:6px;color:#10b981;font-size:11px;font-weight:700;"
                        "background:rgba(16,185,129,.10);padding:3px 8px;border-radius:20px;white-space:nowrap'>"
                        f"Na fila</span>"
                    )
                    return f"<div>{main}{elapsed_row}</div>"
                target_ms = int(run_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
                main = (
                    "<span style='display:inline-flex;flex-direction:column;gap:1px;color:var(--text);font-size:11px;white-space:nowrap'>"
                    f"<span style='color:#f59e0b;font-weight:700'>Próxima: {stage}</span>"
                    f"<span>em <b data-ph-countdown-target='{target_ms}'>...</b></span>"
                    "</span>"
                )
                return f"<div>{main}{elapsed_row}</div>"
            if p_obj.status == PostStatus.processing:
                main = "<span style='font-size:11px;color:var(--muted);white-space:nowrap'>Finalizando...</span>"
                return f"<div>{main}{elapsed_row}</div>"
            main = "<span style='font-size:11px;color:var(--muted);white-space:nowrap'>Aguardando fila</span>"
            return f"<div>{main}{elapsed_row}</div>"

        def _build_pub_rows(items):
            out = ""
            for row in items:
                p_obj = row[0]
                title = row[1]
                canonical_url = row[2] if len(row) > 2 else None
                t = html.escape(str(title or "")[:80] + ("\u2026" if len(str(title or "")) > 80 else ""))
                dt = html.escape(_fmt_dt(p_obj.published_at or p_obj.created_at, user=user))
                dt_iso = (p_obj.published_at or p_obj.created_at or "").isoformat() if (p_obj.published_at or p_obj.created_at) else ""
                chk = f"<input type='checkbox' name='post_id' value='{html.escape(p_obj.id)}' style='width:14px;height:14px;cursor:pointer'/>"
                wp_link = (f"<a href='{html.escape(p_obj.wp_url)}' target='_blank' rel='noopener' "
                           f"style='display:inline-flex;align-items:center;gap:4px;color:#10b981;font-size:12px;font-weight:600;text-decoration:none'>"
                           f"&#8599; Ver</a>") if p_obj.wp_url else "<span style='color:var(--muted);font-size:12px'>\u2014</span>"
                src_link = (f"<a href='{html.escape(str(canonical_url))}' target='_blank' rel='noopener' "
                            f"style='display:inline-flex;align-items:center;gap:4px;color:#6366f1;font-size:12px;text-decoration:none;max-width:200px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis' "
                            f"title='{html.escape(str(canonical_url))}'>"
                            f"&#8599; Fonte</a>") if canonical_url else "<span style='color:var(--muted);font-size:12px'>\u2014</span>"
                _correct_url = html.escape(f"/app/profiles/{pr_id}/posts/{p_obj.id}/correct")
                correct_btn = (
                    f"<button class='btn flat' type='button' "
                    f"style='font-size:11px;padding:4px 10px;color:#f59e0b;border-color:rgba(245,158,11,.35);background:transparent' "
                    f"onclick=\"if(confirm('Reprocessar o texto deste post e atualizar no WordPress?')){{var f=document.createElement('form');f.method='post';f.action='{_correct_url}';document.body.appendChild(f);f.submit();}}\">Corrigir</button>"
                )
                out += (f"<tr style='border-top:1px solid rgba(16,185,129,.12);border-left:3px solid #10b981;background:rgba(16,185,129,.03)' data-pub-date='{html.escape(dt_iso)}'>"
                        f"<td style='padding:10px 14px;width:36px'>{chk}</td>"
                        f"<td style='padding:10px 14px'><div style='display:flex;align-items:center;gap:8px'>"
                        f"<span style='width:20px;height:20px;border-radius:50%;background:#10b981;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:10px;color:#fff'>✓</span>"
                        f"<span style='font-size:13px;font-weight:500;color:var(--text)'>{t}</span></div></td>"
                        f"<td style='padding:10px 14px'><span style='color:#10b981;font-size:11px;font-weight:700;background:rgba(16,185,129,.12);padding:3px 8px;border-radius:20px'>✓ Publicado</span></td>"
                        f"<td style='padding:10px 14px;font-size:12px;color:var(--muted);white-space:nowrap'>{dt}</td>"
                        f"<td style='padding:10px 14px'>{wp_link}</td>"
                        f"<td style='padding:10px 14px'>{src_link}</td>"
                        f"<td style='padding:10px 14px'>{correct_btn}</td></tr>")
            return out

        def _build_pend_rows(items):
            out = ""
            for row in items:
                p_obj, title = row[0], row[1]
                t = html.escape(str(title or "")[:80] + ("\u2026" if len(str(title or "")) > 80 else ""))
                dt = html.escape(_fmt_dt(p_obj.created_at, user=user))
                chk = f"<input type='checkbox' name='post_id' value='{html.escape(p_obj.id)}' style='width:14px;height:14px;cursor:pointer'/>"
                is_proc = p_obj.status == PostStatus.processing
                badge_color = "#6366f1" if is_proc else "#f59e0b"
                badge_bg = "rgba(99,102,241,.12)" if is_proc else "rgba(245,158,11,.12)"
                badge_lbl = "\u26a1 Processando" if is_proc else "\u23f3 Pendente"
                timer_html = _pending_timer_html(p_obj)
                out += (f"<tr style='border-top:1px solid var(--border)'>"
                        f"<td style='padding:10px 14px;width:36px'>{chk}</td>"
                        f"<td style='padding:10px 14px;font-size:13px;color:var(--text)'>{t}</td>"
                        f"<td style='padding:10px 14px'><span style='color:{badge_color};font-size:11px;font-weight:700;background:{badge_bg};padding:3px 8px;border-radius:20px'>{badge_lbl}</span></td>"
                        f"<td style='padding:10px 14px;font-size:12px;color:var(--muted);white-space:nowrap'>{dt}</td>"
                        f"<td style='padding:10px 14px;font-size:12px;color:var(--muted)'>{timer_html}</td></tr>")
            return out

        def _build_collect_plan_rows(count: int, plan: dict) -> str:
            if count <= 0:
                return ""
            next_run = plan.get("next_run")
            interval = int(plan.get("interval_minutes") or 0)
            rows = ""
            for idx in range(count):
                if isinstance(next_run, datetime) and next_run > now_utc:
                    target = next_run + timedelta(minutes=interval * idx if interval else 0)
                    target_ms = int(target.replace(tzinfo=timezone.utc).timestamp() * 1000)
                    tempo = f"coleta em <b data-ph-countdown-target='{target_ms}'>...</b>"
                    when = html.escape(_fmt_dt(target, user=user))
                elif plan.get("running"):
                    tempo = "coletando fontes agora"
                    when = "agora"
                else:
                    tempo = "na fila de coleta"
                    when = "agora"
                cadence = f"<div style='font-size:11px;color:var(--muted);margin-top:3px'>Cadencia: {interval} min entre posts.</div>" if interval else ""
                rows += (
                    "<tr style='border-top:1px solid var(--border);background:rgba(245,158,11,.05)'>"
                    "<td style='padding:10px 14px;width:36px'>"
                    "<input type='checkbox' disabled style='width:14px;height:14px;opacity:.35'/></td>"
                    f"<td style='padding:10px 14px;font-size:13px;color:var(--text)'><b>Post planejado {idx + 1} de {count}</b>"
                    "<div style='font-size:11px;color:var(--muted);margin-top:3px'>"
                    "Ainda em coleta. Vai virar item real assim que uma receita valida for encontrada.</div>"
                    f"{cadence}</td>"
                    "<td style='padding:10px 14px'><span style='color:#f59e0b;font-size:11px;font-weight:700;"
                    "background:rgba(245,158,11,.12);padding:3px 8px;border-radius:20px'>"
                    "Planejado</span></td>"
                    f"<td style='padding:10px 14px;font-size:12px;color:var(--muted);white-space:nowrap'>{when}</td>"
                    f"<td style='padding:10px 14px;font-size:12px;color:var(--muted);white-space:nowrap'>{tempo}</td></tr>"
                )
            return rows

        def _build_fail_rows(items):
            out = ""
            for row in items:
                p_obj, title = row[0], row[1]
                t = html.escape(str(title or "")[:80] + ("\u2026" if len(str(title or "")) > 80 else ""))
                dt = html.escape(_fmt_dt(p_obj.created_at, user=user))
                chk = f"<input type='checkbox' name='post_id' value='{html.escape(p_obj.id)}' style='width:14px;height:14px;cursor:pointer'/>"
                is_canceled = isinstance(p_obj.outputs_json, dict) and bool(p_obj.outputs_json.get("canceled_by_user"))
                err_msg = ""
                if isinstance(p_obj.outputs_json, dict):
                    err_msg = str(p_obj.outputs_json.get("error") or "")[:80]
                badge_lbl = "Cancelado" if is_canceled else "Erro"
                err_div = f"<div style='font-size:11px;color:#ef4444;margin-top:3px;padding-left:28px'>{html.escape(err_msg)}</div>" if err_msg else ""
                out += (f"<tr style='border-top:1px solid rgba(239,68,68,.12);border-left:3px solid #ef4444;background:rgba(239,68,68,.03)'>"
                        f"<td style='padding:10px 14px;width:36px'>{chk}</td>"
                        f"<td style='padding:10px 14px'><div>"
                        f"<div style='display:flex;align-items:center;gap:8px'>"
                        f"<span style='width:20px;height:20px;border-radius:50%;background:#ef4444;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:10px;color:#fff'>\u00d7</span>"
                        f"<span style='font-size:13px;font-weight:500;color:var(--text)'>{t}</span></div>{err_div}</div></td>"
                        f"<td style='padding:10px 14px'><span style='color:#ef4444;font-size:11px;font-weight:700;background:rgba(239,68,68,.12);padding:3px 8px;border-radius:20px'>{badge_lbl}</span></td>"
                        f"<td style='padding:10px 14px;font-size:12px;color:var(--muted);white-space:nowrap'>{dt}</td>"
                        f"<td style='padding:10px 14px;font-size:12px;color:var(--muted)'>\u2014</td></tr>")
            return out

        pub_rows  = _build_pub_rows(pub_posts)
        pend_rows = _build_collect_plan_rows(c_pend_planned, collect_plan) + _build_pend_rows(pend_posts)
        fail_rows = _build_fail_rows(fail_posts)

        # bot header
        if pr.active:
            status_badge = "<span style='display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:700;color:#10b981;background:rgba(16,185,129,.12);border:1px solid rgba(16,185,129,.3);border-radius:20px;padding:2px 10px'><span class='dot-pulse'></span>Online</span>"
        else:
            status_badge = "<span style='font-size:11px;font-weight:600;color:var(--muted);background:var(--surface2);border:1px solid var(--border);border-radius:20px;padding:2px 10px'>Inativo</span>"

        icon_bg = "linear-gradient(135deg,#10b981,#059669)" if pr.active else "linear-gradient(135deg,var(--primary),var(--pink))"
        proc_badge = (f'<span style="display:flex;flex-direction:column;align-items:center;gap:1px">'
                      f'<span style="font-size:15px;font-weight:800;color:#6366f1">{c_pend}</span>'
                      f'<span style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Pend.</span></span>') if True else ""
        can_run_now = _b_qd > 0 or _b_rj > 0 or c_pend > 0
        can_retry_ai = c_fail > 0
        can_cancel_pending = c_pend > 0
        can_delete_completed = c_pub > 0
        run_disabled = "" if can_run_now else "disabled"
        retry_disabled = "" if can_retry_ai else "disabled"
        cancel_disabled = "" if can_cancel_pending else "disabled"
        del_completed_disabled = "" if can_delete_completed else "disabled"
        run_style = "" if can_run_now else "opacity:.45;cursor:not-allowed;"
        retry_style = "" if can_retry_ai else "opacity:.45;cursor:not-allowed;"
        cancel_style = "" if can_cancel_pending else "opacity:.45;cursor:not-allowed;"
        del_completed_style = "" if can_delete_completed else "opacity:.45;cursor:not-allowed;"
        pub_action_disabled = "" if pub_posts else "disabled"
        pend_action_disabled = "" if pend_posts else "disabled"
        fail_action_disabled = "" if fail_posts else "disabled"
        pub_action_style = "" if pub_posts else "opacity:.45;cursor:not-allowed;"
        pend_action_style = "" if pend_posts else "opacity:.45;cursor:not-allowed;"
        fail_action_style = "" if fail_posts else "opacity:.45;cursor:not-allowed;"

        bot_sections += _ph(f"bot-section-{pr_id}") + f"""
    <details class="card toggle-section" data-persist-toggle="bot-{pr_id}" data-default-open="1" style="margin-bottom:20px;padding:0;overflow:hidden">
      <summary style="display:flex;align-items:center;justify-content:space-between;padding:16px 20px;flex-wrap:wrap;gap:10px">
        <div style="display:flex;align-items:center;gap:12px">
          <div style="width:40px;height:40px;border-radius:10px;background:{icon_bg};display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0">{pr_emoji}</div>
          <div>
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
              <span style="font-weight:700;font-size:15px">{pr_name}</span>
              {status_badge}
            </div>
            {f'<div style="font-size:11px;color:var(--muted);margin-top:2px">{html.escape(wp_domain)}</div>' if wp_domain else ''}
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
          <div style="display:flex;gap:12px">
            <div style="text-align:center"><div style="font-size:17px;font-weight:800;color:#10b981">{c_pub}</div><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Publicados</div></div>
            <div style="text-align:center"><div style="font-size:17px;font-weight:800;color:#f59e0b">{c_pend}</div><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Pendentes</div></div>
            <div style="text-align:center"><div style="font-size:17px;font-weight:800;color:#ef4444">{c_fail}</div><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Falhas</div></div>
          </div>
          <span class="ts-arrow">&#9658;</span>
        </div>
      </summary>
      <div style="border-top:1px solid var(--border)">
        <div style="display:flex;gap:6px;flex-wrap:wrap;padding:12px 20px;border-bottom:1px solid var(--border);background:var(--surface2)">
            <button class="btn secondary ph-run-now-btn" {run_disabled} data-bot-id="{pr_id}" title="Libera e processa jobs pendentes deste bot agora" style="font-size:11px;padding:5px 10px;{run_style}">&#9654; Rodar agora</button>
            <form method="post" action="/app/robot/retry-ai" style="margin:0">
              <input type="hidden" name="bot_id" value="{pr_id}">
              <button class="btn secondary" type="submit" {retry_disabled} title="Reprocessa posts com falha" style="font-size:11px;padding:5px 10px;{retry_style}">&#8634; Reprocessar IA ({c_fail})</button>
            </form>
            <form method="post" action="/app/profiles/{pr_id}/posts/cancel-all" style="margin:0">
              <button class="btn secondary" type="submit" {cancel_disabled} style="font-size:11px;padding:5px 10px;{cancel_style}" title="Cancelar todos os posts pendentes deste bot">Cancelar pendentes</button>
            </form>
            <form method="post" action="/app/profiles/{pr_id}/posts/delete-completed" style="margin:0">
              <button class="btn secondary" type="submit" {del_completed_disabled} style="font-size:11px;padding:5px 10px;{del_completed_style}" title="Apagar todos os publicados deste bot do PostHub">Apagar publicados</button>
            </form>
        </div>
      <!-- Publicados -->
      <details class="toggle-section" data-persist-toggle="bot-pub-{pr_id}" data-default-open="{'1' if pub_posts else '0'}"  >
        <summary style="padding:12px 20px;border-bottom:1px solid var(--border)">
          <span class="ts-title" style="display:flex;align-items:center;gap:8px">
            <span style="width:22px;height:22px;border-radius:6px;background:rgba(16,185,129,.15);display:inline-flex;align-items:center;justify-content:center;font-size:11px">✓</span>
            Publicados
            <span class="ts-badge" style="color:#10b981;border-color:rgba(16,185,129,.3)">{c_pub}</span>
            <a href="/app/history?bot={pr_id}" onclick="event.stopPropagation()" style="font-size:11px;padding:3px 9px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;text-decoration:none;color:var(--muted);display:inline-flex;align-items:center;gap:4px;white-space:nowrap">&#128203; Hist&#243;rico</a>
          </span>
          <span class="ts-arrow">▶</span>
        </summary>
        <div class="ts-body" style="padding:14px 20px">
          <div style="display:flex;justify-content:flex-end;margin-bottom:8px">
            <button type="button" id="sort-pub-{pr_id}" onclick="(function(){{var tid='tbl-pub-{pr_id}';var btn=document.getElementById('sort-pub-{pr_id}');var tb=document.getElementById(tid).querySelector('tbody');var rows=Array.from(tb.querySelectorAll('tr[data-pub-date]'));var asc=btn.dataset.sortAsc==='1';rows.sort(function(a,b){{var da=a.getAttribute('data-pub-date')||'';var db=b.getAttribute('data-pub-date')||'';return asc?(da<db?-1:da>db?1:0):(da>db?-1:da<db?1:0);}});rows.forEach(function(r){{tb.appendChild(r);}});btn.dataset.sortAsc=asc?'0':'1';btn.innerHTML=asc?'&#8593; Data (mais antigo)':'&#8595; Data (mais recente)';}})()" data-sort-asc="0"
              style="font-size:11px;padding:4px 10px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;cursor:pointer;color:var(--muted);display:flex;align-items:center;gap:4px">&#8595; Data (mais recente)</button>
          </div>
          <form id="bulk-pub-form-{pr_id}" method="post" action="/app/profiles/{pr_id}/posts/bulk">
            <input type="hidden" name="mode" value="delete">
            {_ptable(f"tbl-pub-{pr_id}", pub_rows, "Nenhum post publicado ainda.", "Link", "Fonte", "Corrigir")}
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center">
              <button class="btn secondary" type="button" {pub_action_disabled} style="font-size:12px;padding:5px 12px;{pub_action_style}"
                onclick="(function(){{var n=document.querySelectorAll('#tbl-pub-{pr_id} input[name=post_id]:checked').length;if(!n){{alert('Selecione ao menos um post marcando a caixa na esquerda.');return;}}if(!confirm('Excluir '+n+' post(s) do PostHub? O artigo no WordPress \u00e9 mantido.'))return;document.getElementById('bulk-pub-form-{pr_id}').querySelector('input[name=mode]').value='delete';document.getElementById('bulk-pub-form-{pr_id}').submit();}})()">Excluir selecionados (PostHub)</button>
              <button class="btn flat" type="button" {pub_action_disabled} style="font-size:12px;padding:5px 12px;color:#ef4444;border-color:rgba(239,68,68,.45);background:transparent;{pub_action_style}"
                onclick="(function(){{var n=document.querySelectorAll('#tbl-pub-{pr_id} input[name=post_id]:checked').length;if(!n){{alert('Selecione ao menos um post marcando a caixa na esquerda.');return;}}if(!confirm('Apagar '+n+' post(s) do WordPress? Esta a\u00e7\u00e3o n\u00e3o pode ser desfeita.'))return;document.getElementById('bulk-pub-form-{pr_id}').querySelector('input[name=mode]').value='delete_wp';document.getElementById('bulk-pub-form-{pr_id}').submit();}})()">&#128465; Apagar do WordPress</button>
            </div>
          </form>
        </div>
      </details>
      <!-- Pendentes -->
      <details class="toggle-section" data-persist-toggle="bot-pend-{pr_id}" data-default-open="{'1' if pend_posts else '0'}" style="border-top:1px solid var(--border)">
        <summary style="padding:12px 20px;border-bottom:1px solid var(--border)">
          <span class="ts-title" style="display:flex;align-items:center;gap:8px">
            <span style="width:22px;height:22px;border-radius:6px;background:rgba(245,158,11,.15);display:inline-flex;align-items:center;justify-content:center;font-size:11px">⏳</span>
            Pendentes / Processando
            <span class="ts-badge" style="color:#f59e0b;border-color:rgba(245,158,11,.3)">{c_pend}</span>
          </span>
          <span class="ts-arrow">▶</span>
        </summary>
        <div class="ts-body" style="padding:14px 20px">
          <form method="post" action="/app/profiles/{pr_id}/posts/bulk">
            {_ptable(f"tbl-pend-{pr_id}", pend_rows, "Nenhum post pendente.", "Tempo")}
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center">
              <button class="btn secondary" type="submit" name="mode" value="cancel" {pend_action_disabled} style="font-size:12px;padding:5px 12px;{pend_action_style}">Cancelar selecionados</button>
              <button class="btn secondary" type="submit" name="mode" value="delete" {pend_action_disabled} style="font-size:12px;padding:5px 12px;{pend_action_style}">Excluir selecionados</button>
            </div>
          </form>
          <form method="post" action="/app/profiles/{pr_id}/posts/cancel-all" style="margin-top:6px">
            <button class="btn flat" type="submit" {pend_action_disabled} style="font-size:11px;padding:4px 10px;color:#ef4444;border-color:rgba(239,68,68,.3);{pend_action_style}" onclick="return confirm('Cancelar todos os pendentes?')">&#128465; Cancelar todos pendentes</button>
          </form>
        </div>
      </details>
      <!-- Falhas -->
      <details class="toggle-section" data-persist-toggle="bot-fail-{pr_id}" data-default-open="{'1' if fail_posts else '0'}" style="border-top:1px solid var(--border)">
        <summary style="padding:12px 20px">
          <span class="ts-title" style="display:flex;align-items:center;gap:8px">
            <span style="width:22px;height:22px;border-radius:6px;background:rgba(239,68,68,.15);display:inline-flex;align-items:center;justify-content:center;font-size:11px">✕</span>
            Falhas
            <span class="ts-badge" style="color:#ef4444;border-color:rgba(239,68,68,.3)">{c_fail}</span>
          </span>
          <span class="ts-arrow">▶</span>
        </summary>
        <div class="ts-body" style="padding:14px 20px">
          <form method="post" action="/app/profiles/{pr_id}/posts/bulk">
            {_ptable(f"tbl-fail-{pr_id}", fail_rows, "Nenhuma falha registrada.")}
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center">
              <button class="btn secondary" type="submit" name="mode" value="delete" {fail_action_disabled} style="font-size:12px;padding:5px 12px;{fail_action_style}">Excluir selecionados</button>
            </div>
          </form>
          <form method="post" action="/app/profiles/{pr_id}/posts/delete-failed" style="margin-top:6px">
            <button class="btn flat" type="submit" {fail_action_disabled} style="font-size:11px;padding:4px 10px;color:#ef4444;border-color:rgba(239,68,68,.3);{fail_action_style}" onclick="return confirm('Excluir todas as falhas?')">&#128465; Excluir todas falhas</button>
          </form>
        </div>
      </details>
      <!-- Live Log -->
      <details class="toggle-section" data-persist-toggle="bot-log-{pr_id}" data-default-open="0" style="border-top:1px solid var(--border)">
        <summary style="padding:12px 20px">
          <span class="ts-title" style="display:flex;align-items:center;gap:8px">
            <span style="width:22px;height:22px;border-radius:6px;background:rgba(99,102,241,.12);display:inline-flex;align-items:center;justify-content:center;font-size:11px">&#9889;</span>
            Log de atividade
          </span>
          <span class="ts-arrow">&#9658;</span>
        </summary>
        <div class="ts-body" style="padding:0">
          <div id="livelog-{pr_id}" style="overflow-x:auto;-webkit-overflow-scrolling:touch">
            <table style="width:100%;border-collapse:collapse;font-size:12px;min-width:480px">
              <thead><tr style="background:var(--surface2)">
                <th style="padding:8px 12px;text-align:left;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.5px">T\u00edtulo / URL</th>
                <th style="padding:8px 12px;text-align:left;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.5px">Etapa</th>
                <th style="padding:8px 12px;text-align:left;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.5px">Status</th>
                <th style="padding:8px 12px;text-align:left;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.5px">Hor\u00e1rio</th>
                <th style="padding:8px 12px;text-align:left;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.5px">Dura\u00e7\u00e3o</th>
              </tr></thead>
              <tbody id="livelog-body-{pr_id}">
                <tr><td colspan="5" style="padding:16px;text-align:center;color:var(--muted)">Carregando...</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </details>
    </div>
</details>"""

    if not all_profiles:
        bot_sections = """<div class="card" style="text-align:center;padding:48px 20px">
          <div style="font-size:48px;margin-bottom:12px">📭</div>
          <div style="font-size:16px;font-weight:700;margin-bottom:8px">Nenhum projeto criado</div>
          <div style="color:var(--muted);margin-bottom:20px">Crie seu primeiro projeto para começar a publicar.</div>
          <a href="/app/robot" class="btn">🤖 Ir para Robô</a>
        </div>"""

    # Auto-refresh when any bot has active jobs
    _active_job_count = 0
    _active_post_count = 0
    if all_ids:
        _active_job_count = int(db.scalar(
            select(func.count()).select_from(Job).where(
                Job.profile_id.in_(all_ids),
                Job.status.in_([JobStatus.queued, JobStatus.running])
            )
        ) or 0)
        _active_post_count = int(db.scalar(
            select(func.count()).select_from(Post).where(
                Post.profile_id.in_(all_ids),
                Post.status.in_([PostStatus.pending, PostStatus.processing])
            )
        ) or 0)
    _auto_tick_ids = []
    for _pr_auto in all_profiles:
        if not _pr_auto.active:
            continue
        _auto_cfg = dict(_pr_auto.publish_config_json or {})
        if _auto_cfg.get("run_stopped_at"):
            continue
        _auto_jobs = int(db.scalar(select(func.count()).select_from(Job).where(
            Job.profile_id == _pr_auto.id,
            Job.status.in_([JobStatus.queued, JobStatus.running]),
        )) or 0)
        _auto_posts = int(db.scalar(select(func.count()).select_from(Post).where(
            Post.profile_id == _pr_auto.id,
            Post.status.in_([PostStatus.pending, PostStatus.processing]),
        )) or 0)
        if _auto_jobs + _auto_posts > 0:
            _auto_tick_ids.append(_pr_auto.id)
    # Script de persistência de toggles (sempre presente nesta página)
    # Técnica: servidor NÃO renderiza open/closed ” JS lê localStorage ou data-default-open
    # Isso garante que nunca há flash de "abre e fecha" no auto-reload de 5s
    refresh_js = """<script>
(function(){
  var P='ph-tgl:';
  function _save(el){
    if(el.hasAttribute('data-persist-toggle'))
      localStorage.setItem(P+el.getAttribute('data-persist-toggle'),el.open?'1':'0');
  }
  function _restore(){
    document.querySelectorAll('details[data-persist-toggle]').forEach(function(el){
      var saved=localStorage.getItem(P+el.getAttribute('data-persist-toggle'));
      el.open = saved!==null ? saved==='1' : el.getAttribute('data-default-open')==='1';
    });
  }
  _restore();
  document.addEventListener('toggle',function(e){if(e.target.tagName==='DETAILS')_save(e.target);},true);
})();

/* ── Rodar agora (AJAX) ── */
document.addEventListener('click', function(e) {
  var btn = e.target.closest('.ph-run-now-btn');
  if (!btn || btn.disabled) return;
  e.preventDefault();
  var botId = btn.getAttribute('data-bot-id') || '';
  btn.disabled = true;
  var orig = btn.innerHTML;
  btn.innerHTML = '&#8987; Processando...';
  btn.style.opacity = '0.7';
  var fd = new FormData();
  if (botId) fd.append('bot_id', botId);
  fetch('/app/robot/tick-now', {method:'POST', body:fd, credentials:'same-origin'})
    .then(function(r){ return r.ok ? r.json() : {ticks:0}; })
    .then(function(d){
      btn.innerHTML = d.ticks > 0 ? ('&#10003; ' + d.ticks + ' job(s) processado(s)') : '&#10003; Sem jobs na fila';
      btn.style.opacity = '1';
      setTimeout(function(){ location.reload(); }, 1200);
    })
    .catch(function(){
      btn.disabled = false;
      btn.innerHTML = orig;
      btn.style.opacity = '1';
    });
});

/* ── Parar Bot (AJAX) ── */
document.addEventListener('click', function(e) {
  var btn = e.target.closest('.ph-stop-bot-btn');
  if (!btn || btn.disabled) return;
  e.preventDefault();
  var botId = btn.getAttribute('data-bot-id') || '';
  btn.disabled = true;
  var orig = btn.innerHTML;
  btn.innerHTML = '&#8987; Parando...';
  btn.style.opacity = '0.7';
  var fd = new FormData();
  if (botId) fd.append('bot_id', botId);
  fetch('/app/robot/stop', {method:'POST', body:fd, credentials:'same-origin', headers:{Accept:'application/json'}})
    .then(function(r){ return r.ok ? r.json() : {ok:false}; })
    .then(function(d){
      btn.innerHTML = d.ok ? '&#10003; Bot parado' : '&#10007; Erro ao parar';
      btn.style.opacity = '1';
      setTimeout(function(){ location.reload(); }, 1000);
    })
    .catch(function(){
      btn.disabled = false;
      btn.innerHTML = orig;
      btn.style.opacity = '1';
    });
});

/* ── Column resize for all pub tables ── */
(function(){
  function initResize(table){
    if(table.dataset.resizeInit) return;
    table.dataset.resizeInit='1';
    table.style.tableLayout='fixed';
    var ths=table.querySelectorAll('thead th');
    ths.forEach(function(th){
      var handle=document.createElement('div');
      handle.style.cssText='position:absolute;right:0;top:0;bottom:0;width:5px;cursor:col-resize;user-select:none;z-index:1';
      th.style.position='relative';
      th.style.overflow='hidden';
      var startX,startW;
      handle.addEventListener('mousedown',function(e){
        startX=e.pageX; startW=th.offsetWidth;
        document.addEventListener('mousemove',onMove);
        document.addEventListener('mouseup',function(){document.removeEventListener('mousemove',onMove);},{once:true});
        e.preventDefault();
      });
      function onMove(e){th.style.width=Math.max(40,startW+(e.pageX-startX))+'px';}
      th.appendChild(handle);
    });
  }
  function scanTables(){
    document.querySelectorAll('table[id^="tbl-pub-"]').forEach(initResize);
  }
  document.addEventListener('DOMContentLoaded',scanTables);
  setTimeout(scanTables,800);
})();
</script>"""
    if _active_job_count > 0 or _active_post_count > 0:
        refresh_js += f"""<script>
(function(){{
  var ids = {json.dumps(_auto_tick_ids)};
  if (!ids.length || window.__phAutoTickRunning) return;
  window.__phAutoTickRunning = true;
  var i = 0;
  function runNext(){{
    if (i >= ids.length) {{
      window.__phAutoTickRunning = false;
      setTimeout(function(){{ location.reload(); }}, 800);
      return;
    }}
    var fd = new FormData();
    fd.append('bot_id', ids[i++]);
    fetch('/app/robot/tick-now', {{method:'POST', body:fd, credentials:'same-origin'}})
      .then(function(){{ runNext(); }})
      .catch(function(){{ window.__phAutoTickRunning = false; }});
  }}
  setTimeout(runNext, 400);
}})();
</script>"""
        refresh_js += """<script>
(function(){
  var _hash=null, _streak=0, _delay=8000, _tid=null;
  function _schedule(){ _tid=setTimeout(_check,_delay); }
  function _check(){
    if(document.hidden){ _schedule(); return; }
    fetch('/app/posts/status-hash')
      .then(function(r){ return r.ok?r.json():null; })
      .then(function(d){
        if(!d){ _schedule(); return; }
        if(_hash===null){ _hash=d.hash; _schedule(); return; }
        if(d.hash!==_hash){
          // estado mudou — recarrega
          location.reload();
        } else {
          // sem mudança — aumenta intervalo gradualmente (max 30s)
          _streak++;
          if(_streak>=3) _delay=Math.min(30000,_delay+6000);
          _schedule();
        }
      })
      .catch(function(){ _schedule(); });
  }
  document.addEventListener('visibilitychange',function(){
    if(!document.hidden && _hash!==null){ clearTimeout(_tid); _check(); }
  });
  _schedule();
})();
</script>"""

    body = flash_html + _ph("secao-posts") + summary_bar + worker_notice + cron_helper + help_menu + bot_sections + refresh_js
    return _layout("Posts", body, user=user, active_nav="posts")


@router.get("/app/profiles/{profile_id}/history", include_in_schema=False)
def profile_history_redirect(profile_id: str, user: User = Depends(get_current_user)):
    """Redirect old per-profile history URL to global history page."""
    return RedirectResponse(f"/app/history?bot={profile_id}", status_code=status.HTTP_302_FOUND)


@router.get("/app/history", include_in_schema=False)
def history_page(
    request: Request,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Histórico completo de publicações ” todos os bots, separados por seção."""
    all_profiles = list(db.scalars(
        select(AutomationProfile)
        .where(AutomationProfile.user_id == user.id)
        .order_by(AutomationProfile.active.desc(), AutomationProfile.created_at.asc())
    ))

    th_s = "padding:9px 12px;text-align:left;font-size:10px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;color:var(--muted);white-space:nowrap"

    def _build_hist_rows(items, pr_id):
        out = ""
        for idx, row in enumerate(items):
            p_obj = row[0]
            title = row[1]
            canonical_url = row[2] if len(row) > 2 else None
            t = html.escape(str(title or "")[:90] + ("…" if len(str(title or "")) > 90 else ""))
            dt = html.escape(_fmt_dt(p_obj.published_at or p_obj.created_at, user=user))
            dt_iso = (p_obj.published_at or p_obj.created_at).isoformat() if (p_obj.published_at or p_obj.created_at) else ""
            chk = f"<input type='checkbox' name='post_id' value='{html.escape(p_obj.id)}' style='width:14px;height:14px;cursor:pointer' onchange=\"_phHistCount('{html.escape(pr_id)}')\"/>"
            wp_link = (f"<a href='{html.escape(p_obj.wp_url)}' target='_blank' rel='noopener' "
                       f"style='color:#10b981;font-size:12px;font-weight:600;text-decoration:none;white-space:nowrap'>&#8599; Ver</a>") if p_obj.wp_url else "<span style='color:var(--muted)'>”</span>"
            src_link = (f"<a href='{html.escape(str(canonical_url))}' target='_blank' rel='noopener' "
                        f"style='color:#6366f1;font-size:12px;text-decoration:none;white-space:nowrap' title='{html.escape(str(canonical_url))}'>&#8599; Fonte</a>") if canonical_url else "<span style='color:var(--muted)'>”</span>"
            _correct_url = html.escape(f"/app/profiles/{pr_id}/posts/{p_obj.id}/correct")
            correct_btn = (
                f"<button type='button' class='btn flat' "
                f"style='font-size:11px;padding:3px 8px;color:#f59e0b;border-color:rgba(245,158,11,.35);background:transparent' "
                f"onclick=\"if(confirm('Reprocessar e atualizar no WordPress?')){{var f=document.createElement('form');f.method='post';f.action='{_correct_url}';document.body.appendChild(f);f.submit();}}\">Corrigir</button>"
            )
            out += (
                f"<tr style='border-top:1px solid var(--border)' data-hist-date='{html.escape(dt_iso)}'>"
                f"<td style='padding:8px 12px;width:32px'>{chk}</td>"
                f"<td style='padding:8px 12px;font-size:13px;color:var(--text)'>{t}</td>"
                f"<td style='padding:8px 12px;font-size:12px;color:var(--muted);white-space:nowrap'>{dt}</td>"
                f"<td style='padding:8px 12px'>{wp_link}</td>"
                f"<td style='padding:8px 12px'>{src_link}</td>"
                f"<td style='padding:8px 12px'>{correct_btn}</td>"
                f"</tr>"
            )
        return out

    bot_sections = ""
    grand_total = 0
    for pr in all_profiles:
        pr_id  = html.escape(pr.id)
        pr_name  = html.escape(pr.name)
        pr_emoji = _safe((pr.publish_config_json or {}).get("emoji") or "🤖")
        icon_bg  = "linear-gradient(135deg,#10b981,#059669)" if pr.active else "linear-gradient(135deg,var(--primary),var(--pink))"

        total_pub = int(db.scalar(select(func.count()).select_from(Post).where(
            Post.profile_id == pr.id, Post.status == PostStatus.completed)) or 0)
        grand_total += total_pub

        rows = list(db.execute(
            select(Post, CollectedContent.title, CollectedContent.canonical_url)
            .join(CollectedContent, CollectedContent.id == Post.collected_content_id)
            .where(Post.profile_id == pr.id, Post.status == PostStatus.completed)
            .order_by(Post.published_at.desc().nullslast(), Post.created_at.desc())
            .limit(300)
        ).all())

        tbl_rows = _build_hist_rows(rows, pr.id)
        if not tbl_rows:
            tbl_rows = f"<tr><td colspan='6' style='padding:24px;text-align:center;color:var(--muted);font-size:13px'>Nenhum post publicado ainda.</td></tr>"

        has_posts = bool(rows)
        btn_dis   = "" if has_posts else "disabled"
        btn_sty   = "" if has_posts else "opacity:.45;cursor:not-allowed;"

        bot_sections += f"""
<details class="card toggle-section" data-persist-toggle="hist-bot-{pr_id}" data-default-open="{'1' if pr.active else '0'}" style="margin-bottom:14px;padding:0;overflow:hidden">
  <summary style="padding:14px 18px;display:flex;align-items:center;justify-content:space-between">
    <span class="ts-title" style="display:flex;align-items:center;gap:10px">
      <div style="width:32px;height:32px;border-radius:8px;background:{icon_bg};display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0">{pr_emoji}</div>
      <span style="font-weight:700">{pr_name}</span>
      <span class="ts-badge" style="color:#10b981;border-color:rgba(16,185,129,.3)">{total_pub} publicados</span>
    </span>
    <span class="ts-arrow">&#9654;</span>
  </summary>
  <div class="ts-body" style="padding:0;border-top:1px solid var(--border)">
    <!-- Toolbar -->
    <div style="display:flex;gap:8px;flex-wrap:wrap;padding:10px 16px;border-bottom:1px solid var(--border);background:var(--surface2);align-items:center">
      <button type="button" id="hist-sort-{pr_id}" data-sort-asc="0"
        onclick="(function(){{var tid='hist-tbl-{pr_id}';var btn=document.getElementById('hist-sort-{pr_id}');var tb=document.getElementById(tid).querySelector('tbody');var rows=Array.from(tb.querySelectorAll('tr[data-hist-date]'));var asc=btn.dataset.sortAsc==='1';rows.sort(function(a,b){{var da=a.getAttribute('data-hist-date')||'';var db=b.getAttribute('data-hist-date')||'';return asc?(da<db?-1:da>db?1:0):(da>db?-1:da<db?1:0);}});rows.forEach(function(r){{tb.appendChild(r);}});btn.dataset.sortAsc=asc?'0':'1';btn.innerHTML=asc?'&#8593; Mais antigo':'&#8595; Mais recente';}})()"
        style="font-size:11px;padding:4px 10px;background:var(--surface);border:1px solid var(--border);border-radius:6px;cursor:pointer;color:var(--muted)">&#8595; Mais recente</button>
      <span id="hist-cnt-{pr_id}" style="font-size:11px;color:var(--muted)"></span>
    </div>
    <!-- Table -->
    <form id="hist-form-{pr_id}" method="post" action="/app/profiles/{pr_id}/posts/bulk">
      <input type="hidden" name="mode" value="delete">
      <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
        <table id="hist-tbl-{pr_id}" style="width:100%;border-collapse:collapse;min-width:480px">
          <thead><tr style="background:var(--surface2)">
            <th style="{th_s};width:32px"><input type="checkbox" style="width:14px;height:14px;cursor:pointer"
              onclick="var h=this;document.getElementById('hist-tbl-{pr_id}').querySelectorAll('input[name=post_id]').forEach(function(c){{c.checked=h.checked;}});_phHistCount('{pr_id}')"></th>
            <th style="{th_s}">T&#237;tulo</th>
            <th style="{th_s};width:130px">Publicado</th>
            <th style="{th_s};width:60px">WP</th>
            <th style="{th_s};width:60px">Fonte</th>
            <th style="{th_s};width:80px">A&#231;&#227;o</th>
          </tr></thead>
          <tbody>{tbl_rows}</tbody>
        </table>
      </div>
      <!-- Bulk actions -->
      <div style="display:flex;gap:8px;flex-wrap:wrap;padding:10px 16px;border-top:1px solid var(--border);align-items:center">
        <button type="button" {btn_dis} style="font-size:12px;padding:5px 12px;{btn_sty}" class="btn secondary"
          onclick="(function(){{var n=document.querySelectorAll('#hist-tbl-{pr_id} input[name=post_id]:checked').length;if(!n){{alert('Selecione ao menos um post.');return;}}if(!confirm('Excluir '+n+' post(s) do PostHub?'))return;document.getElementById('hist-form-{pr_id}').querySelector('input[name=mode]').value='delete';document.getElementById('hist-form-{pr_id}').submit();}})()">Excluir selecionados</button>
        <button type="button" {btn_dis} style="font-size:12px;padding:5px 12px;{btn_sty}" class="btn secondary"
          onclick="(function(){{if(!confirm('Excluir TODOS os {total_pub} posts publicados deste bot do PostHub?'))return;document.querySelectorAll('#hist-tbl-{pr_id} input[name=post_id]').forEach(function(c){{c.checked=true;}});document.getElementById('hist-form-{pr_id}').querySelector('input[name=mode]').value='delete';document.getElementById('hist-form-{pr_id}').submit();}})()">Excluir todos ({total_pub})</button>
        <button type="button" {btn_dis} style="font-size:12px;padding:5px 12px;color:#ef4444;border-color:rgba(239,68,68,.45);background:transparent;{btn_sty}" class="btn flat"
          onclick="(function(){{var n=document.querySelectorAll('#hist-tbl-{pr_id} input[name=post_id]:checked').length;if(!n){{alert('Selecione ao menos um post.');return;}}if(!confirm('Apagar '+n+' post(s) do WordPress? Ação irreversível.'))return;document.getElementById('hist-form-{pr_id}').querySelector('input[name=mode]').value='delete_wp';document.getElementById('hist-form-{pr_id}').submit();}})()">&#128465; Apagar do WordPress</button>
      </div>
    </form>
  </div>
</details>"""

    if not all_profiles:
        bot_sections = """<div class="card" style="text-align:center;padding:48px 20px">
          <div style="font-size:48px;margin-bottom:12px">📭</div>
          <div style="font-size:16px;font-weight:700;margin-bottom:8px">Nenhum bot criado</div>
          <a href="/app/robot" class="btn">🤖 Ir para Robô</a>
        </div>"""

    hist_script = """<script>
function _phHistCount(pid) {
  var n = document.querySelectorAll('#hist-tbl-' + pid + ' input[name=post_id]:checked').length;
  var el = document.getElementById('hist-cnt-' + pid);
  if (el) el.textContent = n > 0 ? n + ' selecionado(s)' : '';
}
/* Toggle persistence */
(function(){
  var P='ph-tgl:';
  document.querySelectorAll('details[data-persist-toggle]').forEach(function(el){
    var saved=localStorage.getItem(P+el.getAttribute('data-persist-toggle'));
    el.open = saved!==null ? saved==='1' : el.getAttribute('data-default-open')==='1';
  });
  document.addEventListener('toggle',function(e){
    if(e.target.tagName==='DETAILS' && e.target.hasAttribute('data-persist-toggle'))
      localStorage.setItem(P+e.target.getAttribute('data-persist-toggle'),e.target.open?'1':'0');
  },true);
})();
/* Column resize */
(function(){
  function initResize(table){
    if(table.dataset.resizeInit) return;
    table.dataset.resizeInit='1';
    table.style.tableLayout='fixed';
    table.querySelectorAll('thead th').forEach(function(th){
      var handle=document.createElement('div');
      handle.style.cssText='position:absolute;right:0;top:0;bottom:0;width:5px;cursor:col-resize;user-select:none;z-index:1';
      th.style.position='relative'; th.style.overflow='hidden';
      var startX,startW;
      handle.addEventListener('mousedown',function(e){
        startX=e.pageX; startW=th.offsetWidth;
        document.addEventListener('mousemove',onMove);
        document.addEventListener('mouseup',function(){document.removeEventListener('mousemove',onMove);},{once:true});
        e.preventDefault();
      });
      function onMove(e){th.style.width=Math.max(40,startW+(e.pageX-startX))+'px';}
      th.appendChild(handle);
    });
  }
  setTimeout(function(){document.querySelectorAll('table[id^="hist-tbl-"]').forEach(initResize);},400);
})();
</script>"""

    flash_msg = (request.query_params.get("msg") or "").strip()
    flash_html = f"<div class='card' style='border-color:rgba(99,102,241,.4);margin-bottom:14px'><b>{html.escape(flash_msg)}</b></div>" if flash_msg else ""

    summary = f"<div style='font-size:13px;color:var(--muted);margin-bottom:16px'>{grand_total} publicações no total em {len(all_profiles)} bot(s)</div>"
    body = flash_html + summary + bot_sections + hist_script
    return _layout("Histórico", body, user=user, active_nav="history")


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
            "Post duplicado ” ignorado automaticamente",
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


def _notification_hidden(post: Post) -> bool:
    return isinstance(post.outputs_json, dict) and bool(post.outputs_json.get("notification_hidden"))


def _notification_completed(post: Post) -> bool:
    return isinstance(post.outputs_json, dict) and bool(post.outputs_json.get("notification_completed"))


def _set_notification_state(post: Post, *, hidden: bool | None = None, completed: bool | None = None) -> None:
    outputs = dict(post.outputs_json or {})
    if hidden is not None:
        outputs["notification_hidden"] = bool(hidden)
    if completed is not None:
        outputs["notification_completed"] = bool(completed)
    post.outputs_json = outputs


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
        if _notification_hidden(post) or _notification_completed(post):
            continue
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
            "ts_ms": int(ts.replace(tzinfo=timezone.utc).timestamp() * 1000) if ts else 0,
            "wp_url": post.wp_url or "",
            "error_label": error_label,
            "fix": fix,
            "fix_url": fix_url,
        })
    return _JSONResponse(feed)


@router.post("/app/notifications/{post_id}/delete", include_in_schema=False)
def notification_delete(post_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    post = db.scalar(select(Post).where(Post.id == post_id, Post.user_id == user.id))
    if post:
        _set_notification_state(post, hidden=True)
        db.add(post)
        db.commit()
    return RedirectResponse("/app/notifications?msg=Notificacao+excluida.", status_code=status.HTTP_302_FOUND)


@router.post("/app/notifications/{post_id}/complete", include_in_schema=False)
def notification_complete(post_id: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    post = db.scalar(select(Post).where(Post.id == post_id, Post.user_id == user.id))
    if post:
        _set_notification_state(post, completed=True, hidden=False)
        db.add(post)
        db.commit()
    return RedirectResponse("/app/notifications?msg=Notificacao+marcada+como+concluida.", status_code=status.HTTP_302_FOUND)


@router.get("/app/notifications", include_in_schema=False)
def notifications_page(request: Request, user: User = Depends(get_current_user), db=Depends(get_db)):
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
        if _notification_hidden(post):
            continue
        is_resolved = _notification_completed(post)
        is_canceled = isinstance(post.outputs_json, dict) and bool(post.outputs_json.get("canceled_by_user"))
        is_ok = post.status == PostStatus.completed or is_resolved
        label = "Concluida" if is_resolved else ("Cancelado" if (not is_ok and is_canceled) else ("Publicado" if is_ok else "Falhou"))
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
            ) if (wp_url and not is_resolved) else ""
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

            fix_btn = (
                "<div style='display:flex;gap:8px;flex-wrap:wrap;margin-top:10px'>"
                f"<form method='post' action='/app/notifications/{html.escape(post.id)}/complete' style='margin:0'>"
                "<button class='btn secondary' type='submit' style='font-size:12px;padding:6px 12px'>Marcar como concluida</button>"
                "</form>"
                f"<form method='post' action='/app/notifications/{html.escape(post.id)}/delete' style='margin:0'>"
                "<button class='btn flat' type='submit' style='font-size:12px;padding:6px 12px;color:#ef4444;border-color:rgba(239,68,68,.35);background:transparent'>Excluir notificacao</button>"
                "</form>"
                "</div>"
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
    flash_msg = (request.query_params.get("msg") or "").strip()
    flash_html = f"<div class='card' style='border-color:rgba(16,185,129,.35);margin-bottom:12px'><b>{html.escape(flash_msg)}</b></div>" if flash_msg else ""

    body = f"""
    {_ph("pagina-notificacoes")}
    {flash_html}
    <div style='display:flex;flex-direction:column;gap:20px'>
      <div class="card" style='padding:0;overflow:hidden'>
        <div style='padding:18px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px'>
          <div>
            <h3 style='margin:0 0 2px'>Notificações</h3>
            <p class='muted' style='margin:0;font-size:13px'>Posts publicados, falhas e alertas resolvidos</p>
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
          <div style='display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border:1px solid var(--border);border-radius:12px;gap:12px;flex-wrap:wrap'>
            <div style='min-width:220px;flex:1'>
              <div style='font-size:13px;font-weight:600'>Notificacoes desktop</div>
              <div style='font-size:12px;color:var(--muted);margin-top:2px'>Mostra avisos do navegador mesmo fora da aba do PostHUB</div>
            </div>
            <div style='display:flex;align-items:center;gap:10px'>
              <button type='button' class='btn secondary' onclick='window._phRequestDesktopNotifications && window._phRequestDesktopNotifications()' style='font-size:12px;padding:7px 12px'>Ativar no navegador</button>
              <label style='position:relative;display:inline-block;width:44px;height:24px;flex-shrink:0'>
                <input type='checkbox' id='ns-desktop' style='opacity:0;width:0;height:0' onchange='_saveNotifSettings()'>
                <span class='ns-track'></span>
              </label>
            </div>
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
          try {{
            var s = JSON.parse(localStorage.getItem(LS) || '{{"success":true,"error":true,"desktop":false}}');
            if (typeof s.desktop === 'undefined') s.desktop = false;
            return s;
          }}
          catch(e) {{ return {{"success":true,"error":true,"desktop":false}}; }}
        }}
        var s = load();
        var cb_s = document.getElementById('ns-success');
        var cb_e = document.getElementById('ns-error');
        var cb_d = document.getElementById('ns-desktop');
        if (cb_s) cb_s.checked = s.success !== false;
        if (cb_e) cb_e.checked = s.error !== false;
        if (cb_d) cb_d.checked = s.desktop === true && ('Notification' in window) && Notification.permission === 'granted';
        window._saveNotifSettings = function() {{
          localStorage.setItem(LS, JSON.stringify({{
            success: cb_s ? cb_s.checked : true,
            error:   cb_e ? cb_e.checked : true,
            desktop: cb_d ? cb_d.checked : false,
          }}));
        }};
      }})();
    </script>
    """
    return _layout("Notificações", body, user=user)
