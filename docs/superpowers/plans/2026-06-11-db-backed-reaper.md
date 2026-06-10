# DB-backed idle-session auto-close — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make idle-session nudge/auto-close reliable on Cloud Run scale-to-zero by persisting minimal session liveness to the DB and driving it from a Cloud Scheduler → `POST /reap` job, replacing the in-memory asyncio reaper.

**Architecture:** A new `chat_sessions` table stores only the fields `idle_action()` reads. `/webhook` upserts liveness on each Telegram turn. A secret-guarded `POST /reap` (called every minute by Cloud Scheduler) loads idle rows, reuses the pure `idle_action()` to decide nudge/close, sends Telegram messages out-of-band, and mirrors close to in-memory `SESSION_STATE`. The in-memory reaper loop is removed.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (SQLite local / Postgres prod), pytest + TestClient, gcloud (Cloud Run + Cloud Scheduler).

**Spec:** `docs/superpowers/specs/2026-06-11-db-backed-reaper-design.md`

---

## File structure

- **Modify** `database.py` — add `ChatSession` ORM model + `Boolean` import. `create_all` makes the table; `_ensure_schema` is untouched.
- **Modify** `agent.py:90` — retune `FOLLOWUP_PROMPT_AFTER_SECONDS` to 8 min.
- **Modify** `main.py` — add message constants, `REAP_SECRET`, `datetime` + `ChatSession` imports, `_touch_session_liveness` helper, wire it into `/webhook`, add `POST /reap`, remove the in-memory reaper + simplify `lifespan`.
- **Create** `test_reaper_endpoint.py` — endpoint + liveness behavior tests.
- **Modify** `.env.example` — document `REAP_SECRET`.
- **Operational** — enable Cloud Scheduler API, create the `/reap` job, redeploy.

---

## Task 1: Add the `ChatSession` liveness model

**Files:**
- Modify: `database.py:3` (imports), after `database.py:102` (new model)
- Test: `test_reaper_endpoint.py` (new)

- [ ] **Step 1: Write the failing test**

Create `test_reaper_endpoint.py`:

```python
"""Tests for the DB-backed idle-session reaper (chat_sessions + POST /reap)."""
import datetime

import main
from database import SessionLocal, ChatSession


def _clear_sessions():
    db = SessionLocal()
    try:
        db.query(ChatSession).delete()
        db.commit()
    finally:
        db.close()


def test_chat_session_model_roundtrips():
    _clear_sessions()
    db = SessionLocal()
    try:
        db.add(ChatSession(
            chat_id="9001",
            last_activity=datetime.datetime.utcnow(),
            followup_prompted=False,
            has_history=True,
        ))
        db.commit()
        row = db.get(ChatSession, "9001")
        assert row is not None
        assert row.has_history is True
        assert row.followup_prompted is False
        assert row.followup_prompted_at is None
    finally:
        db.close()
        _clear_sessions()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_reaper_endpoint.py::test_chat_session_model_roundtrips -v`
Expected: FAIL with `ImportError: cannot import name 'ChatSession' from 'database'`.

- [ ] **Step 3: Add `Boolean` to the SQLAlchemy import**

In `database.py:3`, change:

```python
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey, inspect, text
```

to:

```python
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, ForeignKey, inspect, text
```

- [ ] **Step 4: Add the model**

In `database.py`, immediately after the `CSATRating` class (before `def _ensure_schema`), add:

```python
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest test_reaper_endpoint.py::test_chat_session_model_roundtrips -v`
Expected: PASS. (`Base.metadata.create_all` at import creates the new table automatically.)

- [ ] **Step 6: Commit**

```bash
git add database.py test_reaper_endpoint.py
git commit -m "feat(db): add chat_sessions liveness model for reaper"
```

---

## Task 2: Retune the nudge timing to 8 minutes

**Files:**
- Modify: `agent.py:90`
- Test: `test_reaper_endpoint.py`

- [ ] **Step 1: Write the failing test**

Append to `test_reaper_endpoint.py`:

