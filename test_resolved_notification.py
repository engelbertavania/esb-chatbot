"""Tests for the 'ticket resolved -> notify merchant on Telegram' behavior."""
import datetime

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
    # Pretend a real bot token is configured, and capture sends instead of HTTP.
    monkeypatch.setattr(main, "TELEGRAM_TOKEN", "123:realtoken")
    sent = []
    monkeypatch.setattr(main, "send_telegram_message", lambda chat_id, response, user_info=None: sent.append((chat_id, response)))

    session = TestingSession()
    yield TestClient(main.app), session, sent
    session.close()
    main.app.dependency_overrides.clear()


def _ticket(session, **kw):
    t = Ticket(**{"company_name": "MBUM", "status": "In Progress", "chat_id": "555",
                  "created_at": datetime.datetime(2026, 6, 1)} | kw)
    session.add(t); session.commit(); session.refresh(t)
    return t


def test_resolving_notifies_merchant(client):
    tc, session, sent = client
    t = _ticket(session, chat_id="555", ticket_number="T-1")
    r = tc.post(f"/api/tickets/{t.id}/status", json={"status": "Resolved"})
    assert r.json()["merchant_notified"] is True
    assert len(sent) == 1
    chat_id, response = sent[0]
    assert chat_id == 555  # numeric chat id
    assert response["type"] == "message"
    assert "SELESAI" in response["text"] and "T-1" in response["text"]


def test_no_notify_when_not_resolved(client):
    tc, session, sent = client
    t = _ticket(session, chat_id="555")
    tc.post(f"/api/tickets/{t.id}/status", json={"status": "In Progress"})
    assert sent == []


def test_no_duplicate_when_already_resolved(client):
    tc, session, sent = client
    t = _ticket(session, chat_id="555", status="Resolved")
    r = tc.post(f"/api/tickets/{t.id}/status", json={"status": "Resolved"})
    assert r.json()["merchant_notified"] is False
    assert sent == []


def test_skips_web_chat_tickets(client):
    tc, session, sent = client
    t = _ticket(session, chat_id="web:abc-123")
    r = tc.post(f"/api/tickets/{t.id}/status", json={"status": "Resolved"})
    assert r.json()["merchant_notified"] is False
    assert sent == []


def test_skips_when_no_chat_id(client):
    tc, session, sent = client
    t = _ticket(session, chat_id=None)
    r = tc.post(f"/api/tickets/{t.id}/status", json={"status": "Resolved"})
    assert r.json()["merchant_notified"] is False
    assert sent == []


def test_skips_when_token_not_configured(client, monkeypatch):
    tc, session, sent = client
    monkeypatch.setattr(main, "TELEGRAM_TOKEN", "mock_token")
    t = _ticket(session, chat_id="555")
    r = tc.post(f"/api/tickets/{t.id}/status", json={"status": "Resolved"})
    assert r.json()["merchant_notified"] is False
    assert sent == []
