# Phase 3 — Sukalapor Chatbot Implementation Plan

> Implements `docs/superpowers/specs/2026-06-08-phase3-sukalapor-design.md`.
> Builds on Phase 0; reuses the real `agent.process_message` (Telegram brain).

**Goal:** Port `app-sl` to native Next.js at `/sukalapor` — a web chat wired to
the **real** conversational agent. Ticket submission creates a real ticket.

**Dropped (per spec):** iPhone device-frame mock; hardcoded scripted routing
(replaced by live agent). Web file upload / voice are visual only.

## Backend (done first)

- `_persist_ticket_from_response(response, chat_id, db)` — extracted from the
  Telegram path so web + Telegram persist tickets identically.
- `POST /api/chat` `{session_id, message}` → `process_message("web:<id>", msg)`;
  returns `{messages:[{type,text,options?}], ticket_id?}`. Persists on
  `ticket_form`.
- `POST /api/chat/reset` `{session_id}` → clears `SESSION_STATE["web:<id>"]`.
- pytest `test_chat_endpoints.py` (stubs process_message): message/question/
  ticket-form-persists/reset + web-namespacing.

## Frontend

- `lib/chat.ts` (TDD) — `Bubble` model + `agentToBubbles(resp, idBase, time)`
  adapter (agent message/question/ticket_form → bubbles). Unit-tested.
- `lib/api.ts` += `sendChat`, `resetChat`.
- `components/sl/` — ported chat UI (Header, UserBubble, BotBubble w/
  quick-replies, Typing, InputBar, DateChip, TicketSheet + form fields,
  TopicSheet, Radio, icons) using `useTheme`.
- `components/sl/sukalapor-chat.tsx` — client orchestrator: session id, message
  list, send/await/Typing, quick-reply lock, TicketSheet→`POST /api/tickets`,
  reset, auto-scroll, inline error bubble on failure.
- `app/(suite)/sukalapor/page.tsx` + `error.tsx`.

## Verify
`vitest run`, `tsc`, `npm run build`, backend pytest. Submodule bump, finish.

## Out of scope
Web upload/voice, multi-session history, per-merchant identity.
