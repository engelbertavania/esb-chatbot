# DB-backed idle-session auto-close (Cloud Run safe)

**Date:** 2026-06-11
**Status:** Approved (design)
**Author:** brainstorming session

## Problem

The idle-session reaper (`_session_reaper_loop` in `main.py`) is an in-memory
`asyncio` loop that scans `SESSION_STATE` every 20s and pushes Telegram messages
to nudge ("masih ada yang bisa kami bantu?") and then auto-close quiet sessions.

On Cloud Run with the free-tier config (`--max-instances 1`, default CPU
throttling, scale-to-zero, no `--min-instances`) this loop is unreliable exactly
when it is needed:

1. **CPU throttling** — between requests the CPU is throttled to ~0, so
   `asyncio.sleep` in the loop freezes and the idle timer does not advance.
2. **Scale-to-zero** — after the last request the instance is terminated after a
   non-guaranteed grace period; the loop and `SESSION_STATE` are then gone.

Auto-close must fire precisely when the customer is silent — i.e. when there are
no requests — which is exactly when the loop cannot run. The result: the
goodbye message is usually never sent and the timing is whatever Cloud Run
decides, not the configured interval.

## Goal

Make idle auto-close reliable on the free tier (scale-to-zero, `$0`) by:

- Persisting the minimal session **liveness** state to the database.
- Driving nudge/close from an external **Cloud Scheduler** job that hits a
  `POST /reap` endpoint every minute.
- Keeping the existing pure `idle_action()` as the single source of decision
  logic.

Timing: **nudge after 8 minutes** of silence, **close 2 minutes after the
nudge** (total 10 minutes). Two-stage behaviour is preserved.

Non-goal (YAGNI): persisting the full `SESSION_STATE` (state machine, history,
attachments) to the DB. Only the fields `idle_action()` reads are persisted.

## Approach (chosen: minimal liveness table + reuse `idle_action`)

`idle_action(session, now)` (`agent.py:378`) is pure and reads only:
`session["chat_history"]` (truthy → has a real conversation), `last_activity`,
`followup_prompted`, `followup_prompted_at`. So we persist exactly those.

### 1. Schema — new table `chat_sessions`

Created automatically by `Base.metadata.create_all` (new table; `_ensure_schema`
is only for adding columns to the existing `tickets` table and is not touched).

| Column | Type | Notes |
|---|---|---|
| `chat_id` | String, primary key | Telegram chat id as string. `web:*` sessions are never stored (no push channel). |
| `last_activity` | DateTime (UTC) | Updated on every Telegram turn. |
| `followup_prompted` | Boolean, default `false` | Whether the "still there?" nudge was sent. |
| `followup_prompted_at` | DateTime, nullable | When the nudge was sent. |
| `has_history` | Boolean, default `false` | `true` once a real conversation turn is recorded; gates reaping (empty sessions are never reaped). |

`last_activity` / `followup_prompted_at` are stored as `DateTime` and converted
to epoch seconds when building the pseudo-session dict for `idle_action()`
(which compares against `time.time()`).

### 2. Write path (webhook)

A helper in `main.py`:

```
upsert_session_liveness(db, chat_id, *, last_activity, has_history,
                        followup_prompted=None, followup_prompted_at=None)
```

Called from the `/webhook` handler after `process_message`, **only for Telegram
chat ids** (skip `web:` and non-str ids). On each incoming customer turn:

- `last_activity = now`
- `has_history = true`
- **reset** `followup_prompted = false`, `followup_prompted_at = null`

The reset mirrors the in-memory behaviour: a new message means the customer is
active again, so any prior nudge state is cleared.

Upsert is portable (SQLite local + Postgres prod): select-then-insert/update via
SQLAlchemy ORM (avoids dialect-specific `ON CONFLICT`).

### 3. Endpoint `POST /reap`

