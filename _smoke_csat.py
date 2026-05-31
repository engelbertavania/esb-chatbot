"""Verify the new UX: /start welcome, top-3 drill-down, CSAT after Ya."""
import config  # noqa
from agent import process_message, SESSION_STATE

CHAT = "csat-001"
SESSION_STATE.pop(CHAT, None)


def turn(text: str) -> None:
    resp = process_message(CHAT, text)
    print(f"\n>>> Merchant: {text}")
    print(f"<<< [{resp['type']}]")
    body = resp.get("text", "")
    if len(body) > 400:
        body = body[:400] + "..."
    print(f"    {body}")
    if resp.get("options"):
        for i, opt in enumerate(resp["options"]):
            print(f"      [{i}] {opt}")


print("===== Test 1: /start welcome =====")
turn("/start")

print("\n===== Test 2: drill-down should show only 3 + Lainnya =====")
turn("kendala aktivasi OZE")

print("\n===== Test 3: pick predefined -> Ya -> CSAT -> rating =====")
turn("Outlet belum aktif di OZE")
turn("Ya")
turn("5")

print("\n===== Test 4: invalid CSAT input gets re-prompted =====")
SESSION_STATE.pop(CHAT, None)
turn("/start")
turn("QR tidak bisa di-scan")
turn("QR tidak bisa di-scan")
turn("Ya")
turn("delapan")  # invalid
turn("4")
