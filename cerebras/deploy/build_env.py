#!/usr/bin/env python3
"""Build /opt/app/cerebras/env.d/cerebras.env from EXISTING on-box sources.
Never prints secret values — only which vars were resolved.
Sources: /opt/app/mind/wa_config.json (.key) and the Odoo password already
used by the odoo tasks bridge / kanban scripts.
"""
import json
import os
import re
import sys
from pathlib import Path

OUT = Path("/opt/app/cerebras/env.d/cerebras.env")


def find_odoo_password() -> str | None:
    candidates = []
    for base in ("/opt/app/scripts", "/opt/app/repos/kanban", "/opt/app/mind",
                 "/opt/app/scorer", "/opt/app/cli"):
        p = Path(base)
        if p.is_dir():
            candidates += list(p.glob("*.py"))
    pat = re.compile(r"ODOO_(?:PW|PASS|PASSWORD)\"?,?\s*(?:=|,)\s*[\"']([^\"']+)[\"']")
    for f in candidates:
        try:
            m = pat.search(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if m:
            return m.group(1)
    return None


def main():
    resolved = {}
    try:
        wa = json.loads(Path("/opt/app/mind/wa_config.json").read_text())
        resolved["EVOLUTION_APIKEY"] = wa["key"]
    except Exception:
        pass
    pw = find_odoo_password()
    if pw:
        resolved["ODOO_PASSWORD"] = pw
    resolved.setdefault("ODOO_URL", os.environ.get("ODOO_URL", "http://ODOO_HOST:8069"))
    resolved["ODOO_DB"] = os.environ.get("ODOO_DB", "odoo")
    resolved["ODOO_UID"] = "2"
    resolved["ODOO_PROJECT_ID"] = "1"

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(f"{k}={v}\n" for k, v in resolved.items()), encoding="utf-8")
    os.chmod(OUT, 0o600)
    print("written:", OUT)
    for k in ("EVOLUTION_APIKEY", "ODOO_PASSWORD"):
        print(f"  {k}: {'SET' if k in resolved else 'MISSING'}")
    return 0 if {"EVOLUTION_APIKEY", "ODOO_PASSWORD"} <= set(resolved) else 1


if __name__ == "__main__":
    sys.exit(main())
