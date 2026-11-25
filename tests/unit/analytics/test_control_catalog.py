"""The client and ingest must agree before a named UI event can be retained."""

import json
from pathlib import Path
from tests.company_os_harness import load_company_os_app

def test_closed_control_catalog_matches_console():
    load_company_os_app()
    from app.analytics.control_catalog import CONTROL_FEATURE
    from app.analytics.feature_catalog import CONTROL_SLUG
    # The console copy is generated (see test_console_catalog.py); its legacy label must stay the one history uses.
    source = (Path(__file__).resolve().parents[3] / "apps/console/src/lib/featureCatalog.ts").read_text()
    client = json.loads(source.split("} = ", 1)[1].rsplit(";", 1)[0])["controls"]
    assert {k: v[1] for k, v in client.items() if v[1]} == CONTROL_FEATURE
    assert {k: v[0] for k, v in client.items()} == CONTROL_SLUG
    assert all(len(control_id) <= 40 for control_id in client)
