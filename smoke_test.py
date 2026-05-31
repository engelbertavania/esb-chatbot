"""Smoke test for the LangChain classifier + RAG retriever + conversation flow.

Run after ``python ingest.py``:

    ./venv/Scripts/python.exe smoke_test.py

Works without GCP credentials by falling back to the rule-based mock
classifier and the local keyword retriever in ``rag.py``. With creds and
``VERTEX_DATASTORE_ID`` set, exercises the real Vertex AI Search path.
"""

from __future__ import annotations

import config  # noqa: F401 — loads .env before agent/rag imports fire

import os
import textwrap

from agent import classify_intent, process_message
from rag import retrieve_troubleshooting, _vertex_search_available

# ---------------------------------------------------------------------------
# Part 1: classifier + retriever (one-shot, no conversation state)
# ---------------------------------------------------------------------------

SAMPLES = [
    "Saya butuh aktifasi Platform order Dine In untuk comcode 12345",
    "Push to POS gagal, pesanan tidak masuk ke kasir",
    "Bagaimana cara setup payment gateway dengan QRIS?",
    "Foto menu saya tidak bisa di-upload, error terus",
    "Siapa CEO ESB?",
]


def part_one() -> None:
    print("=" * 72)
    print("PART 1 — classifier + retriever")
    print("Vertex AI Search available:", _vertex_search_available())
    print("=" * 72)

    for i, query in enumerate(SAMPLES, start=1):
        print(f"\n[{i}] Query: {query}")
        intent = classify_intent(query)
        print(f"    -> topic={intent['topic']!r}  sub_topic={intent['sub_topic']!r}  "
              f"confidence={intent['confidence']}")
        if intent["topic"] in ("Out of Scope", "Low Confidence"):
            continue
        docs = retrieve_troubleshooting(
            query=query, topic=intent["topic"], sub_topic=intent["sub_topic"], k=2,
        )
        for j, d in enumerate(docs, start=1):
            issue = textwrap.shorten(d["issue"] or "-", width=70)
            solution = textwrap.shorten(d["solution"] or "-", width=100)
            print(f"    [{j}] score={d['score']}  cat={d['category_tag']}  id={d['source_id']}")
            print(f"        issue: {issue}")
            print(f"        solution: {solution}")


# ---------------------------------------------------------------------------
# Part 2: multi-turn conversation through process_message
# ---------------------------------------------------------------------------

def _show(chat_id: str, user_msg: str) -> dict:
    resp = process_message(chat_id, user_msg)
    print(f"\n  USER ({chat_id}): {user_msg}")
    print(f"  BOT  [{resp['type']}]: {resp['text']}")
    if resp.get("options"):
        print(f"        options: {resp['options']}")
    if resp["type"] == "ticket_form":
        for k in ("category", "sub_topic", "issue_detail", "steps_attempted"):
            if resp.get(k):
                shown = textwrap.shorten(str(resp[k]), width=80)
                print(f"        {k}: {shown}")
    return resp


def part_two() -> None:
    print("\n" + "=" * 72)
    print("PART 2 — full conversation via process_message")
    print("=" * 72)

    # Scenario A: user goes Tidak, Tidak, Tidak -> escalation after K=3 steps
    print("\n--- Scenario A: persistent failure -> escalation ---")
    _show("chat-A", "Push to POS gagal, pesanan tidak masuk ke kasir")
    _show("chat-A", "Tidak")
    _show("chat-A", "Tidak")
    _show("chat-A", "Tidak")

    # Scenario B: user answers Ya on first step -> resolved
    print("\n--- Scenario B: first step resolves it ---")
    _show("chat-B", "Bagaimana cara setup payment gateway dengan QRIS?")
    _show("chat-B", "Ya")
    _show("chat-B", "Terima kasih")  # back to IDLE, this will reclassify

    # Scenario C: free text mid-troubleshooting -> nudge to Ya/Tidak
    print("\n--- Scenario C: free text mid-flow gets nudged ---")
    _show("chat-C", "Aktifasi Platform order untuk comcode 99999")
    _show("chat-C", "Hmm sepertinya tidak jalan")  # non Ya/Tidak

    # Scenario D: Out of Scope
    print("\n--- Scenario D: Out of Scope ---")
    _show("chat-D", "Siapa CEO ESB?")


if __name__ == "__main__":
    part_one()
    part_two()
