import config  # noqa: F401 — loads .env before any os.getenv reads

from fastapi import FastAPI, Request, Depends, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session
import asyncio
import base64
import contextlib
import gzip
import html
import json
import datetime
import logging
import re
import time
import httpx
import os

from database import engine, SessionLocal, Base, Ticket, CSATRating, TicketNote, ChatSession
from agent import (
    process_message, SESSION_STATE, _record_turn, _fresh_session,
    detect_anger, calming_message, idle_action,
)
from rag import vertex_search_available

# PRD US3 attachment constraints.
ALLOWED_IMAGE_MIME = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_VIDEO_MIME = {"video/mp4", "video/quicktime"}
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"}
MAX_FILE_BYTES = 15 * 1024 * 1024     # AC3.7 — 15 MB
MAX_ATTACHMENTS_PER_SESSION = 3       # AC3.1

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "mock_token")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

if TELEGRAM_TOKEN == "mock_token":
    logging.warning(
        "TELEGRAM_TOKEN is unset; bot replies to Telegram will fail silently. "
        "Set TELEGRAM_TOKEN to enable real message delivery.",
    )
if not TELEGRAM_WEBHOOK_SECRET:
    logging.warning(
        "TELEGRAM_WEBHOOK_SECRET is unset; /webhook will accept any caller. "
        "Set it (and pass --secret-token to setWebhook) to require auth.",
    )

REAP_SECRET = os.getenv("REAP_SECRET", "")
if not REAP_SECRET:
    logging.warning(
        "REAP_SECRET is unset; /reap will reject all callers. Set it (and pass "
        "the same value as the X-Reap-Secret header from Cloud Scheduler).",
    )


def _telegram_user(update: dict) -> dict:
    """Pull merchant identity from a Telegram update payload."""
    msg = update.get("message") or update.get("callback_query", {}).get("message", {}) or {}
    user = (
        update.get("message", {}).get("from")
        or update.get("callback_query", {}).get("from")
        or {}
    )
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    full = (first + " " + last).strip() or user.get("username") or "Telegram User"
    return {
        "chat_id": str(msg.get("chat", {}).get("id") or ""),
        "name": full,
        "username": user.get("username") or "",
        "language_code": user.get("language_code") or "",
    }

# Initialize DB
Base.metadata.create_all(bind=engine)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the idle-session reaper (defined below) so the bot can proactively
    # check in on quiet customers and close stale chats.
    task = asyncio.create_task(_session_reaper_loop())
    logging.info("Idle session reaper started (every %ss).", REAPER_INTERVAL_SECONDS)
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="AI Chatbot Backend Phase 1", lifespan=lifespan)

# Enable CORS. Set CORS_ALLOW_ORIGINS to a comma-separated list of frontend
# origins in production (e.g. "https://your-app.vercel.app"). Defaults to "*"
# for local dev. Note: browsers reject "*" together with credentials, so we
# only enable credentials when explicit origins are configured.
_cors_env = os.getenv("CORS_ALLOW_ORIGINS", "*").strip()
if _cors_env in ("", "*"):
    _allow_origins = ["*"]
    _allow_credentials = False
else:
    _allow_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
    _allow_credentials = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/health")
def healthcheck(db: Session = Depends(get_db)):
    """Liveness + readiness probe for Cloud Run.

    Returns 200 when the DB is reachable. The Vertex AI Search flag is
    informational only — the bot has a local fallback retriever so it can
    still serve traffic without GCP creds set.
    """
    checks: dict = {}
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        db_ok = False
        checks["database"] = f"error: {e}"

    checks["vertex_search_configured"] = vertex_search_available()

    payload = {
        "status": "ok" if db_ok else "degraded",
        "checks": checks,
    }
    if not db_ok:
        return JSONResponse(status_code=503, content=payload)
    return payload


# --- Bundle data injection -------------------------------------------------
# The standalone React bundle was authored with an explicit injection hook
# (`window.__INJECTED_TICKETS`) — see _bundle_app_f506e129.js line 86. We map
# our DB rows to the bundle's expected schema and inject before the bundler
# loader runs, so the React app renders real data instead of mock data.

# Direct alias map first, then fall back to keyword matching against the bundle's
# TOPICS list (see _bundle_app_f506e129.js TOPIC_TREE).
_BUNDLE_TOPIC_ALIASES = {
    "Menu Management": "Menu Management",
    "Payment Method Setup": "Payment Configuration",
    "Payment Gateway/MDR": "Payment Gateway/MDR",
    "Payment Gateway / MDR": "Payment Gateway/MDR",
    "Order Management": "Order Management",
    "Account/Onboarding": "Account & Activation",
    "Account & Activation": "Account & Activation",
    "ESO Activation/Deactivation": "Account & Activation",
    "ESO Activation / Deactivation": "Account & Activation",
    "Promotions/Discounts": "Promo & Discount",
    "Integration/API": "Integration",
    "Reporting/Analytics": "Other",
    "Out-of-Scope": "Other",
}
# Keyword → topic (lowercased substring match against the raw category string).
_BUNDLE_TOPIC_KEYWORDS = (
    ("menu", "Menu Management"),
    ("payment", "Payment Gateway/MDR"),
    ("qr", "Payment Gateway/MDR"),
    ("mdr", "Payment Gateway/MDR"),
    ("settlement", "Payment Gateway/MDR"),
    ("order", "Order Management"),
    ("push_to_pos", "Order Management"),
    ("eso", "Account & Activation"),
    ("oze", "Account & Activation"),
    ("activation", "Account & Activation"),
    ("onboard", "Account & Activation"),
    ("account", "Account & Activation"),
    ("catalog", "Product Catalog"),
    ("product", "Product Catalog"),
    ("promo", "Promo & Discount"),
    ("discount", "Promo & Discount"),
    ("voucher", "Promo & Discount"),
    ("integration", "Integration"),
    ("api", "Integration"),
)


def _map_bundle_topic(category: str | None) -> str:
    """Best-effort map a free-form issue_category to one of the bundle's TOPICS."""
    if not category:
        return "Other"
    # Try the raw string first
    if category in _BUNDLE_TOPIC_ALIASES:
        return _BUNDLE_TOPIC_ALIASES[category]
    # Try the part before " / " (real rows look like "Order Management / push_to_pos_failed")
    head = category.split(" / ", 1)[0].strip()
    if head in _BUNDLE_TOPIC_ALIASES:
        return _BUNDLE_TOPIC_ALIASES[head]
    # Fall back to keyword scan
    lc = category.lower()
    for needle, topic in _BUNDLE_TOPIC_KEYWORDS:
        if needle in lc:
            return topic
    return "Other"
