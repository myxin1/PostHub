from __future__ import annotations

import re


def clean_text(text: str | None) -> str | None:
    if not text:
        return None
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = t.strip()
    return t or None

