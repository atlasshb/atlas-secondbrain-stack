"""The ONE gated self-heal action of Phase 0: docker restart atlas-stalwart.

Invariants (blueprint critic fixes, do not weaken):
- Every proposal goes through mind/gate.py (risk='irreversible' -> HARD_GATE park,
  human approval required). There is NO code path that executes without an
  approved ledger row — see require_approved().
- The executed command is HARD-CODED. Approval payloads are display metadata
  only and are never interpolated into anything executable.
- Cooldown + daily ceiling enforced from the ledger (source of truth), not app memory.
"""
from __future__ import annotations

import http.client
import os
import socket
import sys
import threading
import time
import traceback
import xmlrpc.client

from .probes import run_probe

STALWART_CONTAINER = "atlas-stalwart"  # hard-coded on purpose
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")

COOLDOWN_MIN = 15          # minutes between executions
DAILY_CEILING = 4          # max executions per rolling 24h
ROOTCAUSE_THRESHOLD = 3    # >N executions in 7d -> open a root-cause Odoo task

ACTION = {
    "id": "stalwart_restart",
    "title": "Restart Stalwart (mail) — fixes the recurring TLS-reload bug",
    "command_label": f"docker restart {STALWART_CONTAINER}",
    "tile_id": "stalwart",
    "domain": "fleet",
    "gate_action": "docker_restart",
    "target": f"container:{STALWART_CONTAINER}",
    "risk": "irreversible",  # forces HARD_GATE park -> human approval, always
}

ACTIONS = {ACTION["id"]: ACTION}


class ActionError(Exception):
    pass


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self.sock_path = sock_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.sock_path)
        self.sock = s


