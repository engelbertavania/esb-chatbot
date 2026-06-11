"""Tests for the Customer Care live-chat handoff."""
import datetime

import main
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
