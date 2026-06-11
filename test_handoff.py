"""Tests for the Customer Care live-chat handoff."""
import datetime

import main
from agent import process_message, SESSION_STATE, HANDOFF_OPTION
from database import SessionLocal, Ticket, LiveMessage


def _clear():
    db = SessionLocal()
    try:
        db.query(LiveMessage).delete()
        db.query(Ticket).filter(Ticket.issue_category == "Customer Care (Live Chat)").delete()
        db.commit()
    finally:
        db.close()


def test_live_message_and_handoff_columns_roundtrip():
    _clear()
    db = SessionLocal()
    try:
        t = Ticket(ticket_number="Ticket #TEST01", chat_id="5001",
                   issue_category="Customer Care (Live Chat)",
                   status="Waiting", handoff_state="requested",
                   handoff_agent=None, handoff_last_activity=datetime.datetime.utcnow())
        db.add(t)
        db.commit()
        db.refresh(t)
        m = LiveMessage(ticket_id=t.id, chat_id="5001", sender="customer", text="halo", author=None)
        db.add(m)
        db.commit()
        got = db.get(LiveMessage, m.id)
        assert got.sender == "customer" and got.text == "halo"
        assert db.get(Ticket, t.id).handoff_state == "requested"
    finally:
        db.close()
        _clear()


def test_picking_handoff_option_returns_handoff_request():
    SESSION_STATE.clear()
    SESSION_STATE["6001"] = {**__import__("agent")._fresh_session(),
                             "state": "CHOOSING_PREDEFINED",
                             "predefined_choices": ["Pesanan tidak masuk POS"],
                             "chat_history": [{"role": "user", "text": "x", "ts": 0}]}
    resp = process_message("6001", HANDOFF_OPTION)
    assert resp["type"] == "handoff_request"
    assert "tunggu" in resp["text"].lower()
    assert SESSION_STATE["6001"]["state"] == "HUMAN_HANDOFF"


def test_no_match_offers_handoff_option():
    SESSION_STATE.clear()
    resp = process_message("6002", "zxcv qwer asdf nonsense unmatchable")
    assert resp["type"] == "question"
    assert HANDOFF_OPTION in resp["options"]


def test_human_handoff_state_does_not_auto_answer():
    SESSION_STATE.clear()
    SESSION_STATE["6003"] = {**__import__("agent")._fresh_session(), "state": "HUMAN_HANDOFF",
                             "chat_history": [{"role": "user", "text": "x", "ts": 0}]}
    resp = process_message("6003", "tolong bantu setting payment gateway")
    assert resp["type"] == "message"
    assert "customer care" in resp["text"].lower()


def test_active_handoff_for_finds_requested_or_active():
    _clear()
    db = SessionLocal()
    try:
        db.add(Ticket(ticket_number="Ticket #TEST02", chat_id="7001",
                      issue_category="Customer Care (Live Chat)",
                      handoff_state="active"))
        db.commit()
        assert main._active_handoff_for(db, "7001") is not None
        assert main._active_handoff_for(db, "9999") is None
    finally:
        db.close()
        _clear()


def test_record_live_message_and_bump_activity():
    _clear()
    db = SessionLocal()
    try:
        t = Ticket(ticket_number="Ticket #TEST03", chat_id="7002",
                   issue_category="Customer Care (Live Chat)", handoff_state="active",
                   handoff_last_activity=datetime.datetime(2020, 1, 1))
        db.add(t); db.commit(); db.refresh(t)
        m = main._record_live_message(db, t, "agent", "halo", author="CC - Ayu")
        assert m.id is not None and m.sender == "agent" and m.author == "CC - Ayu"
        main._bump_handoff_activity(db, t)
        assert db.get(Ticket, t.id).handoff_last_activity > datetime.datetime(2021, 1, 1)
    finally:
        db.close()
        _clear()
