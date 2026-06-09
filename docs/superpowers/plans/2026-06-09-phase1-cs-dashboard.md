# Phase 1 — CS Chatbot Dashboard Implementation Plan

> Implements `docs/superpowers/specs/2026-06-08-phase1-cs-dashboard-design.md`.
> Builds on the Phase 0 foundation (auth, shell, design tokens, theme).

**Goal:** Port the `app-cs` **Overview** view to native Next.js under `/dashboard`,
wired to live ticket data from FastAPI, with honest field derivation, hand-rolled
SVG charts, client-side filters, a ticket drill-down side panel, and a new
`/api/csat` backend endpoint.

**Scope decision (per spec):** app-cs's *internal* Ticket Monitoring kanban is
**dropped** — the dedicated Phase 2 app owns `/monitoring`. `/dashboard` renders
only the Overview view.

**Tech:** Next.js 16, React 19, TS, Tailwind v4, Vitest. FastAPI for data.

---

## Architecture

- **Data flow:** `dashboard/page.tsx` (server, auth already enforced by suite
  layout) → renders `<CsDashboard/>` (client). The client fetches
  `GET /api/tickets` + `GET /api/csat`, maps each raw row through
  `toCsTicket()`, then renders `CSTopBar` + `OverviewView` (+ `SidePanel` /
  `TrendDrawer` on drill).
- **Honesty rule:** every synthesized/derived field is centralised in
  `lib/cs/mappers.ts` and documented; untracked KPIs render "—" or a labelled
  proxy. No fabricated precise numbers presented as truth.
- **Pure logic vs UI:** all bucketing/derivation is pure + unit-tested
  (`lib/cs/*`); components are faithful ports of the JSX (`components/cs/*`)
  using the Phase 0 `useTheme()`.
- **Dev fallback:** when `/api/tickets` is empty AND not production, render the
  ported `seededRandom`/`TICKETS` generator, clearly labelled "demo data".

## File structure

```
main.py                                   # MOD: + /api/csat, /api/csat/summary
test_csat_endpoint.py                     # NEW pytest
frontend/
  lib/cs/
    types.ts            # CsTicket, Filters, RisingTopic, SubTopicRow
    mappers.ts          # toCsTicket(raw, csatByChat) + derivation helpers
    mappers.test.ts
    aggregations.ts     # applyFilters, dailyBuckets, topicDistribution,
                        #   botHealth, keyMetrics, topSubTopics, ticketsToCsv
    aggregations.test.ts
    transcript.ts       # buildTranscript, relativeTime (now-injectable)
    transcript.test.ts
    demo-data.ts        # TOPIC_TREE/TOPICS, TOPIC_PALETTE, RISK_CFG,
                        #   RISING_TOPICS, TOP_SUBTOPICS, CHANNELS/etc.
    demo-seed.ts        # ported seededRandom + generateDemoTickets(): CsTicket[]
  lib/types.ts          # MOD: extend RawTicket (sub_topic, routed_queue, chat_id)
  lib/api.ts            # MOD: getCsat()
  components/cs/
    chips.tsx           # Chip, IntentChip, StatusChip, UrgencyChip (theme port)
    section-head.tsx    # orange 4px rule SectionHead
    cards.tsx           # BotHealthCard, KeyMetricCard
    daily-traffic-chart.tsx
    topic-donut.tsx
    cs-top-bar.tsx
    cs-filter-drawer.tsx
    side-panel.tsx
    trend-drawer.tsx
    overview-view.tsx
    cs-dashboard.tsx    # client orchestrator (fetch + state)
  app/(suite)/dashboard/
    page.tsx            # MOD: render <CsDashboard/>
    error.tsx           # NEW fetch-error boundary
```

## Tasks

1. **Backend `/api/csat`** — `GET /api/csat` (rows) + `GET /api/csat/summary`
   (`{average, count}`) over `CSATRating`. pytest with seeded SQLite. Commit.
2. **Types** — extend `RawTicket` with `sub_topic`, `routed_queue`, `chat_id`;
   add `CsTicket`, `Filters`, `RisingTopic`, `SubTopicRow`, `CsatRow`. Commit.
3. **Mappers (TDD)** — `toCsTicket(raw, csatByChat)` mirrors `_ticket_to_bundle`
   honesty rules: real fields straight through; derived intent/urgency/etc.
   documented; csat joined by `chat_id`. Tests cover real + null/missing rows.
4. **Aggregations (TDD)** — `applyFilters`, `dailyBuckets(now)`,
   `topicDistribution`, `botHealth`, `keyMetrics`, `topSubTopics`,
   `ticketsToCsv`. `now` injected for determinism. Tests.
5. **Transcript helpers (TDD)** — `buildTranscript` (real transcript when
   present else generated), `relativeTime(date, now?)`. Tests.
6. **Demo data + seed** — port static tables verbatim; port seed generator to
   emit `CsTicket[]` (dev fallback only).
7. **CS components** — faithful ports: chips, section-head, cards, charts,
   top-bar, filter-drawer, side-panel, trend-drawer, overview-view.
8. **Dashboard orchestrator + page + error/empty** — fetch, map, filter state,
   drill state, export; dev-seed fallback banner; error boundary.
9. **Verify** — `vitest run`, `tsc --noEmit`, `npm run build`, backend pytest.
   Bump submodule pointer. Finish branch.

## Out of scope
- app-tm standalone monitoring (Phase 2); Sukalapor (Phase 3).
- Persisting filter/theme across sessions; backfilling untracked analytics.
