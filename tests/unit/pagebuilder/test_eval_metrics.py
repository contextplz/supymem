"""The gallery must detect identical imagery and retain failed evidence honestly."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import copy
import hashlib
import json
import sys
from contextlib import nullcontext
from types import SimpleNamespace

from PIL import Image, ImageDraw
import pytest


@pytest.fixture(autouse=True)
def hosted_generation(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")


def load_evaluation():
    root = Path(__file__).resolve().parents[3]
    spec = spec_from_file_location("pagebuilder_eval", root / "scripts/pagebuilder/eval.py")
    evaluation = module_from_spec(spec)
    spec.loader.exec_module(evaluation)
    return evaluation


def test_perceptual_hash_and_duplicate_direction(tmp_path):
    evaluation = load_evaluation()
    image = Image.new("RGB", (256, 256), "white")
    ImageDraw.Draw(image).rectangle((10, 30, 95, 210), fill="black")
    image.save(tmp_path / "original.png")
    image.resize((512, 512)).save(tmp_path / "resized.png")
    assert evaluation.phash(tmp_path / "original.png") == evaluation.phash(tmp_path / "resized.png")
    direction = {"fonts": ["Georgia", "Arial"], "layout": "editorial", "palette_family": "warm", "palette": {"background": "#ffffff"}}
    rows = [{"slug": key, "direction": direction, "hero_phash": "8000000000000000"} for key in ("a", "b")]
    report = evaluation.diversity(rows)
    assert not report["passed"]
    assert report["pairs"][0]["duplicate_direction"]
    assert report["pairs"][0]["hero_phash_distance"] == 0
    assert evaluation.palette_distance({"background": "#ffffff"}, {"background": "#000000"}) == 255


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def grounding_fixture(raw_inputs=None, raw_plan=None):
    from tests.company_os_harness import load_company_os_app
    from tests.unit.pagebuilder.test_pipeline import fixture_input, fixture_draft, review_payload
    load_company_os_app()
    from app.pagebuilder.models import PageInput, PagePlan
    from app.pagebuilder.pipeline import _grounding_prompt, compile_draft_plan

    evaluation = load_evaluation()
    inputs = PageInput.model_validate(fixture_input() if raw_inputs is None else raw_inputs).model_dump(mode="json")
    plan = compile_draft_plan(inputs, fixture_draft() if raw_plan is None else raw_plan).model_dump(mode="json")
    review = review_payload(_grounding_prompt(PagePlan.model_validate(plan), PageInput.model_validate(inputs)))
    render_observation = {"checks": {"contrast": True, "image_clipping": True, "hero_cta_initial": True},
        "contrastSkipped": 0, "contrastUnresolved": [], "imageClippingFailures": [],
        "initialHeroCta": {"visible": True, "withinViewport": True, "hitCoverage": True, "top": 150, "left": 32,
            "right": 280, "bottom": 204, "width": 248, "height": 54}}
    metadata = {"plan": plan, "manifest": {"image_compiler_version": evaluation.IMAGE_COMPILER_VERSION,
        "image_provenance": evaluation.IMAGE_PROVENANCE}, "qa": {"passed": True,
        "render": {"passed": True, "browser_rendered": True, "failures": [],
            "observations": {viewport: copy.deepcopy(render_observation) for viewport in ("desktop", "mobile")}},
        "capability_grounding": {
        "policy": evaluation.GROUNDING_POLICY, "passed": True, "scope": "generation_only", "independent_qa_attestation": False,
        "plan_sha256": digest(plan), "reviews": [{"stage": "capability_review_1", "plan_sha256": digest(plan), "review_sha256": digest(review), "review": review}]}},
        "cost_receipt": {"request_policy": evaluation.REQUEST_POLICY, "reasoning_policy": evaluation.REASONING_POLICY,
            "provider": "azure_foundry", "model": "DeepSeek-V4-Pro",
            "call_timeout_seconds": evaluation.CALL_TIMEOUT_SECONDS,
            "grounding_input": inputs, "grounding_input_sha256": digest(inputs),
            "waitlist_url": inputs["waitlist_url"], "waitlist_backend": {"consent_text": inputs["waitlist_consent_text"], "consent_version": inputs["waitlist_consent_version"]},
            "grounding_operations": [{"stage": "capability_review_1", "operation_key": "variant_0:capability_review_1",
            "provider": "azure_foundry", "model": "DeepSeek-V4-Pro",
            "state": "completed", "settled": True, "review_sha256": digest(review)}]}}
    return evaluation, metadata


@pytest.mark.parametrize("tamper", ["plan", "review", "unsettled", "missing", "duplicate", "scope", "attestation", "policy", "prior_profile", "grounding_policy",
                                    "coverage", "duplicate_field", "quote", "typed", "input", "waitlist", "review_hash", "planning_reasoning", "reviewer_reasoning", "reviewer_reasoning_high", "deadline",
                                    "unsupported_route", "missing_provider", "missing_model", "operation_provider", "operation_model", "missing_operation_provider", "missing_operation_model"])
def test_grounding_requires_exact_plan_and_settled_review(tamper):
    evaluation, metadata = grounding_fixture()
    assert evaluation.grounding_checks(metadata)["passed"]
    changed = copy.deepcopy(metadata)
    if tamper == "plan":
        changed["plan"]["brief"]["proof_points"][0] = "We are exploring role-based access for supervisors."
    elif tamper == "review":
        changed["qa"]["capability_grounding"]["reviews"][-1]["review"]["approved"] = False
    elif tamper == "unsettled":
        changed["cost_receipt"]["grounding_operations"][0]["settled"] = False
    elif tamper == "missing":
        changed["cost_receipt"]["grounding_operations"] = []
    elif tamper == "duplicate":
        changed["cost_receipt"]["grounding_operations"] *= 2
    elif tamper == "scope":
        changed["qa"]["capability_grounding"]["scope"] = "release_qa"
    elif tamper == "attestation":
        changed["qa"]["capability_grounding"]["independent_qa_attestation"] = True
    elif tamper == "policy":
        changed["cost_receipt"]["request_policy"] = "scoped_grounding_v18"
    elif tamper == "prior_profile":
        changed["cost_receipt"]["request_policy"] = "scoped_grounding_v14"
        changed["cost_receipt"]["reasoning_policy"]["reviewer"] = "none"
    elif tamper == "grounding_policy":
        changed["qa"]["capability_grounding"]["policy"] = "semantic_capability_v3"
    elif tamper == "input":
        changed["cost_receipt"]["grounding_input"]["idea"] += " It would also store shift history."
    elif tamper == "waitlist":
        changed["cost_receipt"]["waitlist_backend"]["consent_text"] = "Synthetic altered consent"
    elif tamper == "review_hash":
        changed["qa"]["capability_grounding"]["reviews"][-1]["review_sha256"] = "0" * 64
    elif tamper == "planning_reasoning":
        changed["cost_receipt"]["reasoning_policy"]["planning"] = "none"
    elif tamper == "reviewer_reasoning":
        changed["cost_receipt"]["reasoning_policy"]["reviewer"] = "none"
    elif tamper == "reviewer_reasoning_high":
        changed["cost_receipt"]["reasoning_policy"]["reviewer"] = "high"
    elif tamper == "deadline":
        changed["cost_receipt"]["call_timeout_seconds"] = 180
    elif tamper == "unsupported_route":
        changed["cost_receipt"]["provider"] = "openai"
    elif tamper in {"missing_provider", "missing_model"}:
        del changed["cost_receipt"][tamper.removeprefix("missing_")]
    elif tamper in {"operation_provider", "operation_model"}:
        changed["cost_receipt"]["grounding_operations"][0][tamper.removeprefix("operation_")] = "unquoted_route"
    elif tamper in {"missing_operation_provider", "missing_operation_model"}:
        del changed["cost_receipt"]["grounding_operations"][0][tamper.removeprefix("missing_operation_")]
    else:
        record = changed["qa"]["capability_grounding"]["reviews"][-1]
        fields = record["review"]["field_reviews"]
        if tamper == "coverage":
            fields.pop()
        elif tamper == "duplicate_field":
            fields.append(copy.deepcopy(fields[0]))
        elif tamper == "quote":
            next(field for field in fields if field["classification"] == "supported")["source_quotes"] = ["Paraphrased invented founder evidence"]
        else:
            record["review"]["approved"] = 1
        record["review_sha256"] = digest(record["review"])
        changed["cost_receipt"]["grounding_operations"][0]["review_sha256"] = record["review_sha256"]
    result = evaluation.grounding_checks(changed)
    assert not result["passed"]
    if tamper == "prior_profile":
        assert set(result["failures"]) == {"grounding_request_policy_obsolete", "grounding_reasoning_policy_mismatch"}


def test_grounding_binds_openai_captured_default_route(monkeypatch):
    evaluation, metadata = grounding_fixture()
    receipt = metadata["cost_receipt"]
    receipt.update(provider="openai_compatible", model="gpt-6-luna",
                   reasoning_policy={"planning": None, "reviewer": None, "generation": None})
    receipt["grounding_operations"][0].update(provider="openai_compatible", model="gpt-6-luna")
    monkeypatch.setenv("LLM_PROVIDER", "azure_foundry")
    monkeypatch.setenv("LLM_DEPLOYMENT_PRO", "unquoted_model")
    assert evaluation.grounding_checks(metadata)["passed"]
    changed = copy.deepcopy(metadata)
    changed["cost_receipt"]["reasoning_policy"] = {"planning": "none", "reviewer": "none", "generation": "none"}
    assert not evaluation.grounding_checks(changed)["passed"]
    changed = copy.deepcopy(metadata)
    changed["cost_receipt"]["model"] = "gpt-4.1-mini"
    assert not evaluation.grounding_checks(changed)["passed"]
    changed = copy.deepcopy(metadata)
    changed["cost_receipt"]["grounding_operations"][0]["model"] = "DeepSeek-V4-Pro"
    assert not evaluation.grounding_checks(changed)["passed"]


def test_nightshift_fixture_v2_requires_its_actual_confirmed_input():
    from tests.unit.pagebuilder.test_pipeline import nightshift_draft

    evaluation = load_evaluation()
    fixture = next(item for item in evaluation.FIXTURES if item["slug"] == "nightshift")
    original_input, plan = nightshift_draft()
    original_input["clarified_answers"] = {"audience": fixture["audience"], "category": fixture["category"]}
    evaluation, original = grounding_fixture(original_input, plan)
    assert evaluation.grounding_checks(original)["passed"]
    assert "grounding_synthetic_fixture_mismatch" in evaluation.grounding_checks(original, fixture)["failures"]
    current_input = {**original_input, "idea": fixture["idea"]}
    evaluation, current = grounding_fixture(current_input, plan)
    assert fixture["fixture_version"] == 2
    assert evaluation.grounding_checks(current, fixture)["passed"]


@pytest.mark.parametrize("tamper", ["scene", "svg", "alt", "palette", "asset_version", "asset_provenance",
                                    "manifest_version", "manifest_provenance", "legacy_svg"])
def test_grounding_requires_native_scene_derivation_even_with_current_hashes(tamper):
    evaluation, metadata = grounding_fixture()
    assert evaluation.grounding_checks(metadata)["passed"]
    changed = copy.deepcopy(metadata)
    asset = changed["plan"]["assets"][0]
    if tamper == "scene":
        asset["scene"]["objects"][0]["x"] += 10
    elif tamper == "svg":
        asset["svg"] = asset["svg"].replace("</svg>", '<circle cx="10" cy="10" r="1"/></svg>')
    elif tamper == "alt":
        asset["alt"] = "Original illustration of a clipboard."
    elif tamper == "palette":
        changed["plan"]["direction"]["palette"]["text"] = "#213227"
    elif tamper == "asset_version":
        asset["compiler_version"] = "native_scene_v0"
    elif tamper == "asset_provenance":
        asset["provenance"] = "original_model_svg"
    elif tamper == "manifest_version":
        del changed["manifest"]["image_compiler_version"]
    elif tamper == "manifest_provenance":
        changed["manifest"]["image_provenance"] = "original_model_svg"
    else:
        asset["scene"] = None
        asset["compiler_version"] = None
        asset["provenance"] = "original_model_svg"
    plan_hash = digest(changed["plan"])
    grounding = changed["qa"]["capability_grounding"]
    grounding["plan_sha256"] = plan_hash
    grounding["reviews"][-1]["plan_sha256"] = plan_hash
    assert not evaluation.grounding_checks(changed)["passed"]


@pytest.mark.parametrize("tamper", ["old_guards", "missing_render", "missing_mobile", "qa_failed", "render_failed",
    "not_browser_rendered", "render_failures", "skipped_contrast", "unresolved_contrast", "clipped_image",
    "below_fold", "failed_check", "missing_hit_coverage", "blocked_hit_coverage"])
def test_grounding_requires_saved_initial_fold_and_complete_render_checks(tamper):
    evaluation, metadata = grounding_fixture()
    assert evaluation.grounding_checks(metadata)["passed"]
    changed = copy.deepcopy(metadata)
    qa = changed["qa"]
    render = qa["render"]
    mobile = render["observations"]["mobile"]
    if tamper == "old_guards":
        mobile["checks"] = {"contrast": True}
    elif tamper == "missing_render":
        del qa["render"]
    elif tamper == "missing_mobile":
        del render["observations"]["mobile"]
    elif tamper == "qa_failed":
        qa["passed"] = False
    elif tamper == "render_failed":
        render["passed"] = False
    elif tamper == "not_browser_rendered":
        render["browser_rendered"] = False
    elif tamper == "render_failures":
        render["failures"] = ["render_contrast:mobile"]
    elif tamper == "skipped_contrast":
        mobile["contrastSkipped"] = 15
    elif tamper == "unresolved_contrast":
        mobile["contrastUnresolved"] = [{"reason": "gradient"}]
    elif tamper == "clipped_image":
        mobile["imageClippingFailures"] = [{"id": "hero_art", "axes": ["y"]}]
    elif tamper == "below_fold":
        mobile["initialHeroCta"]["withinViewport"] = False
    elif tamper == "missing_hit_coverage":
        del mobile["initialHeroCta"]["hitCoverage"]
    elif tamper == "blocked_hit_coverage":
        mobile["initialHeroCta"]["hitCoverage"] = False
    else:
        mobile["checks"]["hero_cta_initial"] = False
    assert not evaluation.grounding_checks(changed)["passed"]


@pytest.mark.parametrize("stage", ["capability_review_3", "capability_review_4", "capability_review_5", "capability_review_6"])
def test_grounding_binds_later_final_review(stage):
    evaluation, metadata = grounding_fixture()
    final = metadata["qa"]["capability_grounding"]["reviews"][-1]
    final["stage"] = stage
    operation = metadata["cost_receipt"]["grounding_operations"][0]
    operation.update(stage=stage, operation_key="variant_0:" + stage)
    metadata["qa"]["capability_grounding"]["reviews"].insert(0, {
        "stage": "capability_review_1", "plan_sha256": final["plan_sha256"],
        "invalid_response": True, "validation_diagnostics": {"errors": ["nonliteral source quote"]}})
    assert evaluation.grounding_checks(metadata)["passed"]
    changed = copy.deepcopy(metadata)
    changed["cost_receipt"]["grounding_operations"][0]["settled"] = False
    assert not evaluation.grounding_checks(changed)["passed"]
    changed = copy.deepcopy(metadata)
    changed["qa"]["capability_grounding"]["reviews"].append({
        "stage": stage, "plan_sha256": final["plan_sha256"], "invalid_response": True,
        "validation_diagnostics": {"errors": ["finding not in plan"]}})
    assert not evaluation.grounding_checks(changed)["passed"]
    changed = copy.deepcopy(metadata)
    changed["qa"]["capability_grounding"]["reviews"][-1]["stage"] = "capability_review_7"
    changed["cost_receipt"]["grounding_operations"][0].update(
        stage="capability_review_7", operation_key="variant_0:capability_review_7")
    assert not evaluation.grounding_checks(changed)["passed"]


def test_independent_review_is_bound_to_frozen_source(tmp_path):
    evaluation = load_evaluation()
    directory = tmp_path / "nightshift"
    directory.mkdir()
    source = directory / "index.html"
    source.write_text("Exact synthetic pipeline bytes for review binding only")
    row = {"slug": "nightshift"}
    assert not evaluation.independent_review(tmp_path, row)["passed"]
    review = {"approved": True, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "reviewer": "/root"}
    (directory / "independent-review.json").write_text(json.dumps(review))
    assert evaluation.independent_review(tmp_path, row)["passed"]
    review["reviewer"] = "page_author"
    (directory / "independent-review.json").write_text(json.dumps(review))
    assert not evaluation.independent_review(tmp_path, row)["passed"]
    review["reviewer"] = "/root"
    (directory / "independent-review.json").write_text(json.dumps(review))
    source.write_text("Changed bytes after review")
    assert not evaluation.independent_review(tmp_path, row)["passed"]


@pytest.mark.parametrize("scenario", ["resume", "fresh", "failed"])
def test_main_separates_bundle_cost_from_cumulative_fixture_spend(tmp_path, monkeypatch, scenario):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    evaluation = load_evaluation()
    fixture = next(item for item in evaluation.FIXTURES if item["slug"] == "afterimage")
    output, receipts = tmp_path / "gallery", tmp_path / "receipts"
    directory = output / fixture["slug"]
    directory.mkdir(parents=True)
    receipts.mkdir()
    historical = receipts / "afterimage-rejected.json"
    historical.write_text(json.dumps({"provider_cost_usd": 0.03, "unresolved_ceiling_usd": 0.002}))
    (receipts / "margin-other.json").write_text(json.dumps({"provider_cost_usd": 0.005}))
    historical_bytes = historical.read_bytes()
    metadata = {"direction": {"fonts": ["Georgia", "Arial"], "layout": "editorial", "palette_family": "warm"},
        "cost_receipt": {"metered": True, "provider_cost_usd": 0.013,
            "waitlist_url": "https://example.test/waitlist", "waitlist_backend": {
                "configured": True, "local_details_verified": True, "consent_version": "synthetic-v1"}}}
    calls = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        (receipts / "afterimage-current.json").write_text(json.dumps(metadata["cost_receipt"]))
        if scenario == "failed":
            raise ValueError("pagebuilder_qa_failed")
        return {**metadata, "files": {"index.html": "Synthetic billing regression fixture"}}

    if scenario == "resume":
        (directory / "index.html").write_text("Synthetic billing regression fixture")
        (directory / "bundle.json").write_text(json.dumps(metadata))
        (receipts / "afterimage-current.json").write_text(json.dumps(metadata["cost_receipt"]))
    bundle_bytes = (directory / "bundle.json").read_bytes() if scenario == "resume" else None
    original_import = evaluation.importlib.import_module
    monkeypatch.setattr(evaluation.importlib, "import_module", lambda name: (
        SimpleNamespace(run_evaluation=runner) if name == "billing_stub" else original_import(name)))
    monkeypatch.setattr(evaluation, "grounding_checks", lambda *_: {"passed": True})
    monkeypatch.setattr(evaluation, "serve", lambda *_: nullcontext("https://fixture.invalid"))

    def browser(*args, **kwargs):
        Image.new("RGB", (32, 32), "white").save(directory / "hero.png")
        return {"passed": True}

    monkeypatch.setattr(evaluation, "browser_checks", browser)
    monkeypatch.setattr(evaluation, "lighthouse", lambda *_: {"passed": True, "performance": 100, "accessibility": 100})
    monkeypatch.setattr(sys, "argv", ["eval.py", "--mode", "live", "--runner", "billing_stub:run_evaluation",
        "--output", str(output), "--receipts-dir", str(receipts), "--resume", "--only", fixture["slug"]])
    assert evaluation.main() == 1  # One isolated fixture never grants eight-page acceptance.
    report = json.loads((output / "report.json").read_text())
    row = report["ventures"][0]
    assert row["cost_usd"] == pytest.approx(0.013)
    assert row["cumulative_fixture_spend_usd"] == pytest.approx(0.043)
    assert report["total_spend_usd"] == pytest.approx(0.048)
    assert report["unresolved_ceiling_usd"] == pytest.approx(0.002)
    assert historical.read_bytes() == historical_bytes
    assert len(calls) == (0 if scenario == "resume" else 1)
    if scenario == "resume":
        assert (directory / "bundle.json").read_bytes() == bundle_bytes
        assert not report["attempts"]
    else:
        assert report["attempts"][0]["cost_usd"] == pytest.approx(0.013)
    if scenario == "failed":
        assert row["error"] == "pagebuilder_qa_failed"
