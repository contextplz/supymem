"""Unit tests for front-door entry routing (35 §5)."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def entry_route(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/entry.db")
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    import importlib

    mod = importlib.import_module("app.entry.route")
    # Guard against package attribute shadowing the submodule.
    if not hasattr(mod, "route") or not callable(getattr(mod, "list_modes", None)):
        mod = importlib.reload(mod)
    return mod


def test_explicit_mode_is_certain(entry_route) -> None:
    d = entry_route.route("", explicit_mode="surprise")
    assert d.mode == "surprise"
    assert d.confidence == 1.0
    assert d.confirm_needed is False
    assert d.workspace_kind == "desk"


def test_surprise_keywords(entry_route) -> None:
    d = entry_route.route("surprise me with something")
    assert d.mode == "surprise"
    assert d.confidence >= 0.9


def test_empty_text_defaults_to_surprise(entry_route) -> None:
    d = entry_route.route("")
    assert d.mode == "surprise"
    assert d.workspace_kind == "desk"


def test_repo_url_is_existing_company(entry_route) -> None:
    d = entry_route.route("https://github.com/acme/widgets")
    assert d.mode == "existing_company"
    assert d.workspace_kind == "company"
    assert d.confirm_needed is False


def test_bug_report_never_creates_venture_when_active(entry_route) -> None:
    d = entry_route.route(
        "checkout is broken on Safari",
        active_workspace_kind="venture",
    )
    assert d.mode == "engagement"
    assert d.workspace_kind == "desk"
    assert d.confidence >= 0.9
    assert d.confirm_needed is False


def test_ambiguous_engagement_asks(entry_route) -> None:
    # A lone fix verb without an active workspace sits under the threshold.
    d = entry_route.route("please fix this")
    assert d.mode == "engagement"
    assert d.confirm_needed is True
    assert d.confidence < 0.75


def test_product_sentence_is_idea(entry_route) -> None:
    d = entry_route.route("a marketplace for restoring vintage synthesizers")
    assert d.mode == "idea"
    assert d.workspace_kind == "venture"
    assert d.confirm_needed is False


def test_existing_product_request_is_not_a_new_idea(entry_route) -> None:
    decision = entry_route.route("I already have a product and just need go-to-market help")
    assert decision.mode == "product"
    assert decision.confirm_needed is False
    assert entry_route.wants_gtm_only("I already have a product and just need GTM")
    assert entry_route.wants_gtm_only("I already have a product and only need users")
    assert entry_route.wants_gtm_only("We launched an app and want to find customers")
    assert not entry_route.wants_gtm_only("I need a GTM strategy for a product I want to build")


def test_personal_intent_needs_clear_founder_wording(entry_route) -> None:
    assert entry_route.infer_project_intent("a personal finance app for customers") == "startup"
    assert entry_route.infer_project_intent("a private tracker just for me") == "personal"
    assert entry_route.infer_project_intent("a bot for my own use, not a startup") == "personal"


def test_list_modes_covers_doors_including_product(entry_route) -> None:
    modes = entry_route.list_modes()
    assert {m["mode"] for m in modes} == {
        "idea",
        "surprise",
        "engagement",
        "existing_company",
        "product",
    }


def test_stage_aliases(entry_route) -> None:
    assert entry_route.route(explicit_mode="scratch").mode == "idea"
    assert entry_route.route(explicit_mode="product").mode == "product"
    assert entry_route.route(explicit_mode="company").mode == "existing_company"
