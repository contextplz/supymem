"""Published page hosts are readable, unique and never system names."""
from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

from tests.company_os_harness import load_company_os_app


class _Session:
    def __init__(self, taken):
        self.taken = taken

    def scalar(self, stmt):
        return "app_x" if set(stmt.compile().params.values()) & self.taken else None


def test_slug_for_readable_unique_and_reserved():
    load_company_os_app()
    from app.hosting.firstlook_publish import slug_for

    assert slug_for("Supplify!", session=_Session(set())) == "supplify"
    assert slug_for("Supplify", session=_Session({"supplify"})) == "supplify-2"
    assert slug_for("Supplify", session=_Session({"supplify", "supplify-2"})) == "supplify-3"
    assert slug_for("  Acme  & Sons / Co  ", session=_Session(set())) == "acme-sons-co"
    assert len(slug_for("x" * 90, session=_Session(set()))) <= 40
    for name in ("API", "www", "Pagebuilder", "Supymem", "Admin"):
        got = slug_for(name, session=_Session(set()))
        assert got != name.lower()
    assert slug_for("!!", session=_Session(set())).startswith("site-")


def test_pagebuilder_publish_binding_uses_venture_slug(monkeypatch):
    load_company_os_app()
    import app.product_analytics.target_app as target
    from app.apps import keys
    from app.infra import apps
    from app.pagebuilder import jobs

    seen = {}
    monkeypatch.setattr(jobs.runtime, "mutate", lambda *a: ("ws", "ven", "actor") if len(seen) == 0 else None)
    monkeypatch.setattr(jobs, "db", lambda: (lambda: nullcontext(_Session({"supplify"}))))
    monkeypatch.setattr(target, "select_venture_app", lambda *a: None)
    monkeypatch.setattr(apps, "create_draft", lambda **kw: seen.update(kw) or {"id": "app_1"})
    monkeypatch.setattr(keys, "ensure_ingest_key", lambda _: {"secret": "s"})
    assert jobs.analytics_binding("j", {}, SimpleNamespace(name="Supplify")) == ("app_1", "s")
    assert seen["slug"] == "supplify-2"
