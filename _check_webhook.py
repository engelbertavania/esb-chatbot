"""Check current Telegram webhook + bot identity."""
import config  # noqa
import os
import httpx

token = os.environ["TELEGRAM_TOKEN"]

print("=== Bot identity ===")
r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
me = r.json()
if me.get("ok"):
    u = me["result"]
    print(f"  id:       {u.get('id')}")
    print(f"  username: @{u.get('username')}")
    print(f"  name:     {u.get('first_name')}")
else:
    print(f"  ERROR: {me}")

print("\n=== Webhook info ===")
r = httpx.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=15)
info = r.json().get("result", {})
for k, v in info.items():
    print(f"  {k}: {v}")
