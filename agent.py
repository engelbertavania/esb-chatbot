"""Agent for the ESB Order Phase-1 support chatbot.

Implements the conversation state machine required by PRD HACKATOWNJUARA001
Phase 1 — Foundation. Key behaviours:

* US1 (issue identification) — classify into one of 10 MVP categories with a
  70% confidence floor (AC1.9); offer 2-3 candidate categories when below the
  floor (AC1.9.1); reject out-of-scope queries (AC1.11).
* US2 (guided troubleshooting) — present steps SEQUENTIALLY, one at a time,
  with Yes/No after each (AC2.1, AC2.2). Stop after 3 consecutive No
  responses (AC2.3) and offer escalation.
* US4 (self-service ticket creation) — collect Name/Phone/Company/Branch via
  multi-turn prompts (manual fields), auto-fill Issue Category and Issue
  Detail, attach read-only Chat History / Steps Attempted / Attachments,
  generate ticket number in PRD format ``Ticket #YY######``, route to the
  correct support queue (AC4.4).
* 30-minute idle session timeout (AC1.14) with auto-closing message.
"""
import config  # noqa: F401 — loads .env before any os.getenv reads

from langchain_google_vertexai import ChatVertexAI
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

import logging
import os
import random
import re
import time
from datetime import datetime
from functools import lru_cache
from typing import Optional

from rag import retrieve_troubleshooting
from content_architecture import (
    load_ca,
    match_ca,
    format_response,
    entries_in_category,
    find_by_predefined,
    list_categories,
    MODEL_NAME,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PRD Phase-1 taxonomy — the 10 MVP issue categories
# ---------------------------------------------------------------------------
# Verbatim from PRD US1 "MVP Issue Categories". Order matters only for the
# low-confidence suggestion UI; the classifier must pick exactly one.
MVP_CATEGORIES: list[str] = [
    "ESO Activation / Deactivation",
    "Order Issues",
    "Payment Gateway Setup",
    "Menu Image Upload",
    "Menu Issues",
    "Banner Image Upload",
    "ESO Merchant Issues",
    "Payment & QR Issues",
    "Guiding Configuration",
    "Push to POS Issues",
]

# Routing matrix (PRD AC4.4). Each MVP category maps to a downstream queue.
ROUTING_MATRIX: dict[str, str] = {
    "ESO Activation / Deactivation": "ESO Ops",
    "Order Issues": "Order Ops",
    "Payment Gateway Setup": "Payment Ops",
    "Menu Image Upload": "Menu Ops",
    "Menu Issues": "Menu Ops",
    "Banner Image Upload": "Menu Ops",
    "ESO Merchant Issues": "ESO Ops",
    "Payment & QR Issues": "Payment Ops",
    "Guiding Configuration": "Onboarding Ops",
    "Push to POS Issues": "Integration Ops",
}

SENTINEL_TOPICS = ("Out of Scope", "Low Confidence")
CONFIDENCE_THRESHOLD = 70           # AC1.9
SESSION_TIMEOUT_SECONDS = 30 * 60   # AC1.14 — 30 minutes
# Proactive follow-up + graceful auto-close. After a ticket is created (or an
# issue resolved) the session stays on the SAME issue (state WRAP_UP) instead of
# immediately treating the next message as a brand-new issue. If the customer
# then goes quiet, the reaper (main.py) nudges once ("masih ada yang bisa
# dibantu?") and, after further silence, closes the session — so the NEXT message
# starts fresh. Tunable; kept short enough to demo live.
FOLLOWUP_PROMPT_AFTER_SECONDS = 8 * 60   # silence before the check-in nudge (nudge@8m, close@10m)
FOLLOWUP_CLOSE_AFTER_SECONDS = 2 * 60    # further silence (post-nudge) before close
# A CA match scores +3.0 per curated trigger phrase that appears in the message.
# >= 3.0 means at least one trigger phrase hit -> serve the CA response directly
# (no LLM). Keeps replies instant and quota-independent.
CA_KEYWORD_THRESHOLD = 3.0
MAX_UNRESOLVED_ATTEMPTS = 3         # AC2.3
LOW_CONF_SUGGESTION_COUNT = 3       # AC1.9.1 — "2-3 suggested categories"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class Classification(BaseModel):
    """Structured output for merchant-issue classification."""
    category: str = Field(
        description=(
            "Salah satu kategori dari daftar resmi PRD, atau 'Out of Scope' jika "
            "pertanyaan tidak berkaitan dengan operasional ESB Order, atau "
            "'Low Confidence' jika Anda tidak yakin."
        ),
    )
    confidence: int = Field(
        ge=0, le=100,
        description="Tingkat keyakinan klasifikasi, dari 0 sampai 100.",
    )
    candidates: list[str] = Field(
        default_factory=list,
        description=(
            "Jika confidence rendah, daftar 2-3 kategori paling mungkin dari "
            "daftar resmi (urutan paling mungkin dulu)."
        ),
    )


def _format_categories() -> str:
    return "\n".join(f"- {c}" for c in MVP_CATEGORIES)


_SYSTEM_PROMPT = (
    "Anda adalah asisten Level-1 Support untuk ESB Order (platform pemesanan "
    "untuk merchant F&B di Indonesia). Tugas Anda mengklasifikasikan keluhan "
    "merchant ke dalam SATU kategori dari 10 kategori resmi berikut:\n\n"
    "{categories}\n\n"
    "Aturan:\n"
    "1. Jika pertanyaan tidak berkaitan dengan operasional produk ESB Order "
    "(contoh: 'Berapa banyak merchant ESB?', 'Siapa CTO ESB?'), gunakan "
    "category='Out of Scope', confidence=0, candidates=[].\n"
    "2. Jika Anda kurang yakin (confidence < 70), TETAP isi 'category' dengan "
    "tebakan terbaik tapi juga sediakan 2-3 nama kategori paling mungkin di "
    "'candidates'. Sistem akan menawarkan ini kepada merchant untuk dipilih.\n"
    "3. Nama kategori HARUS PERSIS sama dengan daftar di atas (termasuk kapital "
    "dan spasi). Jangan menerjemahkan, jangan menyingkat.\n"
    "4. Pesan merchant biasanya dalam Bahasa Indonesia. Pahami istilah seperti "
    "'kendala', 'aktifasi', 'push to POS', 'QRIS', 'MDR', 'banner', 'menu', "
    "'foto', 'comcode', 'ESO'."
)
_USER_PROMPT = "Keluhan merchant:\n{user_input}"

_classifier_prompt = ChatPromptTemplate.from_messages(
    [("system", _SYSTEM_PROMPT), ("user", _USER_PROMPT)]
).partial(categories=_format_categories())


def _build_classifier():
    """Build the LLM-backed classifier. See module docstring for selection rules."""
    prefer_vertex = os.getenv("PREFER_VERTEX_AI", "").lower() in ("1", "true", "yes")
    has_vertex = bool(os.getenv("GOOGLE_CLOUD_PROJECT"))
    has_gemini_api = bool(os.getenv("GOOGLE_API_KEY"))

    use_vertex = has_vertex and (prefer_vertex or not has_gemini_api)

    # Fail fast: cap retries + per-request timeout so a depleted-quota (429) or
    # slow call falls through to the local fallback in a couple of seconds
    # instead of the client's default ~30-60s exponential-backoff storm.
    if use_vertex:
        llm = ChatVertexAI(
            model_name="gemini-2.5-flash",
            temperature=0.0,
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
            max_retries=1,
        )
        structured = llm.with_structured_output(Classification)
        logger.info("Classifier: ChatVertexAI (project=%s).",
                    os.environ["GOOGLE_CLOUD_PROJECT"])
        return _classifier_prompt | structured, llm

    if has_gemini_api:
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash", temperature=0.0, max_retries=1, timeout=15,
        )
        structured = llm.with_structured_output(Classification)
        logger.info("Classifier: ChatGoogleGenerativeAI.")
        return _classifier_prompt | structured, llm

    raise RuntimeError(
        "No LLM credentials. Set GOOGLE_CLOUD_PROJECT (Vertex) or GOOGLE_API_KEY."
    )


