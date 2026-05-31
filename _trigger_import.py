"""Trigger a fresh Vertex AI Search import from the GCS JSONL.

Uses INCREMENTAL reconciliation mode so re-runs upsert by document id.
Use FULL to wipe + reimport (slower).
"""
import config  # noqa
import os
import sys
import time

from google.cloud import discoveryengine_v1 as de

project = os.environ["GOOGLE_CLOUD_PROJECT"]
location = os.getenv("VERTEX_SEARCH_LOCATION", "global")
data_store = os.environ["VERTEX_DATASTORE_ID"]
gcs_uri = sys.argv[1] if len(sys.argv) > 1 else "gs://sukatampil_chatbot/vertex_search_corpus.jsonl"

parent = (
    f"projects/{project}/locations/{location}/collections/default_collection/"
    f"dataStores/{data_store}/branches/default_branch"
)
print(f"Parent:  {parent}")
print(f"Source:  {gcs_uri}")

client = de.DocumentServiceClient()
req = de.ImportDocumentsRequest(
    parent=parent,
    gcs_source=de.GcsSource(input_uris=[gcs_uri], data_schema="document"),
    reconciliation_mode=de.ImportDocumentsRequest.ReconciliationMode.FULL,
)
op = client.import_documents(request=req)
print(f"Operation: {op.operation.name}")
print("Waiting (timeout 600s)...")
try:
    res = op.result(timeout=600)
    print("DONE")
    print(f"  successCount: {getattr(res, 'success_count', '?')}")
    print(f"  failureCount: {getattr(res, 'failure_count', '?')}")
    md = op.metadata
    if md:
        print(f"  metadata.successCount: {getattr(md, 'success_count', '?')}")
        print(f"  metadata.failureCount: {getattr(md, 'failure_count', '?')}")
    for err in (getattr(res, "error_samples", None) or [])[:5]:
        print(f"  error: {err}")
except Exception as e:
    print(f"ERROR waiting: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)
