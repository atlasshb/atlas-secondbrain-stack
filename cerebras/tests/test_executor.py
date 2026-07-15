"""Gate no-bypass + budget tests (task #383 acceptance: 'gate no-bypass')."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.action_log import ActionLog
from app.executor import Executor, ActionError, DevRunner, DAILY_CEILING
from app.gate_client import FakeGate


def make_exec(tmp):
    gate = FakeGate(Path(tmp) / "ledger.sqlite")
    log = ActionLog(Path(tmp) / "actions.jsonl")
    sent = []
    ex = Executor(gate, log, lambda t, m: sent.append((t, m)), DevRunner())
    return ex, gate, log, sent


class TestGatedFlow(unittest.TestCase):
    def test_full_flow_propose_approve_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex, gate, log, sent = make_exec(tmp)
            aid = ex.propose("stalwart_restart", "operator")["approval_id"]
            self.assertEqual(len(gate.pending()), 1)
            res = ex.approve_and_run("stalwart_restart", aid, "operator")
            self.assertTrue(res["executed"] and res["verified"])
            self.assertEqual(gate.get(aid)["status"], "executed")
            events = [r["event"] for r in log.tail()]
            self.assertEqual(events, ["proposed", "approved", "executed", "verified"])
            ok, detail = log.verify()
            self.assertTrue(ok, detail)
            self.assertEqual(len(sent), 2)  # proposed + executed ntfy

    def test_no_bypass_without_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex, *_ = make_exec(tmp)
            with self.assertRaises(ActionError):
                ex.approve_and_run("stalwart_restart", 999, "operator")

    def test_no_bypass_foreign_or_decided_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex, gate, log, _ = make_exec(tmp)
            # a foreign (non-cerebras-action) pending row must not be executable
            other = gate.propose("comms", "send_email", "email:x", {}, "external_comms")
            with self.assertRaises(ActionError):
                ex.approve_and_run("stalwart_restart", other["approval_id"], "operator")
            # an already-denied row must not be executable
            aid = ex.propose("stalwart_restart", "operator")["approval_id"]
            ex.reject("stalwart_restart", aid, "operator")
            with self.assertRaises(ActionError):
                ex.approve_and_run("stalwart_restart", aid, "operator")

    def test_unknown_action_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex, *_ = make_exec(tmp)
            with self.assertRaises(ActionError):
                ex.propose("rm_rf_slash", "operator")

    def test_duplicate_proposal_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex, *_ = make_exec(tmp)
            ex.propose("stalwart_restart", "operator")
            with self.assertRaises(ActionError):
                ex.propose("stalwart_restart", "operator")

    def test_daily_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex, gate, *_ = make_exec(tmp)
            con = gate._con()
            for _ in range(DAILY_CEILING):
                con.execute(
                    "INSERT INTO approvals(ts,actor,domain,action,target,status,decided_at)"
                    " VALUES(datetime('now','-2 hours'),'cerebras','fleet','docker_restart',"
                    " 'container:atlas-stalwart','executed',datetime('now','-2 hours'))")
            con.commit()
            con.close()
            with self.assertRaises(ActionError) as cm:
                ex.propose("stalwart_restart", "operator")
            self.assertIn("ceiling", str(cm.exception))

    def test_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex, gate, *_ = make_exec(tmp)
            con = gate._con()
            con.execute(
                "INSERT INTO approvals(ts,actor,domain,action,target,status,decided_at)"
                " VALUES(datetime('now'),'cerebras','fleet','docker_restart',"
                " 'container:atlas-stalwart','executed',datetime('now','-5 minutes'))")
            con.commit()
            con.close()
            with self.assertRaises(ActionError) as cm:
                ex.propose("stalwart_restart", "operator")
            self.assertIn("cooldown", str(cm.exception))


class FailingRunner:
    def restart(self, container):
        raise RuntimeError("docker timeout (restart may still have happened)")

    def reprobe(self):
        return "ok", "unused"


class TestFailureTransition(unittest.TestCase):
    def test_failed_restart_closes_row_and_counts_toward_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = FakeGate(Path(tmp) / "ledger.sqlite")
            log = ActionLog(Path(tmp) / "a.jsonl")
            ex = Executor(gate, log, lambda t, m: None, FailingRunner())
            aid = ex.propose("stalwart_restart", "operator")["approval_id"]
            with self.assertRaises(ActionError):
                ex.approve_and_run("stalwart_restart", aid, "operator")
            row = gate.get(aid)
            self.assertEqual(row["status"], "failed")  # never stranded 'approved'
            # a failed attempt may still have restarted -> consumes cooldown budget
            self.assertEqual(gate.executed_count("docker_restart", 24), 1)
            with self.assertRaises(ActionError) as cm:
                ex.propose("stalwart_restart", "operator")
            self.assertIn("cooldown", str(cm.exception))


class TestFailClosed(unittest.TestCase):
    def test_make_gate_refuses_prod_without_gate_py(self):
        import os
        from app.gate_client import make_gate
        old = os.environ.pop("CEREBRAS_DEV", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["CEREBRAS_MIND_DIR"] = str(Path(tmp) / "nope")
                import app.gate_client as gc
                gc.MIND_DIR = os.environ["CEREBRAS_MIND_DIR"]
                with self.assertRaises(RuntimeError):
                    make_gate(tmp)
        finally:
            if old is not None:
                os.environ["CEREBRAS_DEV"] = old


class TestTornActionLog(unittest.TestCase):
    def test_torn_line_reports_broken_but_keeps_appending(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.jsonl"
            log = ActionLog(p)
            log.append("proposed", action="x")
            with p.open("a", encoding="utf-8") as f:
                f.write('{"ts": "2026-07-03T09:00:00", "event": "torn')  # no newline, no hash
            ok, detail = log.verify()
            self.assertFalse(ok)
            self.assertIn("unparseable", detail)
            log.append("approved", action="x")  # must not raise
            self.assertTrue(any(r.get("event") == "approved" for r in log.tail()))


class TestInvalidStatus(unittest.TestCase):
    def test_invalid_status_coerced_to_unknown(self):
        from app.model import TileState, apply_probe_result
        t = TileState(id="x", title="X")
        apply_probe_result(t, "up", "misconfigured map")
        self.assertEqual(t.status, "unknown")
        self.assertIn("invalid status", t.reason)


class TestMalformedFeeds(unittest.TestCase):
    """Adapter fixtures incl. malformed (task #383)."""

    def test_malformed_json_feed_goes_crit_not_crash(self):
        from app.probes import run_probe
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json!!", encoding="utf-8")
            st, why = run_probe("json_file", {"path": str(bad), "status_field": "x"})
            self.assertEqual(st, "crit")
            self.assertIn("unreadable", why)

    def test_missing_feed_goes_crit(self):
        from app.probes import run_probe
        st, _ = run_probe("json_file", {"path": "/nope.json", "status_field": "x"})
        self.assertEqual(st, "crit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
