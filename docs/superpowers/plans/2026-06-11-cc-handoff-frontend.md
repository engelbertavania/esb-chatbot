# CC Handoff — Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Dashboard UI for live chat with Customer Care — a "Live Chat" kanban lane, a "Join chat" button, and a live two-way composer (poll incoming, send outgoing, end) in the ticket card.

**Architecture:** The backend (already shipped on `feature/cc-handoff`) exposes `handoff_state`/`handoff_agent` on tickets and `POST/GET /api/tickets/{id}/handoff/{join,message,messages,end}`. The frontend adds a derived "Live Chat" lane and, in the card detail, a Join button (when `requested`) and a live chat pane that polls `GET handoff/messages` every 3s and sends via `POST handoff/message`, plus an End button.

**Tech Stack:** Next.js 16 (customized — see CRITICAL below), React, TypeScript, Vitest.

**Spec:** `docs/superpowers/specs/2026-06-11-cc-handoff-design.md`
**Backend plan (done):** `docs/superpowers/plans/2026-06-11-cc-handoff-backend.md`

## CRITICAL — read before any code
- All work is in the **`frontend/` git submodule** (its own repo). `cd frontend` for git/npm.
- `frontend/AGENTS.md` says: *"This is NOT the Next.js you know… Read the relevant guide in `node_modules/next/dist/docs/` before writing any code."* Heed it for any Next-specific API (this plan mostly touches client components + plain TS, but if you touch routing/server bits, read the docs first).
- Run tests with `npm test` (vitest run) from `frontend/`. Type-check with `npx tsc --noEmit` if available.
- Components are inline-styled via `useC()` (no CSS files); match that style. State via React `useState`/`useEffect`.

## File structure (all relative to `frontend/`)
- `lib/types.ts` — add `handoff_state`/`handoff_agent` to `RawTicket`.
- `lib/tm/types.ts` — `KanbanStatus` += "Live Chat"; `MonitoringTicket` += `handoffState`/`handoffAgent`.
- `lib/tm/mappers.ts` — derive "Live Chat" lane + map handoff fields.
- `lib/tm/mappers.test.ts` — Vitest for the lane + fields.
- `components/tm/palette.ts` — `STATUSES`/`STATUS_META` += "Live Chat".
- `lib/api.ts` — `joinHandoff`/`sendHandoffMessage`/`getHandoffMessages`/`endHandoff` + `LiveMsg` type.
- `components/tm/card-detail.tsx` — Join button + live chat pane (poll/send/end).
- `components/tm/tm-monitoring.tsx` — wire handlers + pass through.

---

## Task F1: Submodule branch + `RawTicket` handoff fields

**Files:** `frontend/lib/types.ts`.

- [ ] **Step 1: Prep the submodule branch.** From `frontend/`:
```bash
cd frontend
git status --short          # note existing WIP
git checkout -b feature/cc-handoff
```
If there is uncommitted WIP, commit it first as a checkpoint so handoff commits stay clean:
```bash
git add -A && git commit -m "checkpoint: pre-handoff frontend WIP"
```
(Only if `git status` shows changes. If clean, skip.)

- [ ] **Step 2: Add the raw fields.** In `lib/types.ts`, find the `RawTicket` interface and add (near `assignee`/`assign_to`):
```typescript
  handoff_state: string | null;   // "requested" | "active" | "ended" | null
  handoff_agent: string | null;
```

- [ ] **Step 3: Verify it type-checks.** Run `npx tsc --noEmit` (or `npm run build` if no tsc script) — no new errors. (No test yet; F2 tests the mapping.)

- [ ] **Step 4: Commit**
```bash
git add lib/types.ts
git commit -m "feat(types): handoff fields on RawTicket"
```

---

## Task F2: Mapper — "Live Chat" lane + handoff fields (TDD)

**Files:** `lib/tm/types.ts`, `lib/tm/mappers.ts`, Test: `lib/tm/mappers.test.ts`.