Protected by header `X-Reap-Secret` compared to env `REAP_SECRET`. If
`REAP_SECRET` is unset or the header does not match → `403` (same pattern as
`/webhook`'s `TELEGRAM_WEBHOOK_SECRET`). A startup warning is logged when
`REAP_SECRET` is unset.

Logic for one pass:

1. `now = time.time()`.
2. Query `chat_sessions` rows where `has_history = true`.
3. For each row, build a pseudo-session dict
   `{"chat_history": [1] if has_history else [], "last_activity": <epoch>,
   "followup_prompted": <bool>, "followup_prompted_at": <epoch or now>}` and call
   `idle_action(pseudo, now)`.
4. On `"prompt"`: send the nudge message; set `followup_prompted = true`,
   `followup_prompted_at = now`; if `chat_id` is present in the in-memory
   `SESSION_STATE`, mirror the same flags onto it.
5. On `"close"`: send the close message; **delete** the row; if present in
   `SESSION_STATE`, reset it to `_fresh_session()`.
6. On `None`: leave untouched.
7. Return `{"prompted": n, "closed": m}`.

Sending reuses `send_telegram_message`. Per-row send/DB errors are caught and
logged; one bad row never aborts the pass.

The nudge and close strings move to module-level constants
(`FOLLOWUP_PROMPT_TEXT`, `SESSION_CLOSE_TEXT`) so the (now removed) loop and the
endpoint share one definition.

### 4. Remove the in-memory reaper

Delete `_session_reaper_loop`, `_reap_idle_sessions`, `_send_async`,
`REAPER_INTERVAL_SECONDS`, and the `asyncio.create_task(...)` in `lifespan`.
`lifespan` becomes a minimal no-op context manager (kept for future startup
hooks). `idle_action` stays (now consumed by `/reap`).

### 5. Timing change

In `agent.py`:

- `FOLLOWUP_PROMPT_AFTER_SECONDS = 8 * 60`  (was `2 * 60`)
- `FOLLOWUP_CLOSE_AFTER_SECONDS = 2 * 60`   (unchanged)

### 6. Cloud Scheduler

A job that issues `POST <service-url>/reap` every minute (`* * * * *`) with
header `X-Reap-Secret: <REAP_SECRET>`. Requires the
`cloudscheduler.googleapis.com` API enabled; region `us-central1`. Cloud
Scheduler's first 3 jobs/month are free. A `gcloud scheduler jobs create http`
command is provided in the implementation plan.

### 7. Config & deploy

- New env var `REAP_SECRET` added to `.env.example` and the local `.env`
  (user fills a random 32+ char string).
- `deploy.ps1` already forwards every non-skipped `.env` var, so `REAP_SECRET`
  ships to Cloud Run automatically on the next deploy.
- The same secret value is set in the Scheduler job header.

## Data flow

```
Customer msg ──▶ POST /webhook ──▶ process_message (in-memory SESSION_STATE)
                                └─▶ upsert_session_liveness (DB: last_activity↑, flags reset)

Cloud Scheduler (every 1 min) ──▶ POST /reap (X-Reap-Secret)
   └─ for each chat_sessions row: idle_action()
        ├─ prompt → send nudge, set followup flags (DB + in-memory mirror)
        └─ close  → send goodbye, delete row, reset in-memory session
```

## Error handling

- Missing/!match `REAP_SECRET` → `403`; unset secret logs a startup warning.
- Telegram send failure → logged, row still updated/closed so we don't spam
  retries every minute (a close is attempted once; if the send fails the row is
  still deleted — acceptable for a best-effort goodbye).
- DB error on a single row → caught, logged, pass continues.
- Multi-instance note: design assumes `--max-instances 1` so `/webhook` and
  `/reap` share one process and the in-memory mirror is consistent. If instances
  are ever scaled up, the DB remains the source of truth for nudge/close, so
  messages still fire correctly; a stale in-memory session on another instance
  would linger only until the existing 30-minute `SESSION_TIMEOUT_SECONDS` reset
  on the next message. This is acceptable and explicitly out of scope to fix.

## Testing

New `test_reaper_endpoint.py` (follows `test_deescalation.py`: monkeypatch
`send_telegram_message`, `TestClient`, secret header):

- Row idle < 8 min → `/reap` does nothing (`prompted=0, closed=0`).
- Row idle > 8 min, not prompted → nudge sent, `followup_prompted=true` set.
- Row prompted, < 2 min since → nothing.
- Row prompted, > 2 min since → close message sent, row deleted.
- Row with `has_history=false` → never reaped.
- `/reap` without/with wrong `X-Reap-Secret` → `403`.
- Webhook turn → `upsert_session_liveness` sets `last_activity` and resets
  `followup_prompted`.

Existing `idle_action` unit coverage (if any) is unchanged since the function is
reused as-is.

## Out of scope

- Full session persistence across instances (Approach B).
- Always-on instance / `--no-cpu-throttling` (Approach C, paid).
- Web (`web:*`) session auto-close — web has no out-of-band push channel.
