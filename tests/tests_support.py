"""Shared sample data: session snapshots."""

import json
from datetime import UTC, datetime

EMAIL = "shopper@example.com"


def session_snapshot(now, generation=1):
    return {
        "schema": 1,
        "target": "hktvmall",
        "generation": generation,
        "email": EMAIL,
        "user_agent": "Browser/156",
        "verified_at": datetime.fromtimestamp(now, UTC).isoformat(),
        "cookies": [
            {
                "domain": "www.hktvmall.com",
                "name": "JSESSIONID",
                "value": f"session-{generation}",
                "path": "/",
                "secure": True,
                "expires": None,
            }
        ],
    }


def publish_snapshot(path, now, generation=1, **overrides):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {**session_snapshot(now, generation), **overrides}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(path)
    return data
