# 02 — Data Containers: Odoo, Nextcloud, Firefly III

These three containers are the "life data" sources the Second Brain later
ingests: tasks/CRM (Odoo), files/documents (Nextcloud), and personal finance
(Firefly III). Each runs as a standard upstream Docker image behind the
Caddy + Authentik foundation from doc 01.

## Prerequisites

- The foundation stack from `01-vps-foundation.md` is up (`edge` network,
  Caddy, Authentik).
- DNS records for the subdomains you plan to use, e.g. `tasks.${DOMAIN}`,
  `files.${DOMAIN}`, `money.${DOMAIN}`.

## Compose snippet

```yaml
name: app-data
services:
  # ---- Odoo (ERP / tasks / CRM) ----
  odoo-db:
    image: postgres:16-alpine
    container_name: odoo-db
    restart: unless-stopped
    networks: [edge]
    environment:
      POSTGRES_USER: "${ODOO_DB_USER}"
      POSTGRES_PASSWORD: "${ODOO_DB_PASSWORD}"
      POSTGRES_DB: postgres
    volumes:
      - odoo_db:/var/lib/postgresql/data

  odoo:
    image: odoo:17
    container_name: odoo
    restart: unless-stopped
    networks: [edge]
    depends_on: [odoo-db]
    environment:
      HOST: odoo-db
      USER: "${ODOO_DB_USER}"
      PASSWORD: "${ODOO_DB_PASSWORD}"
    volumes:
      - odoo_data:/var/lib/odoo
    # exposed internally only; Caddy reverse-proxies to odoo:8069

  # ---- Nextcloud (files) ----
  nextcloud-db:
    image: postgres:16-alpine
    container_name: nextcloud-db
    restart: unless-stopped
    networks: [edge]
    environment:
      POSTGRES_USER: "${NEXTCLOUD_DB_USER}"
      POSTGRES_PASSWORD: "${NEXTCLOUD_DB_PASSWORD}"
      POSTGRES_DB: nextcloud
    volumes:
      - nextcloud_db:/var/lib/postgresql/data

  nextcloud:
    image: nextcloud:29-apache
    container_name: nextcloud
    restart: unless-stopped
    networks: [edge]
    depends_on: [nextcloud-db]
    environment:
      POSTGRES_HOST: nextcloud-db
      POSTGRES_USER: "${NEXTCLOUD_DB_USER}"
      POSTGRES_PASSWORD: "${NEXTCLOUD_DB_PASSWORD}"
      POSTGRES_DB: nextcloud
      NEXTCLOUD_TRUSTED_DOMAINS: "files.${DOMAIN}"
      TRUSTED_PROXIES: "172.16.0.0/12"
    volumes:
      - nextcloud_data:/var/www/html

  # ---- Firefly III (personal finance) ----
  firefly-db:
    image: postgres:16-alpine
    container_name: firefly-db
    restart: unless-stopped
    networks: [edge]
    environment:
      POSTGRES_USER: "${FIREFLY_DB_USER}"
      POSTGRES_PASSWORD: "${FIREFLY_DB_PASSWORD}"
      POSTGRES_DB: firefly
    volumes:
      - firefly_db:/var/lib/postgresql/data

  firefly:
    image: fireflyiii/core:latest
    container_name: firefly
    restart: unless-stopped
    networks: [edge]
    depends_on: [firefly-db]
    environment:
      APP_KEY: "${FIREFLY_APP_KEY}"
      DB_CONNECTION: pgsql
      DB_HOST: firefly-db
      DB_PORT: "5432"
      DB_DATABASE: firefly
      DB_USERNAME: "${FIREFLY_DB_USER}"
      DB_PASSWORD: "${FIREFLY_DB_PASSWORD}"
      APP_URL: "https://money.${DOMAIN}"
      TRUSTED_PROXIES: "**"
    volumes:
      - firefly_upload:/var/www/html/storage/upload

networks:
  edge:
    external: true

volumes:
  odoo_db:
  odoo_data:
  nextcloud_db:
  nextcloud_data:
  firefly_db:
  firefly_upload:
```

