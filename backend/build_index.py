"""Build a FAISS vector index from the ingested ticket corpus.

Embeds each ticket's content with Gemini's free ``text-embedding-004`` model
and persists the index under ``./faiss_index/``. Re-run whenever
``vertex_corpus.jsonl`` changes.

Usage:
    .\\venv\\Scripts\\python.exe build_index.py

Prereqs:
    - ``GOOGLE_API_KEY`` must be set (Gemini API key from aistudio.google.com).
    - ``vertex_corpus.jsonl`` exists (run ``ingest.py`` first).
"""

from __future__ import annotations

import config  # noqa: F401 — loads .env before any os.getenv reads

import json
import logging
import os
import sys
import time
from pathlib import Path

from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CORPUS_PATH = Path(__file__).parent / "vertex_corpus.jsonl"
INDEX_DIR = Path(__file__).parent / "faiss_index"
EMBEDDING_MODEL = "models/gemini-embedding-001"
# gemini-embedding-001 free tier is rate-limited; small batches keep it stable.
BATCH_SIZE = 20


def load_corpus() -> list[Document]:
    if not CORPUS_PATH.exists():
        raise SystemExit(
            f"{CORPUS_PATH} not found. Run `python ingest.py` first."
        )

    docs: list[Document] = []
    with CORPUS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            docs.append(
                Document(
                    page_content=obj["content"],
                    metadata={
                        "source_id": obj["id"],
                        **obj.get("metadata", {}),
                    },
                )
            )
    return docs


def main() -> None:
    if not os.getenv("GOOGLE_API_KEY"):
        raise SystemExit(
            "GOOGLE_API_KEY env var is not set. Get a key at "
            "https://aistudio.google.com/apikey then set it for this shell:\n"
            '  $env:GOOGLE_API_KEY = "AIzaSy..."'
        )

    logger.info("Loading corpus from %s", CORPUS_PATH)
    docs = load_corpus()
    logger.info("Loaded %d documents", len(docs))

    embeddings = GoogleGenerativeAIEmbeddings(model=EMBEDDING_MODEL)

    logger.info("Embedding first batch to validate the API key...")
    t0 = time.time()
    vectorstore = FAISS.from_documents(docs[:BATCH_SIZE], embeddings)
    logger.info("First batch indexed in %.1fs", time.time() - t0)

    # Embed the rest in batches. FAISS supports add_documents incrementally.
    remaining = docs[BATCH_SIZE:]
    for i in range(0, len(remaining), BATCH_SIZE):
        batch = remaining[i : i + BATCH_SIZE]
        t0 = time.time()
        vectorstore.add_documents(batch)
        done = BATCH_SIZE + i + len(batch)
        logger.info(
            "Indexed batch %d-%d (%d/%d, %.1fs)",
            i + BATCH_SIZE, i + BATCH_SIZE + len(batch),
            done, len(docs), time.time() - t0,
        )

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    vectorstore.save_local(str(INDEX_DIR))
    logger.info("Saved FAISS index to %s", INDEX_DIR)


if __name__ == "__main__":
    main()
