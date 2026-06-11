# Live Chat with Customer Care (human handoff) — Design

**Date:** 2026-06-11
**Status:** Approved (design)
**Author:** brainstorming session

## Problem

The bot fully automates customer support. Some customers need a real human.
There is no way today for a customer to reach a live Customer Care (CC) agent,
and no way for a CC agent to chat with a customer from the dashboard — the
dashboard conversation pane is read-only and the internal notes are never sent
to the customer.

## Goal

Add a human-handoff flow:

1. A **"Chat dengan Customer Care"** option in the `/start` predefined menu.
2. When the customer picks it, the bot replies "okay, please wait a moment" and
   **stops auto-responding** for that customer.
3. The request **appears on the dashboard** (reusing the ticket/kanban flow) in a
   "Live Chat" lane.
4. A CC agent clicks **"Join chat"**; the bot tells the customer "you are now
   connected with Customer Care".
5. From then on the **agent and customer chat two-way** — agent types in the
   dashboard → Telegram; customer's Telegram messages → dashboard (polled). The
   bot relays and does not auto-answer.
6. The agent ends the chat (button); the bot tells the customer and control
   returns to the bot. A long-idle handoff auto-ends via the existing reaper.

Decisions locked in brainstorming:
- **Dashboard model:** reuse tickets/kanban (least new UI).
- **Transport:** polling (agent dashboard re-fetches; customer gets Telegram pushes).
- **Ending:** agent "End chat" button + auto-end on inactivity (reuse `/reap`).
- **Data model:** dedicated `live_messages` table (Approach A) — structured,
  race-free, clean "since" polling, survives Cloud Run scale-to-zero.
- **Joining agent identity:** the logged-in dashboard user (existing assignee
  lock pattern).
- **Incoming images during handoff:** recorded as a `[lampiran]` transcript
  marker; viewable via the existing `/api/attachments/{file_id}` proxy. Text is
  the core relay.

Constraint: must work under Cloud Run scale-to-zero + `--max-instances 1`. The
authoritative handoff state therefore lives in the **DB**, not in-memory
`SESSION_STATE` (which is lost on instance recycle).

Non-goals (YAGNI): typing indicators, read receipts, agent-initiated handoff,
file uploads from the agent side, multi-agent chat, Supabase Realtime.

## Architecture overview

```
Customer (Telegram)                 Backend (FastAPI / Cloud Run)              CC agent (dashboard)
  picks "Chat dengan CC" ──webhook──▶ process_message -> {type: handoff_request}
                                       send path creates Ticket(handoff_state=requested),
                                       records system live_message, sends "wait a moment"
                          ◀──"wait a moment"──
                                                                    kanban "Live Chat" lane shows it
                                                                    agent clicks Join ──POST /handoff/join──▶
                          ◀──"connected with CC"── (handoff_state=active, assignee=agent)
  types message ──webhook──▶ DB handoff active? -> record live_message(customer), send nothing
                                                                    dashboard polls GET /handoff/messages?after_id=
                                                                    agent types ──POST /handoff/message──▶
                          ◀──relayed text── (record live_message(agent), send_telegram_message)
                                                                    agent clicks End ──POST /handoff/end──▶
                          ◀──"chat ended"── (handoff_state=ended, status Resolved, SESSION_STATE reset)
```

## Data model (`database.py`)

### New table `live_messages`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | autoincrement; the polling cursor (`after_id`) |
| `ticket_id` | Integer, FK→tickets.id, indexed | owning handoff ticket |
| `chat_id` | String, indexed | Telegram chat id (str) |
| `sender` | String | `customer` \| `agent` \| `system` |
| `text` | Text | message body (or `[lampiran: <kind>]` marker) |
| `author` | String, nullable | agent name for `agent`/`system` rows |
| `created_at` | DateTime, indexed | `datetime.utcnow`, used for ordering |

Created automatically by `Base.metadata.create_all` (new table).

