from __future__ import annotations

import os
import socket

import uvicorn


def _is_port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) != 0


def main() -> None:
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    os.environ.setdefault("POSTHUB_INLINE_WORKER", "1")

    default_host = "0.0.0.0" if os.getenv("PORT") or os.getenv("RAILWAY_ENVIRONMENT") else "127.0.0.1"
    host = os.getenv("HOST", default_host)
    base_port = int(os.getenv("PORT", "8040"))
    port = base_port
    while port < base_port + 20 and not _is_port_free(host, port):
        port += 1

    reload_enabled = os.getenv("RELOAD", "0") == "1"
    print(f"PostHub login:      http://{host}:{port}/app/login")
    uvicorn.run("app.main:app", host=host, port=port, reload=reload_enabled, log_level="info")


if __name__ == "__main__":
    main()
