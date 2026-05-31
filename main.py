import config  # noqa: F401 — loads .env before any os.getenv reads

from fastapi import FastAPI, Request, Depends, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session
import json
import logging
import httpx
import os

from database import engine, SessionLocal, Base, Ticket
from agent import process_message, SESSION_STATE, _record_turn, _fresh_session
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

app = FastAPI(title="AI Chatbot Backend Phase 1")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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


# The bundle's OverviewView, BotView, and Filter code use
# `new Date('2026-05-22T...')` as their reference "now" for the 30-day window.
# Real tickets dated after that fall outside the chart range, so the line
# chart appears empty. We patch the constant in each JS asset once at startup
# (or whenever the date rolls over) and cache the rebuilt HTML.
_PATCHED_BUNDLE_CACHE: dict = {"date": None, "html": None}


def _build_patched_bundle() -> str:
    """Return the standalone HTML with all hardcoded reference dates rewritten to today."""
    import base64
    import datetime as _dt
    import gzip
    import re as _re

    today = _dt.date.today().isoformat()
    if _PATCHED_BUNDLE_CACHE["date"] == today and _PATCHED_BUNDLE_CACHE["html"]:
        return _PATCHED_BUNDLE_CACHE["html"]

    with open("CS Chatbot Dashboard _standalone_.html", "r", encoding="utf-8") as f:
        html = f.read()

    m = _re.search(
        r'(<script type="__bundler/manifest">\s*)(.*?)(\s*</script>)',
        html,
        _re.DOTALL,
    )
    if not m:
        _PATCHED_BUNDLE_CACHE.update(date=today, html=html)
        return html

    manifest = json.loads(m.group(2))
    new_t00 = f"{today}T00:00:00"
    new_t12 = f"{today}T12:00:00"

    patched_any = False
    for uuid, entry in manifest.items():
        mime = entry.get("mime", "")
        if not mime.endswith("javascript"):
            continue
        raw_bytes = base64.b64decode(entry["data"])
        was_compressed = bool(entry.get("compressed"))
        if was_compressed:
            try:
                raw_bytes = gzip.decompress(raw_bytes)
            except Exception:
                continue
        try:
            src = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue
        needs_patch = (
            "2026-05-22" in src
            or "function buildTranscript" in src
            or "TOPIC_TREE[tp].includes(sub)" in src
            or "function EscalationView" in src
        )
        if not needs_patch:
            continue
        patched = (
            src.replace("2026-05-22T00:00:00", new_t00)
               .replace("2026-05-22T12:00:00", new_t12)
        )
        # Bundle's buildTranscript() synthesizes a fake English chat. Replace
        # the English strings with Indonesian and prefer the real transcript
        # we inject onto each ticket.
        patched = patched.replace(
            "function buildTranscript(t) {\n  const messages = [",
            "function buildTranscript(t) {\n"
            "  if (t.transcript && t.transcript.length) return t.transcript;\n"
            "  const messages = [",
        )
        patched = patched.replace(
            "I can help with that! Here's how: navigate to Settings → ${t.topic} → "
            "${t.subTopic.replace(/_/g, ' ')}. Let me know if you need more details.",
            "Saya bisa bantu! Buka Pengaturan → ${t.topic} → "
            "${t.subTopic.replace(/_/g, ' ')}. Beri tahu saya jika butuh detail lebih lanjut.",
        )
        patched = patched.replace(
            "I'm having trouble understanding. Let me connect you with a CS agent.",
            "Maaf, saya kurang paham. Saya akan menghubungkan Anda dengan agen CS.",
        )
        patched = patched.replace(
            "Yes please, I need help with this.",
            "Iya tolong, saya butuh bantuan untuk ini.",
        )
        patched = patched.replace(
            "Connecting you to ${t.agent || 'an agent'} now. They will respond shortly.",
            "Menghubungkan Anda ke ${t.agent || 'agen'} sekarang. Mereka akan segera membalas.",
        )
        patched = patched.replace(
            "Thanks, waiting...",
            "Terima kasih, ditunggu...",
        )
        # TopicView's "Top 10 Sub-Topics" table reverse-looks-up each sub-topic
        # in the hardcoded TOPIC_TREE. Our injected sub-topic slugs aren't in
        # that list, so they all bucket as "Other". Consult our injected map
        # before giving up.
        patched = patched.replace(
            "topic: TOPICS.find(tp => TOPIC_TREE[tp].includes(sub)) || 'Other',",
            "topic: TOPICS.find(tp => TOPIC_TREE[tp].includes(sub)) "
            "|| (window.__SUBTOPIC_TO_TOPIC && window.__SUBTOPIC_TO_TOPIC[sub]) "
            "|| 'Other',",
        )
        # EscalationView Status Board — make cards draggable between columns.
        # Cards POST /api/tickets/{dbId}/status on drop; on success the page
        # reloads so the Kanban (and KPIs) reflect the new state.
        patched = patched.replace(
            "<div key={tk.id} onClick={() => onDrill(`${tk.id}`, `${tk.user}`, [tk])}",
            "<div key={tk.id} draggable={true}"
            " onDragStart={e => { e.dataTransfer.setData('text/plain', String(tk.dbId || '')); e.dataTransfer.effectAllowed = 'move'; e.currentTarget.style.opacity = '0.45'; }}"
            " onDragEnd={e => { e.currentTarget.style.opacity = '1'; }}"
            " onClick={() => onDrill(`${tk.id}`, `${tk.user}`, [tk])}",
        )
        patched = patched.replace(
            "<div key={col.status} style={{ background:t.surface2, borderRadius:10",
            "<div key={col.status}"
            " onDragOver={e => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; e.currentTarget.style.outline = '2px dashed ' + t.accent; e.currentTarget.style.outlineOffset = '-2px'; }}"
            " onDragLeave={e => { e.currentTarget.style.outline = 'none'; }}"
            " onDrop={async e => {"
            "   e.preventDefault();"
            "   e.currentTarget.style.outline = 'none';"
            "   const dbId = e.dataTransfer.getData('text/plain');"
            "   if (!dbId) return;"
            "   const moved = allTickets.find(x => String(x.dbId) === String(dbId));"
            "   if (!moved || moved.status === col.status) return;"
            "   const newStatus = col.status;"
            "   if (typeof window.__setTickets === 'function') {"
            "     window.__setTickets(prev => prev.map(x => String(x.dbId) === String(dbId) ? Object.assign({}, x, {status: newStatus}) : x));"
            "   }"
            "   try {"
            "     const r = await fetch('/api/tickets/' + encodeURIComponent(dbId) + '/status', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({status: newStatus}) });"
            "     if (!r.ok) throw new Error('HTTP ' + r.status);"
            "   } catch (err) {"
            "     console.error('Status update failed', err);"
            "     alert('Gagal memperbarui status tiket: ' + err.message);"
            "     if (typeof window.__setTickets === 'function') {"
            "       const prevStatus = moved.status;"
            "       window.__setTickets(prev => prev.map(x => String(x.dbId) === String(dbId) ? Object.assign({}, x, {status: prevStatus}) : x));"
            "     }"
            "   }"
            " }}"
            " style={{ background:t.surface2, borderRadius:10",
        )
        out = patched.encode("utf-8")
        if was_compressed:
            out = gzip.compress(out)
        entry["data"] = base64.b64encode(out).decode("ascii")
        patched_any = True

    if not patched_any:
        _PATCHED_BUNDLE_CACHE.update(date=today, html=html)
        return html

    new_manifest = json.dumps(manifest, separators=(",", ":"))
    rebuilt = html[: m.start(2)] + new_manifest + html[m.end(2) :]

    # Patch the inline App component (lives in the bundler/template JSON blob)
    # to put TICKETS into useState and expose the setter on window. Without
    # this hook, the kanban drop handler would have to full-reload the page to
    # see status changes.
    #
    # Note: we don't use re.search here — the template's JSON-encoded content
    # contains literal "</script>" substrings (json.dumps doesn't escape "/"),
    # which would terminate a non-greedy regex too early. Anchor instead to
    # the file's final </script>.
    tmpl_open = '<script type="__bundler/template">'
    tmpl_start = rebuilt.find(tmpl_open)
    if tmpl_start != -1:
        content_start = tmpl_start + len(tmpl_open)
        content_end = rebuilt.rfind("</script>")
        if content_end > content_start:
            json_blob = rebuilt[content_start:content_end].strip()
            try:
                template_html = json.loads(json_blob)
            except json.JSONDecodeError:
                template_html = None
            if template_html and "function App()" in template_html:
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
                    "  React.useEffect(() => { window.__setTickets = setTickets; "
                    "return () => { if (window.__setTickets === setTickets) delete window.__setTickets; }; }, []);\n"
                    "  const [active, setActive] = useState('overview');"
                )
                patched_tpl = template_html.replace(old_app_open, new_app_open)
                patched_tpl = patched_tpl.replace(
                    "const filteredTickets = useMemo(() => applyFilters(TICKETS, filters), [filters]);",
                    "const filteredTickets = useMemo(() => applyFilters(tickets, filters), [filters, tickets]);",
                )
                patched_tpl = patched_tpl.replace(
                    "<View tickets={filteredTickets} allTickets={TICKETS} onDrill={handleDrill} />",
                    "<View tickets={filteredTickets} allTickets={tickets} onDrill={handleDrill} />",
                )
                if patched_tpl != template_html:
                    # Escape "</" -> "<\/" so the JSON-encoded template can't
                    # prematurely close the surrounding <script> tag. The
                    # original bundle stored these as </ unicode escapes;
                    # json.dumps emits them as literal "</", which the HTML
                    # parser would see as the end of the wrapper script.
                    new_tpl_json = json.dumps(patched_tpl).replace("</", "<\\/")
                    rebuilt = (
                        rebuilt[:content_start]
                        + "\n"
                        + new_tpl_json
                        + "\n  "
                        + rebuilt[content_end:]
                    )

    _PATCHED_BUNDLE_CACHE.update(date=today, html=rebuilt)
    logging.info("Patched bundle reference date to %s (cache rebuilt)", today)
    return rebuilt


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(db: Session = Depends(get_db)):
    """Serves the standalone CS Chatbot Dashboard bundle with REAL ticket data injected.

    The React bundle reads ``window.__INJECTED_TICKETS`` first and falls back
    to its mock generator only when that's missing. We inject before the
    bundler's loader runs (which fires on DOMContentLoaded); the loader uses
    ``documentElement.replaceWith`` rather than ``document.open()``, so our
    ``window`` mutation survives the swap and the inner React app sees it.

    We also rewrite the bundle's hardcoded "today" (2026-05-22) to the real
    current date so the 30-day line chart shows real tickets.
    """
    html = _build_patched_bundle()
    # Bundle structure changes whenever we tweak the patcher. Disable browser
    # caching so a stale copy doesn't render against a fresh manifest.
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

    # Derive a subTopic -> topic map from the real tickets. The bundle's
    # TopicView falls back to a hardcoded TOPIC_TREE for reverse lookup, which
    # doesn't contain our sub-topic slugs — every real sub-topic ends up
    # bucketed as "Other" without this override.
    sub_to_topic: dict[str, str] = {}
    for tk in bundle_data:
        sub = tk.get("subTopic")
        top = tk.get("topic")
        if sub and top and top != "Other" and sub not in sub_to_topic:
            sub_to_topic[sub] = top

    data_json = json.dumps(bundle_data, ensure_ascii=False, default=str)
    sub_map_json = json.dumps(sub_to_topic, ensure_ascii=False)
    safe = data_json.replace("</", "<\\/")  # never close the script prematurely
    safe_map = sub_map_json.replace("</", "<\\/")
    injection = (
        "<script>(function(){"
        f"var raw = {safe};"
        "raw.forEach(function(t){ t.created = new Date(t.created); });"
        "window.__INJECTED_TICKETS = raw;"
        f"window.__SUBTOPIC_TO_TOPIC = {safe_map};"
        "})();</script>"
    )

    # Insert immediately after the opening <head> tag so it runs before any
    # other script in the page.
    import re as _re
    m = _re.search(r"<head[^>]*>", html, _re.IGNORECASE)
    if not m:
        return HTMLResponse(content=html)  # malformed; serve as-is
    insert_at = m.end()
    return HTMLResponse(
        html[:insert_at] + injection + html[insert_at:],
        headers=no_cache,
    )