_BUNDLE_STATUS_MAP = {
    "Open": "New",
    "In Progress": "In Progress",
    "Waiting": "Waiting",
    "Resolved": "Resolved",
    "Closed": "Resolved",
}


def _parse_chat_history(chat_history: str | None) -> list[dict]:
    """Parse the stored ``[HH:MM:SS] Actor: text`` transcript into structured turns.

    The agent persists chat history via ``render_chat_history`` (agent.py:365)
    which produces one line per turn. The bundle's transcript panel wants
    ``[{role: 'user'|'bot', text: str}, ...]``.
    """
    import re as _re

    if not chat_history:
        return []
    pattern = _re.compile(
        r"^\[\d{2}:\d{2}:\d{2}\]\s+(Merchant|Bot|User|Assistant):\s*(.*)$"
    )
    out: list[dict] = []
    current: dict | None = None
    for line in chat_history.splitlines():
        m = pattern.match(line)
        if m:
            if current:
                out.append(current)
            actor = m.group(1).lower()
            role = "user" if actor in ("merchant", "user") else "bot"
            current = {"role": role, "text": m.group(2)}
        elif current is not None:
            current["text"] += "\n" + line
    if current:
        out.append(current)
    return out


def _ticket_to_bundle(t: Ticket) -> dict:
    """Map a Ticket row to the React bundle's expected shape."""
    import datetime as _dt

    topic = _map_bundle_topic(t.issue_category)
    # sub_topic in DB is often "#tag_name"; the bundle expects a slug. If absent,
    # try to pull a slug from the second half of "Category / sub_topic".
    raw_sub = (t.sub_topic or "").lstrip("#").lower().strip()
    if not raw_sub and t.issue_category and " / " in t.issue_category:
        raw_sub = t.issue_category.split(" / ", 1)[1].strip().lower()
    sub_topic = raw_sub or "general_inquiry"
    status = _BUNDLE_STATUS_MAP.get(t.status or "Open", "New")
    escalated = bool(t.routed_queue)
    resolved = status == "Resolved" and not escalated
    conf = float(t.confidence_score or 70) / 100.0
    detail = (t.issue_detail or "").lower()

    if escalated or any(k in detail for k in ("tidak", "gagal", "error", "kendala", "rusak")):
        intent = "Issue/Complaint"
    elif "?" in detail or "bagaimana" in detail or "apakah" in detail:
        intent = "Question"
    else:
        intent = "Task Request"

    created = t.created_at or _dt.datetime.utcnow()
    return {
        "id": t.ticket_number or f"CS-{t.id:05d}",
        "dbId": t.id,  # used by the kanban drag-and-drop to PUT status updates
        "created": created.isoformat(),
        "user": t.name or t.company_name or "Merchant",
        "userType": "Merchant",
        "channel": "WhatsApp",  # bundle CHANNELS doesn't include Telegram; map to closest
        "intent": intent,
        "topic": topic,
        "subTopic": sub_topic,
        "status": status,
        "urgency": "High" if intent == "Issue/Complaint" else "Medium",
        "sentiment": "Negative" if intent == "Issue/Complaint" else "Neutral",
        "confidence": round(conf, 2),
        "resolvedByBot": resolved,
        "resolutionMin": 5 if resolved else 60,
        "slaTargetMin": 30 if intent == "Issue/Complaint" else 120,
        "slaBreachRisk": False,
        "phrasing": (t.issue_detail or t.issue_category or "Tiket support")[:200],
        "messageCount": max(len(_parse_chat_history(t.chat_history)), 4),
        "escalated": escalated,
        "agent": None if resolved else (t.routed_queue or "Tim Support"),
        "csat": None,
        # Real Indonesian transcript; the bundle's buildTranscript() is patched
        # to use this when present instead of its English mock fallback.
        "transcript": _parse_chat_history(t.chat_history),
    }


# The new Sukabot Suite ships as a top-level shell with three iframes
# (app-cs, app-tm, app-sl). The CS Chatbot Dashboard we care about lives in
# `app-cs` as an HTML-entity-encoded srcdoc. We decode that srcdoc, patch the
# inner __bundler/manifest (date pivot + Indonesian transcript) and
# __bundler/template (App() state refactor + per-request data placeholder),
# then re-encode it back into the suite shell.
#
# Per-request ticket data is spliced in via a fixed-text placeholder so the
# heavy patch work runs once a day; serve_dashboard() only does two string
# replaces on the cached output.
SUITE_HTML_PATH = "[Prototype] Sukabot Suite.html"
LEGACY_BUNDLE_PATH = "CS Chatbot Dashboard _standalone_.html"
DATA_PLACEHOLDER = "__SUKABOT_DATA_PLACEHOLDER__"
SUBMAP_PLACEHOLDER = "__SUKABOT_SUBMAP_PLACEHOLDER__"

# Cache holds the suite HTML with structural patches applied but with the
# data placeholders intact — they're substituted per-request.
_PATCHED_BUNDLE_CACHE: dict = {"date": None, "html": None}


