"""Active receipts take precedence over stale completed research."""
from tests.company_os_harness import load_company_os_app


def test_running_dossier_never_reoffers_page_choices():
    load_company_os_app()
    from app.ventures.next_step import _founder_resolve
    step = _founder_resolve("ven_fixture", "/v/ven_fixture", {
        "idea_confirmed": True, "dossier_state": "running", "active_jobs": False})
    assert step["key"] == "research.running"
    assert "alternatives" not in step