```python
from agent import idle_action, FOLLOWUP_PROMPT_AFTER_SECONDS, FOLLOWUP_CLOSE_AFTER_SECONDS


def test_timing_constants_are_8_then_2_minutes():
    assert FOLLOWUP_PROMPT_AFTER_SECONDS == 8 * 60
    assert FOLLOWUP_CLOSE_AFTER_SECONDS == 2 * 60


def test_idle_action_prompts_after_8_minutes():
    now = 10_000.0
    seven_min = {"chat_history": [1], "last_activity": now - 7 * 60}
    nine_min = {"chat_history": [1], "last_activity": now - 9 * 60}
    assert idle_action(seven_min, now) is None
    assert idle_action(nine_min, now) == "prompt"


def test_idle_action_closes_2_minutes_after_prompt():
    now = 10_000.0
    just_prompted = {"chat_history": [1], "followup_prompted": True,
                     "followup_prompted_at": now - 60}
    long_prompted = {"chat_history": [1], "followup_prompted": True,
                     "followup_prompted_at": now - 3 * 60}
    assert idle_action(just_prompted, now) is None
    assert idle_action(long_prompted, now) == "close"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_reaper_endpoint.py::test_timing_constants_are_8_then_2_minutes -v`
Expected: FAIL — `assert 120 == 480` (current value is `2 * 60`).

- [ ] **Step 3: Change the constant**

In `agent.py:90`, change:

```python
FOLLOWUP_PROMPT_AFTER_SECONDS = 2 * 60   # silence before the check-in nudge
```

to:

```python
FOLLOWUP_PROMPT_AFTER_SECONDS = 8 * 60   # silence before the check-in nudge (nudge@8m, close@10m)
```

Leave `FOLLOWUP_CLOSE_AFTER_SECONDS = 2 * 60` unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_reaper_endpoint.py -k "timing or idle_action" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add agent.py test_reaper_endpoint.py
git commit -m "feat(agent): retune idle nudge to 8 min (close at 10 min)"
```

---

## Task 3: Add constants, config, and the liveness upsert helper to `main.py`

**Files:**
- Modify: `main.py:20` (import), `main.py:36` (after webhook secret), new helper near the webhook section
- Test: `test_reaper_endpoint.py`

- [ ] **Step 1: Write the failing test**

Append to `test_reaper_endpoint.py`:

```python
def test_touch_session_liveness_creates_and_resets():
    _clear_sessions()
    # Pre-seed a prompted row, then a new turn must reset the follow-up state.
    db = SessionLocal()
    try:
        db.add(ChatSession(chat_id="9100", last_activity=datetime.datetime(2020, 1, 1),
                           followup_prompted=True,
                           followup_prompted_at=datetime.datetime(2020, 1, 1),
                           has_history=True))
        db.commit()
    finally:
        db.close()

    main._touch_session_liveness("9100")

    db = SessionLocal()
    try:
        row = db.get(ChatSession, "9100")
        assert row.has_history is True
        assert row.followup_prompted is False
        assert row.followup_prompted_at is None
        assert row.last_activity > datetime.datetime(2021, 1, 1)
    finally:
        db.close()
        _clear_sessions()


def test_touch_session_liveness_skips_web_sessions():
    _clear_sessions()
    main._touch_session_liveness("web:abc")
    db = SessionLocal()
    try:
        assert db.get(ChatSession, "web:abc") is None
    finally:
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_reaper_endpoint.py::test_touch_session_liveness_creates_and_resets -v`
Expected: FAIL — `AttributeError: module 'main' has no attribute '_touch_session_liveness'`.

- [ ] **Step 3: Add imports**

In `main.py:20`, change:

```python
from database import engine, SessionLocal, Base, Ticket, CSATRating, TicketNote
```

to:

```python
from database import engine, SessionLocal, Base, Ticket, CSATRating, TicketNote, ChatSession
```

Ensure `import datetime` is present in the stdlib import block (around `main.py:8-18`). If absent, add it on its own line:

```python
import datetime
```

- [ ] **Step 4: Add `REAP_SECRET` config**

In `main.py`, immediately after the `TELEGRAM_WEBHOOK_SECRET` block (after `main.py:46`), add:

```python
REAP_SECRET = os.getenv("REAP_SECRET", "")
if not REAP_SECRET:
    logging.warning(
        "REAP_SECRET is unset; /reap will reject all callers. Set it (and pass "
        "the same value as the X-Reap-Secret header from Cloud Scheduler).",
    )