def _patch_appcs_srcdoc(decoded: str, today: str) -> str:
    """Patch the decoded app-cs iframe srcdoc.

    Returns a new decoded srcdoc with:
      * date pivots (2026-05-22) rewritten to ``today`` in every JS payload
      * ``buildTranscript`` short-circuited to use ``t.transcript`` when present
      * App() converted to ticket state so window.__INJECTED_TICKETS wins
      * an inline <script> with DATA_PLACEHOLDER / SUBMAP_PLACEHOLDER inserted
        before <div id="root"> so it executes before the babel-transpiled
        scripts that define App
    """
    new_t00 = f"{today}T00:00:00"
    new_t12 = f"{today}T12:00:00"

    # ── Manifest: rewrite each JS payload in-place ────────────────────────
    mm = re.search(
        r'<script type="__bundler/manifest">(.*?)</script>',
        decoded, re.DOTALL,
    )
    if not mm:
        return decoded
    manifest = json.loads(mm.group(1))
    for entry in manifest.values():
        mime = entry.get("mime", "")
        if not mime.endswith("javascript"):
            continue
        raw = base64.b64decode(entry["data"])
        was_compressed = bool(entry.get("compressed"))
        if was_compressed:
            try:
                raw = gzip.decompress(raw)
            except Exception:
                continue
        try:
            src = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if "2026-05-22" not in src and "function buildTranscript" not in src:
            continue
        patched = (
            src.replace("2026-05-22T00:00:00", new_t00)
               .replace("2026-05-22T12:00:00", new_t12)
        )
        # buildTranscript: short-circuit to real transcript when present, then
        # Indonesianize the English mock fallback strings.
        patched = patched.replace(
            "function buildTranscript(t) {\n  return [",
            "function buildTranscript(t) {\n"
            "  if (t.transcript && t.transcript.length) return t.transcript;\n"
            "  return [",
        )
        patched = patched.replace(
            "`I can help with that! Navigate to Settings → ${t.topic} → ${t.subTopic.replace(/_/g,' ')}. Let me know if you need more details.`",
            "`Saya bisa bantu! Buka Pengaturan → ${t.topic} → ${t.subTopic.replace(/_/g,' ')}. Beri tahu saya jika butuh detail lebih lanjut.`",
        )
        patched = patched.replace(
            "`I'm having trouble understanding. Let me connect you with a CS agent.`",
            "`Maaf, saya kurang paham. Saya akan menghubungkan Anda dengan agen CS.`",
        )
        patched = patched.replace(
            "'Yes please, I need help with this.'",
            "'Iya tolong, saya butuh bantuan untuk ini.'",
        )
        patched = patched.replace(
            "`Connecting you to ${t.agent || 'an agent'} now. They will respond shortly.`",
            "`Menghubungkan Anda ke ${t.agent || 'agen'} sekarang. Mereka akan segera membalas.`",
        )
        out = patched.encode("utf-8")
        if was_compressed:
            out = gzip.compress(out)
        entry["data"] = base64.b64encode(out).decode("ascii")
    new_manifest = json.dumps(manifest, separators=(",", ":"))
    decoded = decoded[: mm.start(1)] + new_manifest + decoded[mm.end(1):]

    # ── Template: rewrite App() + inject data placeholder ─────────────────
    # The template's JSON-encoded content contains literal "</script>"
    # substrings (json.dumps doesn't escape "/"), so a non-greedy regex would
    # stop too early. Anchor instead to the file's final </script> — the
    # template script is the last one in the srcdoc body.
    tpl_open = '<script type="__bundler/template">'
    tpl_start = decoded.find(tpl_open)
    if tpl_start == -1:
        return decoded
    content_start = tpl_start + len(tpl_open)
    content_end = decoded.rfind("</script>")
    if content_end <= content_start:
        return decoded
    try:
        template_html = json.loads(decoded[content_start:content_end].strip())
    except json.JSONDecodeError:
        return decoded

    old_app_open = (
        "function App() {\n"
        "  const [active, setActive] = useState('overview');"
    )
    new_app_open = (
        "function App() {\n"
        "  const [tickets, setTickets] = useState(\n"
        "    (window.__INJECTED_TICKETS && window.__INJECTED_TICKETS.length)\n"
        "      ? window.__INJECTED_TICKETS\n"
        "      : TICKETS\n"
        "  );\n"
        "  React.useEffect(() => { window.__setTickets = setTickets;"
        " return () => { if (window.__setTickets === setTickets) delete window.__setTickets; }; }, []);\n"
        "  const [active, setActive] = useState('overview');"
    )
    patched_tpl = template_html.replace(old_app_open, new_app_open)
    patched_tpl = patched_tpl.replace(
        "const filteredTickets = useMemo(() => applyFilters(TICKETS, filters), [filters]);",
        "const filteredTickets = useMemo(() => applyFilters(tickets, filters), [filters, tickets]);",
    )
    patched_tpl = patched_tpl.replace(
        "allTickets={TICKETS}",
        "allTickets={tickets}",
    )

    # Per-request data injection. The placeholders are pure ASCII so they
    # survive both JSON-encoding (template) and HTML-entity-encoding (srcdoc)
    # unchanged, letting serve_dashboard substitute with a single .replace().
    injection = (
        '<script id="sukabot-data">\n'
        f'window.__INJECTED_TICKETS = {DATA_PLACEHOLDER};\n'
        f'window.__SUBTOPIC_TO_TOPIC = {SUBMAP_PLACEHOLDER};\n'
        'if (window.__INJECTED_TICKETS && window.__INJECTED_TICKETS.length) {\n'
        '  window.__INJECTED_TICKETS.forEach(function(t){ t.created = new Date(t.created); });\n'
        '}\n'
        '</script>'
    )
    patched_tpl = patched_tpl.replace(
        '<body>\n<div id="root"></div>',
        f'<body>\n{injection}\n<div id="root"></div>',
        1,
    )

    # Escape "</" -> "<\/" so the JSON-encoded template can't prematurely
    # close the wrapping <script> tag once the iframe srcdoc is decoded.
    new_tpl_json = json.dumps(patched_tpl, ensure_ascii=False).replace("</", "<\\/")
    decoded = decoded[:content_start] + "\n" + new_tpl_json + "\n  " + decoded[content_end:]
    return decoded


_APPCS_IFRAME_RE = re.compile(
    r'(<iframe\s+id="app-cs"[^>]*\bsrcdoc=")([^"]*)(")',
    re.DOTALL,
)