_classifier, llm = _build_classifier()


def classify_intent(user_input: str) -> dict:
    """Classify a merchant message into one of the 10 MVP categories.

    Returns::

        {
          "category": "<one of MVP_CATEGORIES | 'Out of Scope' | 'Low Confidence'>",
          "confidence": <int 0..100>,
          "candidates": [<2-3 MVP category names>],   # populated when confidence < 70
        }
    """
    try:
        result: Classification = _classifier.invoke({"user_input": user_input})
    except Exception as e:
        logger.error("Classifier error: %s", e)
        return {"category": "Low Confidence", "confidence": 0, "candidates": []}

    category = result.category
    confidence = int(result.confidence)
    candidates = [c for c in result.candidates if c in MVP_CATEGORIES][:LOW_CONF_SUGGESTION_COUNT]

    if category == "Out of Scope":
        return {"category": "Out of Scope", "confidence": confidence, "candidates": []}

    # Demote unknown categories to Low Confidence.
    if category not in MVP_CATEGORIES:
        category = "Low Confidence"

    # Apply AC1.9 — confidence floor.
    if category != "Low Confidence" and confidence < CONFIDENCE_THRESHOLD:
        if not candidates:
            candidates = [category]  # at least the model's best guess
        category = "Low Confidence"

    return {"category": category, "confidence": confidence, "candidates": candidates}


# ---------------------------------------------------------------------------
# Step synthesizer — produce N sequential troubleshooting steps from RAG docs
# ---------------------------------------------------------------------------

class TroubleshootingPlan(BaseModel):
    steps: list[str] = Field(
        description=(
            "3-5 langkah troubleshooting sekuensial dalam Bahasa Indonesia. "
            "Setiap langkah satu kalimat actionable yang bisa dikerjakan "
            "merchant secara langsung. Sebutkan path menu konkret bila relevan "
            "(contoh: 'Master > Menu > Foto Menu')."
        ),
    )


_PLAN_SYSTEM_PROMPT = (
    "Anda adalah agen Level-1 Support untuk ESB Order. Susun 3-5 langkah "
    "troubleshooting SEKUENSIAL untuk masalah merchant berikut. Gunakan "
    "konteks tiket internal sebagai referensi utama; jika tiket terlalu "
    "singkat (contoh: 'Done', 'Master > Menu'), lengkapi sendiri menjadi "
    "langkah konkrit berdasarkan praktik standar ESB Order.\n\n"
    "Aturan:\n"
    "1. Setiap langkah satu kalimat, dimulai dengan kata kerja imperatif "
    "(Buka, Pilih, Tekan, Pastikan, Coba, dst).\n"
    "2. Urutkan dari yang paling mudah ke yang paling teknis.\n"
    "3. Sebutkan menu/path konkret (contoh: 'Master > Menu > Foto Menu').\n"
    "4. JANGAN sebut 'tiket internal', 'database', atau frase yang membuka "
    "informasi internal.\n"
    "5. Bahasa Indonesia formal namun ramah.\n"
    "6. Jangan mengulang langkah yang sama."
)
_PLAN_USER_PROMPT = (
    "Kategori: {category}\n"
    "Pertanyaan merchant: {query}\n\n"
    "Konteks tiket internal yang relevan:\n{context}\n\n"
    "Susun langkah troubleshooting sekuensial."
)
_plan_prompt = ChatPromptTemplate.from_messages(
    [("system", _PLAN_SYSTEM_PROMPT), ("user", _PLAN_USER_PROMPT)]
)


