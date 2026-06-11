# CC Handoff — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backend for live chat with Customer Care — a customer reaches a human from the predefined menu, the bot goes silent and relays, and a CC agent joins/chats/ends from REST endpoints; idle handoffs auto-end via `/reap`.

**Architecture:** DB-backed handoff (survives Cloud Run scale-to-zero): a `live_messages` table + handoff columns on `tickets`. The Telegram webhook intercepts handoff before `process_message`. CC actions are plain JSON endpoints under `/api/tickets/{id}/handoff/*`. This plan is backend-only and fully testable via pytest; the dashboard UI is a follow-up plan.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (SQLite local / Postgres prod), pytest + TestClient.

**Spec:** `docs/superpowers/specs/2026-06-11-cc-handoff-design.md`
**Branch:** `feature/cc-handoff`

Use `python -m pytest` (fallback `.\venv\Scripts\python.exe -m pytest` on Windows if `python` is not found). Locate code by CONTENT (line numbers may drift).

---

## File structure

- **Modify** `database.py` — `LiveMessage` model; `Ticket` handoff columns + `_ensure_schema` entries.
- **Modify** `agent.py` — `HANDOFF_OPTION`; append to predefined option lists; `CHOOSING_PREDEFINED` + IDLE-no-match handling; defensive `HUMAN_HANDOFF` branch.
- **Modify** `main.py` — handoff helpers, webhook handling (handoff_request + intercept), 4 endpoints, `/reap` auto-end, `_ticket_to_dict` fields.
- **Create** `test_handoff.py` — backend tests.

Shared names (must match across tasks): model `LiveMessage(id, ticket_id, chat_id, sender, text, author, created_at)`; Ticket columns `handoff_state`/`handoff_agent`/`handoff_last_activity`; helpers `_active_handoff_for(db, chat_id)`, `_record_live_message(db, ticket, sender, text, author=None)`, `_bump_handoff_activity(db, ticket)`, `_create_handoff_ticket(db, chat_id, user_info)`, `_start_handoff(chat_id, user_info)`, `_relay_if_handoff(chat_id, text, attachment)`, `_live_message_to_dict(m)`; constants `HANDOFF_OPTION`, `HANDOFF_REQUEST_TEXT`, `HANDOFF_JOIN_TEXT`, `HANDOFF_END_TEXT`, `HANDOFF_IDLE_TIMEOUT`.

---

## Task 1: DB — `LiveMessage` model + `Ticket` handoff columns

**Files:** Modify `database.py`. Test: `test_handoff.py` (new).

- [ ] **Step 1: Write the failing test** — create `test_handoff.py`:

```python
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
```

- [ ] **Step 2: Run** `python -m pytest test_handoff.py::test_live_message_and_handoff_columns_roundtrip -v` → FAIL (`cannot import name 'LiveMessage'`).

- [ ] **Step 3:** In `database.py`, after the `ChatSession` class and BEFORE `def _ensure_schema`, add:

```python
class LiveMessage(Base):
    """One message in a live Customer-Care handoff conversation (both directions).

    Source of truth for the live transcript; polled by the dashboard via
    GET /api/tickets/{id}/handoff/messages. See
    docs/superpowers/specs/2026-06-11-cc-handoff-design.md.
    """
    __tablename__ = "live_messages"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), index=True)
    chat_id = Column(String, index=True)              # Telegram chat id (str)
    sender = Column(String)                           # customer | agent | system
    text = Column(Text)
    author = Column(String, nullable=True)            # agent name for agent/system rows
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
```

- [ ] **Step 4:** In `database.py`, add three columns to the `Ticket` model (after the Phase 2 `assign_to` column):

```python
    # Customer Care live-chat handoff.
    handoff_state = Column(String)               # requested | active | ended | None
    handoff_agent = Column(String)               # agent who joined
    handoff_last_activity = Column(DateTime)     # bumped on each relayed msg; drives auto-end
```

