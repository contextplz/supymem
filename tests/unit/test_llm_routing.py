"""Every purpose string used in the repo must resolve to a tier."""

from __future__ import annotations

import ast
from pathlib import Path

from llm_gateway.routing import UnroutedPurposeError, all_purposes, resolve_tier

ROOT = Path(__file__).resolve().parents[2]
LLM_CALLS = {"transmit", "transmit_stream", "chat", "achat", "chat_json", "synthesize", "resolve_tier"}


def literal_llm_purposes(source: str) -> set[str]:
    """Inspect model entry points, excluding ledger and non-model tool purposes.

    Module constants are included; dynamic purposes remain checked at runtime.
    This is a source regression check, not general Python data-flow analysis.
    """
    tree = ast.parse(source)
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
        if name not in LLM_CALLS:
            continue
        value = next((kw.value for kw in node.keywords if kw.arg == "purpose"), None)
        if name == "resolve_tier" and node.args:
            value = node.args[0]
        purpose = value.value if isinstance(value, ast.Constant) else constants.get(value.id) if isinstance(value, ast.Name) else None
        if isinstance(purpose, str):
            found.add(purpose)
    return found


def test_routing_table_has_expected_purposes() -> None:
    purposes = all_purposes()
    assert "intake.classify" in purposes
    assert "charter.synthesize" in purposes
    assert resolve_tier("intake.classify") == "flash"
    assert resolve_tier("charter.synthesize") == "pro"


def test_unrouted_purpose_hard_error() -> None:
    try:
        resolve_tier("this.purpose.does.not.exist")
        assert False, "expected UnroutedPurposeError"
    except UnroutedPurposeError:
        pass


def test_every_purpose_string_in_repo_is_routed() -> None:
    """Fail on unknown literal/constant model purposes, including new families."""
    found: set[str] = set()
    scan_roots = [
        ROOT / "services" / "company_os",
        ROOT / "packages" / "llm_gateway",
        ROOT / "packages" / "coding_agents",
    ]
    for root in scan_roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            found.update(literal_llm_purposes(path.read_text(encoding="utf-8")))
    missing = []
    for purpose in sorted(found):
        try:
            resolve_tier(purpose)
        except UnroutedPurposeError:
            missing.append(purpose)
    assert not missing, f"unrouted purposes: {missing}"


def test_inventory_distinguishes_model_calls_from_credit_and_search_operations() -> None:
    assert literal_llm_purposes('''
PURPOSE = "novel.model"
sole.transmit([], purpose=PURPOSE)
resolve_tier("content.writer")
synthesize(run, payload, purpose="idea.validate")
pools.allocate(session, purpose="build." + key)
CreditReservation(purpose="build.run")
perform(run, key, purpose="research.search", callback=search)
''') == {"novel.model", "content.writer", "idea.validate"}


def test_production_build_and_content_writer_routes() -> None:
    assert resolve_tier("build.model") == "pro"
    assert resolve_tier("content.writer") == "pro"
    assert resolve_tier("simulation.rehearse") == "pro"