def _format_context(docs: list[dict]) -> str:
    if not docs:
        return "(tidak ada tiket internal yang cocok — gunakan praktik standar ESB Order)"
    blocks: list[str] = []
    for i, d in enumerate(docs[:3], start=1):
        blocks.append(
            f"[Tiket {i}]\n"
            f"  Issue:      {d.get('issue', '') or '-'}\n"
            f"  Root cause: {d.get('root_cause', '') or '-'}\n"
            f"  Solution:   {d.get('solution', '') or '-'}"
        )
    return "\n\n".join(blocks)


def _fallback_steps(category: str) -> list[str]:
    """Deterministic safety net when Gemini fails."""
    return [
        f"Pastikan aplikasi ESB Order Anda sudah versi terbaru dan login dengan akun admin.",
        f"Periksa koneksi internet outlet Anda — pastikan stabil dan dapat membuka aplikasi lain.",
        f"Coba tutup aplikasi sepenuhnya lalu buka kembali, kemudian ulangi langkah terkait '{category}'.",
        "Jika masih bermasalah, restart perangkat POS dan coba sekali lagi.",
    ]


def synthesize_steps(query: str, category: str, docs: list[dict]) -> list[str]:
    """Return a list of 3-5 troubleshooting steps for the merchant's issue."""
    try:
        chain = _plan_prompt | llm.with_structured_output(TroubleshootingPlan)
        plan: TroubleshootingPlan = chain.invoke({
            "category": category or "-",
            "query": query,
            "context": _format_context(docs),
        })
        steps = [s.strip() for s in plan.steps if s and s.strip()]
        if steps:
            return steps[:5]
    except Exception as e:
        logger.warning("synthesize_steps failed: %s", e)
    return _fallback_steps(category)


# ---------------------------------------------------------------------------
# Session state machine
# ---------------------------------------------------------------------------

# In-memory session store. Each session keyed by chat_id.
#
# Session shape::
#
#     {
#       "state": str,
#       "topic": str,                # category from classifier
#       "original_query": str,       # first merchant message in the issue
#       "confidence": int,
#       "solution_steps": list[str], # pre-synthesized sequential steps
#       "step_index": int,           # 0-based: index of step currently shown
#       "unresolved_attempts": int,  # increments on 'Tidak'; ≥ 3 → escalate
#       "docs": list[dict],          # RAG hits used to build solution_steps
#       "chat_history": list[dict],  # {"role", "text", "ts"} per turn
#       "attachments": list[dict],   # {file_id, mime, size_bytes}
#       "ticket_form": dict,         # name, phone, company, branch (manual)
#       "low_conf_candidates": list[str],
#       "last_activity": float,      # epoch seconds — AC1.14 timeout source
#     }
SESSION_STATE: dict[str, dict] = {}

_AFFIRMATIVE = {"ya", "yes", "iya", "sudah", "ok", "oke", "bisa", "selesai", "teratasi"}
_NEGATIVE = {"tidak", "no", "belum", "gagal", "ga", "engga", "nggak", "masih"}

# Indonesian phone — accepts +62 / 62 / 0 prefix, 9-13 digits total. AC4.7.
_PHONE_RE = re.compile(r"^(?:\+?62|0)\d{8,12}$")


def _now() -> float:
    return time.time()


def _fresh_session() -> dict:
    return {
        "state": "IDLE",
        "topic": "",
        "original_query": "",
        "confidence": 0,
        "solution_steps": [],
        "step_index": 0,
        "unresolved_attempts": 0,
        "docs": [],
        "chat_history": [],
        "attachments": [],
        "ticket_form": {},
        "low_conf_candidates": [],
        "last_activity": _now(),
    }


def _record_turn(session: dict, role: str, text: str) -> None:
    """Append a turn to chat_history (capped at 200 entries to bound memory)."""
    history = session.setdefault("chat_history", [])
    history.append({"role": role, "text": text, "ts": _now()})
    if len(history) > 200:
        del history[: len(history) - 200]
    session["last_activity"] = _now()


def idle_action(session: dict, now: float) -> str | None:
    """Decide what the reaper should do for an idle session.

    Returns ``"prompt"`` (send the "still there?" check-in), ``"close"`` (no
    reply after the check-in — end the session), or ``None`` (leave it alone).
    Pure/deterministic so it's unit-testable without timers. Sessions with no
    conversation yet are never reaped.
    """
    if not session.get("chat_history"):
        return None
    if session.get("followup_prompted"):
        prompted_at = session.get("followup_prompted_at", now)
        return "close" if now - prompted_at > FOLLOWUP_CLOSE_AFTER_SECONDS else None
    idle = now - session.get("last_activity", now)
    return "prompt" if idle > FOLLOWUP_PROMPT_AFTER_SECONDS else None


# ── Customer de-escalation (calm a furious customer) ─────────────────────────
# Purpose-built to be keyword/heuristic based: zero LLM cost and quota-independent
# (Gemini credits don't matter), so it always fires even when the model is down.
# Tuned for Bahasa Indonesia (the bot's primary language) plus common English.

# Strong profanity — matched as whole tokens so we don't trip on substrings.
_ANGER_PROFANITY = {
    "anjing", "anjg", "bangsat", "bgst", "brengsek", "goblok", "tolol", "bego",
    "kampret", "sialan", "tai", "taik", "kontol", "memek", "ngentot", "bajingan",
    "fuck", "fucking", "shit", "damn", "asshole", "bullshit", "wtf",
}

