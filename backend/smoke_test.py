from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx


def main():
    base = "http://127.0.0.1:8010"
    email = f"u{int(time.time())}@mydomain.com"
    password = "password123"

    print("START", email)

    with httpx.Client(timeout=30.0) as c:
        reg = c.post(
            f"{base}/api/auth/register",
            json={"email": email, "password": password, "password_confirm": password},
        )
        reg.raise_for_status()
        token = c.post(f"{base}/api/auth/login", json={"login": email, "password": password}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        profile = c.post(
            f"{base}/api/profiles",
            headers=headers,
            json={"name": "Demo", "active": True, "schedule_config": {}, "anti_block_config": {}},
        ).json()
        c.post(
            f"{base}/api/profiles/{profile['id']}/sources",
            headers=headers,
            json={"type": "URL", "value": "https://example.com", "active": True},
        ).raise_for_status()
        c.post(f"{base}/api/profiles/{profile['id']}/run", headers=headers).raise_for_status()

        deadline = time.time() + 30
        last_status = None
        while time.time() < deadline:
            posts = c.get(f"{base}/api/posts", headers=headers).json()
            if posts:
                last_status = posts[0]["status"]
                if last_status in ("completed", "failed"):
                    break
            time.sleep(1.0)

        logs = c.get(f"{base}/api/logs", headers=headers).json()

    print("EMAIL", email)
    print("FINISHED_AT", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    print("LATEST_POST_STATUS", last_status)
    print("LATEST_LOGS", [(l["stage"], l["status"]) for l in logs[:12]])


if __name__ == "__main__":
    main()