```

- [ ] **Step 5: Add message constants + epoch helper + the upsert helper**

In `main.py`, just before the `@app.post("/webhook")` decorator (`main.py:1211`), add:

```python
# Out-of-band idle-session messages — shared by the scheduled /reap pass.
FOLLOWUP_PROMPT_TEXT = (
    "Apakah masih ada yang bisa kami bantu? 🙏 Jika tidak ada balasan "
    "beberapa saat lagi, sesi ini akan saya tutup. Anda bisa mulai lagi "
    "kapan saja dengan mengirim pesan."
)
SESSION_CLOSE_TEXT = (
    "Sesi saya tutup dulu ya karena tidak ada balasan. Terima kasih sudah "
    "menghubungi ESB Order 🙏 Kirim pesan kapan saja jika ada kendala lain."
)


def _epoch(dt: "datetime.datetime | None") -> float | None:
    """Convert a naive-UTC DateTime column to epoch seconds (matches time.time())."""
    if dt is None:
        return None
    return dt.replace(tzinfo=datetime.timezone.utc).timestamp()


def _touch_session_liveness(chat_id: str) -> None:
    """Record a customer turn for the DB-backed reaper: bump last_activity, mark
    the session active, and clear any pending follow-up (a reply means they're
    not idle). Best-effort: never let a DB hiccup break the webhook. Web
    sessions have no push channel, so they're skipped."""
    if chat_id.startswith("web:"):
        return
    db = SessionLocal()
    try:
        row = db.get(ChatSession, chat_id)
        if row is None:
            row = ChatSession(chat_id=chat_id)
            db.add(row)
        row.last_activity = datetime.datetime.utcnow()
        row.has_history = True
        row.followup_prompted = False
        row.followup_prompted_at = None
        db.commit()
    except Exception as e:  # never break message handling over liveness tracking
        logging.warning("session liveness upsert failed for %s: %s", chat_id, e)
        db.rollback()
    finally:
        db.close()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest test_reaper_endpoint.py -k "touch_session" -v`
Expected: PASS (2 tests).

- [ ] **Step 7: Commit**

```bash
git add main.py test_reaper_endpoint.py
git commit -m "feat(main): liveness upsert helper + reap config/constants"
```

---

## Task 4: Wire liveness tracking into `/webhook`

**Files:**
- Modify: `main.py` webhook handler (after the `if chat_id is None: return` guard, ~`main.py:1237`)
- Test: `test_reaper_endpoint.py`

- [ ] **Step 1: Write the failing test**

Append to `test_reaper_endpoint.py`:

```python
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def webhook_client(monkeypatch):
    monkeypatch.setattr(main, "send_telegram_message", lambda *a, **k: None)
    monkeypatch.setattr(main, "process_message",
                        lambda chat_id, text: {"type": "message", "text": "ok"})
    main.SESSION_STATE.clear()
    _clear_sessions()
    client = TestClient(main.app)
    headers = {"X-Telegram-Bot-Api-Secret-Token": main.TELEGRAM_WEBHOOK_SECRET}
    yield client, headers
    _clear_sessions()


def test_webhook_turn_records_liveness(webhook_client):
    client, headers = webhook_client
    client.post("/webhook", headers=headers,
                json={"message": {"chat": {"id": 9200}, "text": "halo"}})
    db = SessionLocal()
    try:
        row = db.get(ChatSession, "9200")
        assert row is not None
        assert row.has_history is True
        assert row.followup_prompted is False
    finally:
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_reaper_endpoint.py::test_webhook_turn_records_liveness -v`
Expected: FAIL — `assert None is not None` (no liveness row written yet).

- [ ] **Step 3: Call the helper in the webhook**

In `main.py`, locate the guard in `telegram_webhook` (~`main.py:1236-1237`):

```python
    if chat_id is None:
        return {"status": "ok"}
```

Immediately after it, add:

```python
    # Mirror liveness to the DB so the scheduled /reap can nudge/close this
    # session even after in-memory SESSION_STATE is lost on instance recycle.
    _touch_session_liveness(str(chat_id))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_reaper_endpoint.py::test_webhook_turn_records_liveness -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add main.py test_reaper_endpoint.py