# Frustration / complaint phrases — matched as substrings (some are multi-word).
# Deliberately excludes bare "lama" (would hit "selamat") — uses "lambat" /
# "lama banget" instead.
_ANGER_TERMS = (
    "marah", "kesal", "kesel", "kecewa", "parah", "payah", "buruk", "jelek",
    "lambat", "lama banget", "lelet", "menyebalkan", "nyebelin", "muak", "geram",
    "emosi", "frustrasi", "komplain", "ga becus", "gak becus", "tidak becus",
    "tidak profesional", "gak profesional", "ga jelas", "gak jelas",
    "gimana sih", "gmn sih", "useless", "terrible", "worst", "ridiculous",
    "unacceptable", "angry", "furious", "frustrated", "fed up",
)

_EXCLAIM_RE = re.compile(r"!{3,}")
_TOKEN_RE = re.compile(r"[a-z]+")

# Empathetic, calming replies sent ahead of the normal bot response. Acknowledge
# the feeling, take ownership, and steer back to resolving the issue.
CALMING_MESSAGES = (
    "Mohon maaf yang sebesar-besarnya atas ketidaknyamanan ini 🙏 Saya benar-benar "
    "memahami kekecewaan Anda, dan saya di sini untuk membantu menyelesaikannya "
    "secepat mungkin. Boleh ceritakan kembali kendalanya supaya bisa langsung saya bantu?",
    "Saya mengerti hal ini pasti sangat membuat frustrasi, dan saya minta maaf atas "
    "pengalaman yang kurang menyenangkan ini 🙏 Tenang, kita selesaikan bersama. "
    "Mohon jelaskan kendala Anda agar saya bisa segera menindaklanjutinya.",
    "Maaf atas kerepotan yang Anda alami 🙏 Keluhan Anda sangat kami perhatikan dan "
    "akan kami bantu sampai tuntas. Boleh dibantu jelaskan detail masalahnya?",
)


def _is_shouting(text: str) -> bool:
    """True when the message is mostly uppercase letters (shouting)."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 8:  # ignore short bursts like "OK" / "QR"
        return False
    uppers = sum(1 for c in letters if c.isupper())
    return uppers / len(letters) >= 0.7


def detect_anger(text: str) -> bool:
    """Heuristic, LLM-free check for a furious/frustrated customer message."""
    if not text:
        return False
    raw = text.strip()
    if not raw:
        return False
    low = raw.lower()
    if set(_TOKEN_RE.findall(low)) & _ANGER_PROFANITY:
        return True
    if any(term in low for term in _ANGER_TERMS):
        return True
    if _EXCLAIM_RE.search(raw):
        return True
    return _is_shouting(raw)


def calming_message() -> str:
    """Pick a calming reply to prepend before the normal bot response."""
    return random.choice(CALMING_MESSAGES)


# ── Off-script input during ticket-form collection ───────────────────────────
# While collecting name/phone/company/branch the customer may push back or ask a
# question ("kenapa butuh nomor HP saya?!") instead of giving the value. Detect
# that and answer logically (explain WHY we ask, then re-ask) instead of storing
# the question as the value or rejecting it as an invalid format.

# Per-field reason + the question to re-ask. Keyed by collection state.
_COLLECTION_FIELDS = {
    "COLLECTING_NAME": {
        "why": "Nama Anda kami butuhkan agar tim support tahu harus menyapa siapa dan mencocokkannya dengan akun Anda.",
        "ask": "Boleh dibantu, dengan siapa saya berbicara?",
    },
    "COLLECTING_PHONE": {
        "why": "Tenang, nomor HP hanya dipakai agar tim support bisa menghubungi Anda untuk menindaklanjuti tiket ini — tidak dibagikan ke pihak lain.",
        "ask": "Boleh dibantu nomor HP yang bisa dihubungi? (contoh: 081234567890)",
    },
    "COLLECTING_COMPANY": {
        "why": "Nama perusahaan/brand membantu kami menemukan akun dan konfigurasi outlet Anda.",
        "ask": "Apa nama perusahaan / brand Anda?",
    },
    "COLLECTING_BRANCH": {
        "why": "Nama outlet/cabang membantu tim mempersempit lokasi kendala dengan cepat.",
        "ask": "Apa nama outlet / cabang Anda?",
    },
}

# Words that signal a question/objection rather than the requested value. Kept
# tight to avoid mistaking a real name/company for a question.
_QUESTION_MARKERS = (
    "kenapa", "mengapa", "knp", "ngapain", "buat apa", "untuk apa", "buat apaan",
    "ga jelas", "privasi", "rahasia", "aman ga", "aman gak", "aman kah",
    "why", "what for", "for what", "what do you", "why do you",
)
_REFUSAL_MARKERS = (
    "ga mau", "gak mau", "nggak mau", "ngga mau", "tidak mau", "gamau", "gakmau",
    "ga usah", "gak usah", "nggak usah", "tidak usah", "ga perlu", "gak perlu",
    "tidak perlu", "ga kasih", "gak kasih", "ogah", "males", "malas",
    "don't want", "dont want", "no need", "won't give", "wont give", "refuse",
)


def _looks_like_question_or_refusal(text: str) -> bool:
    low = text.strip().lower()
    if not low:
        return False
    if "?" in low:
        return True
    return any(m in low for m in _QUESTION_MARKERS) or any(m in low for m in _REFUSAL_MARKERS)


def _explain_and_reask(session: dict, state: str) -> dict:
    info = _COLLECTION_FIELDS[state]
    text_out = f"{info['why']}\n\n{info['ask']}"
    _record_turn(session, "assistant", text_out)
    return {"type": "message", "text": text_out}


# Find a valid Indonesian phone number anywhere in the message, so a furious but
# compliant reply like "fine, ini 081234567890" still works (not just bare digits).
_PHONE_SEARCH_RE = re.compile(r"(?:\+?62|0)[\d\s-]{8,14}")


def _extract_phone(text: str) -> str | None:
    m = _PHONE_SEARCH_RE.search(text or "")
    if not m:
        return None
    cleaned = re.sub(r"[\s-]", "", m.group(0))
    return cleaned if _PHONE_RE.match(cleaned) else None


def _format_ticket_number() -> str:
    """AC4.3 — Ticket #[YY][6-digit random]. Year 2-digit + 100000–999999."""
    yy = datetime.now().strftime("%y")
    rand = random.randint(100000, 999999)
    return f"Ticket #{yy}{rand}"