- [ ] **Step 5:** In `database.py` `_ensure_schema`, add to the `new_columns` dict:

```python
        # Customer Care handoff.
        "handoff_state": "VARCHAR",
        "handoff_agent": "VARCHAR",
        "handoff_last_activity": "TIMESTAMP",
```

- [ ] **Step 6: Run** `python -m pytest test_handoff.py::test_live_message_and_handoff_columns_roundtrip -v` → PASS (`create_all` makes `live_messages`; `_ensure_schema` adds the columns to an existing `tickets` table).

- [ ] **Step 7: Commit**

```bash
git add database.py test_handoff.py
git commit -m "feat(db): live_messages table + ticket handoff columns"
```

---

## Task 2: agent.py — menu entry, `handoff_request`, defensive `HUMAN_HANDOFF`

**Files:** Modify `agent.py`. Test: `test_handoff.py`.

- [ ] **Step 1: Write failing tests** — append to `test_handoff.py`:

```python
from agent import process_message, SESSION_STATE, HANDOFF_OPTION


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
```

- [ ] **Step 2: Run** `python -m pytest test_handoff.py -k "handoff_option or no_match_offers or human_handoff_state" -v` → FAIL (`cannot import name 'HANDOFF_OPTION'`).

- [ ] **Step 3:** In `agent.py`, near the other menu constants (e.g. by `ESCAPE_OPTION`), add:

```python
HANDOFF_OPTION = "💬 Chat dengan Customer Care"
HANDOFF_REQUEST_TEXT = (
    "Baik, mohon tunggu sebentar ya 🙏 Tim Customer Care kami akan segera "
    "bergabung dengan Anda."
)
```

- [ ] **Step 4:** Append `HANDOFF_OPTION` to the option lists in the three presenters. In `_present_matching_predefined` change the return to:

```python
    return {"type": "question", "text": text, "options": choices + [ESCAPE_OPTION, HANDOFF_OPTION]}
```

In `_present_predefined_menu` change its return `"options"` to `choices + [ESCAPE_OPTION, HANDOFF_OPTION]`. In `_present_category_issues` change its return `"options"` to `choices + [ESCAPE_OPTION, HANDOFF_OPTION]`.

- [ ] **Step 5:** In `process_message`, in the `CHOOSING_PREDEFINED` branch, add the handoff check as the FIRST thing inside `if state == "CHOOSING_PREDEFINED":` (before `choices = ...`):

```python
        if raw == HANDOFF_OPTION:
            session["state"] = "HUMAN_HANDOFF"
            _record_turn(session, "assistant", HANDOFF_REQUEST_TEXT)
            return {"type": "handoff_request", "text": HANDOFF_REQUEST_TEXT}
```

(`raw` is the original-case text; `HANDOFF_OPTION` contains an emoji, so match on `raw`, not the lowercased `msg`.)

- [ ] **Step 6:** Make handoff reachable from the IDLE no-match branch. Replace the final no-match return in `process_message` (the "Maaf, saya belum menemukan kendala yang cocok..." block) with a `question` offering the handoff option:

```python
    text_out = (
        "Maaf, saya belum menemukan kendala yang cocok dengan pesan Anda.\n"
        "Coba jelaskan dengan kata lain ya — misalnya \"pesanan tidak masuk POS\", "
        "\"upload foto menu\", atau \"setting payment\".\n\n"
        "Atau, jika ingin berbicara langsung dengan tim kami, pilih di bawah ini:"
    )
    session["state"] = "CHOOSING_PREDEFINED"
    session["predefined_choices"] = []
    _record_turn(session, "assistant", text_out)
    return {"type": "question", "text": text_out, "options": [HANDOFF_OPTION]}
```

(State becomes `CHOOSING_PREDEFINED` with empty choices so the existing dispatch handles a `HANDOFF_OPTION` pick via Step 5; any other reply falls through to a fresh IDLE match.)

