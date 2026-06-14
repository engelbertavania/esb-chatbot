"""Tests for customer de-escalation (calm a furious customer).

Two layers:
  1. detect_anger() — the keyword/heuristic classifier (no LLM, no network).
  2. /webhook — a furious message gets ONE calming reply sent ahead of the
     normal bot response, at most once per session, and never for commands.
"""
import pytest
from fastapi.testclient import TestClient

import main
from agent import detect_anger, calming_message


# ── detect_anger() ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "Bot ini goblok banget!",                 # profanity token
    "Saya sangat kecewa dengan layanan ini",  # frustration term
    "kenapa lambat sekali sih",               # frustration term
    "TOLONG SELESAIKAN SEKARANG JUGA",        # shouting (all caps, long)
    "ini parah!!!",                           # 3+ exclamation
    "this is absolutely terrible",            # english frustration
    "gak becus kerjanya",                     # multi-word phrase
])
def test_detect_anger_positive(text):
    assert detect_anger(text) is True


@pytest.mark.parametrize("text", [
    "Selamat pagi, mau tanya soal menu",   # 'lama' substring must NOT trip
    "Halo, QR saya tidak muncul",          # short caps (QR) must NOT shout-trip
    "Bagaimana cara upload foto menu?",    # neutral question
    "Terima kasih banyak ya",              # positive
    "OK",                                   # too short to be shouting
    "",                                     # empty
])
def test_detect_anger_negative(text):
    assert detect_anger(text) is False


def test_calming_message_is_a_nonempty_string():
    assert isinstance(calming_message(), str) and calming_message().strip()


# ── /webhook integration ─────────────────────────────────────────────────────
@pytest.fixture
def webhook(monkeypatch):
    sent: list[dict] = []

    def fake_send(chat_id, response, user_info=None):
        sent.append({"chat_id": chat_id, "text": response.get("text", ""), "type": response.get("type")})

    def fake_process(chat_id, text):
        return {"type": "message", "text": f"(handled: {text})"}

    monkeypatch.setattr(main, "send_telegram_message", fake_send)
    monkeypatch.setattr(main, "process_message", fake_process)
    main.SESSION_STATE.clear()

    client = TestClient(main.app)
    headers = {"X-Telegram-Bot-Api-Secret-Token": main.TELEGRAM_WEBHOOK_SECRET}

    def post_text(chat_id, text):
        sent.clear()
        client.post("/webhook", headers=headers, json={"message": {"chat": {"id": chat_id}, "text": text}})
        return list(sent)

    return post_text


def test_furious_message_gets_calming_reply_first(webhook):
    out = webhook(7001, "kalian payah banget, lambat sekali!!!")
    assert len(out) == 2, out
    # Calming note arrives before the normal handler response (FIFO tasks).
    assert "maaf" in out[0]["text"].lower()
    assert out[1]["text"] == "(handled: kalian payah banget, lambat sekali!!!)"


def test_calm_message_only_sends_the_normal_response(webhook):
    out = webhook(7002, "Halo, QR saya tidak muncul")
    assert len(out) == 1
    assert out[0]["text"] == "(handled: Halo, QR saya tidak muncul)"


def test_calming_is_sent_at_most_once_per_session(webhook):
    first = webhook(7003, "ini parah!!!")
    assert len(first) == 2  # calming + response
    second = webhook(7003, "masih saja error, menyebalkan!")
    assert len(second) == 1  # already de-escalated this session -> no repeat


def test_commands_never_trigger_calming(webhook):
    out = webhook(7004, "/start")
    assert len(out) == 1
    assert out[0]["text"] == "(handled: /start)"