git commit -m "feat(webhook): record session liveness on each Telegram turn"
```

---

## Task 5: Add the `POST /reap` endpoint

**Files:**
- Modify: `main.py` (add endpoint after `telegram_webhook`, ~`main.py:1281`)
- Test: `test_reaper_endpoint.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_reaper_endpoint.py`:

```python
import datetime as _dt


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


def _seed(chat_id, *, idle_min, prompted=False, prompted_min_ago=None, has_history=True):
    db = SessionLocal()
    try:
        db.add(ChatSession(
            chat_id=chat_id,
            last_activity=_dt.datetime.utcnow() - _dt.timedelta(minutes=idle_min),
            followup_prompted=prompted,
            followup_prompted_at=(None if prompted_min_ago is None
                                  else _dt.datetime.utcnow() - _dt.timedelta(minutes=prompted_min_ago)),
            has_history=has_history,
        ))
        db.commit()
    finally:
        db.close()


def test_reap_rejects_without_secret(reap_client):
    client, _headers, _sent = reap_client
    assert client.post("/reap").status_code == 403
    assert client.post("/reap", headers={"X-Reap-Secret": "wrong"}).status_code == 403


def test_reap_does_nothing_for_recent_session(reap_client):
    client, headers, sent = reap_client
    _seed("8001", idle_min=3)
    body = client.post("/reap", headers=headers).json()
    assert body == {"prompted": 0, "closed": 0}
    assert sent == []


def test_reap_nudges_after_8_minutes(reap_client):
    client, headers, sent = reap_client
    _seed("8002", idle_min=9)
    body = client.post("/reap", headers=headers).json()
    assert body["prompted"] == 1
    assert "masih ada" in sent[0]["text"].lower()
    db = SessionLocal()
    try:
        row = db.get(ChatSession, "8002")
        assert row.followup_prompted is True
        assert row.followup_prompted_at is not None
    finally:
        db.close()


def test_reap_closes_2_minutes_after_nudge(reap_client):
    client, headers, sent = reap_client
    _seed("8003", idle_min=11, prompted=True, prompted_min_ago=3)
    main.SESSION_STATE["8003"] = {"state": "WRAP_UP"}
    body = client.post("/reap", headers=headers).json()
    assert body["closed"] == 1
    assert "tutup" in sent[0]["text"].lower()
    db = SessionLocal()
    try:
        assert db.get(ChatSession, "8003") is None     # row deleted on close
    finally:
        db.close()
    assert main.SESSION_STATE["8003"]["state"] == "IDLE"  # in-memory reset to fresh


def test_reap_ignores_sessions_without_history(reap_client):
    client, headers, sent = reap_client
    _seed("8004", idle_min=20, has_history=False)
    body = client.post("/reap", headers=headers).json()
    assert body == {"prompted": 0, "closed": 0}
    assert sent == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest test_reaper_endpoint.py -k reap -v`
Expected: FAIL — `/reap` returns 404 (route not defined).

- [ ] **Step 3: Implement the endpoint**

In `main.py`, after the `telegram_webhook` function (after `main.py:1281`), add:

```python
@app.post("/reap")
def reap_idle_sessions(request: Request, db: Session = Depends(get_db)):
    """Scheduled idle-session sweep (called every minute by Cloud Scheduler).

    Reads liveness rows from chat_sessions, reuses the pure agent.idle_action()
    to decide, and pushes the nudge/close Telegram message out of band. Replaces
    the old in-memory reaper so it works under Cloud Run scale-to-zero.
    """
    if not REAP_SECRET or request.headers.get("X-Reap-Secret", "") != REAP_SECRET:
        raise HTTPException(status_code=403, detail="invalid reap secret")

    now = time.time()
    prompted = 0
    closed = 0
    rows = db.query(ChatSession).filter(ChatSession.has_history.is_(True)).all()
    for row in rows:
        pseudo = {
            "chat_history": [1] if row.has_history else [],
            "last_activity": _epoch(row.last_activity) or now,
            "followup_prompted": bool(row.followup_prompted),
            "followup_prompted_at": _epoch(row.followup_prompted_at) or now,
        }
        action = idle_action(pseudo, now)
        try:
            if action == "prompt":
                send_telegram_message(int(row.chat_id), {"type": "message", "text": FOLLOWUP_PROMPT_TEXT})
                row.followup_prompted = True
                row.followup_prompted_at = datetime.datetime.utcnow()
                db.commit()
                mem = SESSION_STATE.get(row.chat_id)
                if mem is not None:
                    mem["followup_prompted"] = True
                    mem["followup_prompted_at"] = now
                prompted += 1
            elif action == "close":
                send_telegram_message(int(row.chat_id), {"type": "message", "text": SESSION_CLOSE_TEXT})
                if row.chat_id in SESSION_STATE:
                    SESSION_STATE[row.chat_id] = _fresh_session()
                db.delete(row)
                db.commit()
                closed += 1
        except Exception as e:  # one bad row must not abort the whole pass
            logging.warning("reap failed for %s: %s", row.chat_id, e)
            db.rollback()

    return {"prompted": prompted, "closed": closed}
