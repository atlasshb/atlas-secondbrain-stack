# 03 — Mail: Stalwart Mail Server + JMAP

Stalwart is a modern, all-in-one mail server (SMTP + IMAP + JMAP + Sieve) that
runs comfortably as a single container. This stack uses it as the self-hosted
mailbox, and exposes its **JMAP** API on port 8096 so the Second Brain's
ingestion layer can pull mail into the corpus without scraping IMAP.

## Prerequisites

- The foundation stack (doc 01) is up — Caddy for TLS, Authentik if you want
  the web-admin behind SSO.
- MX/A DNS records for your mail domain pointed at the VPS, and reverse DNS
  (PTR) set on the VPS IP — required for outbound deliverability regardless
  of which mail server you run.
- Ports 25 (SMTP), 465/587 (submission), 143/993 (IMAP) reachable from the
  internet; JMAP (8096) only needs to be reachable from the Second Brain
  ingestion process (keep it internal to the `edge` network unless you need
  external JMAP clients).

## Compose snippet

```yaml
name: app-mail
services:
  stalwart:
    image: stalwartlabs/mail-server:latest
    container_name: stalwart
    restart: unless-stopped
    networks: [edge]
    ports:
      - "25:25"      # SMTP
      - "465:465"    # SMTPS (submission)
      - "587:587"    # STARTTLS submission
      - "143:143"    # IMAP
      - "993:993"    # IMAPS
      # JMAP stays internal to the `edge` network; Caddy/ingestion reach it
      # at stalwart:8096 without publishing it to the host.
    volumes:
      - stalwart_data:/opt/stalwart-mail/data
      - stalwart_config:/opt/stalwart-mail/etc

networks:
  edge:
    external: true

volumes:
  stalwart_data:
  stalwart_config:
```

Caddy block for the admin UI + JMAP, behind Authentik forward_auth (see
doc 01 for the reusable `sec_headers_full` snippet):

```caddyfile
mail-admin.${DOMAIN} {
    route {
        reverse_proxy /outpost.goauthentik.io/* authentik-server:9000
        forward_auth authentik-server:9000 {
            uri /outpost.goauthentik.io/auth/caddy
            copy_headers X-Authentik-Username X-Authentik-Groups X-Authentik-Email
            trusted_proxies private_ranges
        }
        reverse_proxy stalwart:8080
    }
    import sec_headers_full
}
```

## Setup steps

1. `docker compose up -d`.
2. On first boot, Stalwart prints an initial admin password to its container
   logs — capture it immediately: `docker logs stalwart 2>&1 | grep -i password`.
3. Open the admin UI (`https://mail-admin.${DOMAIN}` or directly at the
   container's web port) and:
   - Add your mail domain and enable DKIM (the UI generates the keypair —
     add the printed TXT record to your DNS).
   - Create the mailbox(es) you want the Second Brain to read.
   - Under **Server → Listener**, confirm JMAP is enabled on port 8096.
4. Enable JMAP for the account(s) you'll ingest from (Stalwart supports
   per-account or server-wide JMAP; enable it for the ingestion account).
5. Generate an app-specific credential (or reuse the account password) for
   the ingestion script to authenticate with JMAP — store it only in the
   ingestion process's own env file, never committed.
6. Point `secondbrain/feed.sh` (or your ingestion cron) at
   `http://stalwart:8096/jmap/` with those credentials — see
   `06-ingestion.md` for the pull-and-chunk step.

## Verification

```bash
# JMAP session endpoint should return a JSON capabilities document
docker exec stalwart curl -s -u '<mailbox>:<app-password>' \
  http://127.0.0.1:8096/.well-known/jmap | head -c 200

# TLS handshake on the STARTTLS submission port
openssl s_client -connect mail.${DOMAIN}:587 -starttls smtp </dev/null 2>/dev/null | grep "Verify return code"
```

## How this ties into the Second Brain

Mail is one of the highest-signal, most time-sensitive data sources a personal
or business brain can have — invoices, client requests, confirmations, and
decisions all arrive there first. JMAP is deliberately chosen over IMAP for
ingestion because it's a modern JSON API (batchable, easy to poll
incrementally by state token) rather than a stateful line protocol — the
ingestion script can ask "what changed since state X" and get back exactly
the new/changed messages to chunk into the corpus. Once ingested, a question
like "what did the supplier quote us last month" becomes a `brain.py recall`
call instead of a mailbox search, and mail sits in the same fused
FTS5+embedding index as tasks, files, and transactions.