def render_chat_history(session: dict) -> str:
    """Plain-text transcript embedded in the generated ticket."""
    lines: list[str] = []
    for turn in session.get("chat_history", []):
        ts = datetime.fromtimestamp(turn.get("ts", 0)).strftime("%H:%M:%S")
        actor = "Merchant" if turn["role"] == "user" else "Bot"
        lines.append(f"[{ts}] {actor}: {turn['text']}")
    return "\n".join(lines)


def render_steps_attempted(session: dict) -> str:
    """Numbered list of steps the bot actually showed to the merchant."""
    steps = session.get("solution_steps", [])
    shown = steps[: session.get("step_index", 0) + 1] if steps else []
    return "\n".join(f"{i+1}. {s}" for i, s in enumerate(shown))


# ---------------------------------------------------------------------------
# Flow handlers
# ---------------------------------------------------------------------------

def _present_step(session: dict) -> dict:
    """Return the response for the current step (0-based step_index).

    Two presentation modes:

    * **CA mode** (``source == 'ca'``): each "step" is a full pre-authored
      response. We show it as-is with a brief prefix when there are
      alternates.
    * **Synthesized mode** (``source == 'llm'``): each step is a single
      action from a Gemini-generated plan. We label them ``Langkah X dari N``.
    """
    steps = session["solution_steps"]
    idx = session["step_index"]
    step_no = idx + 1
    total = len(steps)
    source = session.get("source", "llm")

    if source == "ca":
        # CA responses are self-contained — no "Langkah X dari N" wrapper.
        prefix = ""
        if idx > 0:
            prefix = "Coba pendekatan lain:\n\n"
        body = (
            f"{prefix}{steps[idx]}\n\n"
            f"Apakah panduan ini menyelesaikan masalah Anda?"
        )
    else:
        body = (
            f"Langkah {step_no} dari {total}:\n"
            f"{steps[idx]}\n\n"
            f"Apakah langkah ini menyelesaikan masalah Anda?"
        )

    _record_turn(session, "assistant", body)
    return {"type": "question", "text": body, "options": ["Ya", "Tidak"]}


def _start_troubleshooting(session: dict, category: str, query: str, confidence: int) -> dict:
    """Start a troubleshooting flow.

    Resolution order:

    1. **Content Architecture V.4** — keyword-matched pre-authored responses
       from the support team. Highest priority because they're hand-reviewed
       and use ESB's exact terminology.
    2. **Vertex AI Search + Gemini synthesis** — fallback when no CA entry
       scores above the keyword threshold.
    """
    ca_matches = match_ca(query, category=category, k=3)

    if ca_matches:
        steps = [format_response(m["response"]) for m in ca_matches]
        # Tag from the top match drives sub-topic + analytics.
        top_tag = (ca_matches[0]["tags"] or [""])[0]
        session.update({
            "state": "TROUBLESHOOTING",
            "topic": category,
            "sub_topic": top_tag,
            "original_query": query,
            "confidence": confidence,
            "solution_steps": steps,
            "step_index": 0,
            "unresolved_attempts": 0,
            "source": "ca",
            "ca_matches": ca_matches,   # kept for ticket payload
            "docs": [],
        })
        logger.info("CA match (score=%s, %d alternates): %s",
                    ca_matches[0].get("match_score"), len(ca_matches) - 1,
                    ca_matches[0]["predefined"])
        return _present_step(session)

    # Fallback to Gemini synthesis over the RAG corpus.
    docs = retrieve_troubleshooting(query=query, topic=category, k=3)
    steps = synthesize_steps(query, category, docs)
    session.update({
        "state": "TROUBLESHOOTING",
        "topic": category,
        "sub_topic": "",
        "original_query": query,
        "confidence": confidence,
        "solution_steps": steps,
        "step_index": 0,
        "unresolved_attempts": 0,
        "source": "llm",
        "docs": docs,
    })
    return _present_step(session)


def _resolved_close(session: dict) -> dict:
    """AC2.2.1 — mark Resolved and ask for a CSAT rating before closing.

    Session topic/sub_topic/original_query are PRESERVED across this turn so
    the CSAT handler can persist them when the merchant picks a rating.
    """
    text = (
        "Bagus! Senang masalahnya sudah teratasi.\n\n"
        "Sebelum sesi ditutup, mohon beri penilaian untuk bantuan saya:\n"
        "(1 = sangat buruk, 5 = sangat baik)"
    )
    session["state"] = "AWAITING_CSAT"
    _record_turn(session, "assistant", text)
    return {
        "type": "question",
        "text": text,
        "options": ["1", "2", "3", "4", "5"],
    }


def _persist_csat(chat_id: str, rating: int, session: dict) -> None:
    """Save a CSAT rating row. Imported lazily to avoid a circular import on
    module load — database.py imports config, which sometimes pulls agent.py
    transitively during tests."""
    from database import SessionLocal, CSATRating
    db = SessionLocal()
    try:
        row = CSATRating(
            chat_id=chat_id,
            rating=rating,
            category=session.get("topic", ""),
            sub_topic=session.get("sub_topic", ""),
            original_query=session.get("original_query", ""),
            resolved_via=session.get("source", ""),
        )
        db.add(row)
        db.commit()
    except Exception as e:
        logger.warning("Failed to persist CSAT rating: %s", e)
    finally:
        db.close()


