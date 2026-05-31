"""Register Telegram webhook with the public ngrok URL + secret token.

Usage:
    .\\venv\\Scripts\\python.exe _register_webhook.py https://xxxx.ngrok-free.app
"""
import config  # noqa
import os
import sys
import httpx

if len(sys.argv) < 2:
    print("Usage: python _register_webhook.py <https://your-ngrok-url>")
    sys.exit(2)

base = sys.argv[1].rstrip("/")
url = f"{base}/webhook"
token = os.environ["TELEGRAM_TOKEN"]
secret = os.environ["TELEGRAM_WEBHOOK_SECRET"]

print(f"Registering webhook: {url}")
r = httpx.post(
    f"https://api.telegram.org/bot{token}/setWebhook",
    json={"url": url, "secret_token": secret, "drop_pending_updates": True},
    timeout=15,
)
print("setWebhook:", r.json())

r = httpx.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=15)
info = r.json().get("result", {})
print("\ngetWebhookInfo:")
for k in ("url", "has_custom_certificate", "pending_update_count", "last_error_date", "last_error_message"):
    if k in info:
        print(f"  {k}: {info[k]}")
