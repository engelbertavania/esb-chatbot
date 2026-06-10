import config  # noqa: F401 — loads .env before any os.getenv reads

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, ForeignKey, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import datetime
import logging
import os

logger = logging.getLogger(__name__)

# DATABASE_URL — set in prod (Supabase, Neon, etc.). Falls back to local SQLite
# for development. Examples:
#   postgresql://user:pass@host:5432/dbname   (managed Postgres)
#   sqlite:///./tickets.db                    (local dev, default)
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./tickets.db")

if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
else:
    # Managed Postgres connections can drop after idle periods; pool_pre_ping
    # validates each connection before checkout so we don't hand stale ones to
    # request handlers.
    engine = create_engine(SQLALCHEMY_DATABASE_URL, pool_pre_ping=True)

logger.info("DB engine: %s", engine.url.render_as_string(hide_password=True))
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, index=True)
    ticket_number = Column(String, unique=True, index=True)
    name = Column(String)
    phone_number = Column(String)
    company_name = Column(String)
    branch_name = Column(String)
    issue_category = Column(String)
    issue_detail = Column(Text)
    chat_history = Column(Text)
    attachments = Column(Text)
    steps_attempted = Column(Text)
    status = Column(String, default="Open")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    # Populated by the Telegram webhook.
    chat_id = Column(String, index=True)
    confidence_score = Column(Integer)
    # PRD Phase 1 additions.
    routed_queue = Column(String, index=True)   # AC4.4 — support queue label
    sub_topic = Column(String)                  # historical, kept for migrated rows
    # Phase 2 (Ticket Monitoring) additions.
    priority = Column(String)                   # High | Medium | Low | Compliment
    assignee = Column(String)                   # current handler, e.g. "CC - Ayu Rahayu"
    assign_to = Column(String)                  # escalation target

    notes = relationship(
        "TicketNote",
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="TicketNote.created_at",
    )


class TicketNote(Base):
    """Phase 2 resolution-note timeline entry attached to a ticket."""
    __tablename__ = "ticket_notes"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), index=True)
    type = Column(String)        # "IN PROGRESS" | "ESCALATED TO ANOTHER TEAM" | "FIXED"
    text = Column(Text)
    author = Column(String)
    images = Column(Text)        # JSON array of attachment refs/URLs
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    ticket = relationship("Ticket", back_populates="notes")


class CSATRating(Base):
    """Post-resolution merchant satisfaction rating (1-5).

    Captured when a merchant replies "Ya" to the resolution-confirmation
    prompt. Distinct from tickets — CSAT is recorded for *resolved* sessions
    (no ticket created), so it has its own table.

    PRD success metric: average CSAT >= 3.5 within 60 days of launch.
    """
    __tablename__ = "csat_ratings"

    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(String, index=True)
    rating = Column(Integer)                    # 1..5
    category = Column(String, index=True)       # MVP category at time of resolution
    sub_topic = Column(String)                  # CA issue tag, e.g. "#qr_code"
    original_query = Column(Text)               # merchant's first message that session
    resolved_via = Column(String)               # "ca" or "llm"
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class ChatSession(Base):
    """Minimal idle-liveness record per Telegram chat, driving the scheduled
    /reap auto-close (see docs/superpowers/specs/2026-06-11-db-backed-reaper-design.md).

    Stores ONLY the fields agent.idle_action() reads, so the reaper survives
    Cloud Run instance recycling where in-memory SESSION_STATE is lost. The rich
    conversation state still lives in memory; this table is liveness only.
    """
    __tablename__ = "chat_sessions"

    chat_id = Column(String, primary_key=True, index=True)   # Telegram chat id (str); web:* never stored
    last_activity = Column(DateTime, default=datetime.datetime.utcnow)
    followup_prompted = Column(Boolean, default=False)
    followup_prompted_at = Column(DateTime, nullable=True)
    has_history = Column(Boolean, default=False)


def _ensure_schema() -> None:
    """Idempotent migration — add new columns to an existing ``tickets`` table
    without dropping it. Works on both SQLite and PostgreSQL.

    ``Base.metadata.create_all`` only creates missing tables, never missing
    columns on existing tables, so we use SQLAlchemy's Inspector to diff and
    apply portable ``ALTER TABLE ADD COLUMN`` statements.
    """
    new_columns = {
        "chat_id": "VARCHAR",
        "confidence_score": "INTEGER",
        "routed_queue": "VARCHAR",
        "sub_topic": "VARCHAR",
        # Phase 2 additions.
        "priority": "VARCHAR",
        "assignee": "VARCHAR",
        "assign_to": "VARCHAR",
    }
    inspector = inspect(engine)
    if not inspector.has_table("tickets"):
        return  # fresh DB — create_all already produced the right schema

    existing = {col["name"] for col in inspector.get_columns("tickets")}
    with engine.begin() as conn:
        for name, sql_type in new_columns.items():
            if name not in existing:
                logger.info("Migrating tickets table: adding column %s", name)
                conn.execute(text(f"ALTER TABLE tickets ADD COLUMN {name} {sql_type}"))


Base.metadata.create_all(bind=engine)
_ensure_schema()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
