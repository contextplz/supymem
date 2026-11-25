#!/usr/bin/env python3
"""Content-free evaluation of the synthetic intake cases; no provider calls."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).with_name("golden.json")


def case_text(case: dict) -> str:
    repeated = case.get("repeat", {})
    return case["text"] + repeated.get("text", "") * repeated.get("count", 0) + case.get("suffix", "")


def evaluate(classifier, cases: list[dict]) -> dict:
    """Only identifiers and path metadata leave this evaluator."""
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("golden cases require unique IDs and at least one case")
    correct = 0
    live_total = live_safe = 0
    forbidden_assets = []
    failures = []
    language_failures = []
    for case in cases:
        try:
            result = classifier(case_text(case), page_context=case.get("page_context")).to_dict()
            actual = result["path"]
        except Exception as exc:
            result, actual = {"extracted": {}}, "error:" + type(exc).__name__
        correct += actual == case["expected_path"]
        if actual != case["expected_path"]:
            failures.append({"id": case["id"], "expected": case["expected_path"], "actual": actual})
        if case.get("live_site"):
            live_total += 1
            live_safe += actual in {"gtm", "landing_gtm", "research", "clarify"}
        if case.get("must_not_record_site") and result["extracted"].get("existing_site_url"):
            forbidden_assets.append(case["id"])
        if case.get("expected_language") and result["extracted"].get("language") != case["expected_language"]:
            language_failures.append(case["id"])
    accuracy = correct / len(cases)
    live_safety = live_safe / live_total if live_total else 0.0
    return {"mode": "deterministic_fallback_no_provider", "cases": len(cases), "correct": correct,
            "path_accuracy": round(accuracy, 4), "live_sites": live_total,
            "live_site_no_build_accuracy": round(live_safety, 4),
            "forbidden_asset_ids": forbidden_assets, "language_mismatch_ids": language_failures,
            "failures": failures, "passed": accuracy >= 0.9 and live_safety == 1.0 and not forbidden_assets}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=GOLDEN)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "services" / "company_os"))
    from app.intake.smart import classify_fallback
    report = evaluate(classify_fallback, json.loads(args.golden.read_text()))
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