def _docker_restart(container: str, timeout_s: int = 10) -> None:
    conn = _UnixHTTPConnection(DOCKER_SOCK, timeout=timeout_s + 30)
    try:
        conn.request("POST", f"/v1.41/containers/{container}/restart?t={timeout_s}",
                     headers={"Host": "docker"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        if resp.status not in (204, 200):
            raise ActionError(f"docker restart HTTP {resp.status}: {body[:200]}")
    finally:
        conn.close()


class DevRunner:
    """CEREBRAS_DEV=1 stand-in: no docker, pretends the restart worked."""

    def restart(self, container):
        time.sleep(0.5)

    def reprobe(self):
        return "ok", "dev: simulated TLS re-probe ok"


class ProdRunner:
    def __init__(self, reprobe_params: dict):
        self.reprobe_params = reprobe_params

    def restart(self, container):
        _docker_restart(container)

    def reprobe(self, budget_s: int = 75):
        """Stalwart needs a few seconds to come back; poll TLS until ok or budget out."""
        deadline = time.time() + budget_s
        last = ("unknown", "no probe ran")
        while time.time() < deadline:
            last = run_probe("tls", self.reprobe_params)
            if last[0] == "ok":
                return last
            time.sleep(5)
        return last


class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: float = 15):
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


class Executor:
    def __init__(self, gate, action_log, notify, runner, odoo_cfg: dict | None = None):
        self.gate = gate
        self.log = action_log
        self.notify = notify
        self.runner = runner
        self.odoo_cfg = self._validate_odoo_cfg(odoo_cfg or {})
        # single-action cockpit: one lock serialises propose/approve/reject so
        # check-then-act sequences (pending scan, budget check) stay atomic
        self._lock = threading.Lock()

    @staticmethod
    def _validate_odoo_cfg(cfg: dict) -> dict:
        if not cfg:
            return {}
        required = {"url", "db", "uid", "password"}
        missing = required - {k for k, v in cfg.items() if v}
        if missing:
            print(f"[cerebras] rootcause escalation DISABLED: incomplete ODOO_* "
                  f"config (missing: {', '.join(sorted(missing))})", file=sys.stderr)
            return {}
        return cfg

    # ---------------------------------------------------------------- propose

    def propose(self, action_id: str, username: str) -> dict:
        with self._lock:
            return self._propose(action_id, username)

    def _propose(self, action_id: str, username: str) -> dict:
        action = ACTIONS.get(action_id)
        if not action:
            raise ActionError(f"unknown action {action_id!r}")
        self._check_budget(action)
        if any(p["action"] == action["gate_action"] for p in self.gate.pending()):
            raise ActionError("already proposed — approve or dismiss the pending one")
        res = self.gate.propose(
            domain=action["domain"], action=action["gate_action"],
            target=action["target"],
            payload={"ui": "cerebras", "action_id": action_id,
                     "command_label": action["command_label"],
                     "proposed_by": username},
            risk=action["risk"])
        if res.get("decision") != "park":
            # gate allowed it outright — policy changed under us; refuse to
            # auto-run anyway, Phase 0 is strictly human-approved
            raise ActionError(f"gate returned {res.get('decision')!r}; expected park")
        aid = res["approval_id"]
        self.log.append("proposed", action=action_id, approval_id=aid, by=username,
                        command=action["command_label"])
        self.notify(f"Cerebras: actie voorgesteld #{aid}",
                    f"{action['command_label']} — wacht op approve in het cockpit.")
        return {"approval_id": aid}

    # ---------------------------------------------------------- approve + run

    def approve_and_run(self, action_id: str, proposal_id: int, username: str) -> dict:
        with self._lock:
            return self._approve_and_run(action_id, proposal_id, username)

    def _approve_and_run(self, action_id: str, proposal_id: int, username: str) -> dict:
        action = ACTIONS.get(action_id)
        if not action:
            raise ActionError(f"unknown action {action_id!r}")
        self.require_approvable(action, proposal_id)
        self._check_budget(action)
        if not self.gate.decide(proposal_id, "approved", by=f"cerebras:{username}"):
            raise ActionError(f"approval #{proposal_id} is no longer pending")
        self.log.append("approved", action=action_id, approval_id=proposal_id, by=username)

        try:
            self.runner.restart(STALWART_CONTAINER)
        except Exception as e:
            # failure transition: close the row (a timed-out restart may still
            # have happened, so it stays visible to the budget accounting)
            self.gate.mark_failed(proposal_id, note=f"execution failed: {e}")
            self.log.append("failed", action=action_id, approval_id=proposal_id,
                            error=f"{e.__class__.__name__}: {e}")
            self.gate.audit(action["gate_action"], action["target"], "failed")
            self.notify("Cerebras: actie MISLUKT",
                        f"#{proposal_id} {action['command_label']}: {e}")
            raise ActionError(f"restart failed: {e}") from e

        self.log.append("executed", action=action_id, approval_id=proposal_id,
                        by=username, command=action["command_label"])
        try:
            self.gate.mark_executed(proposal_id, note=f"cerebras ui by {username}")
            self.gate.audit(action["gate_action"], action["target"], "executed")
        except Exception as e:
            # bookkeeping failed (locked ledger?) — the restart DID happen;
            # be loud, the budget accounting is now incomplete
            self.notify("Cerebras: ledger-write MISLUKT na restart",
                        f"#{proposal_id} uitgevoerd maar niet als 'executed' "
                        f"geboekt: {e}. Budgettelling onvolledig.")

        status, reason = self.runner.reprobe()
        verified = status == "ok"
        self.log.append("verified" if verified else "failed",
                        action=action_id, approval_id=proposal_id,
                        reprobe_status=status, reprobe_reason=reason)
        self.notify(
            "Cerebras: Stalwart herstart " + ("✓ TLS ok" if verified else "⚠ re-probe " + status),
            f"#{proposal_id} door {username}. Re-probe: {reason}")
        self._maybe_rootcause_task(action)
        return {"executed": True, "verified": verified, "reprobe": reason}

    def reject(self, action_id: str, proposal_id: int, username: str) -> dict:
        with self._lock:
            return self._reject(action_id, proposal_id, username)

    def _reject(self, action_id: str, proposal_id: int, username: str) -> dict:
        action = ACTIONS.get(action_id)
        if not action:
            raise ActionError(f"unknown action {action_id!r}")
        self.require_approvable(action, proposal_id)
        if not self.gate.decide(proposal_id, "denied", by=f"cerebras:{username}",
                                note="dismissed in cerebras ui"):
            raise ActionError(f"approval #{proposal_id} is no longer pending")
        self.log.append("rejected", action=action_id, approval_id=proposal_id, by=username)
        return {"rejected": True}

    # ------------------------------------------------------------- guards

    def require_approvable(self, action: dict, proposal_id: int) -> dict:
        """No-bypass invariant: the ledger row must exist, be OURS, be for THIS
        action, and be pending. Anything else refuses to execute."""
        row = self.gate.get(proposal_id)
        if not row:
            raise ActionError(f"no approval #{proposal_id}")
        if row.get("actor", "cerebras") != "cerebras" or row["action"] != action["gate_action"]:
            raise ActionError(f"approval #{proposal_id} is not a {action['id']} proposal")
        if row.get("status", "pending") != "pending":
            raise ActionError(f"approval #{proposal_id} is {row.get('status')}, not pending")
        return row

    def _check_budget(self, action: dict):
        day = self.gate.executed_count(action["gate_action"], since_hours=24)
        if day >= DAILY_CEILING:
            raise ActionError(f"daily ceiling reached ({day}/{DAILY_CEILING} restarts in 24h)"
                              " — root-cause instead of restarting again")
        recent = self.gate.executed_count(action["gate_action"], since_hours=COOLDOWN_MIN / 60)
        if recent:
            raise ActionError(f"cooldown: laatste restart < {COOLDOWN_MIN}m geleden")

    def _maybe_rootcause_task(self, action: dict):
        """Fire-and-forget: runs on a daemon thread with a bounded transport so a
        hung Odoo can never hold the approve request (or its budget lock) hostage."""
        if not self.odoo_cfg:
            return
        week = self.gate.executed_count(action["gate_action"], since_hours=24 * 7)
        if week <= ROOTCAUSE_THRESHOLD:
            return
        threading.Thread(target=self._create_rootcause_task, args=(week,),
                         daemon=True).start()

    def _create_rootcause_task(self, week: int):
        try:
            name = "Root-cause: Stalwart TLS-reload bug (Cerebras auto-melding)"
            common = xmlrpc.client.ServerProxy(
                f"{self.odoo_cfg['url']}/xmlrpc/2/object",
                transport=_TimeoutTransport(15))
            uid, pw, db = self.odoo_cfg["uid"], self.odoo_cfg["password"], self.odoo_cfg["db"]
            existing = common.execute_kw(db, uid, pw, "project.task", "search_count",
                                         [[["name", "=", name], ["stage_id.fold", "=", False]]])
            if existing:
                return
            common.execute_kw(db, uid, pw, "project.task", "create", [{
                "name": name,
                "project_id": int(self.odoo_cfg.get("project_id", 1)),
                "description": f"<p>{week} Stalwart-restarts in 7 dagen via Cerebras. "
                               f"Zie /opt/app/cerebras/data/actions.log.jsonl en "
                               f"memory 'Stalwart TLS + auth' (ReloadTlsCertificates bug). "
                               f"Structurele fix nodig i.p.v. herstarten.</p>"}])
            self.notify("Cerebras: root-cause taak aangemaakt",
                        f"{week} Stalwart restarts in 7d — Odoo taak op Takenbord gezet.")
        except Exception:
            # best-effort by design, but never silently: the traceback goes to
            # the container log so a broken escalation is diagnosable
            traceback.print_exc(file=sys.stderr)
