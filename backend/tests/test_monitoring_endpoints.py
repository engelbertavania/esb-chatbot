"""Tests for the Phase 2 ticket-monitoring endpoints (assign, escalate, notes,
agent workload, status move) + schema migration idempotency."""
import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import main
import database
from database import Base, Ticket


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[main.get_db] = override_get_db
    session = TestingSession()
    yield TestClient(main.app), session
    session.close()
    main.app.dependency_overrides.clear()


def _ticket(session, **kw):
    t = Ticket(**{
        "company_name": "MBUM BROWNIS", "branch_name": "BINTARO",
        "issue_category": "Payment Configuration", "issue_detail": "Gagal push to POS",
        "status": "Open", "created_at": datetime.datetime(2026, 6, 1, 10, 0, 0),
        **kw,
    })
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def test_ticket_dict_includes_phase2_fields(client):
    tc, session = client
    _ticket(session, priority="High", assignee="CC - Ayu Rahayu")
    row = tc.get("/api/tickets").json()[0]
    assert {"priority", "assignee", "assign_to", "notes"} <= set(row.keys())
    assert row["priority"] == "High"
    assert row["assignee"] == "CC - Ayu Rahayu"
    assert row["notes"] == []


def test_assign_and_escalate(client):
    tc, session = client
    t = _ticket(session)
    r = tc.post(f"/api/tickets/{t.id}/assign", json={"assignee": "CC - Ica Caca Marica"})
    assert r.status_code == 200
    assert r.json()["assignee"] == "CC - Ica Caca Marica"
    r = tc.post(f"/api/tickets/{t.id}/escalate", json={"assign_to": "DEV - Immanuel"})
    assert r.json()["assign_to"] == "DEV - Immanuel"


def test_assign_404(client):
    tc, _ = client
    assert tc.post("/api/tickets/999/assign", json={"assignee": "x"}).status_code == 404


def test_notes_crud(client):
    tc, session = client
    t = _ticket(session)
    r = tc.post(f"/api/tickets/{t.id}/notes", json={
        "type": "IN PROGRESS", "text": "Sedang dicek", "author": "CC - Sulis", "images": ["a.jpg"],
    })
    assert r.status_code == 200
    note = r.json()
    assert note["text"] == "Sedang dicek"
    assert note["images"] == ["a.jpg"]

    # appears nested on the ticket
    row = tc.get("/api/tickets").json()[0]
    assert len(row["notes"]) == 1

    # edit
    r = tc.put(f"/api/tickets/{t.id}/notes/{note['id']}", json={"text": "Selesai", "type": "FIXED"})
    assert r.json()["text"] == "Selesai"
    assert r.json()["type"] == "FIXED"


def test_note_requires_text(client):
    tc, session = client
    t = _ticket(session)
    assert tc.post(f"/api/tickets/{t.id}/notes", json={"text": "  "}).status_code == 400


def test_status_move_supports_escalated(client):
    tc, session = client
    t = _ticket(session)
    r = tc.post(f"/api/tickets/{t.id}/status", json={"status": "Escalated"})
    assert r.status_code == 200
    assert r.json()["db_status"] == "Escalated"


def test_agents_workload(client):
    tc, session = client
    _ticket(session, assignee="CC - Ayu Rahayu", status="In Progress")
    _ticket(session, assignee="CC - Ayu Rahayu", status="Resolved")
    _ticket(session, assignee="CC - Ica", status="Open")
    _ticket(session)  # unassigned → excluded
    rows = tc.get("/api/agents/workload").json()
    by_agent = {r["agent"]: r for r in rows}
    assert by_agent["CC - Ayu Rahayu"] == {"agent": "CC - Ayu Rahayu", "active": 1, "resolved": 1}
    assert by_agent["CC - Ica"]["active"] == 1
    assert "CC - Ayu Rahayu" in by_agent and len(rows) == 2  # unassigned excluded


def test_ensure_schema_idempotent():
    # running the migration twice must not raise
    database._ensure_schema()
    database._ensure_schema()
