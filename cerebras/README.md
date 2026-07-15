# Cerebras — Phase 0 (Pulse cockpit)

Branded ops cockpit ON TOP of the existing Atlas Mind engine. Phase 0 =
read-only Pulse tile grid + needs-you strip + exactly ONE approval-gated
self-heal.

## What it does
- Normalises six existing host feeds into `{status, last_check, last_good, reason}`
  (see `connectors.yaml`): bank_sync_watcher band, tasks.json freshness,
  Stalwart JMAP/TLS, Evolution instance state, commerce_sensor run, ntfy health.
- Renders them as a tile grid with heartbeat history; crit/pending items float
  into the **needs-you** strip (empty when healthy).
- ONE action: `docker restart atlas-stalwart` (the recurring ReloadTlsCertificates
  fix) — proposed → parked by **mind/gate.py** (`risk=irreversible` ⇒ HARD_GATE)
  → human approve in UI → hard-coded Docker-API restart → TLS re-probe on :8443
  → auto tile-flip → cooldown 15m / max 4-per-24h / root-cause Odoo task past 3-in-7d.
- Immutable action log: append-only JSONL + SHA-256 hash chain (`/log` verifies),
  `chattr +a` applied on the host file.

## What it deliberately does NOT do (reuse mandate)
- No second Uptime-Kuma, no Langfuse clone, no new notify pipe, no new approval
  store: it imports `gate.py`/`atlas_notify.py` and writes the existing
  `ledger.sqlite`/`kernel.sqlite` via them.
- Note: spec said "JMAP :8096 TLS probe" — :8096 is plaintext HTTP on this box;
  the TLS bug manifests on :8443, so the tile probes both (tcp:8096 + tls:8443).

## Security invariants (do not weaken)
- Every action goes through `gate.request()`; `Executor.require_approvable()`
  refuses rows that aren't ours/pending. Tests: `tests/test_executor.py`.
- The executed command is hard-coded; approval payloads are display-only
  (injection-safety: untrusted feed content is DATA, never instructions;
  Jinja autoescape everywhere).
- docker.sock is mounted for exactly this one hard-coded restart; the app
  offers no arbitrary-command surface. Revisit before adding action #2
  (move to a host-side effector like `mind/send_effector.py`).
- App refuses non-`/healthz` requests without `X-Authentik-Username`
  (defense-in-depth behind Caddy forward_auth).

## Run (dev)
```
python tests/dev_seed.py
CEREBRAS_DEV=1 CEREBRAS_CONNECTORS=connectors.dev.yaml CEREBRAS_DATA_DIR=devdata \
  uvicorn app.main:app --port 8141
```

## Deploy
`/opt/app/cerebras`, `docker compose up -d --build`, Caddy snippet in
`deploy/caddy-cerebras.snippet` (staging host first; live block after
REVIEW-gate sign-off). `deploy/patch_gate_hardgate.py` applies the
blueprint-critic HARD_GATE additions (+calendar, +memory_write) with
backup + py_compile + auto-revert.

## Tests
`python -m unittest discover -s tests -v` — 20+ cases: normaliser fixtures
(incl. malformed feeds), gate no-bypass, cooldown/ceiling, hash-chain tamper.