- [ ] **Step 1: Write the failing test.** Append to `lib/tm/mappers.test.ts` (match the existing `raw()` helper + describe/it style already in the file):
```typescript
import { toMonitoringTicket } from "./mappers";

describe("handoff → Live Chat lane", () => {
  it("maps requested/active handoff tickets into the Live Chat lane", () => {
    const t = toMonitoringTicket(raw({ handoff_state: "active", handoff_agent: "CC - Ayu", status: "In Progress" }));
    expect(t.status).toBe("Live Chat");
    expect(t.handoffState).toBe("active");
    expect(t.handoffAgent).toBe("CC - Ayu");
  });
  it("leaves ended/none handoffs on their normal status", () => {
    expect(toMonitoringTicket(raw({ handoff_state: "ended", status: "Resolved" })).status).toBe("Resolved");
    expect(toMonitoringTicket(raw({ handoff_state: null, status: "Open" })).status).toBe("New");
  });
});
```
If the file's `raw()` helper doesn't accept arbitrary fields, extend it to spread overrides (it builds a partial `RawTicket`); ensure `handoff_state`/`handoff_agent` default to `null` there.

- [ ] **Step 2: Run** `npm test` → FAIL (`status` is "In Progress"/`handoffState` missing).

- [ ] **Step 3: Extend the view-model types.** In `lib/tm/types.ts`:
  - Change `KanbanStatus` to: `export type KanbanStatus = "New" | "In Progress" | "Escalated" | "Resolved" | "Live Chat";`
  - Add to `MonitoringTicket`:
    ```typescript
      handoffState: "requested" | "active" | "ended" | null;
      handoffAgent: string | null;
    ```

- [ ] **Step 4: Map in `toMonitoringTicket`** (`lib/tm/mappers.ts`). After computing `status` via `dbToUiStatus(t.status)`, override for handoff and add the fields. Change the return object's `status` line and add fields:
```typescript
    status: (t.handoff_state === "requested" || t.handoff_state === "active")
      ? "Live Chat"
      : dbToUiStatus(t.status),
    handoffState: (t.handoff_state as MonitoringTicket["handoffState"]) ?? null,
    handoffAgent: t.handoff_agent ?? null,
```
(Import is already `import type { ... MonitoringTicket ... } from "./types";`.)

- [ ] **Step 5: Run** `npm test` → PASS. Also confirm existing mapper tests still pass.

- [ ] **Step 6: Commit**
```bash
git add lib/tm/types.ts lib/tm/mappers.ts lib/tm/mappers.test.ts
git commit -m "feat(tm): derive Live Chat lane + handoff fields"
```

---

## Task F3: Palette — register the "Live Chat" column

**Files:** `components/tm/palette.ts`.

- [ ] **Step 1:** Add "Live Chat" to `STATUSES` (place it before "Resolved" so the lane reads New → In Progress → Live Chat → Escalated → Resolved, or after "In Progress" — pick before Escalated):
```typescript
export const STATUSES = ["New", "In Progress", "Live Chat", "Escalated", "Resolved"] as const;
```

- [ ] **Step 2:** Add a `STATUS_META` entry for it (purple-ish "live" accent, reusing existing palette hexes):
```typescript
  "Live Chat": { chip: "#E9EAFE", border: "#6951E5", pillBg: "#E9EAFE", pillTx: "#6951E5" },
```

- [ ] **Step 3:** Verify the board renders 5 columns with no console/type errors: `npx tsc --noEmit`. (The board maps `STATUSES` → columns automatically.)

- [ ] **Step 4: Commit**
```bash
git add components/tm/palette.ts
git commit -m "feat(tm): add Live Chat kanban column"
```

> Note: dragging cards INTO/OUT OF "Live Chat" should be prevented (handoff state is driven by the backend, not drag). If the board's drag handler calls `onStatus` for any column, F6 must ignore status changes to/from "Live Chat" (handled there). Flag if the board allows free drag between all columns.

---

## Task F4: API client — handoff functions

**Files:** `lib/api.ts`.

- [ ] **Step 1:** Add a `LiveMsg` type and four functions (match the existing `post`/`apiFetch` style). Append near the other mutations:
```typescript
export interface LiveMsg {
  id: number;
  sender: "customer" | "agent" | "system";
  text: string;
  author: string | null;
  created_at: string | null;
}

export function joinHandoff(id: number, agent: string): Promise<RawTicket> {
  return post<RawTicket>(`/api/tickets/${id}/handoff/join`, { agent });
}

export function sendHandoffMessage(id: number, text: string, author: string): Promise<LiveMsg> {
  return post<LiveMsg>(`/api/tickets/${id}/handoff/message`, { text, author });
}

export function getHandoffMessages(id: number, afterId = 0): Promise<LiveMsg[]> {
  return apiFetch<LiveMsg[]>(`/api/tickets/${id}/handoff/messages?after_id=${afterId}`);
}

export function endHandoff(id: number, agent: string): Promise<RawTicket> {
  return post<RawTicket>(`/api/tickets/${id}/handoff/end`, { agent });
}
```

