"""Independent varied corpus checks fallback routing and live-site safety."""
import importlib.util
import json
from pathlib import Path

from tests.company_os_harness import load_company_os_app


def test_synthetic_golden_paths_meet_acceptance_bar():
    load_company_os_app()
    from app.intake.smart import classify_fallback
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("intake_eval", root / "scripts/intake/eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = json.loads((root / "scripts/intake/golden.json").read_text())
    report = module.evaluate(classify_fallback, cases)
    assert report["cases"] >= 40
    assert report["path_accuracy"] >= 0.9, report["failures"]
    assert report["live_site_no_build_accuracy"] == 1.0, report["failures"]
    assert report["forbidden_asset_ids"] == []