def _csat_thanks_and_close(session: dict, rating: int) -> dict:
    """Send the final thank-you and enter WRAP_UP (NOT a blank IDLE).

    Keeps the issue context so a follow-up ("ternyata masih error") stays on the
    same matter rather than being re-classified as a new issue. The reaper closes
    the session on silence; a fresh issue starts only after that (or via /start).
    """
    session.update({
        "state": "WRAP_UP",
        "solution_steps": [],
        "step_index": 0,
        "unresolved_attempts": 0,
        "docs": [],
        "low_conf_candidates": [],
        "predefined_choices": [],
        "ticket_form": {},
        "followup_prompted": False,
        "followup_prompted_at": None,
    })
    text = (
        f"Terima kasih atas penilaian Anda ({rating}/5)!\n"
        f"Jika masih ada yang ingin disampaikan soal kendala ini, silakan balas. "
        f"Untuk kendala baru, ketik /start."
    )
    _record_turn(session, "assistant", text)
    return {"type": "message", "text": text}


def _begin_escalation(session: dict) -> dict:
    """Kick off the multi-turn ticket form (US4)."""
    session["state"] = "COLLECTING_NAME"
    session["ticket_form"] = {}
    text = (
        "Saya akan eskalasikan ke tim support. Untuk membuat tiket, mohon "
        "isi data berikut.\n\nSiapa nama Anda?"
    )
    _record_turn(session, "assistant", text)
    return {"type": "message", "text": text}


def _ticket_form_step(session: dict, field_msg: tuple[str, str]) -> dict:
    next_state, prompt = field_msg
    session["state"] = next_state
    _record_turn(session, "assistant", prompt)
    return {"type": "message", "text": prompt}


def _finalize_ticket(session: dict) -> dict:
    """Build the ticket payload after all manual fields are collected."""
    category = session.get("topic") or "Other"
    form = session.get("ticket_form", {})
    ticket_number = _format_ticket_number()
    queue = ROUTING_MATRIX.get(category, "General Support")
    confirmation = (
        f"{ticket_number} sudah dibuat dan akan ditangani oleh tim {queue}.\n"
        f"Tim kami akan merespons dalam 1-2 jam pada jam operasional "
        f"(08:00-22:00 WIB)."
    )
    payload = {
        "type": "ticket_form",
        "text": confirmation,
        "ticket_number": ticket_number,
        "category": category,
        "sub_topic": session.get("sub_topic", ""),  # CA issue tag, e.g. "#push_to_pos"
        "issue_detail": session.get("original_query", ""),
        "chat_history": render_chat_history(session),
        "steps_attempted": render_steps_attempted(session),
        "attachments": session.get("attachments", []),
        "name": form.get("name", ""),
        "phone": form.get("phone", ""),
        "company": form.get("company", ""),
        "branch": form.get("branch", ""),
        "routed_queue": queue,
        "confidence": session.get("confidence", 0),
    }
    _record_turn(session, "assistant", confirmation)
    # Don't snap back to a blank IDLE (which would treat the customer's very next
    # message as a NEW issue). Enter WRAP_UP and KEEP the issue context (topic /
    # sub_topic / original_query) so follow-ups stay tied to THIS ticket. The
    # reaper closes the session on silence; only then does a new message start
    # fresh.
    session.update({
        "state": "WRAP_UP",
        "last_ticket_number": ticket_number,
        "solution_steps": [],
        "step_index": 0,
        "unresolved_attempts": 0,
        "docs": [],
        "ticket_form": {},
        "followup_prompted": False,
        "followup_prompted_at": None,
    })
    return payload


def _present_low_conf(session: dict, candidates: list[str], original_query: str) -> dict:
    session.update({
        "state": "CHOOSING_CATEGORY",
        "low_conf_candidates": candidates,
        "original_query": original_query,
    })
    text = (
        "Saya kurang yakin memahami kendala Anda. Mungkin salah satu kategori "
        "berikut yang Anda maksud?"
    )
    _record_turn(session, "assistant", text)
    return {"type": "question", "text": text, "options": candidates + ["Bukan, jelaskan ulang"]}


PREDEFINED_SUGGESTIONS = 3  # PRD UX: show 3 ranked suggestions + escape hatch
ESCAPE_OPTION = "Lainnya — jelaskan ulang"


def _present_predefined_menu(session: dict, category: str, query: str, confidence: int) -> dict:
    """Show the top-N predefined issues in a category (PRD AC1.3 / AC1.4 drill-down).

    Uses ``match_ca`` to rank entries by relevance to the merchant's actual
    wording, then takes the top-3. Always appends an escape option
    ("Lainnya — jelaskan ulang") so the merchant can re-describe if none fit.

    Falls back to authoring-order top-3 when the keyword matcher returns
    nothing (e.g., merchant said only the category name).
    """
    ranked = match_ca(query, category=category, k=PREDEFINED_SUGGESTIONS)
    if not ranked:
        ranked = entries_in_category(category)[:PREDEFINED_SUGGESTIONS]
    if not ranked:
        return _start_troubleshooting(session, category, query, confidence)

    choices = [e["predefined"] for e in ranked]
    session.update({
        "state": "CHOOSING_PREDEFINED",
        "topic": category,
        "original_query": query,
        "confidence": confidence,
        "predefined_choices": choices,
    })
    text = (
        f"Saya menangkap kendala Anda di kategori \"{category}\".\n\n"
        f"Mana yang paling sesuai dengan masalah Anda?"
    )
    _record_turn(session, "assistant", text)
    return {
        "type": "question",
        "text": text,
        "options": choices + [ESCAPE_OPTION],
    }


RELEVANT_PREDEFINED_COUNT = 5  # how many matching issues to offer
MIN_RELEVANCE_SCORE = 1.0      # below this, a CA entry isn't relevant enough


