"""Tests for the DB-backed idle-session reaper (chat_sessions + POST /reap)."""
import datetime

import main
from database import SessionLocal, ChatSession
from agent import idle_action, FOLLOWUP_PROMPT_AFTER_SECONDS, FOLLOWUP_CLOSE_AFTER_SECONDS


def _clear_sessions():
    db = SessionLocal()
    try:
        db.query(ChatSession).delete()
        db.commit()
    finally:
        db.close()


def test_chat_session_model_roundtrips():
    _clear_sessions()
    db = SessionLocal()
    try:
        db.add(ChatSession(
            chat_id="9001",
            last_activity=datetime.datetime.utcnow(),
            followup_prompted=False,
            has_history=True,
        ))
        db.commit()
        row = db.get(ChatSession, "9001")
        assert row is not None
        assert row.has_history is True
        assert row.followup_prompted is False
        assert row.followup_prompted_at is None
    finally:
        db.close()
        _clear_sessions()


def test_timing_constants_are_8_then_2_minutes():
    assert FOLLOWUP_PROMPT_AFTER_SECONDS == 8 * 60
    assert FOLLOWUP_CLOSE_AFTER_SECONDS == 2 * 60


def test_idle_action_prompts_after_8_minutes():
    now = 10_000.0
    seven_min = {"chat_history": [1], "last_activity": now - 7 * 60}
    nine_min = {"chat_history": [1], "last_activity": now - 9 * 60}
    assert idle_action(seven_min, now) is None
    assert idle_action(nine_min, now) == "prompt"


def test_idle_action_closes_2_minutes_after_prompt():
    now = 10_000.0
    just_prompted = {"chat_history": [1], "followup_prompted": True,
                     "followup_prompted_at": now - 60}
    long_prompted = {"chat_history": [1], "followup_prompted": True,
                     "followup_prompted_at": now - 3 * 60}
    assert idle_action(just_prompted, now) is None
    assert idle_action(long_prompted, now) == "close"


def test_touch_session_liveness_creates_and_resets():
    _clear_sessions()
    # Pre-seed a prompted row, then a new turn must reset the follow-up state.
    db = SessionLocal()
    try:
        db.add(ChatSession(chat_id="9100", last_activity=datetime.datetime(2020, 1, 1),
                           followup_prompted=True,
                           followup_prompted_at=datetime.datetime(2020, 1, 1),
                           has_history=True))
        db.commit()
    finally:
        db.close()

    main._touch_session_liveness("9100")

    db = SessionLocal()
    try:
        row = db.get(ChatSession, "9100")
        assert row.has_history is True
        assert row.followup_prompted is False
        assert row.followup_prompted_at is None
        assert row.last_activity > datetime.datetime(2021, 1, 1)
    finally:
        db.close()
        _clear_sessions()


def test_touch_session_liveness_skips_web_sessions():
    _clear_sessions()
    main._touch_session_liveness("web:abc")
    db = SessionLocal()
    try:
        assert db.get(ChatSession, "web:abc") is None
    finally:
        db.close()


import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def webhook_client(monkeypatch):
    monkeypatch.setattr(main, "send_telegram_message", lambda *a, **k: None)
    monkeypatch.setattr(main, "process_message",
                        lambda chat_id, text: {"type": "message", "text": "ok"})
    main.SESSION_STATE.clear()
    _clear_sessions()
    client = TestClient(main.app)
    headers = {"X-Telegram-Bot-Api-Secret-Token": main.TELEGRAM_WEBHOOK_SECRET}
    yield client, headers
    _clear_sessions()


def test_webhook_turn_records_liveness(webhook_client):
    client, headers = webhook_client
    client.post("/webhook", headers=headers,
                json={"message": {"chat": {"id": 9200}, "text": "halo"}})
    db = SessionLocal()
    try:
        row = db.get(ChatSession, "9200")
        assert row is not None
        assert row.has_history is True
        assert row.followup_prompted is False
    finally:
        db.close()


import datetime as _dt


