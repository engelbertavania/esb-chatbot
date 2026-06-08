# Phase 0 — Foundation Design

**Date:** 2026-06-08
**Status:** Approved (design)
**Part of:** Sukabot Suite → Next.js native rewrite (Phases 0–3)

## Goal

Stand up the Next.js foundation that all three ported apps share: the design
system, the outer shell that replaces the iframe wrapper, Supabase
authentication with a route gate, and the typed data layer. Outcome: a user can
log in and see the empty three-tab Sukabot Suite shell with correct chrome and
working light/dark mode. No app screens yet.

## Context

- The current dashboard is a 7.4 MB static `[Prototype] Sukabot Suite.html`
  served by FastAPI. It wraps three Babel-in-browser React apps in nested
  `<iframe srcdoc>` elements (`app-cs`, `app-tm`, `app-sl`).
- A Next.js app already exists at `frontend/` (App Router, TypeScript, Tailwind
  v4) as a git submodule (`esb-chatbot-frontend`). It currently has a single
  `app/page.tsx` that lists tickets.
- `frontend/AGENTS.md` warns: **"This is NOT the Next.js you know — read
  `node_modules/next/dist/docs/` before writing code."** Treat the installed
  version's docs as the source of truth for App Router, middleware, and config
  APIs.
- The database is Supabase-hosted Postgres (project `midahxroauieyzaiiuvf`,
  region ap-southeast-1), so Supabase Auth runs on the **same project** — no new
  infra.

## Serving model

- **Next.js (port 3000):** serves all UI + auth.
- **FastAPI (port 8000):** stays the data/API + Telegram webhook backend.
- FastAPI's HTML dashboard route (`serve_dashboard` at `/`) is retired from the
  primary path; keep `/dashboard-legacy` working as a safety net. Do not delete
  the suite patcher code in this phase — just stop pointing users at it.
