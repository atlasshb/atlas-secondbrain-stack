"""Unified connector state model — the one shape every feed normalises into.

status: ok | warn | crit | unknown
last_check: when the probe last ran (UTC ISO)
last_good: when the feed was last seen ok (persisted across restarts)
reason: one human sentence explaining the current status
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("ok", "warn", "crit", "unknown")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


@dataclass
class TileState:
    id: str
    title: str
    status: str = "unknown"
    last_check: str | None = None
    last_good: str | None = None
    reason: str = "not probed yet"
    needs_you: bool = False
    action_id: str | None = None  # self-heal action offered on this tile, if any
    group: str = "core"

    def as_dict(self) -> dict:
        return asdict(self)


class StateStore:
    """Tiny sqlite persistence for last_good/last_check per connector.

    Deliberately NOT kernel.sqlite — the kernel stays owned by mind/;
    this is presentation-layer state only.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        con = self._con()
        con.execute(
            "CREATE TABLE IF NOT EXISTS tile_state ("
            " id TEXT PRIMARY KEY, status TEXT, last_check TEXT,"
            " last_good TEXT, reason TEXT)"
        )
        con.commit()
        con.close()

    def _con(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def load(self, tile: TileState) -> TileState:
        with self._lock:
            con = self._con()
            row = con.execute(
                "SELECT status, last_check, last_good, reason FROM tile_state WHERE id=?",
                (tile.id,),
            ).fetchone()
            con.close()
        if row:
            tile.status, tile.last_check, tile.last_good, tile.reason = row
        return tile

    def save(self, tile: TileState) -> None:
        with self._lock:
            con = self._con()
            con.execute(
                "INSERT INTO tile_state(id,status,last_check,last_good,reason)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET status=excluded.status,"
                " last_check=excluded.last_check, last_good=excluded.last_good,"
                " reason=excluded.reason",
                (tile.id, tile.status, tile.last_check, tile.last_good, tile.reason),
            )
            con.commit()
            con.close()


def apply_probe_result(tile: TileState, status: str, reason: str) -> TileState:
    """Fold a probe result into the tile, maintaining last_good semantics."""
    if status not in STATUSES:
        # a misconfigured status_map must degrade the tile, never wedge it
        status, reason = "unknown", f"probe returned invalid status {status!r}: {reason}"
    now = iso(utcnow())
    tile.status = status
    tile.reason = reason
    tile.last_check = now
    if status == "ok":
        tile.last_good = now
    return tile