- [ ] **Step 2:** `npx tsc --noEmit` — clean.

- [ ] **Step 3: Commit**
```bash
git add lib/api.ts
git commit -m "feat(api): handoff join/message/messages/end clients"
```

---

## Task F5: Card detail — Join button + live chat pane

**Files:** `components/tm/card-detail.tsx`.

This is the largest task. The `CardDetail` component (around line 279) renders a read-only `<Conversation convo={ticket.convo} />` (line ~353). Add live-chat behavior driven by `ticket.handoffState`. READ the whole `card-detail.tsx` first and match its inline-`useC()` style, the `ResolveModal`/`NoteComposer` patterns for composer inputs, and the `CustomerMsg`/`BotMsg` bubble components.

- [ ] **Step 1:** Add new props to `CardDetail` (extend the destructure + the prop type):
  - `onJoinHandoff: () => void`
  - `onEndHandoff: () => void`
  These are wired in F6. (Keep all existing props.)

- [ ] **Step 2:** Build a `LiveChatPane` component in the same file (sibling to `Conversation`). It:
  - Props: `{ ticketDbId: number; agent: string; onEnd: () => void }`.
  - State: `msgs: LiveMsg[]`, `lastId: number`, `draft: string`, `sending: boolean`.
  - On mount + every 3s (via `useEffect` + `setInterval`, cleared on unmount), call `getHandoffMessages(ticketDbId, lastId)`; append any returned rows; set `lastId` to the max id. Guard against overlap (don't fire a new poll while one is in flight).
  - Render the message list reusing the bubble look: `sender === "agent"` → right-aligned agent bubble (use `C.brand`/`C.custBubble`-style), `customer` → left, `system` → centered muted line (e.g. "— {text} —").
  - Composer: a text input + Send button (match `ResolveModal`'s textarea/button styling). On submit (Enter or click) with non-empty `draft`: set `sending`, call `sendHandoffMessage(ticketDbId, draft, agent)`, optimistically append the returned `LiveMsg`, clear `draft`, bump `lastId`. On error, leave the draft and show nothing fancy (console.warn).
  - An "End chat" button in the pane header (calls `onEnd`).
  - Auto-scroll to the newest message on update (a `ref` + `scrollIntoView`, optional).
  - Import `getHandoffMessages`, `sendHandoffMessage`, `LiveMsg` from `@/lib/api` (or the relative path the file already uses for api imports).

- [ ] **Step 3:** Wire the pane into `CardDetail`'s render. Replace the right-side `<Conversation convo={ticket.convo} />` with conditional rendering:
  - `ticket.handoffState === "active"` → `<LiveChatPane ticketDbId={ticket.dbId} agent={ticket.assignee || currentUser} onEnd={onEndHandoff} />`.
  - else → `<Conversation convo={ticket.convo} />` (unchanged for normal tickets; for `ended`/`requested` the bot transcript still shows).

- [ ] **Step 4:** Add a **"Join chat"** button when `ticket.handoffState === "requested"`. Put a prominent button in the card header area (near the status control) or atop the conversation pane: label "Join chat", calls `onJoinHandoff`. Style it with `C.accent`/`C.green`.

- [ ] **Step 5:** Verify build + types: `npx tsc --noEmit` clean; `npm run build` succeeds (if build is quick) OR at least tsc. There's no Vitest for components here; correctness is verified by F7 manual smoke. Confirm no runtime import errors by loading the monitoring page in dev (F7).

- [ ] **Step 6: Commit**
```bash
git add components/tm/card-detail.tsx
git commit -m "feat(tm): live chat pane + Join chat in card detail"
```

---

## Task F6: Orchestrator — wire join/end + status guard

**Files:** `components/tm/tm-monitoring.tsx`.

- [ ] **Step 1:** Import `joinHandoff`, `endHandoff` from the api module (alongside the existing `addNote`/`setTicketStatus` imports).

- [ ] **Step 2:** Add two handlers near the existing `onAddNote`/`onStatus` handlers (which use `dbId` + `await refetch()`):
```typescript
  async function onJoinHandoff(dbId: number) {
    try { await joinHandoff(dbId, currentUser); } finally { await refetch(); }
  }
  async function onEndHandoff(dbId: number) {
    try { await endHandoff(dbId, currentUser); } finally { await refetch(); }
  }
```
(Use whatever id the existing handlers use — match the `selected`/`dbId` pattern. `currentUser` is already a prop of `TmMonitoring`.)

- [ ] **Step 3:** Pass them to `<CardDetail .../>`: add `onJoinHandoff={() => onJoinHandoff(selected.dbId)}` and `onEndHandoff={() => onEndHandoff(selected.dbId)}`.

- [ ] **Step 4:** Status-change guard. The card's status `Dropdown` (`onStatus`) must not let an agent manually move a handoff ticket. In the existing `onStatus` handler (or where status changes are applied), if `selected.handoffState === "active" || selected.handoffState === "requested"`, ignore/no-op manual status changes (the lane is backend-driven). Also, if the kanban board allows drag between columns, make the drop handler skip when source or target is "Live Chat". Keep this minimal — a guard clause.

- [ ] **Step 5:** Verify: `npx tsc --noEmit` clean.

- [ ] **Step 6: Commit**
```bash
git add components/tm/tm-monitoring.tsx
git commit -m "feat(tm): wire join/end handoff + lane guard"
```

---

## Task F7: Build, lint, and manual smoke

**Files:** none (verification).

- [ ] **Step 1:** `npm test` (Vitest) → all pass. `npx tsc --noEmit` → clean. `npm run build` → succeeds (catches Next 16 build-time issues).

- [ ] **Step 2: Manual smoke (with the backend running locally).** Start the backend (`uvicorn main:app` or the project's run script) and the frontend dev server. Seed a handoff: either via Telegram (pick "Chat dengan Customer Care") or by inserting a `requested` handoff ticket. Then in the dashboard:
  - The ticket appears in the **Live Chat** lane.
  - Open it → **Join chat** button shows → click → backend `join` fires; the pane switches to the live composer; (Telegram would receive the "connected" message).
  - Type a message + Send → it appears in the pane and `POST handoff/message` fires.
  - Insert a customer `live_message` via the backend (or send from Telegram) → within ~3s it appears in the pane (polling).
  - Click **End chat** → ticket leaves the Live Chat lane (→ Resolved); pane stops polling.

- [ ] **Step 3:** Record results. If all pass, the frontend is done. Commit any small fixes found during smoke with clear messages.

---

## Self-review

- **Spec coverage:** Live Chat lane (F2/F3); Join button + "connected" (F5 + backend); two-way relay — send via composer (F5), poll incoming every 3s (F5); End → Resolved + bot resumes (F5/F6 + backend); handoff fields surfaced (F1/F2). Status-lane keyed off `handoffState` per spec — F2/F6 resolve the Minor "status vs handoff_state" concern from the backend review.
- **Type consistency:** `handoff_state`/`handoff_agent` (raw, snake_case) → `handoffState`/`handoffAgent` (view-model, camelCase); `KanbanStatus` includes "Live Chat" everywhere (types, palette STATUSES, STATUS_META, FILTER_GROUPS if status filter must list it — add "Live Chat" to `FILTER_GROUPS.Status` too if the Status filter should show it). `LiveMsg` shape matches backend `_live_message_to_dict` (`id, sender, text, author, created_at`).
- **Placeholder scan:** data-layer tasks (F1-F4) have complete code; UI tasks (F5/F6) are integration specs against an existing inline-styled component — the implementer reads the file and matches patterns (ResolveModal/Conversation/bubbles). Not placeholders — explicit integration instructions.
- **Submodule:** all commits land in the `frontend/` repo on its own `feature/cc-handoff` branch; the parent repo's submodule pointer is bumped separately at finish.
- **Open item for F2:** also add `"Live Chat"` to `FILTER_GROUPS.Status` in palette.ts if the Status filter should include the lane (add as a sub-step of F3).