```

Note: `_fresh_session` and `idle_action` are already imported at `main.py:21-24`; `Request`, `Depends`, `HTTPException` at `main.py:3`; `Session` at `main.py:7`; `get_db` defined at `main.py:96`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_reaper_endpoint.py -k reap -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add main.py test_reaper_endpoint.py
git commit -m "feat(main): add secret-guarded POST /reap idle sweep"
```

---

## Task 6: Remove the in-memory reaper and simplify `lifespan`

**Files:**
- Modify: `main.py:72-81` (lifespan), `main.py:1129-1184` (reaper block)
- Test: `test_reaper_endpoint.py`

- [ ] **Step 1: Write the failing test**

Append to `test_reaper_endpoint.py`:

```python
def test_in_memory_reaper_is_removed():
    # The asyncio reaper is replaced by the scheduled /reap endpoint.
    assert not hasattr(main, "_session_reaper_loop")
    assert not hasattr(main, "_reap_idle_sessions")
    assert not hasattr(main, "REAPER_INTERVAL_SECONDS")


def test_app_still_boots_and_health_ok(monkeypatch):
    client = TestClient(main.app)   # lifespan startup must not raise
    assert client.get("/health").status_code in (200, 503)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_reaper_endpoint.py::test_in_memory_reaper_is_removed -v`
Expected: FAIL — `assert not True` (the attributes still exist).

- [ ] **Step 3: Simplify `lifespan`**

In `main.py:72-81`, replace:

```python
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the idle-session reaper (defined below) so the bot can proactively
    # check in on quiet customers and close stale chats.
    task = asyncio.create_task(_session_reaper_loop())
    logging.info("Idle session reaper started (every %ss).", REAPER_INTERVAL_SECONDS)
    try:
        yield
    finally:
        task.cancel()
```

with:

```python
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Idle nudge/auto-close now runs out-of-process via Cloud Scheduler -> POST
    # /reap (see docs/superpowers/specs/2026-06-11-db-backed-reaper-design.md),
    # so no in-process reaper task is started here.
    yield
```

- [ ] **Step 4: Delete the in-memory reaper block**

In `main.py`, delete the entire block from the `# ── Idle session reaper ...` comment header through the end of `_session_reaper_loop` (originally `main.py:1129-1184`): the `REAPER_INTERVAL_SECONDS` constant, `_send_async`, `_reap_idle_sessions`, and `_session_reaper_loop`. Keep `_maybe_send_calming` (which follows it) intact.

- [ ] **Step 5: Run the full suite to verify nothing regressed**

Run: `python -m pytest test_reaper_endpoint.py -v`
Expected: PASS (all tests, including the two new ones).

Run: `python -m pytest -q`
Expected: the existing suite (de-escalation, monitoring, csat, chat endpoints, session lifecycle) still passes. If `test_session_lifecycle.py` asserts on the removed `_reap_idle_sessions`/`_session_reaper_loop`, update those tests to drive `idle_action` / `POST /reap` instead (do not re-introduce the loop).

- [ ] **Step 6: Commit**

