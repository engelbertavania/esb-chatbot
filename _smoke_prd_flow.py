"""Smoke test for the PRD-aligned conversation flow.

Walks a fake merchant through:
  1. Initial issue → classification → step 1
  2. 'Tidak' x3 → escalation prompt
  3. Multi-turn ticket form (Name → Phone → Company → Branch)
  4. Final ticket payload

No network to Telegram. Exercises the real LLM (classifier + step synthesizer).
"""
import config  # noqa
import json
from agent import process_message, SESSION_STATE, _fresh_session

CHAT = "smoke-001"
SESSION_STATE.pop(CHAT, None)


def turn(text: str) -> None:
    resp = process_message(CHAT, text)
    print(f"\n>>> Merchant: {text}")
    print(f"<<< [{resp['type']}] {resp.get('text', '')[:400]}")
    if resp.get("options"):
        print(f"    options: {resp['options']}")
    if resp["type"] == "ticket_form":
        print(f"    ticket_number: {resp['ticket_number']}")
        print(f"    routed_queue:  {resp['routed_queue']}")
        print(f"    category:      {resp['category']}")
        print(f"    name/phone:    {resp['name']} / {resp['phone']}")
        print(f"    company/branch:{resp['company']} / {resp['branch']}")
        print(f"    steps:\n{resp['steps_attempted']}")


turn("Foto menu saya tidak muncul di aplikasi setelah saya upload")
turn("Tidak")
turn("Tidak")
turn("Tidak")  # should trigger escalation
turn("Budi Santoso")
turn("not-a-phone")  # should re-prompt
turn("081234567890")
turn("Warung Sederhana")
turn("Cabang Kemang")
