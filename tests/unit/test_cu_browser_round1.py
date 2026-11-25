"""Round-1 designer YAML loads, every task has an oracle, and the fixture never forwards writes off-origin."""
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/desktop"))
import cu_browser_round1 as r1  # noqa: E402
from cu_browser_suite import T  # noqa: E402
from cu_tasks.round1_fixture import Fx2  # noqa: E402

DIR = ROOT / "docs/supymem/beta-2026-10-04/browser-tests"


def test_all_round1_tasks_load_with_oracles():
    tasks = r1.load_tasks([DIR / f"{n}_tasks.yaml" for n in ("short", "long", "ambiguous")], T, Fx2)
    assert len(tasks) == 91 and len({t.id for t in tasks}) == 91
    for t in tasks:
        assert callable(t.check) and callable(t.guard) and t.fx_cls is Fx2
        assert t.done_ok or t.needs_ok, t.id


def test_readme_designer_scope_allows_raw_source_get_but_no_write_or_foreign_document(monkeypatch):
    monkeypatch.delenv("CU_R1_PUBLIC_XHR", raising=False)
    tasks = yaml.safe_load((DIR / "short_tasks.yaml").read_text())["tasks"]
    task = next(t for t in tasks if t["id"] == "s10_github_readme_install")
    assert task["allowed_origins"] == ["github.com", "raw.githubusercontent.com"]
    handlers, receipt = [], {}
    r1.make_guard(task["allowed_origins"], public=True)(
        SimpleNamespace(route=lambda pattern, fn: handlers.append(fn)),
        SimpleNamespace(base="http://127.0.0.1:7"), receipt,
    )
    for method, url, expected in (
        ("GET", "https://raw.githubusercontent.com/pallets/flask/main/README.md", "allowed"),
        ("POST", "https://raw.githubusercontent.com/pallets/flask/main/README.md", "blocked"),
        ("GET", "https://foreign.example/README.md", "blocked"),
    ):
        outcomes = []
        route = SimpleNamespace(
            request=SimpleNamespace(method=method, url=url, resource_type="document",
                                    frame=SimpleNamespace(parent_frame=None)),
            continue_=lambda: outcomes.append("allowed"), abort=lambda: outcomes.append("blocked"),
        )
        handlers[0](route)
        assert outcomes == [expected]
    assert len(receipt["blocked"]) == 2
