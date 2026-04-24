from __future__ import annotations

import os
import socket
import time
import traceback

from app.worker import run_worker_tick


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.05, float(os.getenv(name, str(default))))
    except Exception:
        return default


def main() -> None:
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    os.environ.setdefault("POSTHUB_INLINE_WORKER", "0")

    worker_id = os.getenv("POSTHUB_WORKER_ID") or f"worker:{socket.gethostname()}:{os.getpid()}"
    idle_sleep = _float_env("POSTHUB_WORKER_IDLE_SLEEP", 1.0)
    busy_sleep = _float_env("POSTHUB_WORKER_BUSY_SLEEP", 0.2)

    print(f"PostHub worker: {worker_id}", flush=True)
    while True:
        try:
            did_work = run_worker_tick(worker_id=worker_id)
        except KeyboardInterrupt:
            raise
        except Exception:
            print(traceback.format_exc(), flush=True)
            time.sleep(max(1.0, idle_sleep))
            continue
        time.sleep(busy_sleep if did_work else idle_sleep)


if __name__ == "__main__":
    main()
