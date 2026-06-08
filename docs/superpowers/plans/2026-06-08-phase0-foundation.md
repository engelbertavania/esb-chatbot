# Phase 0 — Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the Next.js foundation — design system, suite shell, Supabase login + route gate, and typed data layer — so a user can log in and see the empty three-tab Sukabot Suite shell with working dark mode.

**Architecture:** Reuse the existing `frontend/` Next.js App Router app. Auth via `@supabase/ssr` (cookie sessions) with a Next.js 16 **Proxy** (`proxy.ts`, the renamed Middleware) as the route gate plus a server-side check in the authenticated layout. The PUJASERA design tokens are ported into `globals.css`; shared primitives and a `ThemeProvider` live in `components/`. FastAPI stays the data/API backend on :8000.

**Tech Stack:** Next.js 16.2.6, React 19.2.4, TypeScript 5, Tailwind v4, `@supabase/ssr` + `@supabase/supabase-js`, Vitest (pure-logic unit tests).

---

## Critical version notes (read before starting)

- This is **Next.js 16** — read `frontend/node_modules/next/dist/docs/01-app/01-getting-started/16-proxy.md` and `.../02-guides/authentication.md` if anything below is unclear. Per `frontend/AGENTS.md`, the installed docs are the source of truth.
- **Middleware → Proxy:** create `proxy.ts` (NOT `middleware.ts`) at the `frontend/` root. Export an `async function proxy(request: NextRequest)` and a `config.matcher`.
- **`cookies()` is async:** always `const cookieStore = await cookies()`.
- **Forms:** React 19 `useActionState` + Server Actions (`'use server'`).
- **Working directory:** all paths are relative to `frontend/` unless stated. Run all `npm`/`npx` commands from `frontend/`.
- **Submodule:** `frontend/` is a git submodule (`esb-chatbot-frontend`). Commits in `frontend/` are separate from the parent repo. After finishing, the parent repo's submodule pointer must be bumped (Task 15).
- **Reference source:** the decoded Suite source is at `_src_app-cs.jsx` / `_tpl_app-cs.html` in the parent repo root (gitignored). "Port verbatim from <file>" instructions refer to these.

---

## File structure (created/modified in this phase)

```
frontend/
  proxy.ts                         # NEW route gate (Next 16 Proxy)
  package.json                     # MOD deps
  vitest.config.ts                 # NEW test config
  .env.local                       # NEW (gitignored) Supabase keys
  app/
    globals.css                    # MOD: PUJASERA tokens
    layout.tsx                     # MOD: Inter font + ThemeProvider
    login/
      page.tsx                     # NEW login form (client)
      actions.ts                   # NEW signIn server action
    (suite)/
      layout.tsx                   # NEW authed shell (server check + chrome)
      dashboard/page.tsx           # NEW placeholder
      monitoring/page.tsx          # NEW placeholder
      sukalapor/page.tsx           # NEW placeholder
  lib/
    auth/paths.ts                  # NEW isPublicPath (pure, tested)
    auth/paths.test.ts             # NEW vitest
    supabase/client.ts             # NEW browser client
    supabase/server.ts             # NEW server client (async cookies)
    supabase/proxy.ts              # NEW session refresh for proxy.ts
    actions/session.ts            # NEW signOut server action
    api.ts                         # MOD: apiFetch helper
    types.ts                       # NEW shared view-model base types
  components/
    theme/theme-objects.ts         # NEW T.light / T.dark (ported)
    theme/theme-provider.tsx       # NEW ThemeProvider + useTheme
    theme/theme-objects.test.ts    # NEW vitest
    shell/brand-bar.tsx            # NEW
    shell/side-nav.tsx             # NEW (+ nav config)
    shell/nav-config.ts            # NEW nav items (pure, tested)
    shell/nav-config.test.ts       # NEW vitest
    ui/card.tsx                     # NEW
    ui/chip.tsx                     # NEW
    ui/section-head.tsx             # NEW
    ui/status-chips.tsx             # NEW StatusChip/IntentChip/UrgencyChip
    ui/icon.tsx                     # NEW inline SVG icon set
main.py                             # MOD (parent repo): retire `/` dashboard
```

---

## Task 1: Install dependencies

**Files:**
- Modify: `frontend/package.json` (via npm)

- [ ] **Step 1: Install runtime + dev deps**

Run (from `frontend/`):
```bash
npm install @supabase/ssr@^0.10.3 @supabase/supabase-js@^2
npm install -D vitest@^2
```
Expected: `package.json` gains the three packages; `npm install` exits 0.

- [ ] **Step 2: Add a test script**

Edit `frontend/package.json` `"scripts"` to add:
```json
"test": "vitest run",
"test:watch": "vitest"
```

- [ ] **Step 3: Commit**

```bash
git add package.json package-lock.json
git commit -m "chore: add @supabase/ssr, supabase-js, vitest"
```

---

## Task 2: Vitest config + first pure unit (route allowlist)