- [ ] **Step 7:** Add the defensive `HUMAN_HANDOFF` branch. In `process_message`, right after `state = session["state"]` and before the `WRAP_UP` branch, add:

```python
    # ---- HUMAN_HANDOFF — a CC agent owns this chat; never auto-answer. ----
    # The webhook normally intercepts handoff before reaching here (DB-backed);
    # this is a fallback if only in-memory state says handoff.
    if state == "HUMAN_HANDOFF":
        text_out = "Mohon tunggu ya 🙏 Tim Customer Care kami akan segera membantu Anda."
        _record_turn(session, "assistant", text_out)
        return {"type": "message", "text": text_out}
```

- [ ] **Step 8: Run** `python -m pytest test_handoff.py -k "handoff_option or no_match_offers or human_handoff_state" -v` → PASS (3).

- [ ] **Step 9: Commit**

```bash
git add agent.py test_handoff.py
git commit -m "feat(agent): Customer Care handoff menu option + handoff_request"
```

---

## Task 3: main.py — handoff DB helpers

**Files:** Modify `main.py`. Test: `test_handoff.py`.

- [ ] **Step 1: Write failing tests** — append to `test_handoff.py`:

```python
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
```

- [ ] **Step 2: Run** `python -m pytest test_handoff.py -k "active_handoff_for or record_live_message" -v` → FAIL (`module 'main' has no attribute '_active_handoff_for'`).

- [ ] **Step 3:** In `main.py`, add `LiveMessage` to the database import:

```python
from database import engine, SessionLocal, Base, Ticket, CSATRating, TicketNote, ChatSession, LiveMessage
```

- [ ] **Step 4:** In `main.py`, near the other handoff/helper code (after `_touch_session_liveness` is fine), add the constants and helpers:

```python
# --- Customer Care live-chat handoff ---------------------------------------
HANDOFF_JOIN_TEXT = (
    "Anda sekarang terhubung dengan Customer Care kami. Silakan sampaikan "
    "kebutuhan Anda 🙏"
)
HANDOFF_END_TEXT = (
    "Sesi live chat dengan Customer Care telah berakhir. Terima kasih 🙏 "
    "Ketik pesan kapan saja untuk memulai lagi dengan asisten kami."
)
HANDOFF_IDLE_TIMEOUT = 15 * 60  # seconds of inactivity before a live chat auto-ends


def _active_handoff_for(db, chat_id: str):
    """Return the most recent Ticket for chat_id whose handoff is requested or
    active, else None."""
    return (
        db.query(Ticket)
        .filter(Ticket.chat_id == str(chat_id),
                Ticket.handoff_state.in_(["requested", "active"]))
        .order_by(Ticket.id.desc())
        .first()
    )


def _record_live_message(db, ticket, sender: str, text: str, author: str | None = None):
    """Append a row to live_messages for this handoff ticket and commit."""
    m = LiveMessage(ticket_id=ticket.id, chat_id=ticket.chat_id,
                    sender=sender, text=text, author=author)
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _bump_handoff_activity(db, ticket) -> None:
    ticket.handoff_last_activity = datetime.datetime.utcnow()
    db.commit()


def _live_message_to_dict(m) -> dict:
    return {
        "id": m.id,
        "sender": m.sender,
        "text": m.text,
        "author": m.author,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }
```

- [ ] **Step 5: Run** `python -m pytest test_handoff.py -k "active_handoff_for or record_live_message" -v` → PASS (2).

- [ ] **Step 6: Commit**

```bash
git add main.py test_handoff.py
git commit -m "feat(main): handoff DB helpers + constants"
```

---

## Task 4: main.py — webhook starts a handoff on `handoff_request`

**Files:** Modify `main.py`. Test: `test_handoff.py`.

- [ ] **Step 1: Write failing test** — append to `test_handoff.py`:

