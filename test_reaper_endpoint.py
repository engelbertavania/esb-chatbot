"""Tests for the DB-backed idle-session reaper (chat_sessions + POST /reap)."""
import datetime

import main
from database import SessionLocal, ChatSession


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