**Files:**
- Create: `frontend/vitest.config.ts`
- Create: `frontend/lib/auth/paths.ts`
- Test: `frontend/lib/auth/paths.test.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/lib/auth/paths.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { isPublicPath } from "./paths";

describe("isPublicPath", () => {
  it("treats /login as public", () => {
    expect(isPublicPath("/login")).toBe(true);
  });
  it("treats nested login routes as public", () => {
    expect(isPublicPath("/login/callback")).toBe(true);
  });
  it("treats app routes as protected", () => {
    expect(isPublicPath("/dashboard")).toBe(false);
    expect(isPublicPath("/")).toBe(false);
  });
});
```

- [ ] **Step 2: Create vitest config**

Create `frontend/vitest.config.ts`:
```ts
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["**/*.test.ts"],
  },
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `npx vitest run lib/auth/paths.test.ts`
Expected: FAIL — `Cannot find module './paths'`.

- [ ] **Step 4: Write minimal implementation**

Create `frontend/lib/auth/paths.ts`:
```ts
// Paths reachable without an authenticated session. Everything else is gated
// by proxy.ts. Keep this pure (no next/server imports) so it is unit-testable.
export const PUBLIC_PATHS = ["/login"] as const;

export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.some(
    (p) => pathname === p || pathname.startsWith(p + "/"),
  );
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `npx vitest run lib/auth/paths.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add vitest.config.ts lib/auth/paths.ts lib/auth/paths.test.ts
git commit -m "test: add vitest + isPublicPath route allowlist"
```

---

## Task 3: Environment + Supabase clients

**Files:**
- Create: `frontend/.env.local`
- Create: `frontend/lib/supabase/client.ts`
- Create: `frontend/lib/supabase/server.ts`

> **User action required:** the Supabase URL + anon key come from the Supabase
> dashboard → Project `midahxroauieyzaiiuvf` → Settings → API. The implementer
> must pause and ask the user for these if not already provided.

- [ ] **Step 1: Create `.env.local` (gitignored)**

Create `frontend/.env.local`:
```bash
NEXT_PUBLIC_SUPABASE_URL=https://midahxroauieyzaiiuvf.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=<paste anon public key here>
NEXT_PUBLIC_BACKEND_URL=http://localhost:8000
```
Verify `.gitignore` already ignores `.env*.local` (it does — line 4). Confirm:
```bash
git check-ignore .env.local
```
Expected: prints `.env.local` (ignored).

- [ ] **Step 2: Browser client**

Create `frontend/lib/supabase/client.ts`:
```ts
import { createBrowserClient } from "@supabase/ssr";

// Browser-side Supabase client (Client Components). Anon key is public by design.
export function createClient() {
  return createBrowserClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
  );
}
```

- [ ] **Step 3: Server client (async cookies — Next 16)**

Create `frontend/lib/supabase/server.ts`:
```ts
import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

// Server-side Supabase client. cookies() is async in Next.js 16.
export async function createClient() {
  const cookieStore = await cookies();
  return createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() {
          return cookieStore.getAll();
        },
        setAll(cookiesToSet) {
          try {
            cookiesToSet.forEach(({ name, value, options }) =>
              cookieStore.set(name, value, options),
            );
          } catch {
            // setAll called from a Server Component (read-only cookies).
            // Safe to ignore — the proxy refreshes the session cookie.
          }
        },
      },
    },
  );
}
```

- [ ] **Step 4: Type-check**

Run: `npx tsc --noEmit`
Expected: no errors from these files.

- [ ] **Step 5: Commit**

```bash
git add lib/supabase/client.ts lib/supabase/server.ts
git commit -m "feat: add Supabase browser + server clients"
```
(Do not commit `.env.local` — it is gitignored.)

---

## Task 4: Proxy session refresh + route gate

**Files:**
- Create: `frontend/lib/supabase/proxy.ts`
- Create: `frontend/proxy.ts`

- [ ] **Step 1: Session-refresh helper**

Create `frontend/lib/supabase/proxy.ts`:
```ts
import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

// Refresh the Supabase auth cookie on each request and return the current user.
// Pattern per @supabase/ssr docs, adapted for the Next.js 16 Proxy.
export async function updateSession(request: NextRequest) {
  let response = NextResponse.next({ request });

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() {
          return request.cookies.getAll();
        },
        setAll(cookiesToSet) {
          cookiesToSet.forEach(({ name, value }) =>
            request.cookies.set(name, value),
          );
          response = NextResponse.next({ request });
          cookiesToSet.forEach(({ name, value, options }) =>
            response.cookies.set(name, value, options),
          );
        },
      },
    },
  );

  const {
    data: { user },
  } = await supabase.auth.getUser();

  return { response, user };
}
```

- [ ] **Step 2: Proxy (route gate)**

Create `frontend/proxy.ts`:
```ts
import { NextResponse, type NextRequest } from "next/server";
import { updateSession } from "@/lib/supabase/proxy";
import { isPublicPath } from "@/lib/auth/paths";

export async function proxy(request: NextRequest) {
  const { response, user } = await updateSession(request);
  const { pathname } = request.nextUrl;

  // Unauthenticated + protected route → /login
  if (!user && !isPublicPath(pathname)) {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    return NextResponse.redirect(url);
  }

  // Authenticated user hitting /login → /dashboard
  if (user && pathname === "/login") {
    const url = request.nextUrl.clone();
    url.pathname = "/dashboard";
    return NextResponse.redirect(url);
  }

  return response;
}

export const config = {
  // Run on everything except Next internals and static asset files.
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|woff2?)$).*)",
  ],
};
```