### New `Ticket` columns

Added to the model AND to the `_ensure_schema` `new_columns` dict (idempotent
`ALTER TABLE ADD COLUMN`, portable SQLite/Postgres):

| Column | Type | Notes |
|---|---|---|
| `handoff_state` | VARCHAR | `requested` \| `active` \| `ended` \| NULL (not a handoff) |
| `handoff_agent` | VARCHAR | agent who joined |
| `handoff_last_activity` | DateTime (migration type `TIMESTAMP`) | bumped on every relayed message + join/request; drives auto-end |

The model column is a SQLAlchemy `DateTime`; the `_ensure_schema` `new_columns`
entry adds it as `TIMESTAMP` (Postgres native; SQLite stores it and SQLAlchemy
serializes the Python datetime). The reaper compares it against a Python
`datetime` via SQLAlchemy, which works on both backends.

A chat is "in handoff" iff a Ticket with that `chat_id` has `handoff_state` in
(`requested`, `active`). `_active_handoff_for(db, chat_id)` returns that Ticket
or None (most recent if several).

## Menu entry & trigger (`agent.py`)

The live `/start` flow is `_ask_describe()` → `IDLE`; when the customer
describes an issue, `_present_matching_predefined()` shows the matching
predefined issues as button options (state `CHOOSING_PREDEFINED`). (The
`_present_category_menu()`/`MENU_CATEGORY` path is dormant — not wired into
`/start` — so the handoff option goes on the active predefined-options path.)

- Module constant `HANDOFF_OPTION = "💬 Chat dengan Customer Care"`.
- Append `HANDOFF_OPTION` to the option lists returned by the predefined-issue
  presenters: `_present_matching_predefined`, `_present_predefined_menu`, and
  `_present_category_issues` (alongside the existing `ESCAPE_OPTION`).
- In the `CHOOSING_PREDEFINED` dispatch, BEFORE the normal exact-match, if the
  picked option equals `HANDOFF_OPTION` return:
  ```python
  {"type": "handoff_request",
   "text": "Baik, mohon tunggu sebentar ya 🙏 Tim Customer Care kami akan segera bergabung dengan Anda."}
  ```
  and set `session["state"] = "HUMAN_HANDOFF"`.
- Also make it reachable when no predefined issue matches: the IDLE
  "nothing matched, please rephrase" branch becomes a `question` whose only
  option is `HANDOFF_OPTION` (so a stuck customer can always reach a human),
  with the same `CHOOSING_PREDEFINED`-style handling.
- Defensive `process_message` branch: when `state == "HUMAN_HANDOFF"`, the agent
  does NOT run normal logic — it returns a short "tim CC akan segera membantu"
  message and records the turn. (In practice the webhook intercepts handoff
  before `process_message` using the DB; this branch only matters if in-memory
  state ever leads here, and must never auto-answer the substantive query.)

## Webhook relay (`main.py`)

In `telegram_webhook`, after extracting `chat_id`/`text` and calling
`_touch_session_liveness`, BEFORE the attachment/text branches:

```python
handoff = _active_handoff_for(db, str(chat_id))   # opens its own session, like _touch_session_liveness
if handoff is not None:
    if text:
        body = text
    elif attachment:
        body = f"[lampiran: {attachment['kind']}]"
    else:
        body = ""
    _record_live_message(handoff, sender="customer", text=body)
    _bump_handoff_activity(handoff)
    return {"status": "ok"}          # bot stays silent; agent will see it via polling
```

Notes:
- Applies to both `requested` and `active` states (messages sent before an agent
  joins are queued so the agent sees them on join).
- A customer image during handoff records a `[lampiran: <kind>]` marker (the file
  is still fetchable via the existing attachment path if needed later).
