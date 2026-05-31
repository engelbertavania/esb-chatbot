"""Smoke test the CA drill-down menu flow.

1. Merchant describes issue -> classify into category -> menu of predefined keys
2. Merchant picks specific predefined -> bot shows the authored CA response
3. Yes -> resolved | Tidak -> escalation -> ticket form
"""
import config  # noqa
from agent import process_message, SESSION_STATE, _fresh_session

CHAT = "drilldown-001"
SESSION_STATE.pop(CHAT, None)


def turn(text: str) -> None:
    resp = process_message(CHAT, text)
    print(f"\n>>> Merchant: {text}")
    print(f"<<< [{resp['type']}]")
    body = resp.get("text", "")
    if len(body) > 500:
        body = body[:500] + "..."
    print(f"    {body}")
    if resp.get("options"):
        for i, opt in enumerate(resp["options"]):
            print(f"      [{i}] {opt}")
    if resp["type"] == "ticket_form":
        print(f"    ticket: {resp['ticket_number']}  queue: {resp['routed_queue']}  tag: {resp['sub_topic']}")


# Test 1: aktivasi flow
turn("Saya ada kendala aktivasi OZE")
turn("Outlet belum aktif di OZE")
turn("Ya")

print("\n" + "=" * 70)
print("Test 2: drill-down -> specific issue -> Tidak -> escalate")
print("=" * 70)
SESSION_STATE.pop(CHAT, None)
turn("Kendala QR")
turn("QR tidak bisa di-scan")
turn("Tidak")
turn("Ani")
turn("081234567890")
turn("Kopi Kita")
turn("Cabang Senayan")
