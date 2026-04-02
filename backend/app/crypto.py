from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings


class CryptoError(Exception):
    pass


def _key_file_path() -> str:
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(base_dir, ".posthub_key")


def _load_key() -> bytes:
    key_b64 = (settings.encryption_key_b64 or "").strip()
    if not key_b64:
        path = _key_file_path()
        if os.path.isfile(path):
            key_b64 = (open(path, "r", encoding="utf-8").read() or "").strip()
        else:
            key_b64 = base64.b64encode(os.urandom(32)).decode("ascii")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(key_b64)
    try:
        key = base64.b64decode(key_b64)
    except Exception as e:
        raise CryptoError("invalid_encryption_key") from e
    if len(key) != 32:
        raise CryptoError("encryption_key_must_be_32_bytes_base64")
    return key


@dataclass(frozen=True)
class EncryptedBlob:
    nonce_b64: str
    ciphertext_b64: str


def encrypt_json(data: dict) -> str:
    key = _load_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    plaintext = json.dumps(data, ensure_ascii=False).encode("utf-8")
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    blob = EncryptedBlob(
        nonce_b64=base64.b64encode(nonce).decode("ascii"),
        ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
    )
    return json.dumps(blob.__dict__)


def decrypt_json(blob_str: str) -> dict:
    key = _load_key()
    aesgcm = AESGCM(key)
    raw = json.loads(blob_str or "{}")
    nonce = base64.b64decode(raw["nonce_b64"])
    ciphertext = base64.b64decode(raw["ciphertext_b64"])
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext.decode("utf-8"))