def _build_patched_bundle() -> str:
    """Read the Sukabot Suite shell and apply structural patches to app-cs.

    Cached per UTC day so the date pivot stays current without re-parsing the
    7 MB suite on every request. The cached HTML still contains
    DATA_PLACEHOLDER / SUBMAP_PLACEHOLDER — those are substituted per request.
    """
    import datetime as _dt

    today = _dt.date.today().isoformat()
    if _PATCHED_BUNDLE_CACHE["date"] == today and _PATCHED_BUNDLE_CACHE["html"]:
        return _PATCHED_BUNDLE_CACHE["html"]

    with open(SUITE_HTML_PATH, "r", encoding="utf-8") as f:
        suite_html = f.read()

    m = _APPCS_IFRAME_RE.search(suite_html)
    if not m:
        logging.warning("app-cs iframe not found in %s — serving as-is", SUITE_HTML_PATH)
        _PATCHED_BUNDLE_CACHE.update(date=today, html=suite_html)
        return suite_html

    decoded = html.unescape(m.group(2))
    patched_decoded = _patch_appcs_srcdoc(decoded, today)
    re_encoded = html.escape(patched_decoded, quote=True)
    new_suite = suite_html[: m.start(2)] + re_encoded + suite_html[m.end(2):]

    _PATCHED_BUNDLE_CACHE.update(date=today, html=new_suite)
    logging.info("Sukabot Suite bundle patched (date pivot=%s)", today)
    return new_suite


def _encode_for_srcdoc_placeholder(json_text: str) -> str:
    """Encode a JSON literal so it can replace a placeholder inside the
    HTML-entity-encoded JSON-encoded template inside the iframe srcdoc.

    Two transforms: first emulate JSON-encoding as if embedded in a JSON
    string (so ``"`` becomes ``\\"``); then HTML-entity-encode (so ``\\"``
    becomes ``\\&quot;``). When the browser HTML-decodes the srcdoc and JSON
    parses the template, the JS source ends up with the original literal.
    """
    # Defensive: never let a value with </script> close our script tag.
    safe = json_text.replace("</", "<\\/")
    json_escaped = json.dumps(safe)[1:-1]  # strip the wrapping quotes
    return html.escape(json_escaped, quote=True)


@app.get("/")
def root():
    # The dashboard UI now lives in the Next.js app (port 3000).
    # Legacy HTML dashboards remain available at the /dashboard-* routes.
    return {
        "service": "esb-chatbot backend",
        "ui": "http://localhost:3000",
        "legacy_dashboards": [
            "/dashboard-suite",
            "/dashboard-raw",
            "/dashboard-legacy",
            "/dashboard-live",
        ],
    }


@app.get("/dashboard-suite", response_class=HTMLResponse)
async def serve_dashboard(db: Session = Depends(get_db)):
    """Serves the Sukabot Suite shell with real ticket data injected into the
    CS Chatbot Dashboard iframe (app-cs).

    The inner App() reads ``window.__INJECTED_TICKETS`` first and falls back
    to the bundle's mock TICKETS only when the array is empty. We inject the
    real data per request via a placeholder substitution against the cached
    daily-patched suite HTML.
    """
    suite_html = _build_patched_bundle()
    no_cache = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    try:
        tickets = db.query(Ticket).order_by(Ticket.created_at.desc()).all()
        bundle_data = [_ticket_to_bundle(t) for t in tickets]
    except Exception as e:
        logging.exception("Failed to load tickets for bundle injection: %s", e)
        bundle_data = []

    # subTopic → topic fallback map (covers slugs not in the bundle's
    # hardcoded TOPIC_TREE). Harmless if the new bundle doesn't consult it.
    sub_to_topic: dict[str, str] = {}
    for tk in bundle_data:
        sub = tk.get("subTopic")
        top = tk.get("topic")
        if sub and top and top != "Other" and sub not in sub_to_topic:
            sub_to_topic[sub] = top

    data_json = json.dumps(bundle_data, ensure_ascii=False, default=str)
    submap_json = json.dumps(sub_to_topic, ensure_ascii=False)

    final_html = (
        suite_html
        .replace(DATA_PLACEHOLDER, _encode_for_srcdoc_placeholder(data_json), 1)
        .replace(SUBMAP_PLACEHOLDER, _encode_for_srcdoc_placeholder(submap_json), 1)
    )

    return HTMLResponse(content=final_html, headers=no_cache)


@app.get("/dashboard-live", response_class=HTMLResponse)
async def serve_dashboard_live():
    """Backup: the hand-built Indonesian dashboard wired to /api/tickets."""
    with open("dashboard.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/dashboard-raw", response_class=HTMLResponse)
async def serve_dashboard_raw():
    """Sukabot Suite shell with no patches or data injection."""
    with open(SUITE_HTML_PATH, "r", encoding="utf-8") as f:
        return HTMLResponse(
            content=f.read(),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )


@app.get("/dashboard-legacy", response_class=HTMLResponse)
async def serve_dashboard_legacy():
    """Previous CS Chatbot Dashboard standalone bundle (pre-Sukabot Suite)."""
    with open(LEGACY_BUNDLE_PATH, "r", encoding="utf-8") as f:
        return HTMLResponse(
            content=f.read(),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )


