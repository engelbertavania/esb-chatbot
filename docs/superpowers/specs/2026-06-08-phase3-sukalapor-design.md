# Phase 3 — Sukalapor Chatbot (app-sl) Design

**Date:** 2026-06-08
**Status:** Approved (design)
**Depends on:** Phase 0 (Foundation)

## Goal

Port the Sukalapor Chatbot (the `app-sl` iframe) to native Next.js under
`/sukalapor`: a web chat interface wired to the **real** conversational agent
(the same `agent.process_message` that drives the Telegram bot), replacing the
scripted demo flow. Ticket submission creates a real ticket.

## Source

- `_src_app-sl.jsx` (~40 KB JSX) and `_tpl_app-sl.html`.
- Ported components: `App` (the chat state machine), `Header`, `UserBubble`,
  `BotBubble`, `Typing`, `InputBar`, `DateChip`, `TimeStamp`, `TicketSheet`
  (+ `FieldLabel`, `FormField`, `FormSelect`, `FormArea`), `TopicSheet`,
  `Radio`, and the inline SVG icons.
- **Dropped (YAGNI):** the iPhone device-frame mock (`Stage`) — render as a
  normal responsive chat panel within the suite shell. **Replaced:** the
  hardcoded scripted routing (`TOPIC_ROUTE`, `HOWTO_*`, `route`/`botSequence`)
  with live agent responses (keep the topic/quick-reply UI affordances, but
  drive replies from the backend).

## View structure

Single chat screen inside the suite shell:
- **Header** — "Sukalapor" title + verified badge + menu ("Mulai ulang chat" →
  reset).
- **Message list** — scrollable; `UserBubble` / `BotBubble` (with optional
  quick-reply buttons) / `DateChip` / `Typing` indicator; auto-scroll to bottom.
- **InputBar** — text input + send; emoji/attach/camera icons (visual; upload
  deferred).
- **Modals** — `TicketSheet` (Data Laporan form), `TopicSheet` (topic radio
  list).

## Backend additions (do first)

The bot's conversational logic already exists server-side in
`agent.process_message` (used by the Telegram webhook) with `SESSION_STATE`
keyed by chat id. Expose it to the web client:

- `POST /api/chat` — `{ session_id, message }` → drives `process_message` with a
  **web** session id (namespaced, e.g. `web:<uuid>` so it never collides with
  Telegram chat ids) and returns the agent's response(s) in a shape the bubbles
  can render: `{ messages: [{ type, text, options? }], ... }`.
- `POST /api/chat/reset` — `{ session_id }` → clears that session
  (`SESSION_STATE`) for "Mulai ulang chat".
- Reuse the existing attachment-validation limits (`MAX_ATTACHMENTS_PER_SESSION`)
  if/when web uploads are added (deferred this phase).
- Ticket submission from `TicketSheet` → existing `POST /api/tickets` (map form
  fields `pic→name`, `phone→phone_number`, `company→company_name`,
  `branch→branch_name`, `category→issue_category`, `detail→issue_detail`).

pytest coverage for `/api/chat` and `/api/chat/reset` (mock or exercise
`process_message` with a web session).

## Data flow

1. Client generates a `session_id` (web-namespaced) on first load, persisted in
   memory (and optionally `sessionStorage`).
2. User sends a message or taps a quick-reply → `POST /api/chat` → render the
   returned bot messages (with `Typing` shown during the request).
3. When the agent flow reaches "file a report", the client opens `TicketSheet`;
   submit → `POST /api/tickets`; show confirmation in chat.
4. "Mulai ulang chat" → `POST /api/chat/reset` → clear local messages + restart.

## Mapping the agent response to bubbles

Define a small adapter (`lib/chat.ts`) converting `process_message`'s return
shape into the bubble model `{ id, who, text, time, options?, locked?,
selected? }`. Quick-reply `options` come from the agent when it offers choices;
otherwise free-text input drives the flow.

## Interactions

- Free-text send (Enter / send button), quick-reply option tap (locks the
  previous option group, ported `lockGroup`), topic selection via `TopicSheet`,
  ticket form submit, chat reset.
- Auto-scroll on new message; disable input while awaiting a response.

## Error handling

- `/api/chat` failure → show an inline "couldn't reach assistant, retry" bubble;
  re-enable input.
- Ticket submit failure → keep the sheet open with an error message.
- Reset failure → clear locally anyway and warn.

## Testing

- Backend: pytest for `/api/chat` (returns agent reply for a web session) and
  `/api/chat/reset`; verify web session ids don't collide with Telegram ids.
- `lib/chat.ts` adapter unit tests (agent shape → bubble model).
- Type-check + build. Manual: hold a real conversation, file a ticket from the
  sheet, verify it appears in the DB / monitoring board, reset the chat.

## Out of scope

- Web file/image uploads in chat (icons present but inert this phase).
- Voice input (mic icon is visual only).
- Multi-session history persistence beyond the current browser session.
- Authenticating the chat as a specific merchant (uses the logged-in suite user
  context only; per-merchant identity is future work).
