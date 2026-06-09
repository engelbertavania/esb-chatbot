"""Tests for the Phase 1 CSAT endpoints (/api/csat, /api/csat/summary).

Uses an isolated in-memory SQLite DB via a get_db dependency override so the
real tickets.db is never touched.
"""
import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import main
from database import Base, CSATRating


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


def _add(session, **kw):
    session.add(CSATRating(**kw))
    session.commit()


def test_csat_empty(client):
    tc, _ = client
    assert tc.get("/api/csat").json() == []
    assert tc.get("/api/csat/summary").json() == {"average": None, "count": 0}


def test_csat_rows_and_summary(client):
    tc, session = client
    _add(session, chat_id="111", rating=5, category="Order Management",
         sub_topic="#status_update", resolved_via="ca",
         created_at=datetime.datetime(2026, 6, 1, 10, 0, 0))
    _add(session, chat_id="222", rating=4, category="Menu Management",
         sub_topic="#upload_photo", resolved_via="llm",
         created_at=datetime.datetime(2026, 6, 2, 11, 0, 0))
    _add(session, chat_id="333", rating=3, category="Payment Gateway/MDR",
         sub_topic="#mdr_fee_setup", resolved_via="ca",
         created_at=datetime.datetime(2026, 6, 3, 12, 0, 0))

    rows = tc.get("/api/csat").json()
    assert len(rows) == 3
    # newest first
    assert rows[0]["chat_id"] == "333"
    assert rows[0]["rating"] == 3
    assert {"id", "chat_id", "rating", "category", "sub_topic",
            "resolved_via", "created_at"} <= set(rows[0].keys())

    summary = tc.get("/api/csat/summary").json()
    assert summary["count"] == 3
    assert summary["average"] == 4.0  # (5+4+3)/3


def test_csat_summary_rounds_to_one_decimal(client):
    tc, session = client
    _add(session, chat_id="a", rating=5)
    _add(session, chat_id="b", rating=4)
    _add(session, chat_id="c", rating=4)
    summary = tc.get("/api/csat/summary").json()
    assert summary["count"] == 3
    assert summary["average"] == 4.3  # 13/3 = 4.333 -> 4.3
