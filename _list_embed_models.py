"""List Gemini API models that support embedContent."""
import config  # noqa
import os
from google import genai

c = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
for m in c.models.list():
    methods = getattr(m, "supported_actions", None) or []
    if "embedContent" in methods:
        print(f"{m.name:<50} (input_tokens={getattr(m, 'input_token_limit', '?')})")
