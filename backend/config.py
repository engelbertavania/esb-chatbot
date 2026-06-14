"""Load environment variables from a ``.env`` file in the project root.

Import this module *before* anything that reads ``os.getenv``::

    import config  # noqa: F401 — must run first
    import os
    os.getenv("GOOGLE_API_KEY")

``load_dotenv`` is idempotent and never overrides variables that are already
set in the process environment, so dot-sourcing a real shell export still wins
over the ``.env`` file (useful in CI / prod where secrets come from the
hosting platform, not from a checked-in file).
"""

from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_PATH)
