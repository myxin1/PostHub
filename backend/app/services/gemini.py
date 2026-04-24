from __future__ import annotations

import os
import re
from dataclasses import dataclass

from google import genai

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
            return max(1, int(round(float(m2.group(1)))))
        except Exception:
            return None
    return None


def _get_api_key() -> str:
    return (os.getenv("GEMINI_API_KEY") or settings.gemini_api_key or "").strip()


def _get_model_preference() -> str:
    return (os.getenv("GEMINI_MODEL") or settings.gemini_model or "").strip()


def _pick_working_model(client: genai.Client, preferred: str) -> str:
    try:
        models = list(client.models.list())
    except Exception:
        return preferred or "gemini-1.5-flash-latest"

    names = [
        str(getattr(m, "name", "")).replace("models/", "").strip()
        for m in models
        if getattr(m, "name", "")
    ]

    def score(n: str) -> tuple[int, int, int]:
        s = n.lower()
        pref = preferred.lower().replace("models/", "") if preferred else ""
        return (
            1 if pref and pref in s else 0,
            1 if "flash" in s else 0,
            1 if "1.5" in s else 0,
        )

    names_sorted = sorted([n for n in names if n], key=score, reverse=True)
    return names_sorted[0] if names_sorted else (preferred or "gemini-1.5-flash-latest")


def generate_text(*, prompt: str, content: str, model: str | None = None, api_key: str | None = None) -> GeminiResult:
    resolved_key = (api_key or "").strip() or _get_api_key()
    if not resolved_key:
        raise GeminiError("missing_gemini_api_key")

    client = genai.Client(api_key=resolved_key)
    preferred = (model or _get_model_preference() or "gemini-1.5-flash-latest").replace("models/", "")
    picked = _pick_working_model(client, preferred)

    full_prompt = f"{prompt}\n\n---\n\n{content}"

    try:
        resp = client.models.generate_content(model=picked, contents=full_prompt)
    except Exception as e:
        msg = str(e)
        if "quota" in msg.lower() or "rate limit" in msg.lower() or "429" in msg:
            secs = _extract_retry_delay_seconds(msg) or 30
            raise GeminiError(f"rate_limited:{secs}") from e
        if "not found" in msg.lower() or "404" in msg:
            fallback = _pick_working_model(client, "")
            resp = client.models.generate_content(model=fallback, contents=full_prompt)
        else:
            raise

    text = getattr(resp, "text", None)
    if not text:
        raise GeminiError("empty_response")
    return GeminiResult(text=str(text).strip())
