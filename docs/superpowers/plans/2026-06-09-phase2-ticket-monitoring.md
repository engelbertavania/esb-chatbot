# Phase 2 — Sukabantu Ticket Monitoring Implementation Plan

> Implements `docs/superpowers/specs/2026-06-08-phase2-ticket-monitoring-design.md`.
> Builds on Phase 0 (shell/auth/theme) and reuses Phase 1's data-layer patterns.

**Goal:** Port `app-tm` to native Next.js at `/monitoring` — a drag-drop kanban
(New | In Progress | Escalated | Resolved), ticket detail overlay with persisted
resolution notes + conversation, filters, notifications, agent-workload drawer —
wired to live data with new backend schema + endpoints.

**Dropped (per spec):** Tweaks/edit-mode system, localStorage persistence,
note-image **upload** (accept refs only).

## Backend first (`database.py`, `main.py`, pytest)

1. **Schema** (idempotent, mirrors `_ensure_schema`):
   - `tickets` += `priority`, `assignee`, `assign_to` (all VARCHAR, nullable).
   - New table `ticket_notes` (`id`, `ticket_id` FK, `type`, `text`, `author`,
     `created_at`, `images` TEXT/JSON). `Ticket.notes` relationship.
2. **Endpoints** (pytest each + migration idempotency):
   - Extend `_ticket_to_dict` → `priority`, `assignee`, `assign_to`, `notes[]`.
   - `POST /api/tickets/{id}/assign` `{assignee}`.
   - `POST /api/tickets/{id}/escalate` `{assign_to}`.
   - `POST /api/tickets/{id}/notes` `{type,text,author,images?}`.
   - `PUT /api/tickets/{id}/notes/{noteId}` edit.
   - `GET /api/agents/workload` — active/resolved counts per assignee.
   - Extend `_UI_TO_DB_STATUS` with `"Escalated"` for the kanban drop target.

## Frontend (`frontend/`)

3. **Types + API** — `RawTicket` += `priority/assignee/assign_to/notes`;
   `RawNote`; `lib/api.ts` += `assignTicket/escalateTicket/setStatus/addNote/
   editNote/getAgentWorkload`.
4. **Mapper (TDD)** — `lib/tm/mappers.ts` `toMonitoringTicket(row)` +
   `uiToDbStatus`/`dbToUiStatus` (status round-trip) + `applyTmFilters`. Tests.
5. **Components (`components/tm/`)** — faithful ports using `useTheme`:
   primitives (PriorityBadge, StatusPill, BotAvatar, glyphs), StatCard,
   TicketCard, Column, Board (DnD), CardDetail, MetaField, Dropdown,
   ResolutionNotes/NoteCard/NoteComposer (no upload), Conversation, FilterDrawer,
   NotificationsPanel, AgentWorkloadDrawer.
6. **Orchestrator + page** — `tm-monitoring.tsx` (client): fetch, map, kanban
   state, optimistic mutations w/ rollback, overlays; `monitoring/page.tsx`,
   `error.tsx`. Notifications derived from recent unassigned tickets.

## Verify
`vitest run`, `tsc --noEmit`, `npm run build`, backend pytest. Submodule bump,
finish branch.

## Out of scope
Note-image upload, Tweaks system, real-time push (refetch on focus is enough).