@pytest.fixture
def reap_client(monkeypatch):
    sent = []
    monkeypatch.setattr(main, "send_telegram_message",
                        lambda chat_id, response, user_info=None:
                        sent.append({"chat_id": chat_id, "text": response.get("text", "")}))
    monkeypatch.setattr(main, "REAP_SECRET", "testsecret")
    main.SESSION_STATE.clear()
    _clear_sessions()
    client = TestClient(main.app)
    headers = {"X-Reap-Secret": "testsecret"}
    yield client, headers, sent
    _clear_sessions()


def _seed(chat_id, *, idle_min, prompted=False, prompted_min_ago=None, has_history=True):
    db = SessionLocal()
    try:
        db.add(ChatSession(
            chat_id=chat_id,
            last_activity=_dt.datetime.utcnow() - _dt.timedelta(minutes=idle_min),
            followup_prompted=prompted,
            followup_prompted_at=(None if prompted_min_ago is None
                                  else _dt.datetime.utcnow() - _dt.timedelta(minutes=prompted_min_ago)),
            has_history=has_history,
        ))
        db.commit()
    finally:
        db.close()


def test_reap_rejects_without_secret(reap_client):
    client, _headers, _sent = reap_client
    assert client.post("/reap").status_code == 403
    assert client.post("/reap", headers={"X-Reap-Secret": "wrong"}).status_code == 403


def test_reap_does_nothing_for_recent_session(reap_client):
    client, headers, sent = reap_client
    _seed("8001", idle_min=3)
    body = client.post("/reap", headers=headers).json()
    assert body == {"prompted": 0, "closed": 0}
    assert sent == []


def test_reap_nudges_after_8_minutes(reap_client):
    client, headers, sent = reap_client
    _seed("8002", idle_min=9)
    body = client.post("/reap", headers=headers).json()
    assert body["prompted"] == 1
    assert "masih ada" in sent[0]["text"].lower()
    db = SessionLocal()
    try:
        row = db.get(ChatSession, "8002")
        assert row.followup_prompted is True
        assert row.followup_prompted_at is not None
    finally:
        db.close()


def test_reap_closes_2_minutes_after_nudge(reap_client):
    client, headers, sent = reap_client
    _seed("8003", idle_min=11, prompted=True, prompted_min_ago=3)
    main.SESSION_STATE["8003"] = {"state": "WRAP_UP"}
    body = client.post("/reap", headers=headers).json()
    assert body["closed"] == 1
    assert "tutup" in sent[0]["text"].lower()
    db = SessionLocal()
    try:
        assert db.get(ChatSession, "8003") is None     # row deleted on close
    finally:
        db.close()
    assert main.SESSION_STATE["8003"]["state"] == "IDLE"  # in-memory reset to fresh


def test_reap_ignores_sessions_without_history(reap_client):
    client, headers, sent = reap_client
    _seed("8004", idle_min=20, has_history=False)
    body = client.post("/reap", headers=headers).json()
    assert body == {"prompted": 0, "closed": 0}
    assert sent == []


def test_reap_closes_prompted_row_with_missing_timestamp(reap_client):
    # Defensive: a prompted row with a NULL followup_prompted_at must still close,
    # not get stuck forever.
    client, headers, sent = reap_client
    _seed("8005", idle_min=20, prompted=True, prompted_min_ago=None)
    body = client.post("/reap", headers=headers).json()
    assert body["closed"] == 1
    assert "tutup" in sent[0]["text"].lower()
    db = SessionLocal()
    try:
        assert db.get(ChatSession, "8005") is None
    finally:
        db.close()


def test_in_memory_reaper_is_removed():
    # The asyncio reaper is replaced by the scheduled /reap endpoint.
    assert not hasattr(main, "_session_reaper_loop")
    assert not hasattr(main, "_reap_idle_sessions")
    assert not hasattr(main, "REAPER_INTERVAL_SECONDS")


def test_app_still_boots_and_health_ok():
    client = TestClient(main.app)   # lifespan startup must not raise
    assert client.get("/health").status_code in (200, 503)