```python
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


def test_webhook_starts_handoff_and_creates_ticket(tg):
    client, headers, post, sent = tg
    # Put the session in CHOOSING_PREDEFINED so the option pick triggers handoff.
    main.SESSION_STATE["7100"] = {**_fresh_session(), "state": "CHOOSING_PREDEFINED",
                                  "predefined_choices": [],
                                  "chat_history": [{"role": "user", "text": "x", "ts": 0}]}
    out = post(7100, main.HANDOFF_OPTION_VALUE)
    # Bot sent the "wait a moment" text...
    assert any("tunggu" in s["text"].lower() for s in out)
    # ...and a requested handoff ticket now exists with a system live_message.
    db = SessionLocal()
    try:
        t = main._active_handoff_for(db, "7100")
        assert t is not None and t.handoff_state == "requested"
        msgs = db.query(LiveMessage).filter(LiveMessage.ticket_id == t.id).all()
        assert any(m.sender == "system" for m in msgs)
    finally:
        db.close()
```

Note: the test references `main.HANDOFF_OPTION_VALUE` — expose the agent constant on `main` for convenience in Step 3.

- [ ] **Step 2: Run** `python -m pytest test_handoff.py::test_webhook_starts_handoff_and_creates_ticket -v` → FAIL (`main` has no `HANDOFF_OPTION_VALUE` / no ticket created).

- [ ] **Step 3:** In `main.py`, import the agent option + ticket-number helper and expose the value. Add to the `from agent import (...)` block: `HANDOFF_OPTION as HANDOFF_OPTION_VALUE` and `_format_ticket_number`. (If `_format_ticket_number` is not exported, import it explicitly: `from agent import _format_ticket_number`.)

- [ ] **Step 4:** In `main.py`, add the ticket-creation + start helpers (near the other handoff helpers):

```python
def _create_handoff_ticket(db, chat_id: str, user_info: dict | None):
    """Create a 'requested' handoff Ticket for this chat and record a system
    live_message. Returns the ticket."""
    info = user_info or {}
    t = Ticket(
        ticket_number=_format_ticket_number(),
        name=info.get("full_name") or info.get("first_name") or "",
        phone_number="",
        company_name="",
        branch_name="",
        chat_id=str(chat_id),
        issue_category="Customer Care (Live Chat)",
        issue_detail="Customer meminta live chat dengan Customer Care.",
        status="Waiting",
        handoff_state="requested",
        handoff_last_activity=datetime.datetime.utcnow(),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    _record_live_message(db, t, "system", "Customer meminta live chat dengan Customer Care.")
    return t


def _start_handoff(chat_id: str, user_info: dict | None) -> None:
    """Best-effort: create the handoff ticket (own DB session). Skips web: ids."""
    if str(chat_id).startswith("web:"):
        return
    db = SessionLocal()
    try:
        if _active_handoff_for(db, str(chat_id)) is None:
            _create_handoff_ticket(db, str(chat_id), user_info)
    except Exception as e:
        logging.warning("start handoff failed for %s: %s", chat_id, e)
        db.rollback()
    finally:
        db.close()
```

Verify `_format_ticket_number` and the `user_info` keys: check `_telegram_user` in `main.py` for the exact key names (e.g. `full_name`/`first_name`) and adjust `info.get(...)` to match what `_telegram_user` returns.

- [ ] **Step 5:** In `telegram_webhook`, in the `if text:` branch, intercept the `handoff_request` response. Change:

```python
    if text:
        _maybe_send_calming(chat_id, text, user_info, background_tasks)
        response = process_message(str(chat_id), text)
        background_tasks.add_task(send_telegram_message, chat_id, response, user_info)
```

to:

```python
    if text:
        _maybe_send_calming(chat_id, text, user_info, background_tasks)
        response = process_message(str(chat_id), text)
        if response.get("type") == "handoff_request":
            _start_handoff(str(chat_id), user_info)
            response = {"type": "message", "text": response["text"]}
        background_tasks.add_task(send_telegram_message, chat_id, response, user_info)
```

- [ ] **Step 6: Run** `python -m pytest test_handoff.py::test_webhook_starts_handoff_and_creates_ticket -v` → PASS.

