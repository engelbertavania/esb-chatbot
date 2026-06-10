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
