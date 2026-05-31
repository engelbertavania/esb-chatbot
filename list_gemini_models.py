"""Print every Gemini model your GOOGLE_API_KEY can call for generateContent.

Usage:
    .\\venv\\Scripts\\python.exe list_gemini_models.py

Useful when an agent.py model name returns 404 — Google rotates model
availability on the free Gemini API every few months.
"""

import config  # noqa: F401 — loads .env before any os.getenv reads

import os
import sys


def main() -> None:
    key = os.getenv("GOOGLE_API_KEY")
    if not key:
        print("ERROR: GOOGLE_API_KEY env var is not set.", file=sys.stderr)
        sys.exit(1)

    try:
        from google import genai
    except ImportError:
        print("Installing google-genai...", file=sys.stderr)
        os.system(f'"{sys.executable}" -m pip install --quiet google-genai')
        from google import genai

    client = genai.Client(api_key=key)
    print(f"{'MODEL':<55} {'INPUT_TOKENS':>14}")
    print("-" * 70)
    for m in client.models.list():
        methods = getattr(m, "supported_actions", None) or []
        if "generateContent" not in methods:
            continue
        max_in = getattr(m, "input_token_limit", "")
        print(f"{m.name:<55} {str(max_in):>14}")


if __name__ == "__main__":
    main()
