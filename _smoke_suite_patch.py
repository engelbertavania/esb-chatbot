"""End-to-end smoke test for the Sukabot Suite patcher.

Exercises _build_patched_bundle + the per-request placeholder substitution
that serve_dashboard does. Verifies:
  * The cached output still contains the iframe shell
  * The data placeholder is gone after substitution and the JSON appears
    inside the encoded srcdoc
  * Decoding the iframe srcdoc, rebuilding the bundler template, and reading
    it as JSON all work (i.e., we didn't break the encoding)
"""
import config  # noqa: F401 — load .env
import base64
import gzip
import html as _html
import json
import re

from main import (
    _APPCS_IFRAME_RE,
    _build_patched_bundle,
    _encode_for_srcdoc_placeholder,
    DATA_PLACEHOLDER,
    SUBMAP_PLACEHOLDER,
)


def test_round_trip() -> None:
    patched = _build_patched_bundle()
    assert "<iframe" in patched, "iframe shell missing"
    assert DATA_PLACEHOLDER in patched, "data placeholder lost in caching"
    assert SUBMAP_PLACEHOLDER in patched, "submap placeholder lost in caching"

    sample = [
        {
            "id": "CS-99999",
            "dbId": 1,
            "created": "2026-06-01T12:00:00",
            "user": "Test Merchant",
            "topic": "Order Management",
            "subTopic": "push_to_pos_failed",
            "status": "New",
            "phrasing": 'Order tidak masuk ke POS, ada </script> juga',  # adversarial
            "intent": "Issue/Complaint",
        }
    ]
    submap = {"push_to_pos_failed": "Order Management"}

    data_json = json.dumps(sample, ensure_ascii=False, default=str)
    submap_json = json.dumps(submap, ensure_ascii=False)

    final_html = (
        patched
        .replace(DATA_PLACEHOLDER, _encode_for_srcdoc_placeholder(data_json), 1)
        .replace(SUBMAP_PLACEHOLDER, _encode_for_srcdoc_placeholder(submap_json), 1)
    )

    assert DATA_PLACEHOLDER not in final_html, "data placeholder still present"
    assert SUBMAP_PLACEHOLDER not in final_html, "submap placeholder still present"

    # Pull the patched iframe srcdoc back out and decode it.
    m = _APPCS_IFRAME_RE.search(final_html)
    assert m, "iframe vanished after substitution"
    decoded = _html.unescape(m.group(2))

    # The template script must still be valid JSON containing valid HTML.
    mt = re.search(
        r'<script type="__bundler/template">(.*?)</script>',
        decoded, re.DOTALL,
    )
    assert mt, "template script missing after patch"
    template_html = json.loads(mt.group(1))  # raises on bad JSON
    assert "<div id=\"root\"></div>" in template_html, "root div missing"

    # The data injection script must be present, with our sample data inlined
    # as a parseable JS array.
    inj_match = re.search(
        r'window\.__INJECTED_TICKETS\s*=\s*(\[.*?\]);',
        template_html, re.DOTALL,
    )
    assert inj_match, "injection script not inlined into template"
    parsed = json.loads(inj_match.group(1))
    assert isinstance(parsed, list) and parsed[0]["id"] == "CS-99999", \
        f"injected JSON did not round-trip cleanly: {parsed!r}"
    # Adversarial </script> in the phrasing should have been neutralised.
    assert "</script>" not in inj_match.group(1), "</script> leaked through"
    assert "<\\/script>" in inj_match.group(1) or "<\\u002Fscript>" in inj_match.group(1) \
        or "<\\/" in inj_match.group(1), \
        "</ should be escaped as <\\/"

    # The manifest must still be valid JSON with at least one JS entry whose
    # decoded source contains today's date (the pivot replacement worked).
    mm = re.search(
        r'<script type="__bundler/manifest">(.*?)</script>',
        decoded, re.DOTALL,
    )
    assert mm, "manifest script missing after patch"
    manifest = json.loads(mm.group(1))
    import datetime
    today = datetime.date.today().isoformat()
    found_today = False
    for entry in manifest.values():
        if not entry.get("mime", "").endswith("javascript"):
            continue
        raw = base64.b64decode(entry["data"])
        if entry.get("compressed"):
            try:
                raw = gzip.decompress(raw)
            except Exception:
                continue
        try:
            src = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if f"{today}T12:00:00" in src or f"{today}T00:00:00" in src:
            found_today = True
            break
    assert found_today, f"no JS payload references today ({today}); date pivot failed"

    print("smoke OK")
    print(f"  patched suite size:  {len(patched):,}")
    print(f"  with-data suite:     {len(final_html):,}")
    print(f"  decoded srcdoc:      {len(decoded):,}")
    print(f"  template html:       {len(template_html):,}")
    print(f"  manifest entries:    {len(manifest)}")


if __name__ == "__main__":
    test_round_trip()
