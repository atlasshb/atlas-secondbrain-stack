#!/usr/bin/env bash
# Cerebras Phase 0 — GO-LIVE after operator sign-off at the review gate.
# Adds the cerebras.your-domain.tld Caddy block, points the Authentik
# provider at the live host, flips the kanban cards, fires ntfy.
set -euo pipefail
cd /opt/app/cerebras

if ! grep -q "^cerebras.your-domain.tld" /etc/caddy/Caddyfile; then
  cp -a /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak-cerebras-live-$(date +%Y%m%d-%H%M%S)"
  cat >> /etc/caddy/Caddyfile <<'EOF'

# ── Cerebras Pulse cockpit (LIVE) — Authentik SSO via embedded outpost ──
cerebras.your-domain.tld {
    route {
        reverse_proxy /healthz 127.0.0.1:8141
        reverse_proxy /outpost.goauthentik.io/* 127.0.0.1:8095
        forward_auth 127.0.0.1:8095 {
            uri /outpost.goauthentik.io/auth/caddy
            copy_headers X-Authentik-Username X-Authentik-Groups X-Authentik-Email X-Authentik-Name X-Authentik-Uid X-Authentik-Jwt X-Authentik-Meta-Jwks X-Authentik-Meta-Outpost X-Authentik-Meta-Provider X-Authentik-Meta-App X-Authentik-Meta-Version
            trusted_proxies private_ranges
        }
        reverse_proxy 127.0.0.1:8141
    }
    import sec_headers_full
}
EOF
  caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy
fi

docker exec atlas-authentik-server ak shell -c "
from authentik.providers.proxy.models import ProxyProvider
p = ProxyProvider.objects.get(name='cerebras')
p.external_host = 'https://cerebras.your-domain.tld'
p.save()
print('provider ->', p.external_host)
" 2>/dev/null | tail -1

echo "- live /healthz:"
sleep 3
curl -s -o /dev/null -w '  %{http_code}\n' https://cerebras.your-domain.tld/healthz || true

# optional: mark the relevant tasks done in your own tracker, e.g.
# for t in "${TASK_IDS[@]}"; do
#   python3 /opt/app/scripts/kanban.py log "$t" done "Cerebras P0 live op cerebras.your-domain.tld" cerebras --commit
# done

python3 /opt/app/notify/atlas_notify.py "Cerebras: LIVE" \
  "https://cerebras.your-domain.tld (Authentik SSO)."
echo "DONE — live."
