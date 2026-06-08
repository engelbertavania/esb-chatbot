# Phase 2 — Sukabantu Ticket Monitoring (app-tm) Design

**Date:** 2026-06-08
**Status:** Approved (design)
**Depends on:** Phase 0 (Foundation)

## Goal

Port the Sukabantu Ticket Monitoring app (the `app-tm` iframe) to native Next.js
under `/monitoring`: a kanban board with drag-drop, a full ticket detail panel
with persisted resolution notes, conversation view, filters, notifications, and
agent-workload drawer — wired to live data. Requires backend schema + endpoint
additions.

## Source

- `_src_app-tm.jsx` (~70 KB JSX, ~40 components) and `_tpl_app-tm.html`.
- Ported components: `Sidebar` (superseded by Phase 0 shell), `Board`, `Topbar`,
  `Column`, `TicketCard`, `StatCard`, `CardDetail`, `MetaField`, `Dropdown`,
  `ResolutionNotes`, `NoteCard`, `NoteComposer`, `Conversation`, `CustomerMsg`,
  `BotMsg`, `NotificationsPanel`, `FilterDrawer`, `AgentWorkloadDrawer`, `Radio`,
  and primitives `PriorityBadge`, `StatusPill`, `BotAvatar`, `PersonGlyph`,
  `ListGlyph`, `Icon`, `BellIcon`, `ImgPlaceholder`.
- **Dropped (YAGNI):** the entire Tweaks/edit-mode system (`TweaksPanel`,
  `useTweaks`, all `Tweak*` controls, the `applyAccent` global-mutation path,
  the host postMessage protocol) — it's a runtime design tool, not a product
  feature. **Dropped:** localStorage persistence (`loadTickets`/
  `sukabantu_tickets_v1`) in favor of the real API.

## View structure

- **Live Operations** — 3 `StatCard`s: In Queue, High Urgency, Active Agents
  (Active Agents drills to `AgentWorkloadDrawer`).
- **Status Board** — 4-column kanban: **New | In Progress | Escalated |
  Resolved**, drag-drop between columns (native HTML5 DnD, ported `onDragStart`/
  `onDrop`).
- **CardDetail** overlay — ID + priority + outlet + status dropdown header;
  metadata pane (channel, user type, date, tag, description, proof image,
  assignee dropdown, assign-to dropdown, resolution notes) + conversation pane.
- Overlays: `FilterDrawer`, `NotificationsPanel`, `AgentWorkloadDrawer`.

## Backend additions (do first)

### Schema (`database.py`, idempotent `_ensure_schema` style)

Add to `tickets`:
- `priority` VARCHAR — "High" | "Medium" | "Low" | "Compliment" (nullable).
- `assignee` VARCHAR — current handler, e.g. "CC - Ayu Rahayu" (nullable).
- `assign_to` VARCHAR — escalation target (nullable).

New table `ticket_notes`:
- `id` INTEGER PK
- `ticket_id` INTEGER FK → tickets.id (indexed)
- `type` VARCHAR — "IN PROGRESS" | "ESCALATED TO ANOTHER TEAM" | "FIXED"
- `text` TEXT
- `author` VARCHAR
- `created_at` DATETIME
- `images` TEXT — JSON array of attachment refs (reuse `/api/attachments`
  proxy for Telegram-sourced images; uploaded images out of scope this phase →
  store URLs/refs only)

Migration mirrors the existing portable `ALTER TABLE ADD COLUMN` approach and
`create_all` for the new table.

### Endpoints (`main.py`, pytest-covered)

- `GET /api/tickets` — extend `_ticket_to_dict` to include the new fields +
  nested notes (or a separate `GET /api/tickets/{id}/notes`).
- `POST /api/tickets/{id}/assign` — `{ assignee }`.
- `POST /api/tickets/{id}/escalate` — `{ assign_to }`.
- `POST /api/tickets/{id}/notes` — `{ type, text, author, images? }`.
- `PUT /api/tickets/{id}/notes/{noteId}` — edit a note.
- `GET /api/agents/workload` — aggregate active/resolved counts per
  `assignee`/`routed_queue` for the `AgentWorkloadDrawer`.
- Status move: reuse existing `POST /api/tickets/{id}/status`. Map the kanban's
  "Escalated" column to the DB status used by `_UI_TO_DB_STATUS` (extend the map
  to include "Escalated").

## Data mapping (`lib/mappers.ts`, monitoring view-model)

`toMonitoringTicket(row)`:

| Field | Source | Strategy |
|-------|--------|----------|
| `id` | `ticket_number`/`id` | real |
| `outlet` | `company_name` + `branch_name` | real |
| `topic` | `issue_category` | real |
| `tag` | `sub_topic` | real (or slug of category) |
| `description` | `issue_detail` | real |
| `status` | `status` | real (normalize to kanban statuses) |
| `dateCreate`/`age` | `created_at` | real (format + relative) |
| `channel` | — | default "Telegram" |
| `userType` | — | default "Merchant" |
| `priority` | `priority` | real (nullable → "Medium") |
| `assignee`/`assignTo` | `assignee`/`assign_to` | real |
| `convo` | parse `chat_history` | real if parseable, else [] |
| `notes` | `ticket_notes` | real |

## Interactions

- **Drag-drop:** moving a card calls `POST /api/tickets/{id}/status`; optimistic
  UI update with rollback on error.
- **Assign / escalate:** dropdowns call the assign/escalate endpoints; optimistic
  update.
- **Notes:** `NoteComposer` (type selector + textarea; image upload deferred —
  accept refs only this phase) → `POST notes`; `editNote` → `PUT`. Timeline
  newest-first, expand/collapse.
- **Filters:** `FilterDrawer` multi-select (User, Topics, Status, Channels),
  applied on "Apply"; applied client-side over fetched set.
- **Notifications / agent workload:** fetched from real data where available
  (notifications can be derived from recent unassigned tickets; workload from
  `/api/agents/workload`).

## Error handling

- Optimistic mutations roll back and surface a toast/inline error on failure.
- Empty board → empty columns with a clear "no tickets" state.
- Unparseable `chat_history` → conversation pane shows "No conversation".

## Testing

- Backend: pytest for each new endpoint + migration idempotency (run
  `_ensure_schema` twice).
- `toMonitoringTicket` unit tests incl. null fields.
- Drag-drop status mapping (UI status ↔ DB status) unit tested.
- Type-check + build. Manual: drag a card, assign an agent, add a note, reload →
  state persisted.

## Out of scope

- Image **upload** for notes (accept refs/URLs only; full upload pipeline later).
- The Tweaks/edit-mode system (dropped).
- Real-time push updates (poll/refetch on focus is enough for the demo).
