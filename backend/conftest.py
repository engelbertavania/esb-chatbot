"""Pytest configuration.

Force the test suite onto a disposable local SQLite database so it never reads
from — or, worse, writes to / deletes rows in — the production Supabase database
configured in ``.env``.

This MUST set ``DATABASE_URL`` before ``database.py``/``config.py`` are imported
by any test module. ``config.load_dotenv()`` does not override variables that are
already present in the environment, so this assignment wins over the ``.env``
value. A fresh file is used each session so tests start from a clean schema.
"""
import os
from pathlib import Path

_TEST_DB = Path(__file__).resolve().parent / "test_tickets.db"
try:
    _TEST_DB.unlink()
except FileNotFoundError:
    pass

os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB.as_posix()}"
# Keep tests offline / deterministic regardless of what's in .env.
os.environ.setdefault("TELEGRAM_TOKEN", "mock_token")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "")
