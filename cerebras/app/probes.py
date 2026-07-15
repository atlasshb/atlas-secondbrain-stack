"""Probe implementations. Each probe is a pure-ish function:
   probe_<kind>(params) -> (status, reason)

Stdlib only — no shelling out (injection-safety invariant: connector params
come from connectors.yaml which is operator-owned, but we still never build
shell strings from them).
"""
from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TIMEOUT = 6  # seconds, per probe


def _age_status(age_s: float, warn_s: float, crit_s: float, what: str):
    human = _human_age(age_s)
    if age_s >= crit_s:
        return "crit", f"{what} is {human} old (crit ≥ {_human_age(crit_s)})"
    if age_s >= warn_s:
        return "warn", f"{what} is {human} old (warn ≥ {_human_age(warn_s)})"
    return "ok", f"{what} updated {human} ago"


def _human_age(s: float) -> str:
    s = int(s)
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.1f}d"


def probe_file_age(params: dict):
    """status from a file's mtime.  params: path, warn_s, crit_s, label?"""
    p = Path(params["path"])
    label = params.get("label", p.name)
    if not p.exists():
        return "crit", f"{label} missing ({p})"
    age = time.time() - p.stat().st_mtime
    return _age_status(age, params["warn_s"], params["crit_s"], label)


def probe_json_file(params: dict):
    """status from a field inside a JSON file, and/or an embedded timestamp.

    params:
      path            — JSON file
      status_field?   — dotted path to a value
      status_map?     — {value: status}; unmatched -> warn
      time_field?     — dotted path to ISO timestamp or epoch; aged with warn_s/crit_s
      warn_s/crit_s   — required when time_field or fallback-to-mtime is used
    """
    p = Path(params["path"])
    label = params.get("label", p.name)
    if not p.exists():
        return "crit", f"{label} missing ({p})"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        return "crit", f"{label} unreadable: {e.__class__.__name__}"

    reasons, statuses = [], []

    if params.get("status_field"):
        val = _dig(data, params["status_field"])
        smap = params.get("status_map", {})
        st = smap.get(str(val))
        if st is None:
            statuses.append("warn")
            reasons.append(f"{params['status_field']}={val!r} (unmapped)")
        else:
            statuses.append(st)
            reasons.append(f"{params['status_field']}={val}")

    ts = None
    if params.get("time_field"):
        ts = _parse_ts(_dig(data, params["time_field"]))
    if ts is None and "warn_s" in params:
        ts = p.stat().st_mtime  # fall back to file mtime
    if ts is not None and "warn_s" in params:
        st, why = _age_status(time.time() - ts, params["warn_s"], params["crit_s"], label)
        statuses.append(st)
        reasons.append(why)

    if not statuses:
        return "unknown", f"{label}: no status_field/time_field configured"
    worst = max(statuses, key=_severity)
    return worst, "; ".join(reasons)


def probe_tls(params: dict):
    """TLS handshake against host:port; checks completion + cert expiry.
    params: host, port, warn_days?=7, insecure_name?=False
    """
    host, port = params["host"], int(params["port"])
    ctx = ssl.create_default_context()
    # internal service probed by IP — cert CN won't match, that's expected;
    # the thing that breaks in the wild is the handshake itself (no cert served)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=DEFAULT_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=params.get("sni", host)) as tls:
                cert_bin = tls.getpeercert(binary_form=True)
                if not cert_bin:
                    return "crit", f"TLS handshake OK but no certificate presented on {host}:{port}"
                not_after = _cert_not_after(cert_bin)
        if not_after is not None:
            days = (not_after - datetime.now(timezone.utc)).total_seconds() / 86400
            if days < 0:
                return "crit", f"certificate on {host}:{port} EXPIRED {abs(days):.1f}d ago"
            if days < params.get("warn_days", 7):
                return "warn", f"certificate on {host}:{port} expires in {days:.1f}d"
            return "ok", f"TLS OK on {host}:{port}, cert valid {days:.0f}d"
        return "ok", f"TLS handshake OK on {host}:{port}"
    except (ssl.SSLError, ConnectionResetError) as e:
        return "crit", f"TLS handshake FAILED on {host}:{port}: {e.__class__.__name__}: {e}"
    except (OSError, socket.timeout) as e:
        return "crit", f"cannot reach {host}:{port}: {e.__class__.__name__}: {e}"


