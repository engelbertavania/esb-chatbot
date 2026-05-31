"""Convert vertex_corpus.jsonl into Vertex AI Search format and upload to GCS.

Vertex AI Search structured datastores want each line to be::

    {"id": "ticket-0001", "structData": {"content": "...", "issue": "...", ...}}

Our ingest.py wraps fields under ``metadata``, which Vertex AI Search would
import as a single opaque object. This script flattens metadata into
structData and uploads the result to a GCS bucket so you can point a
Vertex AI Search datastore at it.

Usage:
    .\\venv\\Scripts\\python.exe prepare_vertex_search.py <bucket-name>

Requires:
    - GOOGLE_CLOUD_PROJECT in .env (or shell)
    - `gcloud auth application-default login` already run
    - `pip install google-cloud-storage` (already in requirements.txt)
"""

from __future__ import annotations

import config  # noqa: F401 — loads .env

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent
SOURCE = ROOT / "vertex_corpus.jsonl"
CONVERTED = ROOT / "vertex_search_corpus.jsonl"


def convert(source: Path, dest: Path, flat: bool = False) -> int:
    """Convert ingest.py output to Vertex AI Search JSONL.

    Two output shapes — pick based on what the Console accepts:
      - wrapped (default): ``{"id": "...", "structData": {...}}`` — for
        Discovery Engine direct ingest / generic data stores.
      - flat (``--flat``): top-level fields with ``id`` — for search-app
        data stores that auto-detect schema from flat JSONL.
    """
    n = 0
    with source.open("r", encoding="utf-8") as src, dest.open("w", encoding="utf-8") as dst:
        for line in src:
            line = line.strip()
            if not line:
                continue
            doc = json.loads(line)
            md = doc.get("metadata") or {}
            fields = {
                "content": doc.get("content", ""),
                "category_tag": md.get("category_tag", ""),
                "tags": md.get("tags", ""),
                "services": md.get("services", ""),
                "priority": md.get("priority", ""),
                "issue": md.get("issue", ""),
                "detail": md.get("detail", ""),
                "root_cause": md.get("root_cause", ""),
                "solution": md.get("solution", ""),
            }
            if flat:
                out = {"id": doc["id"], **fields}
            else:
                out = {"id": doc["id"], "structData": fields}
            dst.write(json.dumps(out, ensure_ascii=False) + "\n")
            n += 1
    return n


def upload(bucket_name: str, local_path: Path, blob_name: str) -> str:
    from google.cloud import storage  # lazy import so convert() works without ADC

    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        print("ERROR: GOOGLE_CLOUD_PROJECT is not set in .env or shell.", file=sys.stderr)
        sys.exit(1)

    client = storage.Client(project=project)
    bucket = client.bucket(bucket_name)
    if not bucket.exists():
        print(f"Bucket gs://{bucket_name} does not exist — create it first:")
        print(f"  gcloud storage buckets create gs://{bucket_name} --location=US")
        sys.exit(1)

    blob = bucket.blob(blob_name)
    blob.upload_from_filename(str(local_path))
    uri = f"gs://{bucket_name}/{blob_name}"
    return uri


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]

    if not args:
        print("Usage: python prepare_vertex_search.py <bucket-name> [--flat]", file=sys.stderr)
        print("  --flat: output flat JSONL (no structData wrapper) for search-app data stores", file=sys.stderr)
        sys.exit(2)
    bucket_name = args[0]
    flat = "--flat" in flags

    if not SOURCE.exists():
        print(f"ERROR: {SOURCE} not found. Run `python ingest.py` first.", file=sys.stderr)
        sys.exit(1)

    n = convert(SOURCE, CONVERTED, flat=flat)
    shape = "flat (no structData wrapper)" if flat else "wrapped with structData"
    print(f"Converted {n} documents -> {CONVERTED} [{shape}]")

    uri = upload(bucket_name, CONVERTED, "vertex_search_corpus.jsonl")
    print(f"Uploaded to {uri}")
    print()
    print("=" * 72)
    print("NEXT STEPS — create the Vertex AI Search datastore")
    print("=" * 72)
    print("1. Open https://console.cloud.google.com/gen-app-builder/data-stores")
    print("2. Click CREATE DATA STORE")
    print('3. Source: "Cloud Storage"')
    print(f"   File location: {uri}")
    print('   Kind of data: "Structured data (JSONL)"')
    print("   Import schedule: One time")
    print("4. Configure: region = global (or your GOOGLE_CLOUD_LOCATION)")
    print("5. Name the datastore e.g. esb-tickets — note the DATA STORE ID")
    print("   (looks like esb-tickets_1234567890123)")
    print("6. Add to .env:")
    print("     VERTEX_DATASTORE_ID=<the id from step 5>")
    print()
    print("Import takes ~3-5 minutes. When status shows 'Active', run:")
    print("     .\\venv\\Scripts\\python.exe smoke_test.py")


if __name__ == "__main__":
    main()
