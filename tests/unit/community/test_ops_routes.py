"""API route registration smoke for Community Department."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app(reset=False)


def test_community_routes_registered():
    from app.api.ops_api import router

    paths = {getattr(r, "path", None) for r in router.routes}
    expected = {
        "/api/v1/community/status",
        "/api/v1/community/thesis",
        "/api/v1/community/plan",
        "/api/v1/community/discord/connect",
        "/api/v1/community/discord/status",
        "/api/v1/community/discord/provision",
        "/api/v1/community/discord/action",
        "/api/v1/community/lab/log",
    }
    missing = expected - paths
    assert not missing, f"missing routes: {missing}"