def probe_http(params: dict):
    """GET a URL, optionally check a JSON field.
    params: url, headers?, expect_status?=200, json_field?, status_map?,
            ok_substring?, warn_s/crit_s + time_field? (aged JSON timestamp)
    """
    url = params["url"]
    label = params.get("label", url)
    req = urllib.request.Request(url, headers=params.get("headers", {}))
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            body = resp.read(1 << 20).decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as e:
        return "crit", f"{label}: HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return "crit", f"{label}: unreachable ({getattr(e, 'reason', e)})"

    if code != params.get("expect_status", 200):
        return "crit", f"{label}: HTTP {code}"

    if params.get("json_field"):
        try:
            val = _dig(json.loads(body), params["json_field"])
        except ValueError:
            return "crit", f"{label}: non-JSON response"
        smap = params.get("status_map", {})
        st = smap.get(str(val))
        if st is None:
            return "warn", f"{label}: {params['json_field']}={val!r} (unmapped)"
        return st, f"{label}: {params['json_field']}={val}"

    if params.get("ok_substring") and params["ok_substring"] not in body:
        return "crit", f"{label}: expected content missing"
    return "ok", f"{label}: HTTP {code}"


# ---------------------------------------------------------------- helpers

def _dig(data, dotted: str):
    cur = data
    for part in dotted.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _parse_ts(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        # epoch seconds (or ms)
        return val / 1000 if val > 1e12 else float(val)
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _severity(status: str) -> int:
    return {"ok": 0, "unknown": 1, "warn": 2, "crit": 3}[status]


def _cert_not_after(cert_bin: bytes) -> datetime | None:
    """Extract notAfter from DER cert without external deps.

    ssl only exposes parsed certs when verification is on; with CERT_NONE we
    get DER. A UTCTime/GeneralizedTime scan is enough for a monitoring hint —
    validity is the second SEQUENCE-of-times in a cert, i.e. the 2nd time blob.
    """
    times = []
    i = 0
    while i < len(cert_bin) - 2 and len(times) < 2:
        tag = cert_bin[i]
        ln = cert_bin[i + 1]
        # only commit to a skip when the blob really parses as a time of
        # plausible length — 0x17/0x18 bytes also occur in serials/keys
        if tag in (0x17, 0x18) and ln in (13, 15):
            raw = cert_bin[i + 2 : i + 2 + ln].decode("ascii", "replace")
            t = _asn1_time(raw, tag)
            if t:
                times.append(t)
                i += 2 + ln
                continue
        i += 1
    return times[1] if len(times) == 2 else None


def _asn1_time(raw: str, tag: int) -> datetime | None:
    try:
        if tag == 0x17:  # YYMMDDHHMMSSZ
            dt = datetime.strptime(raw, "%y%m%d%H%M%SZ")
        else:  # YYYYMMDDHHMMSSZ
            dt = datetime.strptime(raw, "%Y%m%d%H%M%SZ")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def probe_tcp(params: dict):
    """Plain TCP connect check. params: host, port, label?"""
    host, port = params["host"], int(params["port"])
    label = params.get("label", f"{host}:{port}")
    try:
        with socket.create_connection((host, port), timeout=DEFAULT_TIMEOUT):
            return "ok", f"{label} accepting connections"
    except (OSError, socket.timeout) as e:
        return "crit", f"{label} unreachable: {e.__class__.__name__}"


def probe_multi(params: dict):
    """Worst-of over sub-probes. params: probes: [{kind, params}, ...]"""
    results = [run_probe(p["kind"], p.get("params", {})) for p in params["probes"]]
    worst = max(results, key=lambda r: _severity(r[0]))[0]
    return worst, "; ".join(r[1] for r in results)


PROBES = {
    "file_age": probe_file_age,
    "json_file": probe_json_file,
    "tls": probe_tls,
    "http": probe_http,
    "tcp": probe_tcp,
    "multi": probe_multi,
}


def run_probe(kind: str, params: dict):
    fn = PROBES.get(kind)
    if fn is None:
        return "unknown", f"unknown probe kind {kind!r}"
    try:
        return fn(params)
    except Exception as e:  # a broken probe must never take down the cockpit
        return "unknown", f"probe error: {e.__class__.__name__}: {e}"