- When NOT in handoff: unchanged flow. `send_telegram_message` already
  special-cases `type == "ticket_form"`; add a `type == "handoff_request"` case
  that:
  1. creates a Ticket: `issue_category="Customer Care (Live Chat)"`,
     `issue_detail="Customer meminta live chat dengan Customer Care."`,
     `chat_id=str(chat_id)`, `status` mapped to the Live Chat lane,
     `handoff_state="requested"`, `handoff_last_activity=utcnow`, name/phone from
     `user_info` when available;
  2. records a `system` live_message ("Customer meminta live chat.");
  3. sends the "wait a moment" text to Telegram.

`_active_handoff_for`, `_record_live_message`, `_bump_handoff_activity` are small
helpers that open their own `SessionLocal` (mirroring `_touch_session_liveness`),
so the webhook handler stays simple and best-effort.

## Handoff endpoints (`main.py`)

Same security posture as existing `/api/tickets/*` (no server-side auth; the
dashboard is behind Supabase auth; CORS configured). All take `ticket_id` path
param and use `Depends(get_db)`.

1. **`POST /api/tickets/{id}/handoff/join`** body `{"agent": "CC - Ayu Rahayu"}`
   - 404 if ticket missing; 409 if `handoff_state != "requested"` (already joined/ended).
   - Set `handoff_state="active"`, `handoff_agent=agent`, `assignee=agent`,
     status → Live Chat, bump activity. Record `system` "Agent <agent> joined".
   - `send_telegram_message(chat_id, {"type":"message","text":"Anda sekarang terhubung dengan Customer Care kami. Silakan sampaikan kebutuhan Anda 🙏"})`.
   - Return the updated `RawTicket`.

2. **`POST /api/tickets/{id}/handoff/message`** body `{"text": "...", "author": "CC - Ayu Rahayu"}`
   - 409 if `handoff_state != "active"`; 400 if empty text.
   - Record `agent` live_message; bump activity; `send_telegram_message(chat_id, {"type":"message","text":text})`.
   - Return the created message `{id, sender, text, author, created_at}`.

3. **`GET /api/tickets/{id}/handoff/messages?after_id=0`**
   - Return `live_messages` for the ticket with `id > after_id`, ordered by `id`.
   - Shape: `[{id, sender, text, author, created_at}]`. Used by dashboard polling.

4. **`POST /api/tickets/{id}/handoff/end`** body `{"agent": "..."}`
   - 409 if `handoff_state` not in (`requested`,`active`).
   - Set `handoff_state="ended"`, status → Resolved. Record `system` "Live chat ended by <agent>".
   - `send_telegram_message(chat_id, {"type":"message","text":"Sesi live chat dengan Customer Care telah berakhir. Terima kasih 🙏 Ketik pesan kapan saja untuk memulai lagi dengan asisten kami."})`.
   - Reset in-memory `SESSION_STATE[chat_id] = _fresh_session()` (if present) so the
     bot resumes for the next message.
   - Return the updated ticket.

## Dashboard (frontend submodule)

- **`lib/api.ts`**: `joinHandoff(id, agent)`, `sendHandoffMessage(id, text, author)`,
  `getHandoffMessages(id, afterId)`, `endHandoff(id, agent)`.
- **`lib/tm/types.ts` + `mappers.ts`**: add `handoffState`, `handoffAgent` to
  `RawTicket`/`MonitoringTicket`; the kanban status mapper places tickets with
  `handoffState` in (`requested`,`active`) into a **"Live Chat"** column.
- **`components/tm/card-detail.tsx`**:
  - `handoffState === "requested"` → prominent **"Join chat"** button (calls
    `joinHandoff` with the logged-in user, then refetches).
  - `handoffState === "active"` → conversation pane becomes a live chat:
    `useEffect` polls `getHandoffMessages(id, lastId)` every ~3s (only while the
    card is open and state is active), appends new rows; a **composer**
    (input + Send → `sendHandoffMessage`, optimistic append) and an **"End chat"**
    button (`endHandoff` → refetch, stop polling).
  - `handoffState === "ended"` → read-only transcript, no composer.