`.env` additions (placeholders only):

```bash
ODOO_DB_USER=odoo
ODOO_DB_PASSWORD=
NEXTCLOUD_DB_USER=nextcloud
NEXTCLOUD_DB_PASSWORD=
FIREFLY_DB_USER=firefly
FIREFLY_DB_PASSWORD=
FIREFLY_APP_KEY=
```

Caddy site blocks (append to `caddy/Caddyfile`, reusing the `sec_headers_full`
snippet and the forward_auth pattern from doc 01 — swap in the real backend
per app):

```caddyfile
tasks.${DOMAIN} {
    route {
        reverse_proxy /outpost.goauthentik.io/* authentik-server:9000
        forward_auth authentik-server:9000 {
            uri /outpost.goauthentik.io/auth/caddy
            copy_headers X-Authentik-Username X-Authentik-Groups X-Authentik-Email
            trusted_proxies private_ranges
        }
        reverse_proxy odoo:8069
    }
    import sec_headers_full
}

files.${DOMAIN} {
    reverse_proxy nextcloud:80
    import sec_headers_full
}

money.${DOMAIN} {
    route {
        reverse_proxy /outpost.goauthentik.io/* authentik-server:9000
        forward_auth authentik-server:9000 {
            uri /outpost.goauthentik.io/auth/caddy
            copy_headers X-Authentik-Username X-Authentik-Groups X-Authentik-Email
            trusted_proxies private_ranges
        }
        reverse_proxy firefly:8080
    }
    import sec_headers_full
}
```

Note: Nextcloud ships its own capable login/2FA and is commonly left on its
native auth rather than forward_auth, to avoid double-guarding WebDAV/sync
clients that can't follow SSO redirects. Put it behind Authentik's forward_auth
too if you want single sign-on to be strict; either way it must sit on the
`edge` network behind Caddy TLS.

## Setup steps

1. `docker compose up -d` for this stack (after the foundation is running).
2. **Odoo**: open `https://tasks.${DOMAIN}`, complete the first-run wizard
   (choose a database name, admin email/password — store the password in your
   own secrets manager, never in `.env` in plaintext long-term). Install the
   *Project* app for a task board.
3. **Nextcloud**: open `https://files.${DOMAIN}`, finish the admin-account
   wizard, and create a folder structure that mirrors what you want ingested
   (e.g. `Documents/`, `Invoices/`).
4. **Firefly III**: open `https://money.${DOMAIN}`, register the first user,
   then either connect a bank-sync integration or import a CSV/GoCardless
   feed to populate transactions.
5. In Authentik, add a Proxy Provider + Application for each subdomain you
   put behind forward_auth (same pattern as doc 01, step 5).

## Verification

```bash
curl -sI https://tasks.${DOMAIN} | head -1
curl -sI https://files.${DOMAIN} | head -1
curl -sI https://money.${DOMAIN} | head -1
docker compose ps   # all three app containers + their DBs "Up"
```

## Why each one is a Second Brain data source

- **Odoo** holds tasks, projects, and CRM activity — the "what am I supposed
  to be doing" signal. Ingesting it lets the brain answer "what's open on
  project X" or "what did I promise client Y" from natural-language recall
  instead of clicking through a UI.
- **Nextcloud** holds documents and files — contracts, notes, exports. Most of
  what a person actually needs to "remember" lives here as unstructured text,
  which is exactly what full-text (FTS5) and dense-embedding recall is built
  to search.
- **Firefly III** holds transactions and budgets — the financial half of a
  personal/business "memory." Feeding its ledger into the corpus lets the
  brain answer spending or cash-flow questions in the same interface as
  everything else, instead of a separate app you have to remember to open.

Each container's data is periodically exported/queried by the ingestion layer
(see `06-ingestion.md`) into the shared corpus that `secondbrain/brain.py`
indexes — these containers are the *sources*, not the brain itself.
