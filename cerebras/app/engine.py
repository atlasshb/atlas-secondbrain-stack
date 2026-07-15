"""Connector engine: loads connectors.yaml, runs probes on a background loop,
maintains tile state + a short status history for the heartbeat strip.
"""
from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

import yaml

from .model import TileState, StateStore, apply_probe_result, iso, utcnow
from .probes import run_probe, _severity

HISTORY_KEEP = 48


def _expand_env(obj):
    """${VAR} interpolation inside connectors.yaml string values."""
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    if isinstance(obj, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), obj)
    return obj


def _resolve_paths(obj, base: Path):
    """Relative `path:` params in connectors.yaml resolve against the yaml's dir."""
    if isinstance(obj, dict):
        return {k: (str(base / v) if k == "path" and isinstance(v, str)
                    and not Path(v).is_absolute() else _resolve_paths(v, base))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_paths(v, base) for v in obj]
    return obj


class Engine:
    def __init__(self, config_path: str | Path, data_dir: str | Path,
                 interval_s: int = 60):
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
        self.config = _resolve_paths(_expand_env(raw), Path(config_path).resolve().parent)
        self.interval_s = interval_s
        self.store = StateStore(Path(data_dir) / "cerebras.db")
        self._init_history()
        self._lock = threading.Lock()
        self.tiles: dict[str, TileState] = {}
        self.connectors: dict[str, dict] = {}
        for c in self.config["connectors"]:
            tile = TileState(id=c["id"], title=c["title"],
                             needs_you=False, action_id=c.get("action"),
                             group=c.get("group", "core"))
            self.store.load(tile)
            self.tiles[c["id"]] = tile
            self.connectors[c["id"]] = c
        self.last_loop: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # bounded shared pool: a wedged probe can't leak a thread per cycle
        self._pool = ThreadPoolExecutor(max_workers=max(4, len(self.connectors)),
                                        thread_name_prefix="probe")
        self._inflight: dict[str, object] = {}

    # ------------------------------------------------------------- probing

    def refresh(self, tile_id: str | None = None) -> None:
        ids = [tile_id] if tile_id else list(self.connectors)
        futures = []
        with self._lock:
            for cid in ids:
                prev = self._inflight.get(cid)
                if prev is not None and not prev.done():
                    continue  # previous probe of this connector still running
                fut = self._pool.submit(self._probe_one, cid)
                self._inflight[cid] = fut
                futures.append(fut)
        wait(futures, timeout=30)
        self.last_loop = time.time()

    def _probe_one(self, cid: str) -> None:
        c = self.connectors[cid]
        status, reason = run_probe(c["probe"]["kind"], c["probe"].get("params", {}))
        with self._lock:
            tile = self.tiles[cid]
            prev = tile.status
            apply_probe_result(tile, status, reason)
            tile.needs_you = status in tuple(c.get("needs_you_when", ["crit"]))
            self.store.save(tile)
            self._push_history(cid, status)
        # tile "flips" surface naturally on the next htmx poll / post-action swap

    # ------------------------------------------------------------- history

    def _init_history(self):
        con = self.store._con()
        con.execute("CREATE TABLE IF NOT EXISTS tile_history ("
                    " tile_id TEXT, ts TEXT, status TEXT)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_hist ON tile_history(tile_id, ts)")
        con.commit()
        con.close()

    def _push_history(self, cid: str, status: str):
        con = self.store._con()
        con.execute("INSERT INTO tile_history(tile_id, ts, status) VALUES(?,?,?)",
                    (cid, iso(utcnow()), status))
        con.execute(
            "DELETE FROM tile_history WHERE tile_id=? AND ts NOT IN ("
            " SELECT ts FROM tile_history WHERE tile_id=? ORDER BY ts DESC LIMIT ?)",
            (cid, cid, HISTORY_KEEP))
        con.commit()
        con.close()

    def history(self, cid: str, n: int = 24) -> list[str]:
        con = self.store._con()
        rows = con.execute(
            "SELECT status FROM tile_history WHERE tile_id=? ORDER BY ts DESC LIMIT ?",
            (cid, n)).fetchall()
        con.close()
        return [r[0] for r in reversed(rows)]

    # ------------------------------------------------------------- loop

    def start(self):
        def loop():
            while not self._stop.is_set():
                try:
                    self.refresh()
                except Exception:
                    pass
                self._stop.wait(self.interval_s)
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------- views

    def board(self) -> list[TileState]:
        with self._lock:
            order = {"crit": 0, "warn": 1, "unknown": 2, "ok": 3}
            return sorted(self.tiles.values(), key=lambda t: (order[t.status], t.id))

    def loop_age_s(self) -> float | None:
        return None if self.last_loop is None else time.time() - self.last_loop
