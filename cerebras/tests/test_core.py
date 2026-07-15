import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.model import TileState, StateStore, apply_probe_result
from app.probes import run_probe, _dig, _parse_ts, _human_age
from app.action_log import ActionLog, GENESIS


class TestModel(unittest.TestCase):
    def test_apply_ok_sets_last_good(self):
        t = TileState(id="x", title="X")
        apply_probe_result(t, "ok", "fine")
        self.assertEqual(t.status, "ok")
        self.assertIsNotNone(t.last_good)
        self.assertEqual(t.last_check, t.last_good)

    def test_apply_crit_keeps_last_good(self):
        t = TileState(id="x", title="X")
        apply_probe_result(t, "ok", "fine")
        good = t.last_good
        apply_probe_result(t, "crit", "broken")
        self.assertEqual(t.status, "crit")
        self.assertEqual(t.last_good, good)
        self.assertNotEqual(t.last_check, None)

    def test_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            store = StateStore(Path(d) / "s.db")
            t = TileState(id="x", title="X")
            apply_probe_result(t, "warn", "meh")
            store.save(t)
            t2 = store.load(TileState(id="x", title="X"))
            self.assertEqual(t2.status, "warn")
            self.assertEqual(t2.reason, "meh")


class TestProbes(unittest.TestCase):
    def test_file_age_ok_and_crit(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            p = f.name
        try:
            st, why = run_probe("file_age", {"path": p, "warn_s": 60, "crit_s": 120})
            self.assertEqual(st, "ok")
            old = time.time() - 7200
            os.utime(p, (old, old))
            st, why = run_probe("file_age", {"path": p, "warn_s": 60, "crit_s": 120})
            self.assertEqual(st, "crit")
        finally:
            os.unlink(p)

    def test_file_age_missing(self):
        st, why = run_probe("file_age", {"path": "/nope/never", "warn_s": 1, "crit_s": 2})
        self.assertEqual(st, "crit")
        self.assertIn("missing", why)

    def test_json_status_map(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"band": "green", "nested": {"state": "open"}}, f)
            p = f.name
        try:
            st, _ = run_probe("json_file", {
                "path": p, "status_field": "band",
                "status_map": {"green": "ok", "amber": "warn", "red": "crit"}})
            self.assertEqual(st, "ok")
            st, _ = run_probe("json_file", {
                "path": p, "status_field": "nested.state",
                "status_map": {"open": "ok"}})
            self.assertEqual(st, "ok")
            st, why = run_probe("json_file", {
                "path": p, "status_field": "band", "status_map": {"red": "crit"}})
            self.assertEqual(st, "warn")  # unmapped value -> warn
        finally:
            os.unlink(p)

    def test_json_time_field_worst_of(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"band": "green", "run_ts": "2020-01-01T00:00:00+00:00"}, f)
            p = f.name
        try:
            st, why = run_probe("json_file", {
                "path": p, "status_field": "band",
                "status_map": {"green": "ok"},
                "time_field": "run_ts", "warn_s": 3600, "crit_s": 86400})
            self.assertEqual(st, "crit")  # stale timestamp dominates ok band
        finally:
            os.unlink(p)

    def test_unknown_kind_and_probe_error(self):
        st, _ = run_probe("nope", {})
        self.assertEqual(st, "unknown")
        st, why = run_probe("json_file", {})  # missing required key -> caught
        self.assertEqual(st, "unknown")
        self.assertIn("probe error", why)

    def test_tls_against_real_endpoint(self):
        # closed port -> crit, unreachable
        st, why = run_probe("tls", {"host": "127.0.0.1", "port": 1})
        self.assertEqual(st, "crit")

    def test_helpers(self):
        self.assertEqual(_dig({"a": {"b": [10, 20]}}, "a.b.1"), 20)
        self.assertIsNone(_dig({"a": 1}, "a.b"))
        self.assertAlmostEqual(_parse_ts(1700000000), 1700000000)
        self.assertAlmostEqual(_parse_ts(1700000000000), 1700000000)  # ms
        self.assertIsNotNone(_parse_ts("2026-07-03T10:00:00Z"))
        self.assertIsNone(_parse_ts("garbage"))
        self.assertEqual(_human_age(30), "30s")


class TestActionLog(unittest.TestCase):
    def test_chain_append_and_verify(self):
        with tempfile.TemporaryDirectory() as d:
            log = ActionLog(Path(d) / "a.jsonl")
            r1 = log.append("proposed", action="stalwart_restart", by="operator")
            r2 = log.append("approved", action="stalwart_restart", by="operator")
            self.assertEqual(r1["prev"], GENESIS)
            self.assertEqual(r2["prev"], r1["hash"])
            ok, detail = log.verify()
            self.assertTrue(ok, detail)
            self.assertEqual(len(log.tail()), 2)

    def test_tamper_detected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.jsonl"
            log = ActionLog(p)
            log.append("proposed", action="x")
            log.append("executed", action="x")
            lines = p.read_text().splitlines()
            rec = json.loads(lines[0])
            rec["action"] = "evil"
            lines[0] = json.dumps(rec, sort_keys=True, ensure_ascii=False)
            p.write_text("\n".join(lines) + "\n")
            ok, detail = log.verify()
            self.assertFalse(ok)
            self.assertIn("broken", detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
