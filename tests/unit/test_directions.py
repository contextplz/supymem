from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.concierge.directions import load_all, validate_entry


def test_catalog_steps_have_deep_link_locator_expect():
    entries = load_all()
    assert entries
    for e in entries:
        errs = validate_entry(e)
        assert not errs, (e.get("_file"), errs)
