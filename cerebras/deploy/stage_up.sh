#!/usr/bin/env bash
# Cerebras Phase 0 — one-shot STAGING bring-up on your host.
# Idempotent. Run as root:  bash /opt/app/cerebras/deploy/stage_up.sh
set -euo pipefail

cd /opt/app/cerebras

echo "== 1/6 build + start container"
docker compose up -d --build

echo "== 2/6 wait for /healthz"
for i in $(seq 1 30); do
  code=$(curl -s -o /tmp/cerebras_hz.json -w '%{http_code}' http://127.0.0.1:8141/healthz || true)
  [ "$code" = "200" ] && break
  sleep 2
done
cat /tmp/cerebras_hz.json; echo
[ "$code" = "200" ] || { echo "healthz not OK ($code) — aborting before Caddy"; docker logs --tail 40 atlas-cerebras-app; exit 1; }

echo "== 3/6 app-level auth check (no header -> 403)"
test "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8141/)" = "403" \
  && echo "OK: refuses without X-Authentik-Username"

echo "== 4/6 immutable action log: chattr +a"
touch data/actions.log.jsonl
chattr +a data/actions.log.jsonl || echo "WARN: chattr failed (non-ext4?)"
lsattr data/actions.log.jsonl || true

echo "== 5/6 Caddy staging block"
if ! grep -q "cerebras-staging.your-domain.tld" /etc/caddy/Caddyfile; then
  cp -a /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak-cerebras-stage-$(date +%Y%m%d-%H%M%S)"
  # staging block only — the live cerebras. block is added at go-live
  sed -n '/^cerebras-staging/,/^}/p' deploy/caddy-cerebras.snippet | head -1 >/dev/null # sanity
  awk '/^# LIVE block/{exit} {print}' deploy/caddy-cerebras.snippet >> /etc/caddy/Caddyfile
  caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy
else
  echo "staging block already present"
fi

echo "== 6/6 external verification (unauthenticated client path)"
sleep 3
echo "- /healthz (must bypass SSO, 200/503):"
curl -s -o /dev/null -w '  %{http_code}\n' https://cerebras-staging.your-domain.tld/healthz || true
echo "- / (must redirect to Authentik):"
curl -s -o /dev/null -w '  %{http_code} -> %{redirect_url}\n' https://cerebras-staging.your-domain.tld/ || true

python3 /opt/app/notify/atlas_notify.py "Cerebras: STAGING LIVE" \
  "https://cerebras-staging.your-domain.tld draait (Authentik SSO). Review-gate: check tiles + gated action, geef akkoord voor go-live." || true
echo "DONE — staging up."
