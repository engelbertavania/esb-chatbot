FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Install Python dependencies first (better Docker layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Backend source.
COPY agent.py database.py main.py rag.py ingest.py ./

# Pre-built RAG corpus (run `python ingest.py` locally before building).
COPY vertex_corpus.jsonl ./

# Dashboard fallback (Next.js frontend is the primary UI; this keeps the old
# `/` route working for backward compatibility).
COPY "CS Chatbot Dashboard _standalone_.html" ./

EXPOSE 8080

# Cloud Run injects $PORT; default to 8080 for local `docker run`.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
