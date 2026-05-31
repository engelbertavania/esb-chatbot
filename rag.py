"""Retrieval layer for the ESB Order support chatbot.

``retrieve_troubleshooting(query, topic, sub_topic, k)`` is the only function
``agent.py`` needs. It returns a list of dicts shaped like::

    {
      "issue": str, "root_cause": str, "solution": str,
      "category_tag": str, "score": float, "source_id": str,
    }

Two retriever backends:

1. **Vertex AI Search** (when ``GOOGLE_CLOUD_PROJECT`` and ``VERTEX_DATASTORE_ID``
   are set). Used in environments with GCP billing + a populated datastore.
2. **Local FAISS index** built from ``vertex_corpus.jsonl`` using Gemini's
   ``gemini-embedding-001`` model (free via ``GOOGLE_API_KEY``). The default for
   hackathon dev and any environment without GCP billing.

Run ``python build_index.py`` once before starting the app — it persists the
FAISS index under ``./faiss_index/`` and is loaded lazily on first query.
"""

from __future__ import annotations

import config  # noqa: F401 — loads .env before any os.getenv reads

import logging
import os
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

INDEX_DIR = Path(__file__).parent / "faiss_index"

# Price optimization: cache identical (query, topic, sub_topic, k) tuples so
# repeat questions don't hit Vertex AI Search (~$2/1000 queries). Bounded LRU
# in-process — wiped on server restart, which is fine for a chatbot. Tracked
# globally so /health and smoke tests can read it.
_QUERY_CACHE_MAX = 256
_query_cache: "OrderedDict[tuple, list[dict[str, Any]]]" = OrderedDict()
_vertex_query_count = 0
_cache_hit_count = 0


def _cache_get(key: tuple) -> list[dict[str, Any]] | None:
    if key not in _query_cache:
        return None
    _query_cache.move_to_end(key)  # mark as recently used
    return [dict(d) for d in _query_cache[key]]  # defensive copy


def _cache_put(key: tuple, value: list[dict[str, Any]]) -> None:
    _query_cache[key] = [dict(d) for d in value]
    _query_cache.move_to_end(key)
    while len(_query_cache) > _QUERY_CACHE_MAX:
        _query_cache.popitem(last=False)  # evict LRU


def get_cost_stats() -> dict[str, int]:
    """Vertex query counts since process start — useful for /health or smoke."""
    return {
        "vertex_queries": _vertex_query_count,
        "cache_hits": _cache_hit_count,
        "cache_size": len(_query_cache),
    }

# Soft mapping: hackathon-brief topic -> candidate Excel `category_tag` values.
# Used to bias retrieval toward topic-aligned tickets; unmapped topics skip the
# filter and rely purely on semantic similarity.
TOPIC_TO_CATEGORY_TAGS: dict[str, list[str]] = {
    "Menu Management": ["menu_price_mismatch"],
    "Payment Configuration": ["payment_issue", "qr_barcode_error"],
    "Payment Gateway / MDR": ["payment_issue"],
    "Order Management": ["order_issue", "pos_failed"],
    "Account & Activation": ["config_activation"],
    "Product Catalog": ["menu_price_mismatch"],
    "Promo & Discount": [],
    "Integration": ["integration_api"],
    "Reporting": [],
    "Other": [],
}


VERTEX_ENGINE_ID = os.getenv("VERTEX_ENGINE_ID", "esb-chatbot-engine")


def vertex_search_available() -> bool:
    """True iff GCP project + Vertex AI Search datastore env vars are set."""
    required = ("GOOGLE_CLOUD_PROJECT", "VERTEX_DATASTORE_ID")
    return all(os.getenv(k) for k in required)


# Backwards-compat alias for existing callers (smoke_test.py, main.py /health).
_vertex_search_available = vertex_search_available


# ---------------------------------------------------------------------------
# FAISS backend
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _faiss_store():
    """Load (lazily) and cache the FAISS index. Returns None if unavailable."""
    if not INDEX_DIR.exists():
        logger.error(
            "FAISS index not found at %s. Run `python build_index.py` first.",
            INDEX_DIR,
        )
        return None
    if not os.getenv("GOOGLE_API_KEY"):
        logger.error(
            "GOOGLE_API_KEY is not set; cannot embed queries. "
            "Set it from https://aistudio.google.com/apikey.",
        )
        return None
    try:
        from langchain_community.vectorstores import FAISS
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        embeddings = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")
        store = FAISS.load_local(
            str(INDEX_DIR),
            embeddings,
            allow_dangerous_deserialization=True,
        )
        logger.info("FAISS index loaded from %s", INDEX_DIR)
        return store
    except Exception as e:
        logger.exception("Could not load FAISS index: %s", e)
        return None