- [ ] **Step 7: Commit**

```bash
git add main.py test_handoff.py
git commit -m "feat(webhook): start handoff + create ticket on handoff_request"
```

---

## Task 5: main.py — webhook intercepts messages during a handoff

**Files:** Modify `main.py`. Test: `test_handoff.py`.

- [ ] **Step 1: Write failing test** — append to `test_handoff.py`:

```python
def test_webhook_relays_customer_message_during_handoff(tg, monkeypatch):
    client, headers, post, sent = tg
    # Seed an ACTIVE handoff for this chat.
    db = SessionLocal()
    try:
        db.add(Ticket(ticket_number="Ticket #TEST05", chat_id="7200",
                      issue_category="Customer Care (Live Chat)", handoff_state="active",
                      handoff_last_activity=datetime.datetime(2020, 1, 1)))
        db.commit()
    finally:
        db.close()
    # process_message must NOT be used to auto-answer during handoff.
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
        assert t.handoff_last_activity > datetime.datetime(2021, 1, 1)  # bumped
    finally:
        db.close()
```

- [ ] **Step 2: Run** `python -m pytest test_handoff.py::test_webhook_relays_customer_message_during_handoff -v` → FAIL (AssertionError: bot answered during handoff).

- [ ] **Step 3:** In `main.py`, add the relay helper (near the other handoff helpers):

```python
def _relay_if_handoff(chat_id: str, text: str | None, attachment: dict | None) -> bool:
    """If this chat is in a handoff, record the customer's message to
    live_messages and return True (caller sends no bot reply). Else False."""
    if str(chat_id).startswith("web:"):
        return False
    db = SessionLocal()
    try:
        h = _active_handoff_for(db, str(chat_id))
        if h is None:
            return False
        if text:
            body = text
        elif attachment:
            body = f"[lampiran: {attachment.get('kind', 'file')}]"
        else:
            body = ""
        _record_live_message(db, h, "customer", body)
        _bump_handoff_activity(db, h)
        return True
    except Exception as e:
        logging.warning("handoff relay failed for %s: %s", chat_id, e)
        db.rollback()
        return False
    finally:
        db.close()
```

- [ ] **Step 4:** In `telegram_webhook`, call the relay BEFORE the attachment/text branches. Right after the existing `_touch_session_liveness(str(chat_id))` line, add:

```python
    # If a CC agent owns this chat, relay the customer's message to the dashboard
    # and stay silent (DB-backed so it survives instance recycle).
    if _relay_if_handoff(str(chat_id), text, attachment):
        return {"status": "ok"}
```

- [ ] **Step 5: Run** `python -m pytest test_handoff.py::test_webhook_relays_customer_message_during_handoff -v` → PASS.

- [ ] **Step 6: Run the file** `python -m pytest test_handoff.py -v` → all green.

- [ ] **Step 7: Commit**

```bash
git add main.py test_handoff.py
git commit -m "feat(webhook): relay customer messages during handoff (bot silent)"
```

---

## Task 6: main.py — `POST /api/tickets/{id}/handoff/join`

**Files:** Modify `main.py`. Test: `test_handoff.py`.

- [ ] **Step 1: Write failing tests** — append to `test_handoff.py`:

```python
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
```

- [ ] **Step 2: Run** `python -m pytest test_handoff.py -k "join_activates or join_twice" -v` → FAIL (404, route undefined).

- [ ] **Step 3:** In `main.py`, after the existing ticket endpoints (e.g. after the notes endpoints), add:

