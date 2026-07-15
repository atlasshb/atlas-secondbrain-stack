# 05 — Pulse: the Monitoring Cockpit

Pulse is a small FastAPI + HTMX app that answers one question at a glance:
*"is everything this stack depends on actually healthy right now?"* It probes
every connector you define in `connectors.yaml` (config-as-data — no code
change needed to add a check), renders red/amber/green tiles with a short
heartbeat history, and supports exactly one class of action: a **gated
self-heal** that always requires a human to click approve before anything
runs.

This doc is based on the real `cerebras/` app in this repo (`app/`,
`connectors.yaml`, `docker-compose.yml`, `deploy/`,
`env.d/cerebras.env.example`), with every host, domain, and internal
identifier replaced by a placeholder.

## Prerequisites

- The foundation stack (doc 01) — Caddy + Authentik — since Pulse expects to
  sit behind `forward_auth` and refuses non-`/healthz` requests without an
  authenticated identity header.
- Whatever you want Pulse to monitor already emitting *something* checkable:
  a JSON status file, an HTTP endpoint, a TCP port, or a TLS certificate.
- Docker + Compose (doc 01).

## `connectors.yaml` — config-as-data health probes

Each connector declares a `probe` (one of `file_age`, `json_file`, `http`,
`tcp`, `tls`, or `multi` — a worst-of combination of the others) plus
thresholds. No Python code is touched to add, remove, or retune a check —
you edit this file and Pulse's background loop (default: every 60s) picks it
up on restart.

```yaml
# connectors.yaml — one entry per thing Pulse should watch.
# ${VARS} are interpolated from env.d/pulse.env at load time.
# `path:` values that aren't absolute resolve relative to this file.

connectors:
  - id: backup_watcher
    title: Backup sync state
    probe:
      kind: json_file
      params:
        path: /var/lib/app/backup_state.json
        label: backup sync state
        status_field: band                 # e.g. "ok" | "warn" | "critical"
        status_map: { ok: ok, warn: warn, critical: crit }
        time_field: last_check
        warn_s: 10800     # watcher runs every 2h; >3h means it died
        crit_s: 25200

  - id: task_bridge
    title: Task-tracker sync feed
    probe:
      kind: file_age
      params:
        path: /opt/app/data/tasks.json
        label: tasks.json
        warn_s: 1800      # bridge runs every 15m
        crit_s: 7200

  - id: mail
    title: Mail server (JMAP + TLS)
    probe:
      kind: multi
      params:
        probes:
          - kind: tcp
            params: { host: stalwart, port: 8096, label: "JMAP :8096" }
          - kind: tls
            params: { host: stalwart, port: 8443, warn_days: 7 }
    needs_you_when: [warn, crit]
    action: mail_restart   # ties this tile to the one gated action below

  - id: whatsapp_gateway
    title: WhatsApp gateway (self-hosted)
    probe:
      kind: http
      params:
        url: http://wa-gateway:8120/instance/connectionState/default
        label: WA gateway
        headers: { apikey: "${WA_GATEWAY_APIKEY}" }
        json_field: instance.state
        status_map: { open: ok, connecting: warn, close: crit }
    needs_you_when: [crit]

  - id: notifications
    title: Push-notification relay
    probe:
      kind: http
      params:
        url: "http://${NTFY_HOST}:8136/v1/health"
        label: ntfy
        json_field: healthy
        status_map: { "True": ok, "False": crit }
    needs_you_when: [crit]
```

Every probe normalizes into the same shape — `{status, last_check, last_good,
reason}` — regardless of what it's checking, which is what lets a single tile
template render all of them.

## `docker-compose.yml`

```yaml
name: app-pulse
services:
  pulse:
    build: ./cerebras
    image: app-pulse:latest
    container_name: pulse-app
    restart: unless-stopped
    mem_limit: 256m
    networks: [edge]
    environment:
      TZ: "${TZ}"
      PULSE_CONNECTORS: /opt/app/pulse/connectors.yaml
      PULSE_DATA_DIR: /opt/app/pulse/data
    env_file:
      - env.d/pulse.env
    volumes:
      # read-only feeds this instance probes
      - /var/lib/app:/var/lib/app:ro
      - /opt/app/data:/opt/app/data:ro
      - /opt/app/notify:/opt/app/notify:ro
      # own config + state (sqlite state db + append-only action log)
      - ./connectors.yaml:/opt/app/pulse/connectors.yaml:ro
      - pulse_data:/opt/app/pulse/data
      # only needed if you wire up the one self-heal action (below)
      - /var/run/docker.sock:/var/run/docker.sock

networks:
  edge:
    external: true

volumes:
  pulse_data:
```

`env.d/pulse.env` (placeholders — never commit real values):

```bash
# WhatsApp gateway API key, if you wire that connector up
WA_GATEWAY_APIKEY=
# Optional: task-tracker RPC for the root-cause escalation task that fires
# after N self-heal runs in 7 days (same convention as other bridges in this stack)
TASKS_URL=http://${TASKS_HOST}:8069
TASKS_DB=app
TASKS_UID=2
TASKS_PASSWORD=
TASKS_PROJECT_ID=1
```

## The FastAPI + HTMX UI

`app/main.py` is deliberately small:

- `GET /` renders the full tile board; `GET /partials/board` and
  `/partials/clock` are HTMX fragment endpoints the page polls, so the board
  refreshes without a full page reload or any client-side JS framework.
- `POST /refresh` forces an immediate re-probe of every connector.
- `GET /healthz` is the one route excluded from the auth middleware — it has
  to stay reachable so an external uptime monitor can check Pulse itself
  without an SSO session.
- Every other route 403s unless the request carries an authenticated-identity
  header (`X-Authentik-...`) injected by Caddy's `forward_auth` — Pulse never
  implements its own login form.
- State-changing POSTs also require a same-origin `Sec-Fetch-Site` (or
  matching `Origin`/`Referer`) header as CSRF defense-in-depth, independent
  of the SSO cookie's `SameSite` setting.
- Jinja2 autoescaping is on everywhere: probe `reason` strings come from
  connector *data* (files/HTTP bodies you don't fully control), so they are
  rendered as text, never interpreted as markup or commands.

## Gated self-heal — how it stays safe

Pulse ships with room for exactly one class of action per connector: a
hard-coded remediation (the reference implementation is `docker restart
<container>` for a mail-TLS-reload connector) that goes through a strict
propose → approve → execute pipeline (`app/executor.py`):

1. **Propose** — a human clicks "propose" in the UI; the action is recorded
   as `pending` in an approvals ledger with `risk=irreversible`, which forces
   a hard gate: nothing about this call path can auto-approve.
2. **Approve** — a *different* click, by an authenticated identity, flips the
   row to `approved` and only then does `executor.py` call the Docker API
   (over the mounted `docker.sock`) to run the **hard-coded** command. The
   command string is never built from request input — approval payloads are
   display-only.
3. **Verify** — after execution, Pulse re-probes the same check (e.g. the TLS
   handshake) and reports whether the fix actually worked, not just that the
   command exited 0.
4. **Guardrails**: a cooldown (default 15 min) and a daily ceiling (default 4)
   are enforced by counting `executed`/`failed` rows in the ledger — not
   in-process memory, so a restart of Pulse itself can't reset the budget.
   If the same action fires more than a handful of times in 7 days, Pulse
   can optionally open a root-cause task in your tracker instead of just
   restarting again forever.
5. **Immutable log**: every propose/approve/execute/verify step is appended
   to a JSONL action log; `GET /log` verifies its hash chain hasn't been
   tampered with.

The approvals ledger itself (`app/gate_client.py`) is a thin adapter: in this
repo it ships a **dev/standalone implementation** — a local SQLite-backed
`FakeGate` that behaves identically to whatever production approval store you
already run, so Pulse works out of the box (`PULSE_DEV=1`) without any
external dependency. If you already have your own approval/audit system,
swap in an adapter with the same four methods (`propose`, `decide`, `get`,
`executed_count`) and Pulse will use it instead — the app fails closed
(refuses to start) rather than silently falling back to the fake gate in
production.

## Setup steps

1. Copy `env.d/cerebras.env.example` to `env.d/pulse.env` and fill in only
   the connectors you actually wired up; leave the rest blank.
2. Edit `connectors.yaml` for your own stack's feeds — start with two or
   three real checks, add more once the pattern feels natural.
3. `docker compose up -d --build`.
4. Add a Caddy site block (reuse the `sec_headers_full` + forward_auth
   pattern from doc 01), bringing up a **staging** subdomain first:

   ```caddyfile
   pulse-staging.${DOMAIN} {
       route {
           reverse_proxy /healthz pulse-app:8141
           reverse_proxy /outpost.goauthentik.io/* authentik-server:9000
           forward_auth authentik-server:9000 {
               uri /outpost.goauthentik.io/auth/caddy
               copy_headers X-Authentik-Username X-Authentik-Groups X-Authentik-Email
               trusted_proxies private_ranges
           }
           reverse_proxy pulse-app:8141
       }
       import sec_headers_full
   }
   ```
5. Reload Caddy (`caddy validate` then `systemctl reload caddy`, or restart
   the Caddy container), confirm `/healthz` is reachable without SSO and `/`
   redirects to your Authentik login.
6. Promote the staging block to your real subdomain once you're happy with
   what the tiles show — the `deploy/` scripts in `cerebras/` are a working
   reference for a scripted stage → verify → go-live flow you can adapt.

## Verification

```bash
# liveness — must succeed without any auth header
curl -s http://127.0.0.1:8141/healthz

# auth enforcement — must 403 without an identity header
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8141/
# expect: 403

# via Caddy, unauthenticated client path
curl -s -o /dev/null -w '%{http_code} -> %{redirect_url}\n' https://pulse-staging.${DOMAIN}/
```

## How this ties into the Second Brain

Pulse doesn't feed *content* into the corpus the way mail or files do — it
feeds **operational trust**. The Second Brain is only as useful as the
sources it recalls from; if the mail server's TLS is silently broken or the
ingestion bridge died three hours ago, recall results go stale without any
signal. Pulse turns "is my brain's data still fresh" from a thing you'd have
to remember to check into a single glanceable board, and its action log gives
you an auditable history of every automated fix — which is itself exactly the
kind of operational record worth feeding back into the corpus later.