@app.get("/dashboard-live", response_class=HTMLResponse)
async def serve_dashboard_live():
    """Backup: the hand-built Indonesian dashboard wired to /api/tickets."""
    with open("dashboard.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/dashboard-raw", response_class=HTMLResponse)
async def serve_dashboard_raw():
    """Original unpatched standalone bundle — for diagnosing whether 404 noise
    is caused by our patches or by the bundle itself."""
    with open("CS Chatbot Dashboard _standalone_.html", "r", encoding="utf-8") as f:
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
        "created_at": t.created_at.isoformat() if t.created_at else None,
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
    "Resolved": "Resolved",
}


@app.post("/api/tickets/{ticket_id}/status")
async def set_ticket_status(
    ticket_id: int, payload: dict, db: Session = Depends(get_db)
):
    """Kanban drop target — accepts {"status": "<ui status>"} and persists."""
    ui_status = (payload or {}).get("status")
    if not ui_status:
        raise HTTPException(status_code=400, detail="status is required")
    db_status = _UI_TO_DB_STATUS.get(ui_status, ui_status)
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ticket.status = db_status
    db.commit()
    return {"id": ticket_id, "ui_status": ui_status, "db_status": db_status}

@app.delete("/api/tickets/{ticket_id}")
def delete_ticket(ticket_id: int, db: Session = Depends(get_db)):
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    db.delete(ticket)
    db.commit()
    return {"message": "Deleted successfully"}

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
        attachments_json = json.dumps(response.get("attachments", []))
        db = SessionLocal()
        try:
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
                attachments=attachments_json,
                routed_queue=response.get("routed_queue", ""),
                confidence_score=response.get("confidence"),
                status="Open",
            )
            db.add(t)
            db.commit()
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
        response = process_message(str(chat_id), text)
        background_tasks.add_task(send_telegram_message, chat_id, response, user_info)

    return {"status": "ok"}
