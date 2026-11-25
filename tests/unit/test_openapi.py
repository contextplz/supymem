"""The generated API contract keeps operation identifiers unique."""

import warnings

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Match

from tests.company_os_harness import load_company_os_app


@pytest.mark.parametrize("fixture_path", ["warehouse/stock.csv", "recipes/dinner.html"])
def test_openapi_operation_ids_are_unique(tmp_path, monkeypatch, fixture_path):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.json"))
    application = load_company_os_app().create_app()
    from app.api.browser_qualification_api import router

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        schema = application.openapi()

    operation_ids = [
        operation["operationId"]
        for path_item in schema["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    assert operation_ids
    assert len(operation_ids) == len(set(operation_ids))
    assert not [warning for warning in caught if "Duplicate Operation ID" in str(warning.message)]

    for method in ("GET", "HEAD", "POST"):
        scope = {
            "type": "http",
            "method": method,
            "root_path": "",
            "path": f"/public/browser-qualification/{'a' * 32}/{fixture_path}",
        }
        matches = [
            route for route in router.routes
            if isinstance(route, APIRoute) and route.matches(scope)[0] == Match.FULL
        ]
        assert len(matches) == 1
        assert matches[0].endpoint.__name__ == "fixture"
        assert "operationId" in schema["paths"][matches[0].path_format][method.lower()]
