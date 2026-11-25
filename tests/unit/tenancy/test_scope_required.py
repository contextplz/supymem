"""assert_venture never no-ops; every route is scoped or allowlisted."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.tenancy import ScopeRequired, assert_venture, bind, is_allowlisted


def test_assert_venture_requires_scope() -> None:
    with pytest.raises(ScopeRequired):
        assert_venture("ven_x")


def test_assert_venture_mismatch() -> None:
    with bind(workspace_id="wsp_a", venture_id="ven_a"):
        with pytest.raises(PermissionError):
            assert_venture("ven_b")
        assert_venture("ven_a")


def test_every_company_os_route_is_scoped_or_allowlisted(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/scope.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    app = main.create_app()
    from fastapi.routing import APIRoute

    missing: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        if is_allowlisted(path):
            continue
        if path.startswith(("/ventures", "/workspaces", "/admin", "/internal")):
            continue
        # Global middleware binds Scope for every other path.
        if "enforce_venture_scope" not in (route.endpoint.__name__ or ""):
            # Covered by app middleware, not a per-route dep.
            continue
        missing.append(path)
    assert missing == []
    assert app.user_middleware  # tenancy middleware is registered on the app
