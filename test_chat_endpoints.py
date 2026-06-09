"""Tests for the Phase 3 web-chat endpoints (/api/chat, /api/chat/reset).

Stubs agent.process_message so the tests are deterministic and don't hit the LLM,
and verify web session ids are namespaced (never collide with Telegram ids) and
that a ticket_form response persists a real ticket.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import main
from database import Base, Ticket


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[main.get_db] = override_get_db
    seen = {}

    def fake_process_message(chat_id, text):
        seen["chat_id"] = chat_id
        if text == "buat tiket":
            return {
                "type": "ticket_form", "text": "T-123 sudah dibuat.",
                "ticket_number": "T-WEB-1", "category": "Payment Configuration",
                "sub_topic": "#qr", "issue_detail": "QR gagal", "chat_history": "",
                "steps_attempted": "", "attachments": [], "name": "Budi",
                "phone": "0811", "company": "Kopi Kenangan", "branch": "Bintaro",
                "routed_queue": "Payment Ops", "confidence": 80,
            }
        if text == "tanya":
            return {"type": "question", "text": "Pilih salah satu", "options": ["Ya", "Tidak"]}
        return {"type": "message", "text": f"echo: {text}"}

    monkeypatch.setattr(main, "process_message", fake_process_message)
    main.SESSION_STATE.clear()
    yield TestClient(main.app), TestingSession, seen
    main.app.dependency_overrides.clear()
    main.SESSION_STATE.clear()


def test_chat_message(client):
    tc, _, seen = client
    r = tc.post("/api/chat", json={"session_id": "abc", "message": "halo"})
    assert r.status_code == 200
    body = r.json()
    assert body["messages"][0] == {"type": "message", "text": "echo: halo"}
    assert body["ticket_id"] is None
    # web-namespaced session id
    assert seen["chat_id"] == "web:abc"


def test_chat_question_includes_options(client):
    tc, _, _ = client
    body = tc.post("/api/chat", json={"session_id": "abc", "message": "tanya"}).json()
    assert body["messages"][0]["options"] == ["Ya", "Tidak"]


def test_chat_requires_session_id(client):
    tc, _, _ = client
    assert tc.post("/api/chat", json={"message": "x"}).status_code == 400


def test_chat_ticket_form_persists_ticket(client):
    tc, TestingSession, _ = client
    body = tc.post("/api/chat", json={"session_id": "abc", "message": "buat tiket"}).json()
    assert body["ticket_id"] is not None
    session = TestingSession()
    try:
        rows = session.query(Ticket).all()
        assert len(rows) == 1
        assert rows[0].ticket_number == "T-WEB-1"
        assert rows[0].chat_id == "web:abc"
        assert rows[0].company_name == "Kopi Kenangan"
    finally:
        session.close()


def test_chat_reset_clears_session(client):
    tc, _, _ = client
    main.SESSION_STATE["web:abc"] = {"foo": "bar"}
    r = tc.post("/api/chat/reset", json={"session_id": "abc"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert "web:abc" not in main.SESSION_STATE


def test_reset_requires_session_id(client):
    tc, _, _ = client
    assert tc.post("/api/chat/reset", json={}).status_code == 400
