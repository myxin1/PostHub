from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image

from app.services.http_client import get_client


@dataclass(frozen=True)
class PreparedImage:
    filename: str
    content_type: str
    data: bytes


def download_and_prepare_image(url: str, *, max_size_px: int = 1600) -> PreparedImage:
    with get_client() as client:
        resp = client.get(url, headers={"user-agent": "PostHubBot/1.0"})
        resp.raise_for_status()
        raw = resp.content
    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, float(max_size_px) / float(max(w, h)))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85, optimize=True)
    data = out.getvalue()
    return PreparedImage(filename="image.jpg", content_type="image/jpeg", data=data)

