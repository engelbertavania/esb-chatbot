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


import pytest
from fastapi.testclient import TestClient
from agent import _fresh_session


@pytest.fixture
def tg(monkeypatch):
    sent = []
    monkeypatch.setattr(main, "send_telegram_message",
                        lambda chat_id, response, user_info=None:
                        sent.append({"chat_id": chat_id, "text": response.get("text", ""),
                                     "type": response.get("type")}))
    main.SESSION_STATE.clear()
    _clear()
    client = TestClient(main.app)
    headers = {"X-Telegram-Bot-Api-Secret-Token": main.TELEGRAM_WEBHOOK_SECRET}

    def post(chat_id, text):
        sent.clear()
        client.post("/webhook", headers=headers,
                    json={"message": {"chat": {"id": chat_id}, "text": text,
                                      "from": {"id": chat_id, "first_name": "Budi"}}})
        return list(sent)

    yield client, headers, post, sent
    _clear()


def test_webhook_relays_customer_message_during_handoff(tg, monkeypatch):
    client, headers, post, sent = tg
    db = SessionLocal()
    try:
        db.add(Ticket(ticket_number="Ticket #TEST05", chat_id="7200",
                      issue_category="Customer Care (Live Chat)", handoff_state="active",
                      handoff_last_activity=datetime.datetime(2020, 1, 1)))
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(main, "process_message",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("bot answered during handoff")))
    out = post(7200, "halo saya masih bingung")
    assert out == []  # no bot reply sent
    db = SessionLocal()
    try:
        t = main._active_handoff_for(db, "7200")
        msgs = db.query(LiveMessage).filter(LiveMessage.ticket_id == t.id,
                                            LiveMessage.sender == "customer").all()
        assert any("bingung" in m.text for m in msgs)
        assert t.handoff_last_activity > datetime.datetime(2021, 1, 1)
    finally:
        db.close()


def test_webhook_starts_handoff_and_creates_ticket(tg):
    client, headers, post, sent = tg
    main.SESSION_STATE["7100"] = {**_fresh_session(), "state": "CHOOSING_PREDEFINED",
                                  "predefined_choices": [],
                                  "chat_history": [{"role": "user", "text": "x", "ts": 0}]}
    out = post(7100, main.HANDOFF_OPTION_VALUE)
    assert any("tunggu" in s["text"].lower() for s in out)
    db = SessionLocal()
    try:
        t = main._active_handoff_for(db, "7100")
        assert t is not None and t.handoff_state == "requested"
        msgs = db.query(LiveMessage).filter(LiveMessage.ticket_id == t.id).all()
        assert any(m.sender == "system" for m in msgs)
    finally:
        db.close()


def _seed_handoff(chat_id, state="requested"):
    db = SessionLocal()
    try:
        t = Ticket(ticket_number=f"Ticket #J{chat_id}", chat_id=str(chat_id),
                   issue_category="Customer Care (Live Chat)", status="Waiting",
                   handoff_state=state, handoff_last_activity=datetime.datetime.utcnow())
        db.add(t); db.commit(); db.refresh(t)
        return t.id
    finally:
        db.close()


def test_join_activates_and_greets(tg):
    client, headers, post, sent = tg
    tid = _seed_handoff("8100", "requested")
    r = client.post(f"/api/tickets/{tid}/handoff/join", json={"agent": "CC - Ayu"})
    assert r.status_code == 200
    assert r.json()["handoff_state"] == "active"
    assert any("terhubung dengan customer care" in s["text"].lower() for s in sent)
    db = SessionLocal()
    try:
        t = db.get(Ticket, tid)
        assert t.handoff_agent == "CC - Ayu" and t.assignee == "CC - Ayu"
        assert db.query(LiveMessage).filter(LiveMessage.ticket_id == tid,
                                            LiveMessage.sender == "system").count() >= 1
    finally:
        db.close()


def test_join_twice_returns_409(tg):
    client, headers, post, sent = tg
    tid = _seed_handoff("8101", "active")
    r = client.post(f"/api/tickets/{tid}/handoff/join", json={"agent": "CC - Budi"})
    assert r.status_code == 409


def test_agent_message_records_and_sends(tg):
    client, headers, post, sent = tg
    tid = _seed_handoff("8200", "active")
    r = client.post(f"/api/tickets/{tid}/handoff/message",
                    json={"text": "Halo, ada yang bisa dibantu?", "author": "CC - Ayu"})
    assert r.status_code == 200 and r.json()["sender"] == "agent"
    assert any(s["text"] == "Halo, ada yang bisa dibantu?" for s in sent)


def test_agent_message_409_when_not_active(tg):
    client, headers, post, sent = tg
    tid = _seed_handoff("8201", "requested")
    r = client.post(f"/api/tickets/{tid}/handoff/message",
                    json={"text": "hi", "author": "CC - Ayu"})
    assert r.status_code == 409


def test_get_messages_after_id(tg):
    client, headers, post, sent = tg
    tid = _seed_handoff("8202", "active")
    db = SessionLocal()
    try:
        t = db.get(Ticket, tid)
        m1 = main._record_live_message(db, t, "customer", "satu")
        m2 = main._record_live_message(db, t, "agent", "dua", author="CC - Ayu")
        first_id = m1.id
    finally:
        db.close()
    r = client.get(f"/api/tickets/{tid}/handoff/messages", params={"after_id": first_id})
    rows = r.json()
    assert [m["text"] for m in rows] == ["dua"]


def test_end_resolves_and_resets_session(tg):
    client, headers, post, sent = tg
    tid = _seed_handoff("8300", "active")
    main.SESSION_STATE["8300"] = {**_fresh_session(), "state": "HUMAN_HANDOFF"}
    r = client.post(f"/api/tickets/{tid}/handoff/end", json={"agent": "CC - Ayu"})
    assert r.status_code == 200
    body = r.json()
    assert body["handoff_state"] == "ended" and body["status"] == "Resolved"
    assert any("berakhir" in s["text"].lower() for s in sent)
    assert main.SESSION_STATE["8300"]["state"] == "IDLE"
    assert main._relay_if_handoff("8300", "halo", None) is False
