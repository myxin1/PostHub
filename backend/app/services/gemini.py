from __future__ import annotations

from dataclasses import dataclass
import os
import re

import google.generativeai as genai

from app.config import settings


class GeminiError(Exception):
    pass


@dataclass(frozen=True)
class GeminiResult:
    text: str


def _extract_retry_delay_seconds(msg: str) -> int | None:
    m1 = re.search(r"retry_delay\s*\{\s*seconds:\s*(\d+)\s*\}", msg, flags=re.IGNORECASE)
    if m1:
        try:
            return int(m1.group(1))
        except Exception:
            return None
    m2 = re.search(r"please retry in\s+([0-9]+(?:\.[0-9]+)?)s", msg, flags=re.IGNORECASE)
    if m2:
        try:
            v = float(m2.group(1))
            return max(1, int(round(v)))
        except Exception:
            return None
    return None


def _get_api_key() -> str:
    return (os.getenv("GEMINI_API_KEY") or settings.gemini_api_key or "").strip()


def _get_model_preference() -> str:
    return (os.getenv("GEMINI_MODEL") or settings.gemini_model or "").strip()


def _pick_working_model(preferred: str) -> str:
    try:
        models = list(genai.list_models())
    except Exception:
        return preferred or "gemini-1.5-flash-latest"

    def supports_generate_content(m) -> bool:
        methods = getattr(m, "supported_generation_methods", None) or []
        return any(str(x).lower() == "generatecontent" for x in methods)

    candidates = [m for m in models if supports_generate_content(m)]
    if not candidates:
        return preferred or "gemini-1.5-flash-latest"

    names = [str(getattr(m, "name", "")).strip() for m in candidates]

    def score(n: str) -> tuple[int, int, int]:
        s = n.lower()
        return (
            1 if preferred and preferred.lower() in s else 0,
            1 if "flash" in s else 0,
            1 if "1.5" in s else 0,
        )

    names_sorted = sorted([n for n in names if n], key=score, reverse=True)
    if names_sorted:
        return names_sorted[0].replace("models/", "")
    return preferred or "gemini-1.5-flash-latest"


def generate_text(*, prompt: str, content: str, model: str | None = None, api_key: str | None = None) -> GeminiResult:
    resolved_key = (api_key or "").strip() or _get_api_key()
    if not resolved_key:
        raise GeminiError("missing_gemini_api_key")
    genai.configure(api_key=resolved_key)
    preferred = model or _get_model_preference() or "gemini-1.5-flash-latest"
    picked = _pick_working_model(preferred)
    try:
        m = genai.GenerativeModel(picked)
        resp = m.generate_content(f"{prompt}\n\n---\n\n{content}")
    except Exception as e:
        msg = str(e)
        if "quota" in msg.lower() or "rate limit" in msg.lower() or "429" in msg:
            secs = _extract_retry_delay_seconds(msg) or 30
            raise GeminiError(f"rate_limited:{secs}") from e
        if "not found" in msg.lower() or "404" in msg:
            picked2 = _pick_working_model("")
            m = genai.GenerativeModel(picked2)
            resp = m.generate_content(f"{prompt}\n\n---\n\n{content}")
        else:
            raise
    text = getattr(resp, "text", None)
    if not text:
        raise GeminiError("empty_response")
    return GeminiResult(text=str(text).strip())