- The cloudflared tunnel will point at Next.js for the UI; the Telegram webhook
  continues to target FastAPI directly. (Tunnel reconfiguration is operational,
  not part of this spec's code.)

## Architecture

### Directory layout (within `frontend/`)

```
app/
  globals.css            # PUJASERA design tokens + base resets (ported)
  layout.tsx             # root layout: fonts, ThemeProvider, <html>/<body>
  login/
    page.tsx             # Supabase email/password login (client component)
    actions.ts           # server action: signInWithPassword
  (suite)/
    layout.tsx           # authenticated shell: BrandBar + SideNav, gates session
    dashboard/page.tsx   # placeholder (filled in Phase 1)
    monitoring/page.tsx  # placeholder (filled in Phase 2)
    sukalapor/page.tsx   # placeholder (filled in Phase 3)
middleware.ts            # route gate (redirect unauthenticated → /login)
components/
  ui/                    # shared primitives (see below)
  shell/                 # BrandBar, SideNav, NavItem
  theme/                 # ThemeProvider, useTheme, theme objects (light/dark)
lib/
  supabase/
    client.ts            # browser client (@supabase/ssr createBrowserClient)
    server.ts            # server client (cookies-aware)
    middleware.ts        # session refresh helper for middleware.ts
  api.ts                 # extended typed fetchers (existing file)
  types.ts               # shared view-model types
```

### Auth (Supabase via `@supabase/ssr`)

- Add deps: `@supabase/supabase-js`, `@supabase/ssr`.
- Env (in `frontend/.env.local`, gitignored): `NEXT_PUBLIC_SUPABASE_URL`,
  `NEXT_PUBLIC_SUPABASE_ANON_KEY`. The user supplies these from the Supabase
  dashboard (Settings → API). Do not commit them.
- `lib/supabase/server.ts` and `client.ts` follow the official `@supabase/ssr`
  cookie pattern for the installed Next.js version (verify against
  `node_modules` docs).
- `middleware.ts`: refresh the session and redirect to `/login` for any
  unauthenticated request outside the allowlist (`/login`, `/_next/*`, static
  assets, favicon).
- `(suite)/layout.tsx`: server component that calls `supabase.auth.getUser()`;
  if no user, `redirect('/login')` (defense in depth alongside middleware).
- Login page: email + password form → server action `signInWithPassword` →
  redirect to `/dashboard`. Show auth errors inline. Sign-out is a server action
  that calls `supabase.auth.signOut()` and redirects to `/login`.
- Users are seeded in the Supabase dashboard (Auth → Users) for the demo. No
  self-service signup in this phase.

### Design system

- Port the full PUJASERA token block from `_tpl_app-cs.html` `<style>` into
  `app/globals.css`: color palette + semantic aliases, type scale + `.t-*`
  classes, spacing/radius/elevation/motion tokens, the `.pj-rule-heading` 4px
  orange rule, base resets, and scrollbar styling.
- Fonts: Inter (brand) + Roboto. Load via `next/font` if the installed version
  supports it cleanly; otherwise self-host the TTF/woff2 referenced in the
  template. (Decide during implementation against the installed Next.js font
  API.)
- The three apps use slightly different token subsets; `globals.css` is the
  union. App-specific theme objects (the `T.light`/`T.dark` JS objects) live in
  `components/theme/` and are consumed via `useTheme()`.

### Theme / dark mode

- `ThemeProvider` (client) holds `dark` boolean state and exposes the active
  theme object (`T.light` or `T.dark`) plus a toggle, via context.
- SSR-safe: default to light on the server; the provider hydrates and applies
  `document.body` background/foreground in an effect (mirrors the original
  app's `useEffect`).
- Only the CS Dashboard uses dark mode in the original; the provider lives in the
  root so all apps can opt in.

### Shell

- `(suite)/layout.tsx` renders `BrandBar` (Sukabot Suite branding) + `SideNav`
  and an outlet for the active route.
- Nav structure mirrors the original shell groups:
  - **Sukabot Insight**: Dashboard (`/dashboard`), Ticket Monitoring
    (`/monitoring`)
  - **Sukalapor** (`/sukalapor`)
- `NavItem` highlights the active route (App Router `usePathname`).
- Replaces the original `showGroup()`/`wireNav()`/iframe-swapping logic with
  real client-side navigation.

### Shared primitives (`components/ui/`)

Ported from the app-cs primitives, generalized:

- `Card` — rounded surface, optional hover/elevation.
- `Chip` — badge with variants (default, accent, success, warn, danger, purple,
  teal).
- `SectionHead` — heading with the 4px orange rule.
- `StatusChip`, `IntentChip`, `UrgencyChip` — status/intent/urgency-aware badges
  (color maps ported from `INTENT_COLORS` etc.).
- `Icon` — SVG icon system. Resolve the icon set used across apps; render inline
  SVG (no `window.__resources` blob lookup). Start with the icons the shell
  needs; expand per phase.

### Data layer

- Extend `lib/api.ts`:
  - Keep `BACKEND_URL` (`NEXT_PUBLIC_BACKEND_URL`, default
    `http://localhost:8000`).
  - Keep `getTickets()`; add typed wrappers as needed by later phases.
  - Add a small `apiFetch` helper that throws typed errors on non-OK responses.
- `lib/types.ts`: the raw DB `Ticket` interface (already in `api.ts`) plus the
  shared view-model types that later phases extend. Phase-specific view models
  (rich CS ticket, monitoring ticket) are defined in their own phases but the
  base lives here.

## Data flow

1. Request hits `middleware.ts` → session refreshed → unauthenticated requests
   redirect to `/login`.
2. Authenticated request → `(suite)/layout.tsx` confirms the user server-side →
   renders shell + route.
3. Route placeholders render empty states for now; real data fetching arrives in
   Phases 1–3.

## Error handling

- Auth failures on login: caught in the server action, surfaced inline on the
  login form.
- Missing Supabase env vars: fail fast at startup with a clear console error
  (don't render a broken login silently).
- Route-level `error.tsx` and `not-found.tsx` in `(suite)` for graceful failures
  (filled minimally here, reused by later phases).

## Testing

- **Type-check:** `tsc --noEmit` passes.
- **Auth gate smoke:** an unauthenticated request to `/dashboard` redirects to
  `/login` (verify via a lightweight test or documented manual check; choose the
  approach the installed Next.js test setup supports).
- **Build:** `next build` succeeds.
- Manual: log in with a seeded Supabase user → land on the shell → toggle dark
  mode → navigate between the three tabs.

## Out of scope (this phase)

- Any app screens (Phases 1–3).
- Live ticket data rendering.
- Self-service signup, password reset, roles/permissions.
- Tunnel reconfiguration scripts.

## Risks / open items

- **Installed Next.js version differences** — APIs for middleware, `next/font`,
  and server actions may differ from training data. Mitigation: read
  `node_modules/next/dist/docs/` first (per AGENTS.md).
- **Supabase env provisioning** — blocked on the user pasting the URL + anon key.
  Login cannot be tested end-to-end until provided.
- **Submodule workflow** — `frontend/` is a git submodule; commits there are
  separate from the parent repo. The plan must account for committing in the
  submodule and bumping the pointer in the parent.
