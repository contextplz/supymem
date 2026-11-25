"""The checked-in console catalog copy must match the server catalog."""

import importlib.util
from pathlib import Path

from tests.company_os_harness import load_company_os_app

ROOT = Path(__file__).resolve().parents[3]


def test_console_catalog_is_current():
    load_company_os_app()
    spec = importlib.util.spec_from_file_location("export_console_catalog", ROOT / "scripts/analytics/export_console_catalog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.TARGET.read_text() == module.render(module.build()), (
        "run python3 scripts/analytics/export_console_catalog.py")
