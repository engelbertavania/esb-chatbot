"""Create the Vertex AI Search datastore for ESB ticket retrieval.

Idempotent: if a datastore with the target ID already exists, the script
simply prints its details and exits 0.

Cost configuration (these are the price-optimized choices):
  - solution_type   = SOLUTION_TYPE_SEARCH       (no chat/recommendation extras)
  - industry        = GENERIC                    (no vertical-specific add-ons)
  - content_config  = NO_CONTENT                 (structured-only, no PDF/HTML
                                                  bytes stored alongside)

After the datastore is ready, run `_trigger_import.py` to ingest the corpus
and `_engine_setup.py` to create the Search engine on top of it. Both of
those scripts already use the cheapest tier (SEARCH_TIER_STANDARD, no
add-ons) — see _engine_setup.py:39-42.
"""
import config  # noqa: F401 — loads .env

import os
import sys

from google.api_core.exceptions import AlreadyExists
from google.cloud import discoveryengine_v1 as de


DATASTORE_ID = "esb-tickets"  # short, slug-style id (no auto-timestamp suffix)


def main() -> None:
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    location = os.getenv("VERTEX_SEARCH_LOCATION", "global")

    parent = (
        f"projects/{project}/locations/{location}/collections/default_collection"
    )

    client = de.DataStoreServiceClient()

    # Idempotency: check if it already exists by listing.
    existing = list(client.list_data_stores(parent=parent))
    for ds in existing:
        if ds.name.endswith(f"/dataStores/{DATASTORE_ID}"):
            print(f"Datastore already exists: {ds.name}")
            print(f"  displayName: {ds.display_name}")
            print(f"  contentConfig: {ds.content_config.name}")
            print(f"\nSet in .env:")
            print(f"  VERTEX_DATASTORE_ID={DATASTORE_ID}")
            return

    ds = de.DataStore(
        display_name="ESB Tickets",
        industry_vertical=de.IndustryVertical.GENERIC,
        solution_types=[de.SolutionType.SOLUTION_TYPE_SEARCH],
        content_config=de.DataStore.ContentConfig.NO_CONTENT,
    )
    req = de.CreateDataStoreRequest(
        parent=parent,
        data_store=ds,
        data_store_id=DATASTORE_ID,
    )
    print(f"Creating datastore {DATASTORE_ID} in {parent}...")
    try:
        op = client.create_data_store(request=req)
    except AlreadyExists:
        print("Already exists (race) — re-run to see existing config.")
        return
    print(f"Operation: {op.operation.name}")
    print("Waiting (timeout 300s)...")
    try:
        result = op.result(timeout=300)
        print(f"DONE: {result.name}")
        print(f"\nSet in .env:")
        print(f"  VERTEX_DATASTORE_ID={DATASTORE_ID}")
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
