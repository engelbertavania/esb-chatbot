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

## Repository layout

```
backend/    FastAPI app, agent, RAG, tests, Dockerfile + deploy/tunnel scripts
frontend/   Next.js dashboard (git submodule)
docs/        specs & plans
venv/        Python virtualenv (created at the repo root)
```

## Backend layout (`backend/`)

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
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env             # then fill in real values
```

Configuration is via environment variables — see [`backend/.env.example`](backend/.env.example)
for the full list (Gemini/Vertex, `DATABASE_URL`, `TELEGRAM_TOKEN`,
`TELEGRAM_WEBHOOK_SECRET`, `REAP_SECRET`, …). **Never commit `.env`.**

## Run locally

```bash
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000
```

Expose the webhook over a tunnel (`backend/start-tunnel.ps1`) and register it
with `backend/_register_webhook.py <public-url>`. Or boot both backend and
frontend at once with `./start-suite.ps1` from the repo root.

The dashboard lives in the submodule:

```bash
git submodule update --init
cd frontend && npm install && npm run dev
```

## Tests

```bash
cd backend && pytest -q
```

## Deploy

- **Backend → Google Cloud Run:** `cd backend && ./deploy.ps1 -Project <gcp-project-id>`
  (reads `.env`, builds via the `Dockerfile`, deploys). A Cloud Scheduler job
  calls `POST /reap` every minute to auto-close idle sessions.
- **Frontend → Vercel:** auto-deploys on push to the frontend repo's `main`.

## License

Proprietary — internal ESB hackathon project.
