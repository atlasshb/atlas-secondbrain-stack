"""Thin adapter over the EXISTING /opt/app/mind/gate.py — never reimplements policy.

Production: /opt/app/mind is bind-mounted at the same path, so `import gate`
works unmodified and writes go to the real ledger.sqlite / kernel.sqlite.
Dev (CEREBRAS_DEV=1 or mind/ absent): a fake with the same surface, backed by
a local sqlite file, so the UI + no-bypass tests run anywhere.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

MIND_DIR = os.environ.get("CEREBRAS_MIND_DIR", "/opt/app/mind")

ACTOR = "cerebras"


class RealGate:
    def __init__(self):
        sys.path.insert(0, MIND_DIR)
        import gate  # the real chokepoint
        self.gate = gate
        self.ledger = os.path.join(MIND_DIR, "ledger.sqlite")

    def propose(self, domain, action, target, payload, risk) -> dict:
        return self.gate.request(ACTOR, domain, action, target, payload, risk)

    def decide(self, approval_id: int, status: str, by: str, note: str = "") -> bool:
        return bool(self.gate._decide(approval_id, status, by, note))

    def audit(self, action: str, target: str, result: str, domain: str = "fleet"):
        self.gate._audit(ACTOR, action, target, result, domain=domain)

    def _con(self):
        return sqlite3.connect(self.ledger, timeout=10)

    def pending(self) -> list[dict]:
        con = self._con()
        rows = con.execute(
            "SELECT id, ts, action, target, payload, reason, risk FROM approvals"
            " WHERE status='pending' AND actor=? ORDER BY id", (ACTOR,)).fetchall()
        con.close()
        return [_row(r) for r in rows]

    def get(self, approval_id: int) -> dict | None:
        con = self._con()
        r = con.execute(
            "SELECT id, ts, action, target, payload, reason, risk, status, actor"
            " FROM approvals WHERE id=?", (approval_id,)).fetchone()
        con.close()
        if not r:
            return None
        d = _row(r)
        d["status"], d["actor"] = r[7], r[8]
        return d

    def mark_executed(self, approval_id: int, note: str = ""):
        # same convention as mind/send_effector.py: flip approved -> executed
        self._close_row(approval_id, "executed", note)

    def mark_failed(self, approval_id: int, note: str = ""):
        # failure transition: never strand a row in 'approved' — it would be
        # invisible to the UI (pending-only) and to the budget accounting
        self._close_row(approval_id, "failed", note)

    def _close_row(self, approval_id: int, status: str, note: str):
        con = self._con()
        con.execute(
            "UPDATE approvals SET status=?, decided_at=datetime('now'),"
            " note=? WHERE id=? AND status='approved'", (status, note, approval_id))
        con.commit()
        con.close()

    def executed_count(self, action: str, since_hours: float) -> int:
        # counts failed attempts too: a timed-out docker restart may still
        # have restarted the container, so it must consume budget
        con = self._con()
        n = con.execute(
            "SELECT COUNT(*) FROM approvals WHERE actor=? AND action=?"
            " AND status IN ('executed','failed') AND decided_at > datetime('now', ?)",
            (ACTOR, action, f"-{since_hours} hours")).fetchone()[0]
        con.close()
        return n


def _row(r) -> dict:
    try:
        payload = json.loads(r[4]) if r[4] else {}
    except ValueError:
        payload = {}
    return {"id": r[0], "ts": r[1], "action": r[2], "target": r[3],
            "payload": payload, "reason": r[5], "risk": r[6]}


class FakeGate(RealGate):
    """Dev/test stand-in with identical surface; parks everything like L0 policy."""

    def __init__(self, ledger_path):
        self.ledger = str(ledger_path)
        con = self._con()
        con.execute(
            "CREATE TABLE IF NOT EXISTS approvals ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
            " actor TEXT, domain TEXT, action TEXT NOT NULL, target TEXT,"
            " payload TEXT, reason TEXT, risk TEXT, est_cost_eur REAL DEFAULT 0,"
            " status TEXT DEFAULT 'pending', decided_at TEXT, decided_by TEXT, note TEXT)")
        con.commit()
        con.close()

    def propose(self, domain, action, target, payload, risk) -> dict:
        con = self._con()
        cur = con.execute(
            "INSERT INTO approvals(ts,actor,domain,action,target,payload,reason,risk)"
            " VALUES(datetime('now'),?,?,?,?,?,?,?)",
            (ACTOR, domain, action, target, json.dumps(payload, ensure_ascii=False),
             "dev: parked (fake gate)", risk))
        con.commit()
        aid = cur.lastrowid
        con.close()
        return {"decision": "park", "reason": "dev fake gate", "approval_id": aid}

    def decide(self, approval_id, status, by, note="") -> bool:
        con = self._con()
        cur = con.execute(
            "UPDATE approvals SET status=?, decided_at=datetime('now'),"
            " decided_by=?, note=? WHERE id=? AND status='pending'",
            (status, by, note, approval_id))
        con.commit()
        changed = cur.rowcount
        con.close()
        return bool(changed)

    def audit(self, action, target, result, domain="fleet"):
        pass  # no kernel in dev


def make_gate(data_dir: str | Path):
    if os.environ.get("CEREBRAS_DEV") == "1":
        return FakeGate(Path(data_dir) / "dev_ledger.sqlite")
    # FAIL CLOSED: in production a missing gate.py (dropped mount, empty
    # auto-created dir) must crash the app at startup, never silently swap
    # in the fake gate while ProdRunner does real docker restarts.
    if not Path(MIND_DIR, "gate.py").exists():
        raise RuntimeError(
            f"{MIND_DIR}/gate.py not found — refusing to start without the real "
            "gate chokepoint (is the /opt/app/mind bind-mount present?)")
    return RealGate()