def _ask_describe(session: dict) -> dict:
    """Prompt the merchant to describe their issue so we can surface the
    matching predefined issues."""
    session["state"] = "IDLE"
    text = (
        f"Halo! Saya {MODEL_NAME}, asisten dukungan ESB Order.\n\n"
        "Ada kendala apa yang sedang Anda alami? Jelaskan singkat ya, misalnya:\n"
        "• \"Pesanan tidak masuk ke POS\"\n"
        "• \"Upload foto menu\"\n"
        "• \"Setting payment gateway\"\n\n"
        "Nanti saya tampilkan kendala yang paling sesuai untuk Anda pilih."
    )
    _record_turn(session, "assistant", text)
    return {"type": "message", "text": text}


def _present_matching_predefined(session: dict, query: str) -> dict | None:
    """Show the predefined issues (CA column A) most relevant to what the
    merchant just described. Returns None when nothing is relevant enough."""
    ranked = [e for e in match_ca(query, k=RELEVANT_PREDEFINED_COUNT)
              if e["match_score"] >= MIN_RELEVANCE_SCORE]
    if not ranked:
        return None
    choices = [e["predefined"] for e in ranked]
    session.update({
        "state": "CHOOSING_PREDEFINED",
        "topic": ranked[0].get("category", ""),
        "original_query": query,
        "confidence": 100,
        "predefined_choices": choices,
    })
    text = (
        "Berikut kendala yang paling sesuai dengan yang Anda alami — "
        "silakan pilih yang paling tepat:"
    )
    _record_turn(session, "assistant", text)
    return {"type": "question", "text": text, "options": choices + [ESCAPE_OPTION]}


def _present_category_menu(session: dict) -> dict:
    """First-contact menu: list the CA categories (column-A issues are grouped
    under these 10 categories). Merchant picks one to drill into its issues."""
    cats = list_categories()
    session.update({"state": "MENU_CATEGORY", "menu_categories": cats})
    text = (
        f"Halo! Saya {MODEL_NAME}, asisten dukungan ESB Order.\n\n"
        "Silakan pilih kategori kendala Anda:"
    )
    _record_turn(session, "assistant", text)
    return {"type": "question", "text": text, "options": cats}


def _present_category_issues(session: dict, category: str) -> dict:
    """List every predefined issue (CA column A) in the chosen category, so the
    merchant can pick the exact one and get its column-D response."""
    entries = entries_in_category(category)
    choices = [e["predefined"] for e in entries]
    if not choices:
        return _present_category_menu(session)
    session.update({
        "state": "CHOOSING_PREDEFINED",
        "topic": category,
        "original_query": "",
        "confidence": 100,
        "predefined_choices": choices,
    })
    text = f"Kategori: {category}\n\nMana yang paling sesuai dengan kendala Anda?"
    _record_turn(session, "assistant", text)
    return {"type": "question", "text": text, "options": choices + [ESCAPE_OPTION]}