```bash
git add main.py test_reaper_endpoint.py
git commit -m "refactor(main): remove in-memory reaper; rely on scheduled /reap"
```

---

## Task 7: Document `REAP_SECRET` in `.env.example`

**Files:**
- Modify: `.env.example` (after the Telegram block, ~`.env.example:12`)

- [ ] **Step 1: Add the variable**

In `.env.example`, after the `TELEGRAM_WEBHOOK_SECRET=...` line, add:

```bash

# --- Idle-session reaper -----------------------------------------------------
# Shared secret for the scheduled POST /reap sweep. Cloud Scheduler sends this
# as the X-Reap-Secret header; /reap rejects callers that don't match. Use a
# random 32+ char string. Leave unset locally to disable /reap.
REAP_SECRET=another-random-string-32+chars
```

- [ ] **Step 2: Add the real value to your local `.env`**

Add a line to `.env` (NOT committed) with a real random value:

```
REAP_SECRET=<paste a 32+ char random string>
```

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs(env): document REAP_SECRET for the scheduled reaper"
```

---

## Task 8: Deploy and wire up Cloud Scheduler

**Files:** none (operational). Run from the project root in PowerShell.

- [ ] **Step 1: Redeploy so `REAP_SECRET` and `/reap` reach Cloud Run**

Run: `.\deploy.ps1 -Project gen-lang-client-0768587181`
Expected: build succeeds, prints the service URL. `deploy.ps1` already forwards `REAP_SECRET` from `.env`.

- [ ] **Step 2: Verify `/reap` rejects unauthenticated calls**

Run:
```powershell
$u = "https://esb-chatbot-1041207801679.us-central1.run.app/reap"
try { Invoke-WebRequest -Uri $u -Method POST -UseBasicParsing } catch { $_.Exception.Response.StatusCode.value__ }
```
Expected: `403`.

- [ ] **Step 3: Enable the Cloud Scheduler API**

Run: `gcloud services enable cloudscheduler.googleapis.com --project gen-lang-client-0768587181`
Expected: `Operation ... finished successfully.`

- [ ] **Step 4: Create the every-minute job**

Replace `<REAP_SECRET>` with the value from your `.env`:

```powershell
gcloud scheduler jobs create http esb-reap `
  --project gen-lang-client-0768587181 `
  --location us-central1 `
  --schedule "* * * * *" `
  --uri "https://esb-chatbot-1041207801679.us-central1.run.app/reap" `
  --http-method POST `
  --headers "X-Reap-Secret=<REAP_SECRET>" `
  --attempt-deadline 30s
```
Expected: job `esb-reap` created.

- [ ] **Step 5: Trigger once and confirm it works**

Run: `gcloud scheduler jobs run esb-reap --project gen-lang-client-0768587181 --location us-central1`
Then check logs:
```powershell
gcloud run services logs read esb-chatbot --project gen-lang-client-0768587181 --region us-central1 --limit 20
```
Expected: a `POST /reap` request logged with `200`.

- [ ] **Step 6: End-to-end manual check (optional)**

Send the bot a message, wait ~8 min: a "masih ada yang bisa kami bantu?" nudge should arrive; ~2 min later, the close message. Confirms the full Scheduler → `/reap` → Telegram path.

---

## Self-review

- **Spec coverage:** schema (T1), webhook upsert (T3/T4), `/reap` + `idle_action` reuse + in-memory mirror (T5), remove asyncio reaper (T6), timing 8/10 (T2), Cloud Scheduler (T8), `REAP_SECRET` config (T3/T7), tests (T1-T6). All spec sections mapped.
- **Type/name consistency:** `ChatSession`, `_touch_session_liveness`, `_epoch`, `FOLLOWUP_PROMPT_TEXT`, `SESSION_CLOSE_TEXT`, `REAP_SECRET`, `reap_idle_sessions` used identically across tasks. `idle_action` consumed unchanged. Pseudo-session keys match what `idle_action` reads (`chat_history`, `last_activity`, `followup_prompted`, `followup_prompted_at`).
- **Placeholder scan:** none — every code/command step is concrete.
- **Edge case (stated in spec):** close-message send failure still deletes the row (best-effort goodbye); per-row try/except keeps the pass alive.