- **Notifications**: a `requested` handoff ticket is surfaced as unread via the
  existing client-side derivation (it's a new ticket).

## Auto-end (reuse `/reap`)

Extend the existing `reap_idle_sessions` pass (runs every minute via Cloud
Scheduler) with a handoff sweep:
- Query Tickets where `handoff_state` in (`requested`,`active`) and
  `handoff_last_activity < now - HANDOFF_IDLE_TIMEOUT` (`HANDOFF_IDLE_TIMEOUT =
  15 * 60`).
- For each: `send_telegram_message` the closing text, set `handoff_state="ended"`
  + status Resolved, record a `system` "Live chat auto-ended (inactivity)",
  reset in-memory session if present.
- Return value extends to `{"prompted": n, "closed": m, "handoffs_ended": k}`.
- Per-row try/except so one failure doesn't abort the pass (matches existing
  reaper style).

## Error handling

- Telegram send failure during relay/join/end: logged; DB state still updated and
  the message still recorded (the agent sees it as sent — best effort, matches
  the reaper's posture). The customer can re-trigger if truly undelivered.
- Join race (two agents): the second `join` sees `handoff_state != "requested"`
  and gets `409` with the current ticket state.
- Endpoints validate `handoff_state` and return `409`/`400`/`404` rather than
  silently succeeding.
- All webhook handoff helpers are best-effort (open own session, try/except,
  rollback) so liveness/relay bugs never crash message handling.

## Testing

**Backend `test_handoff.py`** (TestClient + monkeypatched `send_telegram_message`,
following `test_deescalation.py`/`test_reaper_endpoint.py` patterns; clears the
relevant tables between tests):
- Picking the menu option → webhook creates a Ticket with `handoff_state=requested`,
  sends the "wait a moment" text, and a `system` live_message exists.
- While requested/active, an incoming customer Telegram message is recorded to
  `live_messages` (sender=customer) and `process_message` is NOT used to answer
  (no bot reply sent).
- `join` → `handoff_state=active`, `assignee` set, "connected" text sent, system
  message recorded; second concurrent `join` → 409.
- `message` → records agent row + sends to Telegram; 409 when not active.
- `messages?after_id=` → returns only newer rows, ordered.
- `end` → `handoff_state=ended`, status Resolved, closing text sent, in-memory
  session reset; a subsequent customer message goes back through the normal bot
  flow (handoff no longer intercepts).
- Reaper auto-end: an `active` handoff with old `handoff_last_activity` → ended +
  closing text + `handoffs_ended` counted.

**Frontend (Vitest, `lib/tm`)**: status mapper puts handoff tickets in the Live
Chat lane; message-merge appends only new `after_id` rows without duplicates.

## File structure

Backend (repo root):
- `database.py` — `LiveMessage` model; `Ticket` handoff columns + `_ensure_schema` entries.
- `agent.py` — `HANDOFF_OPTION` menu entry; `handoff_request` response; defensive `HUMAN_HANDOFF` branch.
- `main.py` — webhook handoff intercept; `handoff_request` send case; 4 handoff endpoints; `/reap` auto-end; helpers `_active_handoff_for`, `_record_live_message`, `_bump_handoff_activity`.
- `test_handoff.py` — backend tests.

Frontend (`frontend/` submodule):
- `lib/api.ts`, `lib/tm/types.ts`, `lib/tm/mappers.ts` — handoff API + fields + mapping.
- `components/tm/card-detail.tsx` (+ board column) — Join button, live composer, End, polling.
- `lib/tm/*.test.ts` — mapper/merge tests.

## Out of scope

- Typing indicators, read receipts, Supabase Realtime push.
- Agent-side file uploads; rich media beyond the `[lampiran]` marker.
- Agent-initiated handoff; transferring a live chat between agents.
- Authn on the handoff endpoints beyond the existing `/api/tickets/*` posture.