```python
@app.post("/api/tickets/{ticket_id}/handoff/join")
def handoff_join(ticket_id: int, body: dict, db: Session = Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if t is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    if t.handoff_state != "requested":
        raise HTTPException(status_code=409, detail=f"handoff is {t.handoff_state}, not requested")
    agent = (body or {}).get("agent") or "Customer Care"
    t.handoff_state = "active"
    t.handoff_agent = agent
    t.assignee = agent
    t.status = "In Progress"
    t.handoff_last_activity = datetime.datetime.utcnow()
    db.commit()
    _record_live_message(db, t, "system", f"{agent} bergabung ke live chat.", author=agent)
    try:
        send_telegram_message(int(t.chat_id), {"type": "message", "text": HANDOFF_JOIN_TEXT})
    except Exception as e:
        logging.warning("handoff join send failed for %s: %s", t.chat_id, e)
    return _ticket_to_dict(t)
```

- [ ] **Step 4: Run** `python -m pytest test_handoff.py -k "join_activates or join_twice" -v` → PASS (2).

- [ ] **Step 5: Commit**

```bash
git add main.py test_handoff.py
git commit -m "feat(api): POST handoff/join (activate + greet customer)"
```

---

## Task 7: main.py — send message + poll messages

**Files:** Modify `main.py`. Test: `test_handoff.py`.

- [ ] **Step 1: Write failing tests** — append to `test_handoff.py`:

```python
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
```

- [ ] **Step 2: Run** `python -m pytest test_handoff.py -k "agent_message or get_messages" -v` → FAIL (404).

- [ ] **Step 3:** In `main.py`, add:

```python
@app.post("/api/tickets/{ticket_id}/handoff/message")
def handoff_message(ticket_id: int, body: dict, db: Session = Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if t is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    if t.handoff_state != "active":
        raise HTTPException(status_code=409, detail=f"handoff is {t.handoff_state}, not active")
    text = ((body or {}).get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty message")
    author = (body or {}).get("author") or t.handoff_agent or "Customer Care"
    m = _record_live_message(db, t, "agent", text, author=author)
    _bump_handoff_activity(db, t)
    try:
        send_telegram_message(int(t.chat_id), {"type": "message", "text": text})
    except Exception as e:
        logging.warning("handoff message send failed for %s: %s", t.chat_id, e)
    return _live_message_to_dict(m)


@app.get("/api/tickets/{ticket_id}/handoff/messages")
def handoff_messages(ticket_id: int, after_id: int = 0, db: Session = Depends(get_db)):
    rows = (
        db.query(LiveMessage)
        .filter(LiveMessage.ticket_id == ticket_id, LiveMessage.id > after_id)
        .order_by(LiveMessage.id)
        .all()
    )
    return [_live_message_to_dict(m) for m in rows]
```

- [ ] **Step 4: Run** `python -m pytest test_handoff.py -k "agent_message or get_messages" -v` → PASS (3).

- [ ] **Step 5: Commit**

```bash
git add main.py test_handoff.py
git commit -m "feat(api): POST handoff/message + GET handoff/messages"
```

---

## Task 8: main.py — `POST /api/tickets/{id}/handoff/end`

**Files:** Modify `main.py`. Test: `test_handoff.py`.

- [ ] **Step 1: Write failing test** — append to `test_handoff.py`:

```python
def test_end_resolves_and_resets_session(tg):
    client, headers, post, sent = tg
    tid = _seed_handoff("8300", "active")
    main.SESSION_STATE["8300"] = {**_fresh_session(), "state": "HUMAN_HANDOFF"}
    r = client.post(f"/api/tickets/{tid}/handoff/end", json={"agent": "CC - Ayu"})
    assert r.status_code == 200
    body = r.json()
    assert body["handoff_state"] == "ended" and body["status"] == "Resolved"
    assert any("berakhir" in s["text"].lower() for s in sent)
    # In-memory session reset to fresh IDLE so the bot resumes.
    assert main.SESSION_STATE["8300"]["state"] == "IDLE"
    # A subsequent customer message is no longer intercepted as handoff.
    assert main._relay_if_handoff("8300", "halo", None) is False
```

- [ ] **Step 2: Run** `python -m pytest test_handoff.py::test_end_resolves_and_resets_session -v` → FAIL (404).

- [ ] **Step 3:** In `main.py`, add:

