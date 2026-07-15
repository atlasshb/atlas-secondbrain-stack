"""Seed devdata/fixtures with realistic feed shapes (incl. one stale + one crit)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

FIX = Path(__file__).resolve().parents[1] / "devdata" / "fixtures"
FIX.mkdir(parents=True, exist_ok=True)

now = datetime.now(timezone.utc)

(FIX / "bank_sync_state.json").write_text(json.dumps({
    "last_alert": (now - timedelta(hours=3)).isoformat(),
    "band": "critical",
    "last_check": (now - timedelta(minutes=20)).isoformat(),
    "detail": "bank ledger 44d stale, importer=healthy",
}))
(FIX / "tasks.json").write_text(json.dumps({"updated": now.date().isoformat(), "total_open": 200}))
(FIX / "evolution.json").write_text(json.dumps({"instance": {"instanceName": "default", "state": "open"}}))
(FIX / "commerce_state.json").write_text(json.dumps({
    "updated": datetime.now().replace(microsecond=0).isoformat(), "headline": "dev fixture"}))
(FIX / "ntfy_health.json").write_text(json.dumps({"healthy": True}))
print(f"seeded {FIX}")
