# 01 — VPS Foundation: Docker, Caddy (TLS), Authentik (SSO)

Every other doc in this stack assumes this foundation is in place: a single VPS
running Docker, fronted by Caddy for automatic HTTPS, with Authentik providing
single sign-on (SSO) via `forward_auth` so that internal tools don't each need
their own login system.

## Prerequisites

- A VPS (2 vCPU / 4 GB RAM is enough to start; the data containers in doc 02
  will want more as your corpus grows) running a recent Debian/Ubuntu LTS.
- A domain you control, with the ability to create DNS `A`/`AAAA` records
  (e.g. `${DOMAIN}` and `*.${DOMAIN}` pointing at the VPS's public IP).
- SSH access with a non-root sudo user.
- Ports 80 and 443 open (Caddy needs these for the ACME HTTP-01 challenge).

## 1. Install Docker + the Compose plugin

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
# log out/in (or `newgrp docker`) for the group change to take effect
docker compose version
```

## 2. Lay out the project directory

```bash
sudo mkdir -p /opt/app/{caddy,authentik,secrets}
cd /opt/app
```

Everything below assumes a single `docker-compose.yml` per logical stack
(foundation, data containers, mail, Pulse) joined by a shared external Docker
network so Caddy can reverse-proxy into all of them.

```bash
docker network create edge
```

## 3. Caddy — automatic TLS reverse proxy

Caddy gets you free, auto-renewing Let's Encrypt certificates with zero manual
ACME configuration — point a `Caddyfile` at a domain and a backend, and it
handles the rest.

`docker-compose.yml` (foundation stack):

```yaml
name: app-foundation
services:
  caddy:
    image: caddy:2
    container_name: caddy
    restart: unless-stopped
    networks: [edge]
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config

  authentik-server:
    image: ghcr.io/goauthentik/server:2024.8
    container_name: authentik-server
    restart: unless-stopped
    command: server
    networks: [edge]
    environment:
      AUTHENTIK_SECRET_KEY: "${AUTHENTIK_SECRET_KEY}"
      AUTHENTIK_POSTGRESQL__HOST: authentik-db
      AUTHENTIK_POSTGRESQL__USER: "${AUTHENTIK_DB_USER}"
      AUTHENTIK_POSTGRESQL__PASSWORD: "${AUTHENTIK_DB_PASSWORD}"
      AUTHENTIK_REDIS__HOST: authentik-redis
    depends_on: [authentik-db, authentik-redis]

  authentik-worker:
    image: ghcr.io/goauthentik/server:2024.8
    container_name: authentik-worker
    restart: unless-stopped
    command: worker
    networks: [edge]
    environment:
      AUTHENTIK_SECRET_KEY: "${AUTHENTIK_SECRET_KEY}"
      AUTHENTIK_POSTGRESQL__HOST: authentik-db
      AUTHENTIK_POSTGRESQL__USER: "${AUTHENTIK_DB_USER}"
      AUTHENTIK_POSTGRESQL__PASSWORD: "${AUTHENTIK_DB_PASSWORD}"
      AUTHENTIK_REDIS__HOST: authentik-redis
    depends_on: [authentik-db, authentik-redis]

  authentik-db:
    image: postgres:16-alpine
    container_name: authentik-db
    restart: unless-stopped
    networks: [edge]
    environment:
      POSTGRES_USER: "${AUTHENTIK_DB_USER}"
      POSTGRES_PASSWORD: "${AUTHENTIK_DB_PASSWORD}"
      POSTGRES_DB: authentik
    volumes:
      - authentik_db:/var/lib/postgresql/data

  authentik-redis:
    image: redis:7-alpine
    container_name: authentik-redis
    restart: unless-stopped
    networks: [edge]

networks:
  edge:
    external: true

volumes:
  caddy_data:
  caddy_config:
  authentik_db:
```

`.env` for this stack (placeholders — generate real values locally, never commit them):

```bash
AUTHENTIK_SECRET_KEY=
AUTHENTIK_DB_USER=authentik
AUTHENTIK_DB_PASSWORD=
```

Example `caddy/Caddyfile` — one site block for the Authentik login/admin UI
itself, plus the reusable snippet every other app in this stack imports for
its own SSO gate:

```caddyfile
# Authentik's own UI
auth.${DOMAIN} {
    reverse_proxy authentik-server:9000
}

# Reusable security headers, imported by every app site block
(sec_headers_full) {
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "SAMEORIGIN"
        Referrer-Policy "strict-origin-when-cross-origin"
    }
}

# Template for any app that should sit behind Authentik forward_auth.
# Copy this block per-app (see docs 02/03/05) and swap the two backend addresses.
app.${DOMAIN} {
    route {
        reverse_proxy /outpost.goauthentik.io/* authentik-server:9000
        forward_auth authentik-server:9000 {
            uri /outpost.goauthentik.io/auth/caddy
            copy_headers X-Authentik-Username X-Authentik-Groups X-Authentik-Email X-Authentik-Name X-Authentik-Uid
            trusted_proxies private_ranges
        }
        reverse_proxy app-backend:8080
    }
    import sec_headers_full
}
```

## 4. Bring the foundation up

```bash
docker compose up -d
docker compose logs -f caddy   # watch it obtain the certificate for auth.${DOMAIN}
```

## 5. First-run Authentik setup

1. Visit `https://auth.${DOMAIN}/if/flow/initial-setup/` and create the akadmin
   password.
2. In the Authentik admin UI, create a **Proxy Provider** per app you want to
   protect (mode: *forward auth (single application)*), pointing its
   `external_host` at that app's future subdomain.
3. Create an **Application** bound to that provider, and add it to an
   **Embedded Outpost** (or a dedicated one) so `/outpost.goauthentik.io/*`
   is served for that host.
4. Each downstream doc (02, 03, 05) reuses the Caddyfile template above,
   substituting its own subdomain and backend port/service name.

## Verification

```bash
curl -sI https://auth.${DOMAIN} | head -1        # expect: HTTP/2 200 (or 302 to login)
docker compose ps                                 # all containers "healthy"/"Up"
```

## How this ties into the Second Brain

This layer is the **trust boundary** for the whole stack: every data source
(Odoo, Nextcloud, Firefly III, mail) and the Pulse cockpit sit behind the same
Caddy + Authentik gate, so there is exactly one login, one place to revoke
access, and one place to add security headers — instead of each container
inventing its own auth. The Second Brain itself doesn't call this gate
directly (it runs as a local process reading local files/DBs), but every
*source* it ingests from is only reachable, from the outside, through this
foundation — which is what makes it safe to expose the stack on the public
internet at all.
