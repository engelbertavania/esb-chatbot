"""Check how many documents are actually in the data store."""
import config  # noqa
import os
import sys

from google.cloud import discoveryengine_v1 as de

project = os.environ["GOOGLE_CLOUD_PROJECT"]
location = os.getenv("VERTEX_SEARCH_LOCATION", "global")
data_store = os.environ["VERTEX_DATASTORE_ID"]

parent = (
    f"projects/{project}/locations/{location}/collections/default_collection/"
    f"dataStores/{data_store}/branches/default_branch"
)
print(f"Parent: {parent}")

client = de.DocumentServiceClient()
try:
    req = de.ListDocumentsRequest(parent=parent, page_size=5)
    page = client.list_documents(request=req)
    docs = list(page)
    print(f"First page returned {len(docs)} documents")
    for d in docs[:3]:
        print("---")
        print("id:", d.id)
        print("name:", d.name)
        sd = dict(d.struct_data) if d.struct_data else {}
        print("struct_data keys:", list(sd.keys()))
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)