def _retrieve_via_faiss(
    query: str, topic: str | None, sub_topic: str | None, k: int,
) -> list[dict[str, Any]]:
    store = _faiss_store()
    if store is None:
        return []

    # Build a metadata filter when we have a strong topic→category_tag mapping.
    allowed_tags = TOPIC_TO_CATEGORY_TAGS.get(topic, []) if topic else []
    filter_fn = None
    if allowed_tags:
        allowed_set = set(allowed_tags)
        def filter_fn(md: dict[str, Any]) -> bool:
            return md.get("category_tag") in allowed_set

    # Augment the query with the sub_topic key (e.g. "push_to_pos_failed") so
    # the embedding signal includes both the merchant's words and the
    # classifier's structured guess.
    augmented = query
    if sub_topic:
        augmented = f"{query}\n[sub_topic: {sub_topic.replace('_', ' ')}]"

    try:
        # ``similarity_search_with_score_by_vector`` returns (Document, distance).
        # Lower distance = more similar. We convert to a 0..1 score for callers.
        results = store.similarity_search_with_score(
            augmented, k=k * 3 if filter_fn else k, filter=filter_fn,
        )
    except Exception as e:
        logger.warning("FAISS query failed: %s", e)
        return []

    out: list[dict[str, Any]] = []
    for doc, distance in results[:k]:
        md = doc.metadata or {}
        out.append({
            "issue": md.get("issue", ""),
            "root_cause": md.get("root_cause", ""),
            "solution": md.get("solution", ""),
            "category_tag": md.get("category_tag", ""),
            "score": round(1.0 / (1.0 + float(distance)), 4),  # distance -> [0,1]
            "source_id": md.get("source_id", ""),
        })
    return out


# ---------------------------------------------------------------------------
# Vertex AI Search backend (when billing + datastore configured)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _vertex_client():
    """Lazily build a Discovery Engine SearchServiceClient.

    Uses the direct SDK rather than langchain-google-community's
    VertexAISearchRetriever, which silently requests Enterprise-only features
    (extractive answers, etc.) and 400s against Standard-tier engines.
    """
    try:
        from google.cloud import discoveryengine_v1 as de
    except ImportError:
        logger.warning("google-cloud-discoveryengine not installed.")
        return None
    try:
        return de.SearchServiceClient()
    except Exception as e:
        logger.warning("Could not initialize Discovery Engine client: %s", e)
        return None


def _vertex_serving_config() -> str:
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    location = os.getenv("VERTEX_SEARCH_LOCATION", "global")
    return (
        f"projects/{project}/locations/{location}/collections/default_collection/"
        f"engines/{VERTEX_ENGINE_ID}/servingConfigs/default_search"
    )


def _retrieve_via_vertex(
    query: str, topic: str | None, k: int,
) -> list[dict[str, Any]]:
    client = _vertex_client()
    if client is None:
        return []

    from google.cloud import discoveryengine_v1 as de

    # No filter: struct fields aren't marked filterable by default in the
    # auto-detected schema, and trying to use one returns 400. Topic bias is
    # baked into the query via the augmented query string below.
    augmented = query
    if topic:
        augmented = f"{query} [topic: {topic}]"

    req = de.SearchRequest(
        serving_config=_vertex_serving_config(),
        query=augmented,
        # Exactly k results — Vertex charges per query not per result, but
        # smaller responses are slightly cheaper to deserialize and never
        # accidentally pull in extractive_answer/snippet costs.
        page_size=max(k, 1),
    )
    try:
        resp = client.search(req)
        results = list(resp.results)
    except Exception as e:
        logger.warning("Vertex AI Search query failed: %s", e)
        return []

    out: list[dict[str, Any]] = []
    for r in results[:k]:
        sd = dict(r.document.struct_data) if r.document.struct_data else {}
        out.append({
            "issue": sd.get("issue", ""),
            "root_cause": sd.get("root_cause", ""),
            "solution": sd.get("solution", ""),
            "category_tag": sd.get("category_tag", ""),
            "score": 1.0,  # Discovery Engine doesn't surface a numeric score in standard tier
            "source_id": r.document.id,
        })
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def retrieve_troubleshooting(
    query: str,
    topic: str | None = None,
    sub_topic: str | None = None,
    k: int = 3,
) -> list[dict[str, Any]]:
    """Return top-k troubleshooting docs for a merchant query.

    Prefers Vertex AI Search when configured (production with GCP billing).
    Otherwise uses the local FAISS index built from ``vertex_corpus.jsonl``.

    Cost note: every Vertex call costs ~$0.002. Identical queries are served
    from an in-process LRU cache (see _query_cache); very short queries skip
    Vertex entirely. Trailing whitespace / case differences are normalized
    so e.g. "Order Gagal?" and "order gagal?" share a cache slot.
    """
    global _vertex_query_count, _cache_hit_count

    # Skip Vertex on empty / garbage input — no point paying for it.
    q_clean = (query or "").strip()
    if len(q_clean) < 3:
        return []

    # Cache key: normalized query + lowered topic/sub_topic + k.
    cache_key = (
        " ".join(q_clean.lower().split()),
        (topic or "").lower(),
        (sub_topic or "").lower(),
        int(k),
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        _cache_hit_count += 1
        logger.debug("rag cache hit (%d total) for %r", _cache_hit_count, cache_key[0][:60])
        return cached

    results: list[dict[str, Any]] = []
    if vertex_search_available():
        results = _retrieve_via_vertex(query, topic, k)
        _vertex_query_count += 1
        logger.info(
            "vertex query #%d (%d cached, %d cache hits): %r -> %d results",
            _vertex_query_count, len(_query_cache), _cache_hit_count,
            q_clean[:60], len(results),
        )
        if not results:
            logger.warning(
                "Vertex AI Search returned no results; falling back to FAISS.",
            )
    if not results:
        results = _retrieve_via_faiss(query, topic, sub_topic, k)

    _cache_put(cache_key, results)
    return results


def get_retriever():
    """Return the active Discovery Engine search client or None.

    The FAISS path uses :class:`FAISS` directly via ``_faiss_store()``; this
    helper only exposes the Vertex AI Search client for callers that need
    raw SDK access.
    """
    if vertex_search_available():
        return _vertex_client()
    return None