- [ ] **Step 3: Verify build picks up the proxy**

Run: `npx tsc --noEmit`
Expected: no type errors.

- [ ] **Step 4: Commit**

```bash
git add lib/supabase/proxy.ts proxy.ts
git commit -m "feat: add Supabase session refresh + proxy route gate"
```

---

## Task 5: Login page + sign-in server action

**Files:**
- Create: `frontend/app/login/actions.ts`
- Create: `frontend/app/login/page.tsx`

- [ ] **Step 1: Sign-in server action**

Create `frontend/app/login/actions.ts`:
```ts
"use server";

import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";

export type LoginState = { error: string } | undefined;

export async function signIn(
  _prev: LoginState,
  formData: FormData,
): Promise<LoginState> {
  const email = String(formData.get("email") ?? "");
  const password = String(formData.get("password") ?? "");

  if (!email || !password) {
    return { error: "Email and password are required." };
  }

  const supabase = await createClient();
  const { error } = await supabase.auth.signInWithPassword({ email, password });

  if (error) {
    return { error: error.message };
  }

  redirect("/dashboard");
}
```

- [ ] **Step 2: Login page (client, useActionState)**

Create `frontend/app/login/page.tsx`:
```tsx
"use client";

import { useActionState } from "react";
import { signIn, type LoginState } from "./actions";

export default function LoginPage() {
  const [state, action, pending] = useActionState<LoginState, FormData>(
    signIn,
    undefined,
  );

  return (
    <main
      style={{
        minHeight: "100vh",
        display: "grid",
        placeItems: "center",
        background: "var(--bg-page)",
        padding: "var(--space-7)",
      }}
    >
      <form
        action={action}
        style={{
          width: "100%",
          maxWidth: 360,
          background: "var(--bg-surface)",
          border: "1px solid var(--border-default)",
          borderRadius: "var(--radius-lg)",
          boxShadow: "var(--elevation-2)",
          padding: "var(--space-8)",
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-5)",
        }}
      >
        <div className="pj-rule-heading">
          <h1 className="t-h3">Sukabot Suite</h1>
        </div>
        <label className="t-label" htmlFor="email">
          Email
          <input
            id="email"
            name="email"
            type="email"
            autoComplete="email"
            required
            style={inputStyle}
          />
        </label>
        <label className="t-label" htmlFor="password">
          Password
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            style={inputStyle}
          />
        </label>
        {state?.error && (
          <p className="t-body-sm" style={{ color: "var(--status-danger)" }}>
            {state.error}
          </p>
        )}
        <button
          type="submit"
          disabled={pending}
          style={{
            background: "var(--brand-primary)",
            color: "var(--fg-on-primary)",
            border: "none",
            borderRadius: "var(--radius-sm)",
            padding: "var(--space-4)",
            fontWeight: 600,
            cursor: pending ? "default" : "pointer",
            opacity: pending ? 0.7 : 1,
          }}
        >
          {pending ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}

const inputStyle: React.CSSProperties = {
  marginTop: "var(--space-2)",
  width: "100%",
  padding: "var(--space-4)",
  border: "1px solid var(--border-input)",
  borderRadius: "var(--radius-sm)",
  fontSize: "var(--fs-body)",
};
```

- [ ] **Step 3: Type-check**

Run: `npx tsc --noEmit`
Expected: no errors. (CSS vars referenced here are added in Task 7; styles are strings so tsc is unaffected.)

- [ ] **Step 4: Commit**

```bash
git add app/login/actions.ts app/login/page.tsx
git commit -m "feat: add login page + signIn server action"
```

---

## Task 6: Sign-out server action

**Files:**
- Create: `frontend/lib/actions/session.ts`

- [ ] **Step 1: Implement sign-out**

Create `frontend/lib/actions/session.ts`:
```ts
"use server";

import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";

export async function signOut() {
  const supabase = await createClient();
  await supabase.auth.signOut();
  redirect("/login");
}
```

- [ ] **Step 2: Type-check**

Run: `npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add lib/actions/session.ts
git commit -m "feat: add signOut server action"
```

---

## Task 7: Port the PUJASERA design tokens into globals.css

**Files:**
- Modify: `frontend/app/globals.css`
- Reference: `_tpl_app-cs.html` (parent repo root), lines ~210–464

- [ ] **Step 1: Append the token block**

Open `_tpl_app-cs.html` and copy the **`:root { … }`** block (all color, type,
spacing, radius, elevation, motion, layout tokens), the **`.t-*` semantic type
classes**, and the **`.pj-rule-heading`** rule (lines ~210–464 — everything
inside the first `<style>` after the `@font-face` declarations through the
`.pj-rule-heading::before` rule). Paste it into `frontend/app/globals.css`
**after** the existing Tailwind import line, replacing the default theme block.

Keep the existing `@import "tailwindcss";` (or the project's existing Tailwind v4
directive) at the very top. Do NOT copy the `@font-face` blocks — fonts are
handled by `next/font` in Task 8.

- [ ] **Step 2: Add base resets used by the apps**

Append to `frontend/app/globals.css` the base resets from `_tpl_app-cs.html`'s
second `<style>` block (the `* { box-sizing… }`, scrollbar styling, and `.app /
.main-col / .content` layout helpers), adapting `body` to use the CSS var
`background: var(--bg-page); color: var(--fg-default);`.

