"""Probe Vertex AI Search via both serving config paths."""
import config  # noqa
import os
import sys

from google.cloud import discoveryengine_v1 as de

project = os.environ["GOOGLE_CLOUD_PROJECT"]
location = os.getenv("VERTEX_SEARCH_LOCATION", "global")
data_store = os.environ["VERTEX_DATASTORE_ID"]
engine = "esb-chatbot-engine"

paths = {
    "data store": (
        f"projects/{project}/locations/{location}/collections/default_collection/"
        f"dataStores/{data_store}/servingConfigs/default_search"
    ),
    "engine": (
        f"projects/{project}/locations/{location}/collections/default_collection/"
        f"engines/{engine}/servingConfigs/default_search"
    ),
}

client = de.SearchServiceClient()
for label, serving_config in paths.items():
    print(f"\n=== {label} ===")
    print(f"Serving config: {serving_config}")
    req = de.SearchRequest(serving_config=serving_config, query="aktifasi platform order", page_size=3)
    try:
        resp = client.search(req)
        results = list(resp.results)
        print(f"Got {len(results)} results")
        for r in results[:3]:
            sd = dict(r.document.struct_data) if r.document.struct_data else {}
            issue = sd.get("issue", "")[:60]
            print(f"  - id={r.document.id}  issue={issue!r}")
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
