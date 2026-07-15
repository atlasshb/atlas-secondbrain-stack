"""Milestone notifications — reuses the existing atlas_notify pipe when mounted,
falls back to a direct ntfy POST with the same topic (config read from
/opt/app/notify/atlas_notify.json, never hard-coded twice).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

NOTIFY_DIR = os.environ.get("CEREBRAS_NOTIFY_DIR", "/opt/app/notify")


def _load_cfg() -> dict:
    p = Path(NOTIFY_DIR) / "atlas_notify.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def make_notifier():
    if os.environ.get("CEREBRAS_DEV") == "1":
        def dev_notify(title: str, msg: str):
            print(f"[dev-notify] {title}: {msg}", file=sys.stderr)
        return dev_notify

    # prefer the real atlas_notify module (single codepath with the rest of the fleet)
    try:
        sys.path.insert(0, NOTIFY_DIR)
        import atlas_notify  # type: ignore

        def real_notify(title: str, msg: str):
            try:
                atlas_notify.notify(title, msg)
            except Exception:
                _ntfy_direct(title, msg)
        return real_notify
    except Exception:
        return _ntfy_direct


def _ntfy_direct(title: str, msg: str):
    cfg = _load_cfg()
    url = cfg.get("ntfy_url", os.environ.get("NTFY_URL", "http://127.0.0.1:80"))
    topic = cfg.get("ntfy_topic", os.environ.get("NTFY_TOPIC", "your-ntfy-topic"))
    try:
        req = urllib.request.Request(
            f"{url}/{topic}", data=msg.encode("utf-8"),
            headers={"Title": title.encode("ascii", "replace").decode()})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass  # notifications are best-effort, never break the app