def _ticket_to_dict(t: Ticket) -> dict:
    """Serialize a Ticket with attachments parsed as a list (not a JSON string).

    The dashboard expects ``attachments`` to be a list of
    ``{file_id, mime, size_bytes, kind, caption}`` dicts.
    """
    attachments: list = []
    if t.attachments:
        try:
            parsed = json.loads(t.attachments)
            if isinstance(parsed, list):
                attachments = parsed
        except (ValueError, TypeError):
            pass
    return {
        "id": t.id,
        "ticket_number": t.ticket_number,
        "name": t.name,
        "phone_number": t.phone_number,
        "company_name": t.company_name,
        "branch_name": t.branch_name,
        "issue_category": t.issue_category,
        "sub_topic": t.sub_topic,
        "issue_detail": t.issue_detail,
        "chat_history": t.chat_history,
        "steps_attempted": t.steps_attempted,
        "attachments": attachments,
        "status": t.status,
        "routed_queue": t.routed_queue,
        "chat_id": t.chat_id,
        "confidence_score": t.confidence_score,
        "priority": t.priority,
        "assignee": t.assignee,
        "assign_to": t.assign_to,
        "notes": [_note_to_dict(n) for n in t.notes],
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def _note_to_dict(n: TicketNote) -> dict:
    images: list = []
    if n.images:
        try:
            parsed = json.loads(n.images)
            if isinstance(parsed, list):
                images = parsed
        except (ValueError, TypeError):
            pass
    return {
        "id": n.id,
        "ticket_id": n.ticket_id,
        "type": n.type,
        "text": n.text,
        "author": n.author,
        "images": images,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


@app.get("/api/tickets")
def read_tickets(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    tickets = (
        db.query(Ticket)
        .order_by(Ticket.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return [_ticket_to_dict(t) for t in tickets]


def _csat_to_dict(c: CSATRating) -> dict:
    return {
        "id": c.id,
        "chat_id": c.chat_id,
        "rating": c.rating,
        "category": c.category,
        "sub_topic": c.sub_topic,
        "resolved_via": c.resolved_via,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@app.get("/api/csat")
def read_csat(skip: int = 0, limit: int = 500, db: Session = Depends(get_db)):
    """CSAT rating rows for the dashboard's CSAT metric (Phase 1).

    Backed by the ``csat_ratings`` table the agent writes on resolution. The CS
    dashboard joins these to tickets by ``chat_id`` to surface a real CSAT score.
    """
    rows = (
        db.query(CSATRating)
        .order_by(CSATRating.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return [_csat_to_dict(c) for c in rows]


@app.get("/api/csat/summary")
def read_csat_summary(db: Session = Depends(get_db)):
    """Aggregate CSAT: average rating (1 decimal) and number of ratings."""
    ratings = [c.rating for c in db.query(CSATRating.rating).all() if c.rating is not None]
    count = len(ratings)
    average = round(sum(ratings) / count, 1) if count else None
    return {"average": average, "count": count}


# Cache the Telegram file_path lookups in-process. Each entry: file_id -> path.
# Telegram file paths don't change for a given file_id, so caching is safe.
_TELEGRAM_FILE_CACHE: dict[str, str] = {}


@app.get("/api/attachments/{file_id}")
async def serve_attachment(file_id: str):
    """Proxy a Telegram attachment so the dashboard can render it.

    The Telegram file URL requires the bot token; we keep that server-side
    and stream the binary content back to the browser. file_path lookups
    are cached for the process lifetime.
    """
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "mock_token":
        raise HTTPException(status_code=503, detail="Telegram token not configured")

    file_path = _TELEGRAM_FILE_CACHE.get(file_id)
    if not file_path:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{TELEGRAM_API_URL}/getFile",
                params={"file_id": file_id},
            )
        data = r.json()
        if not data.get("ok"):
            raise HTTPException(
                status_code=404,
                detail=f"Telegram getFile failed: {data.get('description', 'unknown')}",
            )
        file_path = data["result"]["file_path"]
        _TELEGRAM_FILE_CACHE[file_id] = file_path

    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    async with httpx.AsyncClient(timeout=30) as client:
        upstream = await client.get(file_url)
    if upstream.status_code != 200:
        raise HTTPException(status_code=upstream.status_code, detail="upstream fetch failed")

    # Sniff content type from the file extension Telegram returns
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    mime_by_ext = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "webp": "image/webp",
        "mp4": "video/mp4", "mov": "video/quicktime",
    }
    content_type = mime_by_ext.get(ext, "application/octet-stream")

    from fastapi.responses import Response
    return Response(
        content=upstream.content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )

@app.post("/api/tickets")
def create_ticket(ticket_data: dict, db: Session = Depends(get_db)):
    new_ticket = Ticket(**ticket_data)
    db.add(new_ticket)
    db.commit()
    db.refresh(new_ticket)
    return new_ticket

@app.put("/api/tickets/{ticket_id}")
def update_ticket_status(ticket_id: int, status: str, db: Session = Depends(get_db)):
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ticket.status = status
    db.commit()
    return {"message": "Updated successfully"}


# Bundle's STATUSES = ['New', 'In Progress', 'Waiting', 'Resolved']; our DB
# uses Open / In Progress / Resolved / Closed. Map both directions. Waiting
# is now stored verbatim so it round-trips correctly through the kanban.
_UI_TO_DB_STATUS = {
    "New": "Open",
    "In Progress": "In Progress",
    "Waiting": "Waiting",
    "Escalated": "Escalated",
    "Resolved": "Resolved",
}


_RESOLVED_STATES = ("Resolved", "Closed")


def _telegram_enabled() -> bool:
    return bool(TELEGRAM_TOKEN) and TELEGRAM_TOKEN != "mock_token"


def _notify_ticket_resolved(ticket: Ticket, background_tasks: BackgroundTasks) -> bool:
    """DM the merchant on Telegram that their ticket is solved.

    Skipped (returns False) for web-chat tickets (chat_id like 'web:<uuid>'),
    missing/non-numeric chat ids, or when no real bot token is configured.
    """
    chat_id = (ticket.chat_id or "").strip()
    if not chat_id or chat_id.startswith("web:") or not chat_id.lstrip("-").isdigit():
        return False
    if not _telegram_enabled():
        return False
    label = ticket.ticket_number or f"#{ticket.id}"
    text = (
        f"Halo! Kabar baik — tiket Anda {label} sudah SELESAI ditangani. ✅\n\n"
        "Kendala yang Anda laporkan telah kami selesaikan. Jika masih ada "
        "kendala atau pertanyaan lanjutan, silakan balas pesan ini ya. "
        "Terima kasih sudah menghubungi ESB Order Support! 🙏"
    )
    background_tasks.add_task(
        send_telegram_message, int(chat_id), {"type": "message", "text": text}
    )
    return True


@app.post("/api/tickets/{ticket_id}/status")
async def set_ticket_status(
    ticket_id: int, payload: dict, background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Kanban drop target — accepts {"status": "<ui status>"} and persists.

    When a ticket transitions INTO a resolved state, the merchant who opened it
    is notified on Telegram that the issue is solved.
    """
    ui_status = (payload or {}).get("status")
    if not ui_status:
        raise HTTPException(status_code=400, detail="status is required")
    db_status = _UI_TO_DB_STATUS.get(ui_status, ui_status)
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    prev_status = ticket.status
    ticket.status = db_status
    db.commit()

    # Only notify on the transition into resolved (not on repeated drops).
    notified = False
    if db_status in _RESOLVED_STATES and prev_status not in _RESOLVED_STATES:
        notified = _notify_ticket_resolved(ticket, background_tasks)

    return {"id": ticket_id, "ui_status": ui_status, "db_status": db_status, "merchant_notified": notified}


def _get_ticket_or_404(ticket_id: int, db: Session) -> Ticket:
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


@app.post("/api/tickets/{ticket_id}/assign")
def assign_ticket(ticket_id: int, payload: dict, db: Session = Depends(get_db)):
    """Set the current handler. Body: {"assignee": "CC - Ayu Rahayu"}."""
    ticket = _get_ticket_or_404(ticket_id, db)
    ticket.assignee = (payload or {}).get("assignee")
    db.commit()
    return _ticket_to_dict(ticket)


@app.post("/api/tickets/{ticket_id}/escalate")
def escalate_ticket(ticket_id: int, payload: dict, db: Session = Depends(get_db)):
    """Hand the ticket to an escalation target. Body: {"assign_to": "DEV - Immanuel"}."""
    ticket = _get_ticket_or_404(ticket_id, db)
    ticket.assign_to = (payload or {}).get("assign_to")
    db.commit()
    return _ticket_to_dict(ticket)


@app.get("/api/tickets/{ticket_id}/notes")
def list_ticket_notes(ticket_id: int, db: Session = Depends(get_db)):
    ticket = _get_ticket_or_404(ticket_id, db)
    return [_note_to_dict(n) for n in ticket.notes]


@app.post("/api/tickets/{ticket_id}/notes")
def add_ticket_note(ticket_id: int, payload: dict, db: Session = Depends(get_db)):
    """Append a resolution note. Body: {type, text, author, images?}."""
    _get_ticket_or_404(ticket_id, db)
    payload = payload or {}
    if not (payload.get("text") or "").strip():
        raise HTTPException(status_code=400, detail="text is required")
    images = payload.get("images")
    note = TicketNote(
        ticket_id=ticket_id,
        type=payload.get("type") or "IN PROGRESS",
        text=payload["text"].strip(),
        author=payload.get("author") or "Unknown",
        images=json.dumps(images) if isinstance(images, list) else None,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return _note_to_dict(note)


@app.put("/api/tickets/{ticket_id}/notes/{note_id}")
def edit_ticket_note(ticket_id: int, note_id: int, payload: dict, db: Session = Depends(get_db)):
    note = (
        db.query(TicketNote)
        .filter(TicketNote.id == note_id, TicketNote.ticket_id == ticket_id)
        .first()
    )
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    payload = payload or {}
    if "text" in payload:
        note.text = (payload["text"] or "").strip()
    if "type" in payload:
        note.type = payload["type"]
    if "images" in payload:
        images = payload["images"]
        note.images = json.dumps(images) if isinstance(images, list) else None
    db.commit()
    db.refresh(note)
    return _note_to_dict(note)


@app.get("/api/agents/workload")
def agents_workload(db: Session = Depends(get_db)):
    """Active/resolved ticket counts per handler (assignee, else routed_queue).

    "Active" = not Resolved/Closed. Sorted by total desc. Powers the
    AgentWorkloadDrawer.
    """
    rows: dict[str, dict] = {}
    for t in db.query(Ticket).all():
        agent = t.assignee or t.routed_queue
        if not agent:
            continue
        bucket = rows.setdefault(agent, {"agent": agent, "active": 0, "resolved": 0})
        if t.status in ("Resolved", "Closed"):
            bucket["resolved"] += 1
        else:
            bucket["active"] += 1
    return sorted(
        rows.values(), key=lambda r: r["active"] + r["resolved"], reverse=True
    )


# --- Web chat (Sukalapor, Phase 3) -----------------------------------------
# Reuse the same conversational agent that drives the Telegram bot, but with a
# web-namespaced session id so web sessions never collide with Telegram chat ids.

def _web_session_id(session_id: str) -> str:
    return f"web:{session_id}"


@app.post("/api/chat")
def web_chat(payload: dict, db: Session = Depends(get_db)):
    """Drive one turn of the support agent for the web client.

    Body: {session_id, message}. Returns {messages:[{type,text,options?}],
    ticket_id?} — the bubble adapter (lib/chat.ts) renders `messages`.
    """
    payload = payload or {}
    session_id = payload.get("session_id")
    message = payload.get("message", "")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    response = process_message(_web_session_id(session_id), message)

    ticket_id = None
    if response.get("type") == "ticket_form":
        # Persist the ticket exactly like the Telegram path does.
        ticket = _persist_ticket_from_response(response, _web_session_id(session_id), db)
        ticket_id = ticket.id

    msg = {"type": response.get("type", "message"), "text": response.get("text", "")}
    if response.get("options"):
        msg["options"] = response["options"]
    return {"messages": [msg], "ticket_id": ticket_id}


@app.post("/api/chat/reset")
def web_chat_reset(payload: dict):
    """Clear a web chat session ("Mulai ulang chat")."""
    payload = payload or {}
    session_id = payload.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    SESSION_STATE.pop(_web_session_id(session_id), None)
    return {"ok": True}


@app.delete("/api/tickets/{ticket_id}")
def delete_ticket(ticket_id: int, db: Session = Depends(get_db)):
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    db.delete(ticket)
    db.commit()
    return {"message": "Deleted successfully"}

def _persist_ticket_from_response(response: dict, chat_id: str, db: Session) -> Ticket:
    """Create a Ticket row from an agent ``ticket_form`` response.

    Shared by the Telegram webhook and the web /api/chat endpoint so both
    channels persist tickets identically (PRD US4).
    """
    t = Ticket(
        ticket_number=response["ticket_number"],
        name=response.get("name") or "",
        phone_number=response.get("phone") or "",
        company_name=response.get("company") or "",
        branch_name=response.get("branch") or "",
        chat_id=str(chat_id),
        issue_category=response.get("category", "Unknown"),
        sub_topic=response.get("sub_topic", ""),
        issue_detail=response.get("issue_detail", ""),
        chat_history=response.get("chat_history", ""),
        steps_attempted=response.get("steps_attempted", ""),
        attachments=json.dumps(response.get("attachments", [])),
        routed_queue=response.get("routed_queue", ""),
        confidence_score=response.get("confidence"),
        status="Open",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# --- Telegram Webhook Endpoint ---
def send_telegram_message(chat_id: int, response: dict, user_info: dict | None = None):
    if response["type"] == "message":
        payload = {"chat_id": chat_id, "text": response["text"]}
        httpx.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)
    elif response["type"] == "question":
        # Inline keyboard for Yes/No (or low-confidence candidate picks).
        keyboard = [[{"text": opt, "callback_data": opt}] for opt in response["options"]]
        payload = {
            "chat_id": chat_id,
            "text": response["text"],
            "reply_markup": {"inline_keyboard": keyboard}
        }
        httpx.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)
    elif response["type"] == "ticket_form":
        # PRD US4 — persist the ticket with manual + auto-filled fields.
        db = SessionLocal()
        try:
            _persist_ticket_from_response(response, str(chat_id), db)
        finally:
            db.close()
        httpx.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            json={"chat_id": chat_id, "text": response["text"]},
        )

def _extract_attachment(message: dict) -> dict | None:
    """Pull a single attachment from a Telegram message.

    Returns ``{file_id, mime, size_bytes, kind, caption}`` or ``None`` if the
    message has no supported media. Telegram sends ``photo`` as a list of
    resolutions — we keep the largest. Documents are also accepted when their
    MIME type matches the PRD-allowed set.
    """
    if "photo" in message and message["photo"]:
        photo = max(message["photo"], key=lambda p: p.get("file_size") or 0)
        return {
            "file_id": photo.get("file_id"),
            "mime": "image/jpeg",  # Telegram re-encodes uploaded photos to JPEG
            "size_bytes": photo.get("file_size") or 0,
            "kind": "photo",
            "caption": message.get("caption", ""),
        }
    if "video" in message and message["video"]:
        video = message["video"]
        return {
            "file_id": video.get("file_id"),
            "mime": video.get("mime_type") or "video/mp4",
            "size_bytes": video.get("file_size") or 0,
            "kind": "video",
            "caption": message.get("caption", ""),
        }
    if "document" in message and message["document"]:
        doc = message["document"]
        mime = doc.get("mime_type") or ""
        if mime in ALLOWED_IMAGE_MIME or mime in ALLOWED_VIDEO_MIME:
            return {
                "file_id": doc.get("file_id"),
                "mime": mime,
                "size_bytes": doc.get("file_size") or 0,
                "kind": "document",
                "caption": message.get("caption", ""),
            }
    return None


def _validate_attachment(att: dict, session_attachments: list[dict]) -> str | None:
    """Return an error message if the attachment violates PRD US3 rules, else None."""
    if len(session_attachments) >= MAX_ATTACHMENTS_PER_SESSION:
        return (
            f"Maaf, maksimum {MAX_ATTACHMENTS_PER_SESSION} lampiran per sesi. "
            f"Mohon lanjutkan tanpa lampiran tambahan."
        )
    mime = att.get("mime", "")
    if mime not in ALLOWED_IMAGE_MIME and mime not in ALLOWED_VIDEO_MIME:
        return (
            "Format file tidak didukung. Mohon kirim JPG, PNG, WEBP, MP4, "
            "atau MOV saja."
        )
    if att.get("size_bytes", 0) > MAX_FILE_BYTES:
        return "Ukuran file melebihi 15 MB. Mohon kirim file yang lebih kecil."
    return None


# ── Idle session reaper (proactive check-in + graceful auto-close) ───────────
# Telegram is one-directional from the customer's side — we only hear from them
# on /webhook. To "ask if they're still there" and later close the chat, a
# background loop scans the in-memory sessions and pushes messages out of band.
REAPER_INTERVAL_SECONDS = 20  # how often to scan for idle sessions


async def _send_async(chat_id: int, text: str) -> None:
    """Fire a plain Telegram message from the async reaper without blocking the
    event loop (send_telegram_message is sync httpx)."""
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "mock_token":
        return  # no real token — nothing to deliver (e.g. local/dev)
    try:
        await asyncio.to_thread(send_telegram_message, chat_id, {"type": "message", "text": text})
    except Exception as e:  # never let a send failure kill the reaper
        logging.warning("reaper send to %s failed: %s", chat_id, e)


async def _reap_idle_sessions() -> None:
    """One pass: nudge sessions that have gone quiet, close the ones that stayed
    quiet after the nudge. Web sessions (no push channel) are skipped."""
    now = time.time()
    for chat_id, session in list(SESSION_STATE.items()):
        if not isinstance(chat_id, str) or chat_id.startswith("web:"):
            continue
        action = idle_action(session, now)
        if action == "prompt":
            session["followup_prompted"] = True
            session["followup_prompted_at"] = now
            msg = (
                "Apakah masih ada yang bisa kami bantu? 🙏 Jika tidak ada balasan "
                "beberapa saat lagi, sesi ini akan saya tutup. Anda bisa mulai lagi "
                "kapan saja dengan mengirim pesan."
            )
            _record_turn(session, "assistant", msg)
            await _send_async(int(chat_id), msg)
        elif action == "close":
            msg = (
                "Sesi saya tutup dulu ya karena tidak ada balasan. Terima kasih sudah "
                "menghubungi ESB Order 🙏 Kirim pesan kapan saja jika ada kendala lain."
            )
            await _send_async(int(chat_id), msg)
            # Fresh session => the NEXT message is treated as a brand-new issue.
            SESSION_STATE[chat_id] = _fresh_session()


async def _session_reaper_loop() -> None:
    while True:
        try:
            await asyncio.sleep(REAPER_INTERVAL_SECONDS)
            await _reap_idle_sessions()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.warning("session reaper pass failed: %s", e)


# Out-of-band idle-session messages — shared by the scheduled /reap pass.
FOLLOWUP_PROMPT_TEXT = (
    "Apakah masih ada yang bisa kami bantu? 🙏 Jika tidak ada balasan "
    "beberapa saat lagi, sesi ini akan saya tutup. Anda bisa mulai lagi "
    "kapan saja dengan mengirim pesan."
)
SESSION_CLOSE_TEXT = (
    "Sesi saya tutup dulu ya karena tidak ada balasan. Terima kasih sudah "
    "menghubungi ESB Order 🙏 Kirim pesan kapan saja jika ada kendala lain."
)


def _epoch(dt):
    """Convert a naive-UTC DateTime column to epoch seconds (matches time.time())."""
    if dt is None:
        return None
    return dt.replace(tzinfo=datetime.timezone.utc).timestamp()


def _touch_session_liveness(chat_id: str) -> None:
    """Record a customer turn for the DB-backed reaper: bump last_activity, mark
    the session active, and clear any pending follow-up (a reply means they're
    not idle). Best-effort: never let a DB hiccup break the webhook. Web
    sessions have no push channel, so they're skipped."""
    if chat_id.startswith("web:"):
        return
    db = SessionLocal()
    try:
        row = db.get(ChatSession, chat_id)
        if row is None:
            row = ChatSession(chat_id=chat_id)
            db.add(row)
        row.last_activity = datetime.datetime.utcnow()
        row.has_history = True
        row.followup_prompted = False
        row.followup_prompted_at = None
        db.commit()
    except Exception as e:  # never break message handling over liveness tracking
        logging.warning("session liveness upsert failed for %s: %s", chat_id, e)
        db.rollback()
    finally:
        db.close()


def _maybe_send_calming(chat_id, text, user_info, background_tasks) -> None:
    """Customer-facing de-escalation: if the incoming message reads as furious,
    queue one empathetic, calming reply AHEAD of the normal bot response.

    Sent at most once per session (tracked on ``session["deescalated"]``) so we
    don't repeat ourselves at every angry turn; a fresh session (/start or idle
    reset) re-arms it. Background tasks run in FIFO order, so adding this before
    the main response guarantees the customer sees the calming note first.
    """
    if not text:
        return
    raw = text.strip()
    if not raw or raw.startswith("/"):  # skip commands like /start, /help
        return
    session = SESSION_STATE.setdefault(str(chat_id), _fresh_session())
    if session.get("deescalated") or not detect_anger(raw):
        return
    session["deescalated"] = True
    msg = calming_message()
    _record_turn(session, "assistant", msg)
    background_tasks.add_task(
        send_telegram_message, chat_id, {"type": "message", "text": msg}, user_info,
    )


@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receives Telegram updates and dispatches them to the agent."""
    if TELEGRAM_WEBHOOK_SECRET:
        sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if sent != TELEGRAM_WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="invalid webhook secret")

    update = await request.json()
    logging.info("Telegram update: %s", update)

    user_info = _telegram_user(update)
    chat_id: int | None = None
    text: str | None = None
    attachment: dict | None = None

    if "message" in update:
        message = update["message"]
        chat_id = message["chat"]["id"]
        text = message.get("text")
        attachment = _extract_attachment(message)
    elif "callback_query" in update:
        chat_id = update["callback_query"]["message"]["chat"]["id"]
        text = update["callback_query"]["data"]

    if chat_id is None:
        return {"status": "ok"}

    # Mirror liveness to the DB so the scheduled /reap can nudge/close this
    # session even after in-memory SESSION_STATE is lost on instance recycle.
    _touch_session_liveness(str(chat_id))

    # --- Attachment-only path (US3 / AC1.12) -------------------------------
    if attachment and not text:
        session = SESSION_STATE.setdefault(str(chat_id), _fresh_session())
        err = _validate_attachment(attachment, session.get("attachments", []))
        if err:
            background_tasks.add_task(
                send_telegram_message, chat_id, {"type": "message", "text": err}, user_info,
            )
            return {"status": "ok"}
        session.setdefault("attachments", []).append(attachment)
        # AC3.4 / AC1.12 — image without text: ack and request a description.
        caption = (attachment.get("caption") or "").strip()
        if caption:
            _maybe_send_calming(chat_id, caption, user_info, background_tasks)
            response = process_message(str(chat_id), caption)
        else:
            ack = (
                "Saya sudah menerima lampiran Anda. Mohon jelaskan kendala "
                "yang sedang Anda alami agar saya bisa membantu."
            )
            _record_turn(session, "assistant", ack)
            response = {"type": "message", "text": ack}
        background_tasks.add_task(send_telegram_message, chat_id, response, user_info)
        return {"status": "ok"}

    # --- Mixed text + attachment ------------------------------------------
    if attachment and text:
        session = SESSION_STATE.setdefault(str(chat_id), _fresh_session())
        err = _validate_attachment(attachment, session.get("attachments", []))
        if err:
            background_tasks.add_task(
                send_telegram_message, chat_id, {"type": "message", "text": err}, user_info,
            )
            return {"status": "ok"}
        session.setdefault("attachments", []).append(attachment)

    if text:
        _maybe_send_calming(chat_id, text, user_info, background_tasks)
        response = process_message(str(chat_id), text)
        background_tasks.add_task(send_telegram_message, chat_id, response, user_info)

    return {"status": "ok"}


@app.post("/reap")
def reap_idle_sessions(request: Request, db: Session = Depends(get_db)):
    """Scheduled idle-session sweep (called every minute by Cloud Scheduler).

    Reads liveness rows from chat_sessions, reuses the pure agent.idle_action()
    to decide, and pushes the nudge/close Telegram message out of band. Replaces
    the old in-memory reaper so it works under Cloud Run scale-to-zero.
    """
    if not REAP_SECRET or request.headers.get("X-Reap-Secret", "") != REAP_SECRET:
        raise HTTPException(status_code=403, detail="invalid reap secret")

    now = time.time()
    prompted = 0
    closed = 0
    rows = db.query(ChatSession).filter(ChatSession.has_history.is_(True)).all()
    for row in rows:
        pseudo = {
            "chat_history": [1] if row.has_history else [],
            "last_activity": _epoch(row.last_activity) or now,
            "followup_prompted": bool(row.followup_prompted),
            "followup_prompted_at": _epoch(row.followup_prompted_at) or 0.0,
        }
        action = idle_action(pseudo, now)
        try:
            if action == "prompt":
                send_telegram_message(int(row.chat_id), {"type": "message", "text": FOLLOWUP_PROMPT_TEXT})
                row.followup_prompted = True
                row.followup_prompted_at = datetime.datetime.utcnow()
                db.commit()
                mem = SESSION_STATE.get(row.chat_id)
                if mem is not None:
                    mem["followup_prompted"] = True
                    mem["followup_prompted_at"] = now
                prompted += 1
            elif action == "close":
                send_telegram_message(int(row.chat_id), {"type": "message", "text": SESSION_CLOSE_TEXT})
                if row.chat_id in SESSION_STATE:
                    SESSION_STATE[row.chat_id] = _fresh_session()
                db.delete(row)
                db.commit()
                closed += 1
        except Exception as e:  # one bad row must not abort the whole pass
            logging.warning("reap failed for %s: %s", row.chat_id, e)
            db.rollback()

    return {"prompted": prompted, "closed": closed}
