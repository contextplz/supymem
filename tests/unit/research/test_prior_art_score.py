"""Synthetic scorer regressions: no research, network, provider, or live claim."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

from scripts.evals.research_prior_art.score import (
    DIMENSIONS, aggregate, citation_valid, identity, score_case,
)

ROOT = Path(__file__).resolve().parents[3]
GOLD = ROOT / "scripts/evals/research_prior_art/gold"


def fixture():
    gold = {"id": "synthetic", "direct": [{"domain": "alpha.example"}, {"domain": "beta.example"}],
            "dead": [{"domain": "old.example"}], "oss": []}
    source = {"id": "fixture_a", "url": "https://alpha.example/", "title": "Alpha",
              "summary": "Alpha tracks beans. Core costs $20/month.", "page_verified": True,
              "prior_art": {"company_name": "Alpha", "published_at": "2025-05-01T00:00:00Z"}}
    old = {"id": "fixture_old", "url": "https://www.ycombinator.com/companies/old", "summary": "Old tracks beans.",
           "prior_art": {"company_name": "Old", "company_url": "https://old.example/", "status": "Inactive"}}
    company = {"key": "alpha.example", "name": "Alpha", "website": "https://alpha.example/",
               "finding_ids": ["fixture_a"], "status": "live", "status_evidence": [{"finding_id": "fixture_a", "basis": "page_available"}],
               "what_they_built": {"finding_id": "fixture_a", "quote": "Alpha tracks beans."},
               "pricing_now": [{"finding_id": "fixture_a", "quote": "Core costs $20/month."}],
               "first_seen": "2025-05-01", "last_seen": "2025-05-01"}
    inactive = {"key": "old.example", "name": "Old", "website": "https://old.example/", "finding_ids": ["fixture_old"],
                "status": "inactive", "status_evidence": [{"finding_id": "fixture_old", "basis": "yc_status"}]}
    dossier = {"evaluation": {"evidence_mode": "offline_fixture"}, "created_at": "2026-10-08T00:00:00Z",
               "completed_at": "2026-10-08T00:00:30Z", "findings": {"prior_art_company": [source, old]},
               "summary": {"usage": {"credits_micro": 20000}, "operations": [{"state": "succeeded", "credits_micro": 20000}], "evidence_dimensions": {
                   dim: {"status": "supporting_leads" if i < 4 else "unknown"} for i, dim in enumerate(DIMENSIONS)},
                   "prior_art": {"companies": [company, inactive], "lessons": [],
                                 "cost": {"usd_estimate": .01, "usd_recorded": .02, "measurement_complete": True}}}}
    return gold, dossier


def test_scores_saved_api_shape_and_separates_fixture_from_live_evidence():
    gold, dossier = fixture()
    score = score_case(gold, dossier)
    assert score["recall_at8"] == .5
    assert score["precision_at8"] == .25
    assert score["precision_of_returned_at8"] == 1
    assert score["dead_recall"] == score["grounding_rate"] == 1
    assert score["dimension_coverage"] == 4
    assert score["cost_usd"] == .02 and score["cost_basis"] == "reported_recorded_cost"
    assert score["charged_cost_usd"] == .02 and score["charged_cost_basis"] == "settled_credits"
    assert score["cost_usd_estimate"] == .01 and score["latency_seconds"] == 30
    assert aggregate([score], 10)["complete_gold_run"] is False
    assert aggregate([score], 1)["evidence_modes"] == ["offline_fixture"]


def test_duplicate_entities_consume_rank_without_inflating_precision_or_recall():
    gold, dossier = fixture()
    dossier["summary"]["prior_art"]["companies"] = [deepcopy(dossier["summary"]["prior_art"]["companies"][0])] * 9
    score = score_case(gold, dossier)
    assert score["returned_at8"] == 8 and score["precision_at8"] == .125
    assert score["recall_at8"] == .5


def test_unjudged_non_gold_is_unknown_not_automatically_irrelevant():
    gold, dossier = fixture()
    dossier["summary"]["prior_art"]["companies"].append({"key": "other.example"})
    score = score_case(gold, dossier)
    assert score["precision_at8"] is None and score["unjudged_at8"] == 1
    assert score_case(gold, dossier, {"other.example": False})["precision_at8"] == .25


def test_dead_recall_requires_status_evidence_and_never_counts_unknown_or_acquired():
    gold, dossier = fixture()
    for status in ("unknown", "acquired", "live"):
        dossier["summary"]["prior_art"]["companies"][1]["status"] = status
        assert score_case(gold, dossier)["dead_recall"] == 0
    dossier["summary"]["prior_art"]["companies"][1].update(status="inactive", status_evidence=[])
    assert score_case(gold, dossier)["dead_recall"] == 0


def test_missing_metrics_stay_unknown_and_do_not_become_free_or_fast():
    gold, dossier = fixture()
    dossier["summary"].pop("usage")
    dossier.pop("completed_at")
    dossier["summary"]["prior_art"]["companies"] = []
    dossier["summary"]["prior_art"]["cost"] = {"usd_estimate": .01}
    gold["dead"] = []
    score = score_case(gold, dossier)
    assert score["cost_usd"] is None and score["targets"]["cost_usd"] is None
    assert score["latency_seconds"] is None and score["grounding_rate"] is None and score["dead_recall"] is None
    dossier["summary"]["usage"] = {"credits_micro": 0}
    dossier["summary"]["operations"] = []
    assert score_case(gold, dossier)["cost_usd"] is None
    dossier["summary"]["operations"] = [{"state": "uncertain"}]
    dossier["summary"]["usage"]["has_unsettled"] = True
    assert score_case(gold, dossier)["cost_basis"] == "incomplete_receipts"
    dossier["summary"]["usage"]["has_unsettled"] = False
    dossier["summary"]["usage"]["credits_micro"] = 20000
    dossier["summary"]["prior_art"]["cost"].update(usd_recorded=.02, measurement_complete=False)
    incomplete = score_case(gold, dossier)
    assert incomplete["cost_usd"] is None and incomplete["targets"]["cost_usd"] is None
    assert incomplete["charged_cost_usd"] == .02


def test_citations_must_resolve_to_exact_stored_quotes_or_valid_selection_index(tmp_path, monkeypatch):
    _, dossier = fixture()
    source = dossier["findings"]["prior_art_company"][0]
    source["additional_excerpts"] = [{"text": "Additional exact pricing evidence."}]
    sources = {source["id"]: source}
    assert citation_valid({"finding_id": source["id"], "quote_index": 1}, sources)
    assert citation_valid({"finding_id": source["id"], "quote": "Additional exact pricing evidence."}, sources)
    for cite in ({"finding_id": "unknown", "quote_index": 0}, {"finding_id": source["id"], "quote_index": 2},
                 {"finding_id": source["id"], "quote_index": True}, {"finding_id": source["id"], "quote": "Invented price"},
                 {"finding_id": source["id"], "quote_index": 0, "quote": "Invented price"}):
        assert not citation_valid(cite, sources)
    from tests.company_os_harness import load_company_os_app
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "company_os"))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.sqlite3"))
    load_company_os_app(reset=False)
    from app.research.selection import quote_options
    from scripts.evals.research_prior_art.score import quote_choices
    raw = {**source, "evidence": {"additional_excerpts": source["additional_excerpts"]}}
    raw["summary"] = "Synthetic words " * 130
    assert quote_choices(raw) == quote_options([raw])[source["id"]]


def test_legacy_baseline_counts_admitted_domains_without_inventing_company_claims():
    gold, dossier = fixture()
    dossier["summary"].pop("prior_art")
    dossier["findings"]["prior_art_company"][0]["evidence_kind"] = "search_excerpt"
    score = score_case(gold, dossier)
    assert score["candidate_basis"] == "legacy_admitted_sources" and score["recall_at8"] == .5
    assert score["grounding_rate"] is None and score["dead_recall"] == 0
    dossier["findings"]["prior_art_company"][0]["evidence_kind"] = "search_lead_unverified"
    assert score_case(gold, dossier)["recall_at8"] == 0


def test_pattern_requires_two_cited_companies_not_two_ids_or_asserted_keys():
    gold, dossier = fixture()
    prior = dossier["summary"]["prior_art"]
    prior["lessons"] = [{"kind": "pattern", "text": "Synthetic inference", "company_keys": ["alpha.example", "old.example"],
                         "citations": [{"finding_id": "fixture_a", "quote_index": 0}]}]
    score = score_case(gold, dossier)
    assert score["pattern_contract_failures"] == 1 and score["grounding_rate"] < 1
    prior["lessons"][0]["citations"].append({"finding_id": "fixture_old", "quote_index": 0})
    assert score_case(gold, dossier)["pattern_contract_failures"] == 0


def test_only_seven_known_dimensions_count_and_bogus_status_does_not():
    gold, dossier = fixture()
    dimensions = dossier["summary"]["evidence_dimensions"]
    dimensions.update(eighth={"status": "supporting_leads"}, market_size={"status": "made_up"})
    assert score_case(gold, dossier)["dimension_coverage"] == 4


def test_exact_identity_keeps_repos_separate_and_rejects_credentials():
    assert identity("https://www.ALPHA.example/pricing") == "alpha.example"
    assert identity("https://github.com/OWNER/REPO/issues/1") == "github.com/owner/repo"
    assert identity("https://github.com/owner/other") != identity("https://github.com/owner/repo")
    assert identity("https://user:password@alpha.example") == ""
    assert identity("not a domain") == ""


def test_ten_gold_cases_have_primary_references_and_mirror_backed_inactive_records():
    golds = [json.loads(p.read_text()) for p in GOLD.glob("*.json")]
    assert len(golds) == len({gold["id"] for gold in golds}) == 10
    dead = [row for gold in golds for row in gold["dead"]]
    assert len(dead) == 4
    assert all(row["status"] == "inactive" and row["cause"] is None for row in dead)
    assert all(row["evidence"][1]["record"]["status"] == "Inactive" for row in dead)
    assert all(row["evidence"] and row["evidence"][0]["url"].startswith("https://")
               for gold in golds for row in [*gold["direct"], *gold["dead"], *gold["oss"]])


def test_cli_reports_missing_gold_and_runs_without_any_provider(tmp_path):
    gold, dossier = fixture()
    gold_dir, export_dir = tmp_path / "gold", tmp_path / "exports"
    gold_dir.mkdir()
    export_dir.mkdir()
    (gold_dir / "synthetic.json").write_text(json.dumps(gold))
    (export_dir / "synthetic.json").write_text(json.dumps(dossier))
    process = subprocess.run([sys.executable, str(ROOT / "scripts/evals/research_prior_art/score.py"),
                              str(export_dir), "--gold", str(gold_dir)], capture_output=True, text=True, check=True)
    report = json.loads(process.stdout)
    assert report["summary"]["complete_gold_run"] and report["cases"][0]["evidence_mode"] == "offline_fixture"
    assert "live execution" in report["summary"]["quality_evidence"]