```python
@app.post("/api/tickets/{ticket_id}/handoff/end")
def handoff_end(ticket_id: int, body: dict, db: Session = Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if t is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    if t.handoff_state not in ("requested", "active"):
        raise HTTPException(status_code=409, detail=f"handoff is {t.handoff_state}")
    agent = (body or {}).get("agent") or t.handoff_agent or "Customer Care"
    t.handoff_state = "ended"
    t.status = "Resolved"
    db.commit()
    _record_live_message(db, t, "system", f"Live chat diakhiri oleh {agent}.", author=agent)
    if t.chat_id in SESSION_STATE:
        SESSION_STATE[t.chat_id] = _fresh_session()
    try:
        send_telegram_message(int(t.chat_id), {"type": "message", "text": HANDOFF_END_TEXT})
    except Exception as e:
        logging.warning("handoff end send failed for %s: %s", t.chat_id, e)
    return _ticket_to_dict(t)
```

- [ ] **Step 4: Run** `python -m pytest test_handoff.py::test_end_resolves_and_resets_session -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add main.py test_handoff.py
git commit -m "feat(api): POST handoff/end (resolve + reset session)"
```

---

## Task 9: main.py — `/reap` auto-ends idle handoffs

**Files:** Modify `main.py`. Test: `test_handoff.py`.

- [ ] **Step 1: Write failing test** — append to `test_handoff.py`:

```python
def test_reap_auto_ends_idle_handoff(tg, monkeypatch):
    client, headers, post, sent = tg
    monkeypatch.setattr(main, "REAP_SECRET", "testsecret")
    db = SessionLocal()
    try:
        old = datetime.datetime.utcnow() - datetime.timedelta(minutes=20)
        db.add(Ticket(ticket_number="Ticket #REAP1", chat_id="8400",
                      issue_category="Customer Care (Live Chat)", status="In Progress",
                      handoff_state="active", handoff_last_activity=old))
        db.commit()
    finally:
        db.close()
    r = client.post("/reap", headers={"X-Reap-Secret": "testsecret"})
    assert r.status_code == 200 and r.json()["handoffs_ended"] == 1
    assert any("berakhir" in s["text"].lower() for s in sent)
    db = SessionLocal()
    try:
        t = db.query(Ticket).filter(Ticket.chat_id == "8400").first()
        assert t.handoff_state == "ended" and t.status == "Resolved"
    finally:
        db.close()
        db2 = SessionLocal()
        try:
            db2.query(LiveMessage).delete()
            db2.query(Ticket).filter(Ticket.issue_category == "Customer Care (Live Chat)").delete()
            db2.commit()
        finally:
            db2.close()
```

- [ ] **Step 2: Run** `python -m pytest test_handoff.py::test_reap_auto_ends_idle_handoff -v` → FAIL (`KeyError: 'handoffs_ended'`).

- [ ] **Step 3:** In `main.py` `reap_idle_sessions`, before the final `return`, add the handoff sweep, and update the return dict:

```python
    handoffs_ended = 0
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(seconds=HANDOFF_IDLE_TIMEOUT)
    stale = (
        db.query(Ticket)
        .filter(Ticket.handoff_state.in_(["requested", "active"]),
                Ticket.handoff_last_activity < cutoff)
        .all()
    )
    for t in stale:
        try:
            send_telegram_message(int(t.chat_id), {"type": "message", "text": HANDOFF_END_TEXT})
            t.handoff_state = "ended"
            t.status = "Resolved"
            db.commit()
            _record_live_message(db, t, "system", "Live chat ditutup otomatis (tidak ada aktivitas).")
            if t.chat_id in SESSION_STATE:
                SESSION_STATE[t.chat_id] = _fresh_session()
            handoffs_ended += 1
        except Exception as e:
            logging.warning("handoff auto-end failed for %s: %s", t.chat_id, e)
            db.rollback()

    return {"prompted": prompted, "closed": closed, "handoffs_ended": handoffs_ended}
```

