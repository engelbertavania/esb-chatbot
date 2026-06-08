# Phase 1 — CS Chatbot Dashboard (app-cs) Design

**Date:** 2026-06-08
**Status:** Approved (design)
**Depends on:** Phase 0 (Foundation)

## Goal

Port the CS Chatbot Dashboard (the `app-cs` iframe) to native Next.js
components under `/dashboard`, wired to live ticket data from FastAPI. This is
the data-richest, highest-value app: Overview analytics + Ticket Monitoring
kanban + ticket drill-down.

## Source

- `_src_app-cs.jsx` (~97 KB JSX, ~25 components) and `_tpl_app-cs.html`.
- Components (ported as native `.tsx`): `App`, `CSSidebar` (superseded by the
  Phase 0 shell nav), `CSTopBar`, `CSFilterDrawer`, `SidePanel`, `OverviewView`,
  `TicketMonitoringView`, `TrendDrawer`, `BotHealthCard`, `KeyMetricCard`,
  `SectionHead`, `KanbanCard`, and the hand-rolled SVG charts
  `DailyTrafficChart`, `TopicDonut` (+ generic `LineChart`, `Donut`, `HBars`,
  `Heatmap`, `Histogram` as needed), plus chips `IntentChip`/`StatusChip`/
  `UrgencyChip`/`Trend`.

## View structure

Two views switched in-page (the original `active` state: `overview` | `ticket`).
With Phase 0's shell, **Dashboard** = Overview view, **Ticket Monitoring** =
the kanban view. Each maps to its own route (`/dashboard`, and the kanban can
live at `/dashboard` toggled or share with `/monitoring` — see note).

> Note: the original app-cs has its *own* internal "Ticket Monitoring" kanban,
> separate from the standalone app-tm (Phase 2). To avoid duplication, **app-cs's
> internal kanban is dropped** in favor of the dedicated Phase 2 monitoring app.
> `/dashboard` renders only the Overview view. (If the user wants the app-cs
> kanban kept too, it can be added — flagged as a decision in implementation.)

### Overview view sections (ported)

1. **Bot Health** — 4 `BotHealthCard`s: Bot-Resolved, Fallbacks, Avg.
   Confidence, Bot Solving Time.
2. **Daily Traffic + Topic Distribution** — `DailyTrafficChart` (30-day SVG
   line + area + linear-regression trend) and `TopicDonut` (by topic).
3. **Key Metrics** — 4 `KeyMetricCard`s: Total Escalated, Escalation Rate %,
   Avg. Resolution Time, CSAT.
4. **Top 10 Sub-Topics** — table (rank, sub-topic, category, count, WoW %).
5. **Rising Topics** — clickable list → `TrendDrawer` (sparkline, probability,
   summary, pattern).
6. **CSTopBar** — title, notifications dropdown, `CSFilterDrawer`, date-range
   dropdown, export.

## Data: live wiring + the rich/lean gap

Source of truth: `GET /api/tickets` (DB `Ticket` rows) + CSAT from
`csat_ratings`.

### Mapping module (`lib/mappers.ts`)

A single function `toCsTicket(row: Ticket): CsTicket` produces the rich
view-model the UI expects. All derivation/defaulting lives here so it is obvious
what is real vs synthesized.

| CS view-model field | Source | Strategy |
|---------------------|--------|----------|
| `id` | `ticket_number` or `CS-${id}` | real |
| `created` | `created_at` | real |
| `user` | `name` / `company_name` | real |
| `topic` | `issue_category` | real |
| `subTopic` | `sub_topic` | real (may be null → "Uncategorized") |
| `status` | `status` | real (normalize Open→New for display) |
| `phrasing` | `issue_detail` | real |
| `confidence` | `confidence_score` | real (0–100 → 0–1) |
| `channel` | — | **default** "Telegram" (all current traffic) |
| `intent` | — | **derived** from issue_category heuristic, else "Question" |
| `urgency` | — | **derived** from category/keywords, else "Medium" |
| `sentiment` | — | **default** "Neutral" (not tracked) |
| `resolvedByBot` | `status`/`routed_queue` | **derived** (resolved & not routed) |
| `escalated` | `routed_queue` present | **derived** |
| `agent` | `routed_queue` | **derived** (queue label) or null |
| `resolutionMin` | — | **null → "—"** (no resolved timestamp tracked) |
| `slaTargetMin`/`slaBreachRisk` | — | **derived** from urgency + age, best-effort |
| `csat` | `csat_ratings` join by chat_id | real where present, else null |
| `messageCount` | parse `chat_history` | real if parseable, else derived |

**Honesty rule:** fields with no real source render a clear placeholder ("—") or
a documented heuristic — never a fabricated precise number presented as truth.
KPIs that depend on untracked data (e.g. Bot Solving Time) display "—" or a
labeled proxy.

### New backend endpoint

- `GET /api/csat` (and/or `GET /api/csat/summary`) returning CSAT rows /
  aggregate (avg rating, count) for the dashboard's CSAT metric. Backed by the
  existing `csat_ratings` table. Add to `main.py` with pytest coverage.

### Fallback

If `/api/tickets` returns empty (fresh DB), render the ported seed generator
(`seededRandom` + `TICKETS`) **only in dev** as a visual fallback, clearly
labeled "demo data". Never in production builds.

## Charts

All charts are hand-rolled SVG (no chart library) — port faithfully as React
components rendering `<svg>`. `DailyTrafficChart` includes the linear-regression
trend line; `TopicDonut` includes hover fade. Bucketing/aggregation
(daily volume, topic counts, top sub-topics, WoW) computed client-side from the
fetched + mapped tickets via helper functions (`applyFilters`, daily-bucket,
topic-group).

## Filters

`CSFilterDrawer` + date-range: applied client-side over the fetched ticket set
(port `applyFilters`). Filter dimensions: date range, user types, topics,
statuses, channels. Channels/userTypes are mostly single-valued given current
data — keep the controls, they just won't segment much yet.

## Interactions

- Date-range dropdown, notifications dropdown (click-outside close via effect),
  export button (CSV of current filtered set — real, replacing the demo
  `alert`).
- `SidePanel` drill-down: list of filtered tickets + detail + conversation
  transcript (`buildTranscript`, overridden to use real `chat_history` when
  present, else the generated fallback — same pattern as the existing patcher).
- `TrendDrawer`: Rising Topics remain partly demo (RISING_TOPICS) since the data
  isn't tracked; label as illustrative or compute simple WoW from real tickets
  where feasible.

## Error handling

- Fetch errors → error state in the view (reuse Phase 0 `error.tsx` pattern),
  with retry.
- Empty data → empty state (or dev fallback as above).
- Malformed `chat_history` → transcript falls back gracefully.

## Testing

- `lib/mappers.ts` unit tests: representative DB rows → expected view-model,
  including null/missing fields hitting the documented fallbacks.
- Aggregation helpers (daily bucket, topic group, WoW, applyFilters) unit
  tested.
- New `/api/csat` endpoint: pytest.
- Type-check + build pass. Manual: log in → `/dashboard` shows live KPIs/charts
  from real tickets.

## Out of scope

- The app-tm standalone monitoring app (Phase 2).
- Persisting filter/dark-mode state across sessions.
- Backfilling untracked analytics fields into the DB (future work; this phase
  derives/labels instead).
