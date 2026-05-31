"""List or create a Vertex AI Search engine (Search App) for the data store."""
import config  # noqa
import os
import sys
import time

from google.cloud import discoveryengine_v1 as de

project = os.environ["GOOGLE_CLOUD_PROJECT"]
location = os.getenv("VERTEX_SEARCH_LOCATION", "global")
data_store = os.environ["VERTEX_DATASTORE_ID"]
engine_id = "esb-chatbot-engine"

collection_parent = (
    f"projects/{project}/locations/{location}/collections/default_collection"
)

eng_client = de.EngineServiceClient()

# 1. List existing engines
print("=== Existing engines ===")
existing = list(eng_client.list_engines(parent=collection_parent))
for e in existing:
    print(f"  - {e.name}")
    print(f"    displayName: {e.display_name}")
    print(f"    dataStoreIds: {list(e.data_store_ids)}")

# 2. If none reference our data store, create one
has_engine = any(data_store in list(e.data_store_ids) for e in existing)
if has_engine:
    print(f"\nData store {data_store} already attached to an engine.")
    sys.exit(0)

print(f"\n=== Creating engine {engine_id} ===")
engine = de.Engine(
    display_name="ESB Chatbot Engine",
    solution_type=de.SolutionType.SOLUTION_TYPE_SEARCH,
    industry_vertical=de.IndustryVertical.GENERIC,
    search_engine_config=de.Engine.SearchEngineConfig(
        search_tier=de.SearchTier.SEARCH_TIER_STANDARD,
        search_add_ons=[],
    ),
    data_store_ids=[data_store],
)
req = de.CreateEngineRequest(parent=collection_parent, engine=engine, engine_id=engine_id)
op = eng_client.create_engine(request=req)
print(f"Operation: {op.operation.name}")
print("Waiting (timeout 300s)...")
try:
    result = op.result(timeout=300)
    print(f"DONE: {result.name}")
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)
