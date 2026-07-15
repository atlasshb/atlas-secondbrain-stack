"""Immutable action log: append-only JSONL with a SHA-256 hash chain.

Every record embeds the hash of the previous record, so any edit or deletion
breaks the chain and is detectable with verify(). The file is opened in
append mode only; on the host it should additionally get `chattr +a`
(done by deploy script) so even root-owned processes can only append.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


class ActionLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _last_hash(self) -> str:
        # a torn/unparseable trailing line (ENOSPC, kill mid-append) must not
        # wedge appends forever: chain from the last PARSEABLE record; verify()
        # will still report the damaged line as a broken chain.
        if not self.path.exists() or self.path.stat().st_size == 0:
            return GENESIS
        last = GENESIS
        with self.path.open("rb") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    last = json.loads(line)["hash"]
                except (ValueError, KeyError, TypeError):
                    continue
        return last

    def append(self, event: str, **fields) -> dict:
        """Append one record. event: proposed|approved|rejected|executed|verified|failed."""
        with self._lock:
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": event,
                **fields,
                "prev": self._last_hash(),
            }
            payload = json.dumps(rec, sort_keys=True, ensure_ascii=False)
            rec["hash"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            line = json.dumps(rec, sort_keys=True, ensure_ascii=False)
            # if a previous append was torn mid-line (ENOSPC/kill), isolate the
            # fragment on its own line so the new record stays parseable
            prefix = ""
            if self.path.exists() and self.path.stat().st_size:
                with self.path.open("rb") as f:
                    f.seek(-1, 2)
                    if f.read(1) != b"\n":
                        prefix = "\n"
            # O_APPEND: atomic appends, plays nice with chattr +a
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
            try:
                os.write(fd, (prefix + line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
            return rec

    def verify(self) -> tuple[bool, str]:
        """Walk the chain; returns (intact, detail)."""
        if not self.path.exists():
            return True, "empty log"
        prev = GENESIS
        n = 0
        with self.path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    claimed = rec.pop("hash")
                except (ValueError, KeyError, TypeError):
                    return False, f"chain broken at line {i}: unparseable record"
                if rec.get("prev") != prev:
                    return False, f"chain broken at line {i}: prev mismatch"
                payload = json.dumps(rec, sort_keys=True, ensure_ascii=False)
                if hashlib.sha256(payload.encode("utf-8")).hexdigest() != claimed:
                    return False, f"chain broken at line {i}: hash mismatch"
                prev = claimed
                n += 1
        return True, f"{n} records intact"

    def tail(self, n: int = 50) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for l in self.path.read_text(encoding="utf-8").splitlines():
            if not l.strip():
                continue
            try:
                out.append(json.loads(l))
            except ValueError:
                out.append({"ts": "?", "event": "unparseable", "hash": "-" * 12})
        return out[-n:]
