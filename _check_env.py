"""Verify env vars exist without printing secret values."""
import config  # noqa
import os

keys = [
    "GOOGLE_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
    "VERTEX_DATASTORE_ID",
    "TELEGRAM_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "DATABASE_URL",
]
for k in keys:
    v = os.environ.get(k, "")
    if v:
        print(f"  {k}: SET ({len(v)} chars)")
    else:
        print(f"  {k}: empty")
