"""Register the Telegram webhook with a public tunnel URL + secret token.

Usage:
    .\\venv\\Scripts\\python.exe _register_webhook.py https://xxxx.trycloudflare.com

Retries setWebhook with backoff because Telegram's DNS resolver often lags a few
seconds behind public DNS for a freshly-minted tunnel hostname, returning
"Failed to resolve host" on the first attempt(s). Also pre-checks that the
tunnel actually reaches the backend (warns on Cloudflare 530 = origin down).
"""
import config  # noqa: F401 — loads .env
import os
import sys
import time

import httpx

ATTEMPTS = 6
BACKOFF_SECONDS = 5

if len(sys.argv) < 2:
    print("Usage: python _register_webhook.py <https://your-tunnel-url>")
    sys.exit(2)

base = sys.argv[1].rstrip("/")
url = f"{base}/webhook"
token = os.environ["TELEGRAM_TOKEN"]
secret = os.environ["TELEGRAM_WEBHOOK_SECRET"]

# Pre-flight: is the tunnel actually serving the backend right now?
try:
    health = httpx.get(f"{base}/health", timeout=10)
    if health.status_code == 200:
        print(f"Tunnel reachable: {base}/health -> 200")
    else:
        print(f"WARNING: {base}/health -> {health.status_code} "
              f"(Cloudflare 530 = tunnel up but backend unreachable; "
              f"check that uvicorn is running on :8000 and restart the tunnel).")
except Exception as e:  # noqa: BLE001
    print(f"WARNING: could not reach {base}/health ({type(e).__name__}). "
          f"The tunnel may still be starting.")

print(f"Registering webhook: {url}")
ok = False
for attempt in range(1, ATTEMPTS + 1):
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json={"url": url, "secret_token": secret, "drop_pending_updates": True},
            timeout=25,
        ).json()
    except Exception as e:  # noqa: BLE001
        print(f"  attempt {attempt}/{ATTEMPTS}: request error {type(e).__name__}")
        time.sleep(BACKOFF_SECONDS)
        continue
    if r.get("ok"):
        print(f"  attempt {attempt}/{ATTEMPTS}: OK — {r.get('description')}")
        ok = True
        break
    desc = r.get("description", "")
    print(f"  attempt {attempt}/{ATTEMPTS}: failed — {desc}")
    # "Failed to resolve host" is the transient resolver-lag case — keep retrying.
    if attempt < ATTEMPTS:
        time.sleep(BACKOFF_SECONDS)

info = httpx.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=15).json().get("result", {})
print("\ngetWebhookInfo:")
for k in ("url", "pending_update_count", "last_error_date", "last_error_message"):
    if k in info:
        print(f"  {k}: {info[k]}")

if not ok:
    print("\nERROR: webhook registration failed after retries. If the message was "
          "'Failed to resolve host', wait ~30s and re-run, or the tunnel URL is dead "
          "(restart the tunnel). If it was a 530 health warning above, the backend "
          "isn't reachable through the tunnel.")
    sys.exit(1)

print("\nDone. Send the bot '/start' to confirm it replies.")
