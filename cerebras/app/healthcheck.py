"""Container healthcheck: 200 and 503 are both 'alive' (503 = degraded)."""
import sys
import urllib.error
import urllib.request

try:
    status = urllib.request.urlopen("http://127.0.0.1:8141/healthz", timeout=8).status
except urllib.error.HTTPError as e:
    status = e.code
except Exception:
    sys.exit(1)
sys.exit(0 if status in (200, 503) else 1)
