# ESB Order Chatbot

AI customer-support assistant for **ESB Order / OZE** (Indonesian POS &
payments). A FastAPI backend drives a Telegram bot and a web chat, triages
merchant issues using a curated knowledge base + LLM fallback, escalates to
tickets, and supports live human handoff. A Next.js dashboard (separate repo,
included here as a submodule) visualizes and works the ticket queue.

## Architecture

```
Telegram  ─┐
Web chat  ─┼──▶  FastAPI (main.py)  ──▶  agent.py (conversation engine)
           │           │                     ├─ content_architecture.py  (curated answers, Excel-driven)
           │           │                     └─ rag.py  (Vertex AI Search + LLM fallback, via LangChain)
           │           ▼
           │     Supabase Postgres (SQLAlchemy, database.py)  ◀── Next.js dashboard (frontend/ submodule)
           ▼
   Cloud Scheduler ──▶ POST /reap  (idle-session sweep)
```

**Conversation flow:** `define → predefine → answer`. The bot offers the issue
categories, then the predefined issues in the chosen category, then the
pre-authored answer with a "did this help?" prompt — falling back to live chat
with Customer Care or a support ticket when it doesn't.

## Tech stack

- **FastAPI** + Uvicorn — HTTP API, Telegram webhook, web-chat endpoints
- **LangChain** — `ChatGoogleGenerativeAI` (Gemini API) / `ChatVertexAI`
- **Vertex AI Search** — retrieval-augmented answers (`rag.py`)
- **SQLAlchemy** — tickets, notes, CSAT, live-chat handoff (`database.py`); SQLite locally, Supabase Postgres in prod
- **Content Architecture** — 50 reviewed responses in `Content architecture V.4.xlsx` (`content_architecture.py`)
- **Next.js** dashboard — `frontend/` git submodule

## Project layout

| File | Responsibility |
|------|----------------|
| `main.py` | FastAPI app: webhook, web chat, ticket/CSAT/handoff endpoints, `/reap` |
| `agent.py` | Conversation state machine, flow logic, ticket/CSAT building |
| `content_architecture.py` | Loads & matches the curated Excel knowledge base |
| `rag.py` | Vertex AI Search retrieval + LLM synthesis fallback |
| `database.py` | SQLAlchemy models + idempotent schema migration |
| `config.py` | Env-driven configuration |
| `ingest.py` | Build the RAG corpus (`vertex_corpus.jsonl`) |
| `test_*.py` | Pytest suite |
| `Dockerfile`, `deploy.ps1` | Container build + Cloud Run deploy |

## Setup

```bash
python -m venv venv && . venv/Scripts/activate   # Windows: venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env                             # then fill in real values
```

Configuration is via environment variables — see [`.env.example`](.env.example)
for the full list (Gemini/Vertex, `DATABASE_URL`, `TELEGRAM_TOKEN`,
`TELEGRAM_WEBHOOK_SECRET`, `REAP_SECRET`, …). **Never commit `.env`.**

## Run locally

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Expose the webhook over a tunnel (`start-tunnel.ps1`) and register it with
`_register_webhook.py <public-url>`.

The dashboard lives in the submodule:

```bash
git submodule update --init
cd frontend && npm install && npm run dev
```

## Tests

```bash
pytest -q
```

## Deploy

- **Backend → Google Cloud Run:** `./deploy.ps1 -Project <gcp-project-id>`
  (reads `.env`, builds via the `Dockerfile`, deploys). A Cloud Scheduler job
  calls `POST /reap` every minute to auto-close idle sessions.
- **Frontend → Vercel:** auto-deploys on push to the frontend repo's `main`.

## License

Proprietary — internal ESB hackathon project.