- [ ] **Step 3: Verify the dev server compiles CSS**

Run: `npm run build`
Expected: build succeeds (CSS is valid). If Tailwind v4 complains about an
unknown at-rule, confirm the Tailwind import is first and the pasted CSS uses
plain custom properties (it does).

- [ ] **Step 4: Commit**

```bash
git add app/globals.css
git commit -m "feat: port PUJASERA design tokens into globals.css"
```

---

## Task 8: Root layout — Inter font + ThemeProvider mount

**Files:**
- Modify: `frontend/app/layout.tsx`
- Depends on: Task 9's `ThemeProvider` (create Task 9 first if executing strictly in order; this task imports it)

> Execution note: do Task 9 before this step's import resolves, or stub the
> import. Recommended order: 9 then 8. Listed as 8 for narrative flow.

- [ ] **Step 1: Replace fonts + wrap children**

Replace `frontend/app/layout.tsx` with:
```tsx
import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { ThemeProvider } from "@/components/theme/theme-provider";

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Sukabot Suite",
  description: "ESB merchant support — CS dashboard, ticket monitoring, Sukalapor.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${inter.variable} h-full antialiased`}>
      <body className="min-h-full">
        <ThemeProvider>{children}</ThemeProvider>
      </body>
    </html>
  );
}
```

- [ ] **Step 2: Point --font-sans at Inter**

In `frontend/app/globals.css`, ensure the `--font-sans` token resolves to the
loaded font by adding (near the top of `:root`, after the pasted tokens):
```css
:root { --font-sans: var(--font-inter), -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
```
(If the pasted block already defines `--font-sans`, override it here so the
`next/font` variable wins.)

- [ ] **Step 3: Type-check (after Task 9 exists)**

Run: `npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add app/layout.tsx app/globals.css
git commit -m "feat: load Inter via next/font and mount ThemeProvider"
```

---

## Task 9: ThemeProvider + ported theme objects

**Files:**
- Create: `frontend/components/theme/theme-objects.ts`
- Create: `frontend/components/theme/theme-provider.tsx`
- Test: `frontend/components/theme/theme-objects.test.ts`
- Reference: `_src_app-cs.jsx` (search for `const T = {`)

- [ ] **Step 1: Write the failing test**

Create `frontend/components/theme/theme-objects.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { T, type Theme } from "./theme-objects";

describe("theme objects", () => {
  it("has light and dark variants", () => {
    expect(T.light).toBeDefined();
    expect(T.dark).toBeDefined();
  });
  it("light and dark expose the same keys", () => {
    const lightKeys = Object.keys(T.light).sort();
    const darkKeys = Object.keys(T.dark).sort();
    expect(darkKeys).toEqual(lightKeys);
  });
  it("exposes required core tokens", () => {
    const required: (keyof Theme)[] = ["bg", "surface", "fg", "border", "accent"];
    for (const k of required) {
      expect(typeof T.light[k]).toBe("string");
      expect(typeof T.dark[k]).toBe("string");
    }
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run components/theme/theme-objects.test.ts`
Expected: FAIL — cannot find `./theme-objects`.

- [ ] **Step 3: Port the theme objects**

Create `frontend/components/theme/theme-objects.ts`. Copy the `T` object literal
**verbatim** from `_src_app-cs.jsx` (search for `const T = {` — it has
`light: { … }` and `dark: { … }` with keys including at least `bg, surface,
surface2, border, borderStrong, fg, fgMuted, fgSubtle, fgDim, accent, accentBg,
accentBorder, success, warn, danger, purple, teal`). Wrap it as a typed export:
```ts
// Ported verbatim from _src_app-cs.jsx (`const T = {`). Do not invent values —
// copy the exact hex/rgb strings so the port matches the prototype pixel-for-pixel.
export const T = {
  light: {
    /* paste the light object's key:value pairs here */
  },
  dark: {
    /* paste the dark object's key:value pairs here */
  },
} as const;

export type Theme = (typeof T)["light"];
export type ThemeMap = typeof T;
```
After pasting, confirm both objects have identical keys (the test enforces this).

- [ ] **Step 4: Implement ThemeProvider**

Create `frontend/components/theme/theme-provider.tsx`:
```tsx
"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { T, type Theme } from "./theme-objects";

type ThemeCtx = { theme: Theme; dark: boolean; toggle: () => void };

const Ctx = createContext<ThemeCtx | null>(null);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [dark, setDark] = useState(false);
  const theme = dark ? T.dark : T.light;

  useEffect(() => {
    document.body.style.background = theme.bg;
    document.body.style.color = theme.fg;
  }, [theme]);

  const value = useMemo<ThemeCtx>(
    () => ({ theme, dark, toggle: () => setDark((d) => !d) }),
    [theme, dark],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTheme(): ThemeCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useTheme must be used within ThemeProvider");
  return ctx;
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `npx vitest run components/theme/theme-objects.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add components/theme/
git commit -m "feat: add ThemeProvider + ported light/dark theme objects"
```

---

## Task 10: Shared UI primitives

**Files:**
- Create: `frontend/components/ui/card.tsx`, `chip.tsx`, `section-head.tsx`, `status-chips.tsx`, `icon.tsx`
- Reference: `_src_app-cs.jsx` (`Card`, `Chip`, `IntentChip`, `StatusChip`, `UrgencyChip`, `INTENT_COLORS`, `SectionHead`)

- [ ] **Step 1: Card**

Create `frontend/components/ui/card.tsx`:
```tsx
"use client";

import { useTheme } from "@/components/theme/theme-provider";

export function Card({
  children,
  hover = false,
  style,
}: {
  children: React.ReactNode;
  hover?: boolean;
  style?: React.CSSProperties;
}) {
  const { theme } = useTheme();
  return (
    <div
      style={{
        background: theme.surface,
        border: `1px solid ${theme.border}`,
        borderRadius: "var(--radius-lg)",
        boxShadow: hover ? "var(--elevation-2)" : "var(--elevation-1)",
        padding: "var(--space-7)",
        ...style,
      }}
    >
      {children}
    </div>
  );
}
```

- [ ] **Step 2: Chip + SectionHead**

Create `frontend/components/ui/chip.tsx`:
```tsx
"use client";

type Variant = "default" | "accent" | "success" | "warn" | "danger" | "purple" | "teal";

const VARIANT_VARS: Record<Variant, { bg: string; fg: string }> = {
  default: { bg: "var(--bg-subtle)", fg: "var(--fg-default)" },
  accent: { bg: "var(--color-blue-50)", fg: "var(--color-blue-700)" },
  success: { bg: "var(--color-success-50)", fg: "var(--color-success-700)" },
  warn: { bg: "var(--color-warning-50)", fg: "var(--color-warning-700)" },
  danger: { bg: "var(--color-danger-50)", fg: "var(--color-danger-700)" },
  purple: { bg: "var(--color-purple-100)", fg: "var(--color-purple-900)" },
  teal: { bg: "#E3FAFC", fg: "#0B7285" },
};

export function Chip({
  children,
  variant = "default",
}: {
  children: React.ReactNode;
  variant?: Variant;
}) {
  const v = VARIANT_VARS[variant];
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        background: v.bg,
        color: v.fg,
        borderRadius: "var(--radius-pill)",
        padding: "2px 10px",
        fontSize: "var(--fs-body-sm)",
        fontWeight: 500,
      }}
    >
      {children}
    </span>
  );
}
```

Create `frontend/components/ui/section-head.tsx`:
```tsx
export function SectionHead({ children }: { children: React.ReactNode }) {
  return (
    <div className="pj-rule-heading">
      <h2 className="t-h4">{children}</h2>
    </div>
  );
}
```

- [ ] **Step 3: Status/Intent/Urgency chips**

Create `frontend/components/ui/status-chips.tsx`:
```tsx
"use client";

import { Chip } from "./chip";

export function StatusChip({ status }: { status: string }) {
  const map: Record<string, Parameters<typeof Chip>[0]["variant"]> = {
    New: "default",
    "In Progress": "accent",
    Waiting: "warn",
    Escalated: "warn",
    Resolved: "success",
  };
  return <Chip variant={map[status] ?? "default"}>{status}</Chip>;
}

export function UrgencyChip({ urgency }: { urgency: string }) {
  const map: Record<string, Parameters<typeof Chip>[0]["variant"]> = {
    High: "danger",
    Medium: "warn",
    Low: "default",
  };
  return <Chip variant={map[urgency] ?? "default"}>{urgency}</Chip>;
}

// INTENT_COLORS ported from _src_app-cs.jsx.
const INTENT_COLORS: Record<string, { bg: string; fg: string }> = {
  Question: { bg: "#E8F4FD", fg: "#1864AB" },
  "Task Request": { bg: "#FFF4E5", fg: "#C05F00" },
  "Issue/Complaint": { bg: "#FFE3E3", fg: "#C92A2A" },
  "Feature Request": { bg: "#F3F0FF", fg: "#6741D9" },
  "Status Inquiry": { bg: "#E3FAFC", fg: "#0B7285" },
};

export function IntentChip({ intent }: { intent: string }) {
  const c = INTENT_COLORS[intent] ?? { bg: "var(--bg-subtle)", fg: "var(--fg-default)" };
  return (
    <span
      style={{
        background: c.bg,
        color: c.fg,
        borderRadius: "var(--radius-pill)",
        padding: "2px 10px",
        fontSize: "var(--fs-body-sm)",
        fontWeight: 500,
      }}
    >
      {intent}
    </span>
  );
}
```

- [ ] **Step 4: Minimal Icon component**

Create `frontend/components/ui/icon.tsx`:
```tsx
// Minimal inline-SVG icon set for the shell. Expand per phase as needed.
type IconName = "dashboard" | "kanban" | "chat" | "logout" | "moon" | "sun";

const PATHS: Record<IconName, React.ReactNode> = {
  dashboard: <path d="M3 13h8V3H3v10Zm0 8h8v-6H3v6Zm10 0h8V11h-8v10Zm0-18v6h8V3h-8Z" />,
  kanban: <path d="M4 4h4v16H4V4Zm6 0h4v10h-4V4Zm6 0h4v7h-4V4Z" />,
  chat: <path d="M4 4h16v12H7l-3 3V4Z" />,
  logout: <path d="M10 17v-2H4V9h6V7l5 5-5 5Zm2-14h6a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-6v-2h6V5h-6V3Z" />,
  moon: <path d="M12 3a9 9 0 1 0 9 9c-5 0-9-4-9-9Z" />,
  sun: <path d="M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10Zm0-5v3m0 14v3m9-10h-3M6 12H3m14.5-6.5-2 2m-7 7-2 2m11 0-2-2m-7-7-2-2" />,
};

export function Icon({
  name,
  size = 20,
  color = "currentColor",
}: {
  name: IconName;
  size?: number;
  color?: string;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke={color}
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      {PATHS[name]}
    </svg>
  );
}
```

- [ ] **Step 5: Type-check**

Run: `npx tsc --noEmit`
Expected: no errors. (CSS vars like `--color-blue-50` exist from Task 7.)

- [ ] **Step 6: Commit**

```bash
git add components/ui/
git commit -m "feat: add shared UI primitives (Card, Chip, chips, Icon, SectionHead)"
```

---

## Task 11: Nav config (tested) + shell components

**Files:**
- Create: `frontend/components/shell/nav-config.ts`
- Test: `frontend/components/shell/nav-config.test.ts`
- Create: `frontend/components/shell/side-nav.tsx`
- Create: `frontend/components/shell/brand-bar.tsx`

- [ ] **Step 1: Write the failing test**

Create `frontend/components/shell/nav-config.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { NAV_GROUPS, findActiveHref } from "./nav-config";

describe("nav-config", () => {
  it("defines the three suite routes", () => {
    const hrefs = NAV_GROUPS.flatMap((g) => g.items.map((i) => i.href));
    expect(hrefs).toEqual(["/dashboard", "/monitoring", "/sukalapor"]);
  });
  it("matches the active href by pathname prefix", () => {
    expect(findActiveHref("/dashboard")).toBe("/dashboard");
    expect(findActiveHref("/monitoring/123")).toBe("/monitoring");
    expect(findActiveHref("/unknown")).toBeNull();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run components/shell/nav-config.test.ts`
Expected: FAIL — cannot find `./nav-config`.

- [ ] **Step 3: Implement nav config**

Create `frontend/components/shell/nav-config.ts`:
```ts
export type NavItem = { label: string; href: string; icon: "dashboard" | "kanban" | "chat" };
export type NavGroup = { title: string; items: NavItem[] };

export const NAV_GROUPS: NavGroup[] = [
  {
    title: "Sukabot Insight",
    items: [
      { label: "Dashboard", href: "/dashboard", icon: "dashboard" },
      { label: "Ticket Monitoring", href: "/monitoring", icon: "kanban" },
    ],
  },
  {
    title: "Sukalapor",
    items: [{ label: "Sukalapor Chatbot", href: "/sukalapor", icon: "chat" }],
  },
];

export function findActiveHref(pathname: string): string | null {
  const all = NAV_GROUPS.flatMap((g) => g.items.map((i) => i.href));
  // Longest-prefix match so /monitoring/123 → /monitoring.
  const match = all
    .filter((h) => pathname === h || pathname.startsWith(h + "/"))
    .sort((a, b) => b.length - a.length)[0];
  return match ?? null;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run components/shell/nav-config.test.ts`
Expected: PASS (2 tests).

- [ ] **Step 5: SideNav (client)**

Create `frontend/components/shell/side-nav.tsx`:
```tsx
"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { NAV_GROUPS, findActiveHref } from "./nav-config";
import { Icon } from "@/components/ui/icon";
import { useTheme } from "@/components/theme/theme-provider";

export function SideNav() {
  const pathname = usePathname();
  const active = findActiveHref(pathname);
  const { theme } = useTheme();

  return (
    <nav
      style={{
        width: "var(--sidebar-width)",
        flex: "0 0 var(--sidebar-width)",
        background: theme.surface,
        borderRight: `1px solid ${theme.border}`,
        padding: "var(--space-6) var(--space-4)",
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-7)",
        overflowY: "auto",
      }}
    >
      {NAV_GROUPS.map((group) => (
        <div key={group.title}>
          <div className="t-meta" style={{ padding: "0 var(--space-4) var(--space-3)" }}>
            {group.title}
          </div>
          {group.items.map((item) => {
            const isActive = active === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "var(--space-4)",
                  padding: "var(--space-4)",
                  borderRadius: "var(--radius-sm)",
                  textDecoration: "none",
                  color: isActive ? "var(--brand-primary)" : theme.fgMuted,
                  background: isActive ? "var(--bg-subtle)" : "transparent",
                  fontWeight: isActive ? 600 : 500,
                }}
              >
                <Icon name={item.icon} />
                <span className="t-body">{item.label}</span>
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}
```

- [ ] **Step 6: BrandBar (client, with dark toggle + sign out)**

Create `frontend/components/shell/brand-bar.tsx`:
```tsx
"use client";

import { useTheme } from "@/components/theme/theme-provider";
import { Icon } from "@/components/ui/icon";
import { signOut } from "@/lib/actions/session";

export function BrandBar({ email }: { email: string | null }) {
  const { dark, toggle, theme } = useTheme();
  return (
    <header
      style={{
        height: "var(--topbar-height)",
        flex: "0 0 var(--topbar-height)",
        display: "flex",
        alignItems: "center",
        gap: "var(--space-5)",
        padding: "0 var(--space-7)",
        background: theme.surface,
        borderBottom: `1px solid ${theme.border}`,
      }}
    >
      <strong className="t-h5" style={{ color: "var(--brand-primary)" }}>
        Sukabot Suite
      </strong>
      <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: "var(--space-5)" }}>
        {email && <span className="t-body-sm" style={{ color: theme.fgMuted }}>{email}</span>}
        <button
          type="button"
          onClick={toggle}
          aria-label="Toggle dark mode"
          style={{ background: "none", border: "none", cursor: "pointer", color: theme.fg }}
        >
          <Icon name={dark ? "sun" : "moon"} />
        </button>
        <form action={signOut}>
          <button
            type="submit"
            aria-label="Sign out"
            style={{ background: "none", border: "none", cursor: "pointer", color: theme.fg, display: "flex" }}
          >
            <Icon name="logout" />
          </button>
        </form>
      </div>
    </header>
  );
}
```

- [ ] **Step 7: Commit**

```bash
git add components/shell/
git commit -m "feat: add suite shell (nav config + SideNav + BrandBar)"
```

---

## Task 12: Authenticated suite layout + placeholder pages

**Files:**
- Create: `frontend/app/(suite)/layout.tsx`
- Create: `frontend/app/(suite)/dashboard/page.tsx`
- Create: `frontend/app/(suite)/monitoring/page.tsx`
- Create: `frontend/app/(suite)/sukalapor/page.tsx`

- [ ] **Step 1: Suite layout (server, defense-in-depth auth check)**

Create `frontend/app/(suite)/layout.tsx`:
```tsx
import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { SideNav } from "@/components/shell/side-nav";
import { BrandBar } from "@/components/shell/brand-bar";

export default async function SuiteLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) redirect("/login");

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh" }}>
      <BrandBar email={user.email ?? null} />
      <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
        <SideNav />
        <main style={{ flex: 1, minWidth: 0, overflowY: "auto", padding: "var(--space-7)" }}>
          {children}
        </main>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Placeholder pages**

Create `frontend/app/(suite)/dashboard/page.tsx`:
```tsx
export default function DashboardPage() {
  return (
    <section>
      <div className="pj-rule-heading">
        <h1 className="t-h2">Dashboard</h1>
      </div>
      <p className="t-body" style={{ marginTop: "var(--space-5)", color: "var(--fg-muted)" }}>
        CS Chatbot Dashboard — coming in Phase 1.
      </p>
    </section>
  );
}
```

Create `frontend/app/(suite)/monitoring/page.tsx`:
```tsx
export default function MonitoringPage() {
  return (
    <section>
      <div className="pj-rule-heading">
        <h1 className="t-h2">Ticket Monitoring</h1>
      </div>
      <p className="t-body" style={{ marginTop: "var(--space-5)", color: "var(--fg-muted)" }}>
        Sukabantu Ticket Monitoring — coming in Phase 2.
      </p>
    </section>
  );
}
```

Create `frontend/app/(suite)/sukalapor/page.tsx`:
```tsx
export default function SukalaporPage() {
  return (
    <section>
      <div className="pj-rule-heading">
        <h1 className="t-h2">Sukalapor</h1>
      </div>
      <p className="t-body" style={{ marginTop: "var(--space-5)", color: "var(--fg-muted)" }}>
        Sukalapor Chatbot — coming in Phase 3.
      </p>
    </section>
  );
}
```

- [ ] **Step 3: Remove the old root page (it conflicts with the gate)**

The existing `frontend/app/page.tsx` (old ticket list) is superseded. Replace it
with a redirect to the dashboard so `/` resolves cleanly:
```tsx
import { redirect } from "next/navigation";

export default function Home() {
  redirect("/dashboard");
}
```

- [ ] **Step 4: Type-check + build**

Run: `npx tsc --noEmit && npm run build`
Expected: both succeed.

- [ ] **Step 5: Commit**

```bash
git add "app/(suite)" app/page.tsx
git commit -m "feat: add authenticated suite layout + placeholder routes"
```

---

## Task 13: Extend the typed API client

**Files:**
- Modify: `frontend/lib/api.ts`
- Create: `frontend/lib/types.ts`

- [ ] **Step 1: Shared base types**

Create `frontend/lib/types.ts`:
```ts
// Raw ticket as returned by FastAPI GET /api/tickets (mirrors _ticket_to_dict).
export interface RawTicket {
  id: number;
  ticket_number: string | null;
  name: string | null;
  phone_number: string | null;
  company_name: string | null;
  branch_name: string | null;
  issue_category: string | null;
  issue_detail: string | null;
  chat_history: string | null;
  attachments: string | null;
  steps_attempted: string | null;
  status: string | null;
  confidence_score: number | null;
  created_at: string | null;
}
```

- [ ] **Step 2: apiFetch helper + re-typed getTickets**

Replace `frontend/lib/api.ts` with:
```ts
// Thin client for the FastAPI backend (main.py).
import type { RawTicket } from "./types";

export const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000";

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BACKEND_URL}${path}`, { cache: "no-store", ...init });
  if (!res.ok) {
    throw new Error(`Backend ${res.status} on ${path}`);
  }
  return res.json() as Promise<T>;
}

export function getTickets(): Promise<RawTicket[]> {
  return apiFetch<RawTicket[]>("/api/tickets");
}

// Back-compat: existing imports of `Ticket` keep working.
export type { RawTicket as Ticket } from "./types";
```

- [ ] **Step 3: Type-check**

Run: `npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add lib/api.ts lib/types.ts
git commit -m "feat: extend typed API client (apiFetch, RawTicket)"
```

---

## Task 14: Retire the FastAPI HTML dashboard route (parent repo)

**Files:**
- Modify: `main.py` (parent repo root) — the `serve_dashboard` route at `/`

- [ ] **Step 1: Locate the route**

Run (parent repo root):
```bash
grep -n "serve_dashboard\|@app.get(\"/\")\|dashboard-legacy" main.py
```
Expected: shows the `/` dashboard route and the existing `/dashboard-legacy`.

- [ ] **Step 2: Repoint `/` to a small JSON pointer**

Change the `/` route so it no longer serves the 7.4 MB suite HTML as the primary
UI. Keep `/dashboard-legacy` exactly as-is. Replace the body of the `@app.get("/")`
handler with a lightweight JSON response pointing at the Next.js UI:
```python
@app.get("/")
def root():
    # The dashboard UI now lives in the Next.js app (port 3000).
    # The legacy bundled HTML remains available at /dashboard-legacy.
    return {
        "service": "esb-chatbot backend",
        "ui": "http://localhost:3000",
        "legacy_dashboard": "/dashboard-legacy",
    }
```
Do not delete the suite patcher functions or `/dashboard-legacy` — they remain
the safety net.

- [ ] **Step 3: Smoke the backend**

Run:
```bash
./venv/Scripts/python.exe -m pytest -q  # if tests exist; else:
./venv/Scripts/python.exe -c "import main; print('import OK')"
```
Expected: import succeeds / tests pass.

- [ ] **Step 4: Commit (parent repo)**

```bash
git add main.py
git commit -m "chore: retire root HTML dashboard in favor of Next.js UI"
```

---

## Task 15: Full verification + submodule pointer bump

**Files:**
- Modify: parent repo submodule pointer for `frontend`

- [ ] **Step 1: Run the full test + build in frontend**

Run (from `frontend/`):
```bash
npx vitest run && npx tsc --noEmit && npm run build
```
Expected: all tests pass, no type errors, build succeeds.

- [ ] **Step 2: Manual auth-flow check**

Start backend (parent root): `./venv/Scripts/python.exe -m uvicorn main:app --port 8000`
Start frontend (`frontend/`): `npm run dev`
Then verify in a browser:
1. Visit `http://localhost:3000/dashboard` while logged out → redirected to `/login`.
2. Log in with a seeded Supabase user → lands on `/dashboard`.
3. Toggle dark mode → background/foreground change.
4. Navigate Dashboard → Ticket Monitoring → Sukalapor (placeholders render under shared shell).
5. Sign out → redirected to `/login`; revisiting `/dashboard` redirects to `/login` again.

Document the result. If a seeded user doesn't exist, create one in the Supabase
dashboard (Auth → Users → Add user).

- [ ] **Step 3: Bump the submodule pointer in the parent repo**

After all `frontend/` commits are made, the parent repo sees a new submodule SHA.
From the parent repo root:
```bash
git add frontend
git commit -m "chore: bump frontend submodule to Phase 0 foundation"
```

- [ ] **Step 4: Final status check**

Run (parent root): `git status` and (frontend) `git status`
Expected: both clean.

---

## Self-Review (completed by plan author)

**Spec coverage** (against `2026-06-08-phase0-foundation-design.md`):
- Serving model (Next UI + FastAPI API, retire `/`) → Tasks 12–14. ✅
- Auth (Supabase `@supabase/ssr`, proxy gate, login, sign-out, server check) → Tasks 1, 3–6, 12. ✅
- Design system (globals.css tokens, fonts) → Tasks 7–8. ✅
- Theme/dark mode (ThemeProvider, theme objects) → Task 9. ✅
- Shell (BrandBar + SideNav, nav groups) → Task 11. ✅
- Shared primitives (Card, Chip, SectionHead, status chips, Icon) → Task 10. ✅
- Data layer (apiFetch, types) → Task 13. ✅
- Testing (vitest pure units, tsc, build, manual gate) → Tasks 2, 9, 11, 15. ✅
- Submodule handling → Task 15. ✅

**Placeholder scan:** "paste verbatim from <file>" instructions in Tasks 7 & 9
reference exact, present source files with exact anchors — these are port
instructions, not vague placeholders. No TBD/TODO/"add error handling" left.

**Type consistency:** `createClient` (server vs browser) are separate modules
with the same name by design (imported by path). `useTheme()` returns `{ theme,
dark, toggle }` consumed consistently in BrandBar/SideNav/Card. `RawTicket`
exported and re-aliased as `Ticket` for back-compat. `findActiveHref` /
`isPublicPath` signatures match their tests.

**Known external dependency:** Supabase anon key (Task 3) must be supplied by the
user before the auth flow can be tested end-to-end (Task 15 Step 2).