(Delete the old `return {"prompted": prompted, "closed": closed}` line.)

- [ ] **Step 4: Run** `python -m pytest test_handoff.py::test_reap_auto_ends_idle_handoff -v` → PASS. Also run `python -m pytest test_reaper_endpoint.py -v` to confirm the existing reap tests still pass (the return dict gained a key; `test_reap_does_nothing_for_recent_session` asserts `body == {"prompted": 0, "closed": 0}` and WILL break — update that test to `body["prompted"] == 0 and body["closed"] == 0` as part of this step, and any other reap test asserting exact-dict equality).

- [ ] **Step 5: Commit**

```bash
git add main.py test_handoff.py test_reaper_endpoint.py
git commit -m "feat(reap): auto-end idle Customer Care handoffs"
```

---

## Task 10: main.py — expose handoff fields in the ticket API

**Files:** Modify `main.py`. Test: `test_handoff.py`.

- [ ] **Step 1: Write failing test** — append to `test_handoff.py`:

```python
def test_ticket_dict_includes_handoff_fields(tg):
    client, headers, post, sent = tg
    tid = _seed_handoff("8500", "active")
    db = SessionLocal()
    try:
        d = main._ticket_to_dict(db.get(Ticket, tid))
    finally:
        db.close()
    assert d["handoff_state"] == "active"
    assert "handoff_agent" in d
```

- [ ] **Step 2: Run** `python -m pytest test_handoff.py::test_ticket_dict_includes_handoff_fields -v` → FAIL (`KeyError: 'handoff_state'`).

- [ ] **Step 3:** In `main.py` `_ticket_to_dict`, add two entries before `"notes":`:

```python
        "handoff_state": t.handoff_state,
        "handoff_agent": t.handoff_agent,
```

- [ ] **Step 4: Run** `python -m pytest test_handoff.py::test_ticket_dict_includes_handoff_fields -v` → PASS.

- [ ] **Step 5: Run the FULL suite** `python -m pytest -q` → all green (handoff + reaper + existing).

- [ ] **Step 6: Commit**

```bash
git add main.py test_handoff.py
git commit -m "feat(api): expose handoff_state/handoff_agent in ticket payload"
```

---

## Self-review

- **Spec coverage:** menu entry + handoff_request (T2); DB-backed state survives recycle via `live_messages` + Ticket columns (T1) and webhook intercept (T5); "wait a moment" + ticket creation (T4); join → "connected" + assignee (T6); two-way relay — agent→customer (T7) and customer→agent recorded for polling (T5/T7); poll endpoint (T7); end → resolved + reset + bot resumes (T8); auto-end via /reap (T9); dashboard payload fields (T10). All spec backend sections mapped. (Dashboard UI is the follow-up frontend plan.)
- **Type/name consistency:** `LiveMessage`, `handoff_state`/`handoff_agent`/`handoff_last_activity`, `_active_handoff_for`/`_record_live_message`/`_bump_handoff_activity`/`_create_handoff_ticket`/`_start_handoff`/`_relay_if_handoff`/`_live_message_to_dict`, `HANDOFF_OPTION`(agent)/`HANDOFF_OPTION_VALUE`(main alias), `HANDOFF_REQUEST_TEXT`/`HANDOFF_JOIN_TEXT`/`HANDOFF_END_TEXT`/`HANDOFF_IDLE_TIMEOUT` used consistently. Endpoint shapes match `_live_message_to_dict`/`_ticket_to_dict`.
- **Placeholder scan:** none — every step has concrete code/commands. Two steps require verifying an existing name against the codebase (T4: `_telegram_user` key names + `_format_ticket_number` export; T9: updating exact-dict-equality reap tests) — these are explicit verification instructions, not placeholders.
- **Known follow-up:** the frontend (lib/api, lib/tm mappers + Live Chat lane, card-detail Join/composer/End/polling, Vitest) is a SEPARATE plan, written against the real submodule files after this backend lands.