def _present_specific_ca(session: dict, entry: dict) -> dict:
    """Lock in a specific CA response (after drill-down pick) and present it."""
    formatted = format_response(entry["response"])
    tag = (entry.get("tags") or [""])[0]
    session.update({
        "state": "TROUBLESHOOTING",
        "topic": entry.get("category", session.get("topic", "")),
        "sub_topic": tag,
        "solution_steps": [formatted],
        "step_index": 0,
        "unresolved_attempts": 0,
        "source": "ca",
        "ca_matches": [entry],
        "docs": [],
    })
    return _present_step(session)


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def process_message(chat_id: str, text: str) -> dict:
    """Drive one turn of the support conversation.

    Returns a Telegram-shaped response dict; ``main.py`` translates this into
    the appropriate Bot API call.
    """
    session = SESSION_STATE.setdefault(chat_id, _fresh_session())

    # AC1.14 — 30-minute idle timeout.
    if _now() - session.get("last_activity", _now()) > SESSION_TIMEOUT_SECONDS:
        logger.info("Session %s timed out — resetting.", chat_id)
        SESSION_STATE[chat_id] = _fresh_session()
        session = SESSION_STATE[chat_id]
        # Don't return here — process the incoming message in a fresh session.
        # AC1.14.1's "auto-closing message" is sent by the timeout reaper job
        # (out of scope for this turn — would need a background task).

    raw = (text or "").strip()
    if not raw:
        return {"type": "message", "text": "Mohon kirim pesan teks ya."}

    # Telegram /start (and /help) — issue a welcoming prompt that primes the
    # merchant to describe their kendala. Resets any in-flight session.
    if raw.lower() in ("/start", "/help"):
        SESSION_STATE[chat_id] = _fresh_session()
        session = SESSION_STATE[chat_id]
        return _ask_describe(session)

    _record_turn(session, "user", raw)
    # Any reply restarts the idle/check-in/close cycle (they're clearly active).
    session["followup_prompted"] = False
    session["followup_prompted_at"] = None
    msg = raw.lower()
    state = session["state"]

    # ---- WRAP_UP — issue handled; stay on the SAME issue, don't re-classify ----
    # After a ticket is created or an issue resolved, the customer is often still
    # talking about that same matter. Acknowledge and keep listening instead of
    # spinning up a brand-new classification. A genuinely new issue starts only
    # after the session auto-closes on silence (reaper) or the customer types
    # /start (handled above).
    if state == "WRAP_UP":
        tnum = session.get("last_ticket_number")
        if tnum:
            text_out = (
                f"Baik, saya catat tambahan ini untuk {tnum} ya — tim support akan "
                f"menindaklanjuti. Masih ada lagi yang ingin Anda sampaikan soal "
                f"kendala ini? Untuk kendala baru, ketik /start."
            )
        else:
            text_out = (
                "Baik, saya catat ya. Masih ada lagi yang ingin Anda sampaikan soal "
                "kendala ini? Untuk kendala baru, ketik /start."
            )
        _record_turn(session, "assistant", text_out)
        return {"type": "message", "text": text_out}

    # ---- AWAITING_CSAT — post-resolution rating capture ----
    if state == "AWAITING_CSAT":
        if msg in ("1", "2", "3", "4", "5"):
            rating = int(msg)
            _persist_csat(chat_id, rating, session)
            return _csat_thanks_and_close(session, rating)
        # Invalid input — re-prompt without losing state.
        nudge = (
            "Mohon pilih angka 1 sampai 5 untuk penilaian Anda."
        )
        _record_turn(session, "assistant", nudge)
        return {
            "type": "question",
            "text": nudge,
            "options": ["1", "2", "3", "4", "5"],
        }

    # ---- TROUBLESHOOTING — sequential Yes/No per step (US2) ----
    if state == "TROUBLESHOOTING":
        if msg in _AFFIRMATIVE:
            return _resolved_close(session)
        if msg in _NEGATIVE:
            session["unresolved_attempts"] += 1
            session["step_index"] += 1
            # AC2.3 — escalate after 3 consecutive No OR when steps exhausted.
            if (session["unresolved_attempts"] >= MAX_UNRESOLVED_ATTEMPTS
                    or session["step_index"] >= len(session["solution_steps"])):
                return _begin_escalation(session)
            return _present_step(session)
        # AC2.6 / AC2.7 — ambiguous response: retry once, then proceed.
        nudge = (
            "Mohon balas 'Ya' jika masalah sudah teratasi, atau 'Tidak' untuk "
            "lanjut ke langkah berikutnya."
        )
        _record_turn(session, "assistant", nudge)
        return {"type": "question", "text": nudge, "options": ["Ya", "Tidak"]}

    # ---- CHOOSING_CATEGORY — low-confidence pick (AC1.9.1) ----
    if state == "CHOOSING_CATEGORY":
        candidates = session.get("low_conf_candidates", [])
        # Exact match against one of the offered categories?
        match = next((c for c in candidates if c.lower() == msg), None)
        if match:
            return _present_predefined_menu(
                session, match, session.get("original_query", ""), confidence=70,
            )
        if "bukan" in msg or "ulang" in msg or "jelaskan" in msg:
            session["state"] = "IDLE"
            text_out = "Baik, mohon jelaskan kendala Anda dengan lebih spesifik."
            _record_turn(session, "assistant", text_out)
            return {"type": "message", "text": text_out}
        # Free text → treat as a fresh classification attempt.
        session["state"] = "IDLE"
        # fall through to IDLE handling

    # ---- CHOOSING_PREDEFINED — drill-down pick within a category ----
    if state == "CHOOSING_PREDEFINED":
        choices = session.get("predefined_choices", [])
        match = next((c for c in choices if c.lower() == msg), None)
        if match:
            entry = find_by_predefined(match)
            if entry is not None:
                return _present_specific_ca(session, entry)
            # Shouldn't happen, but fall back to keyword retrieval if it does.
            return _start_troubleshooting(
                session, session.get("topic", ""),
                session.get("original_query", ""),
                session.get("confidence", 0),
            )
        if "lainnya" in msg or "ulang" in msg or "jelaskan" in msg:
            return _ask_describe(session)
        # Anything else → treat as a fresh description and re-match.
        session["state"] = "IDLE"
        # fall through to IDLE handling

    # ---- COLLECTING_NAME / PHONE / COMPANY / BRANCH (US4) ----
    # Off-script guard: a question or pushback ("kenapa butuh nomor HP saya?!")
    # instead of the value — answer it logically and re-ask, don't store/reject.
    if state in _COLLECTION_FIELDS and _looks_like_question_or_refusal(raw):
        return _explain_and_reask(session, state)

    if state == "COLLECTING_NAME":
        session["ticket_form"]["name"] = raw
        return _ticket_form_step(
            session, ("COLLECTING_PHONE", "Berapa nomor HP Anda? (contoh: 081234567890)"),
        )
    if state == "COLLECTING_PHONE":
        phone = _extract_phone(raw)
        if not phone:
            # AC4.7 — re-prompt on invalid phone (genuine attempt, not a question).
            text_out = (
                "Maaf, saya belum menemukan nomor HP yang valid di pesan Anda. "
                "Mohon kirim ulang ya (contoh: 081234567890 atau +6281234567890)."
            )
            _record_turn(session, "assistant", text_out)
            return {"type": "message", "text": text_out}
        session["ticket_form"]["phone"] = phone
        return _ticket_form_step(
            session, ("COLLECTING_COMPANY", "Apa nama perusahaan / brand Anda?"),
        )
    if state == "COLLECTING_COMPANY":
        session["ticket_form"]["company"] = raw
        return _ticket_form_step(
            session, ("COLLECTING_BRANCH", "Apa nama outlet / cabang Anda?"),
        )
    if state == "COLLECTING_BRANCH":
        session["ticket_form"]["branch"] = raw
        return _finalize_ticket(session)

    # ---- IDLE — merchant described their issue ----
    # Surface the predefined issues (CA column A) most relevant to their chat;
    # they pick the exact one and get its column-D response. No LLM.
    matched = _present_matching_predefined(session, raw)
    if matched is not None:
        return matched
    # Nothing relevant enough — ask them to rephrase.
    text_out = (
        "Maaf, saya belum menemukan kendala yang cocok dengan pesan Anda.\n"
        "Coba jelaskan dengan kata lain ya — misalnya \"pesanan tidak masuk POS\", "
        "\"upload foto menu\", atau \"setting payment\"."
    )
    _record_turn(session, "assistant", text_out)
    return {"type": "message", "text": text_out}
