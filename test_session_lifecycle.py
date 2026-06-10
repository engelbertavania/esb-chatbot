"""Tests for conversation continuity + proactive check-in / auto-close.

Covers the fix for "the bot treats my follow-up as a NEW issue":
  - after a ticket is created / issue resolved the session enters WRAP_UP and
    stays on the SAME issue instead of re-classifying;
  - idle_action() decides when to nudge ("still there?") and when to close;
  - the /reap endpoint sends those messages and resets the session so the NEXT
    message is a brand-new issue.
"""
import datetime
import time

import pytest
from fastapi.testclient import TestClient

import agent
import main
from database import SessionLocal, ChatSession


# ── helpers ──────────────────────────────────────────────────────────────────
def _clear_sessions():
    db = SessionLocal()
    try:
        db.query(ChatSession).delete()
        db.commit()
    finally:
        db.close()


def _seed(chat_id, *, idle_min, prompted=False, prompted_min_ago=None, has_history=True):
    db = SessionLocal()
    try:
        db.add(ChatSession(
            chat_id=chat_id,
            last_activity=datetime.datetime.utcnow() - datetime.timedelta(minutes=idle_min),
            followup_prompted=prompted,
            followup_prompted_at=(None if prompted_min_ago is None
                                  else datetime.datetime.utcnow() - datetime.timedelta(minutes=prompted_min_ago)),
            has_history=has_history,
        ))
        db.commit()
    finally:
        db.close()


# ── WRAP_UP continuity (no premature "new issue") ────────────────────────────
def test_wrap_up_keeps_same_issue_and_does_not_reclassify():
    agent.SESSION_STATE.clear()
    cid = "1001"
    sess = agent._fresh_session()
    sess.update({"state": "WRAP_UP", "last_ticket_number": "Ticket #26123456",
                 "topic": "Payment Configuration", "last_activity": time.time(),
                 "chat_history": [{"role": "assistant", "text": "tiket dibuat", "ts": time.time()}]})
    agent.SESSION_STATE[cid] = sess

    resp = agent.process_message(cid, "ternyata masih belum bisa")

    assert resp["type"] == "message"
    assert "Ticket #26123456" in resp["text"]          # tied to the same ticket
    assert agent.SESSION_STATE[cid]["state"] == "WRAP_UP"  # stayed on same issue


def test_finalize_ticket_enters_wrap_up_and_preserves_context():
    sess = agent._fresh_session()
    sess.update({
        "topic": "Payment Configuration", "sub_topic": "#qr",
        "original_query": "QR tidak bisa discan",
        "ticket_form": {"name": "Budi", "phone": "0811", "company": "Kopi", "branch": "Bintaro"},
    })
    payload = agent._finalize_ticket(sess)

    assert payload["type"] == "ticket_form"
    assert sess["state"] == "WRAP_UP"
    assert sess["last_ticket_number"] == payload["ticket_number"]
    assert sess["topic"] == "Payment Configuration"      # context preserved
    assert sess["original_query"] == "QR tidak bisa discan"


def test_csat_close_enters_wrap_up():
    sess = agent._fresh_session()
    sess.update({"state": "AWAITING_CSAT", "topic": "Menu Management"})
    agent._csat_thanks_and_close(sess, 5)
    assert sess["state"] == "WRAP_UP"


# ── idle_action() — the pure reaper decision ─────────────────────────────────
def _live_session(**over):
    s = agent._fresh_session()
    s["chat_history"] = [{"role": "user", "text": "hi", "ts": 0}]
    s.update(over)
    return s


def test_idle_action_none_without_history():
    s = agent._fresh_session()  # empty history
    assert agent.idle_action(s, time.time()) is None


def test_idle_action_none_when_recently_active():
    now = time.time()
    assert agent.idle_action(_live_session(last_activity=now), now) is None


def test_idle_action_prompts_after_silence():
    now = time.time()
    s = _live_session(last_activity=now - agent.FOLLOWUP_PROMPT_AFTER_SECONDS - 1)
    assert agent.idle_action(s, now) == "prompt"


def test_idle_action_closes_after_unanswered_prompt():
    now = time.time()
    s = _live_session(followup_prompted=True,
                      followup_prompted_at=now - agent.FOLLOWUP_CLOSE_AFTER_SECONDS - 1)
    assert agent.idle_action(s, now) == "close"


def test_idle_action_waits_when_prompt_is_recent():
    now = time.time()
    s = _live_session(followup_prompted=True, followup_prompted_at=now)
    assert agent.idle_action(s, now) is None


# ── Reaper end-to-end via POST /reap (prompt -> close -> fresh) ──────────────
# The old in-memory asyncio reaper was removed; reaper logic now lives in the
# scheduled POST /reap endpoint backed by the chat_sessions DB table.  We drive
# it here via TestClient + DB seeds, mirroring test_reaper_endpoint.py patterns.
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


def test_reaper_prompts_then_closes_then_resets(reap_client):
    client, headers, sent = reap_client

    # Web sessions have no push channel — seed one that /reap must ignore.
    # (Web sessions are skipped because has_history rows are only written by
    # _touch_session_liveness which short-circuits for "web:" prefixes.)

    # Seed a Telegram session that has been idle for a long time.
    _seed("555", idle_min=20, has_history=True)
    main.SESSION_STATE["555"] = _live_session(last_activity=time.time() - 10_000)

    # Pass 1: long silence -> a check-in nudge.
    body = client.post("/reap", headers=headers).json()
    assert body["prompted"] == 1
    assert any("masih ada" in m["text"].lower() for m in sent)

    # The DB row must reflect the prompted state.
    db = SessionLocal()
    try:
        row = db.get(ChatSession, "555")
        assert row is not None
        assert row.followup_prompted is True
        assert row.followup_prompted_at is not None
    finally:
        db.close()

    # Pass 2: still no reply — update the DB row to look like the nudge was
    # sent a long time ago, then hit /reap again.
    db = SessionLocal()
    try:
        row = db.get(ChatSession, "555")
        row.followup_prompted_at = (
            datetime.datetime.utcnow() - datetime.timedelta(minutes=20)
        )
        db.commit()
    finally:
        db.close()

    sent.clear()
    body2 = client.post("/reap", headers=headers).json()
    assert body2["closed"] == 1
    assert any("tutup" in m["text"].lower() for m in sent)

    # In-memory state is reset to a fresh idle session.
    assert main.SESSION_STATE["555"]["state"] == "IDLE"
    assert main.SESSION_STATE["555"]["chat_history"] == []
    assert "last_ticket_number" not in main.SESSION_STATE["555"]

    # DB row is deleted on close.
    db = SessionLocal()
    try:
        assert db.get(ChatSession, "555") is None
    finally:
        db.close()
