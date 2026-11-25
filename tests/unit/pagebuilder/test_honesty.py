"""Pre-launch copy is confident; fabricated proof still fails."""
import importlib
import json
from pathlib import Path
import tarfile

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture(autouse=True)
def hosted_generation(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")


@pytest.fixture(scope="module")
def modules():
    load_company_os_app()
    return tuple(importlib.import_module("app.pagebuilder." + name) for name in ("models", "qa", "pipeline"))


def inputs(models):
    return models.PageInput(venture_id="ven_honesty", name="Supplify",
        idea="A tool that normalizes supplier catalogs for Shopify agencies.", clarified_answers={}, research={},
        waitlist_url="https://gateway.supymem.com/public/waitlists/ven_honesty")


@pytest.mark.parametrize("copy", [
    "Supplify normalizes supplier catalogs for Shopify agencies.",
    "Coming soon. We're building Supplify. Join the waitlist for early access.",
    "Supplify is early access, launching soon for Shopify agencies.",
])
def test_confident_prelaunch_copy_passes(modules, copy):
    models, qa, _ = modules
    confirmed = inputs(models).model_copy(update={"clarified_answers": {"public_launch_copy": copy}})
    assert qa.honest_copy(copy, confirmed) == []


@pytest.mark.parametrize("copy", [
    "Supplify saves agencies 12 hours every week.",
    "Trusted by 200 Shopify agencies.",
])
def test_fabricated_metrics_still_fail(modules, copy):
    models, qa, _ = modules
    assert qa.honest_copy(copy, inputs(models))


def test_default_disclosure_and_prompt_do_not_hedge(modules):
    models, _, pipeline = modules
    assert "concept" not in models.PageBrief.model_fields["concept_disclosure"].default.lower()
    prompt = pipeline.plan_prompt(inputs(models))
    assert "retain proposed/planned language" not in prompt and "Never hedge with proposed" in prompt


@pytest.mark.parametrize("name,idea", [
    ("FleetNote", "A delivery scheduling board for independent courier teams."),
    ("GardenBook", "A planting journal for home gardeners."),
])
def test_early_stage_disclosure_cannot_be_rejected_as_unsupported(modules, monkeypatch, name, idea):
    from tests.unit.pagebuilder.test_pipeline import review_payload
    models, qa, pipeline = modules
    page = inputs(models).model_copy(update={"name": name, "idea": idea})
    for disclosure in (f"{name} is pre-launch.", f"{name} is early-stage."):
        plan = pipeline.fallback_plan(page)
        plan.brief.concept_disclosure = disclosure
        accepted = models.CapabilityReview.model_validate(review_payload(pipeline._grounding_prompt(plan, page)))
        saved = accepted.model_dump(mode="json")
        pipeline.validate_grounding_review(accepted, plan, page)
        assert accepted.model_dump(mode="json") == saved
        finding = {"field_id": "brief.concept_disclosure", "claim": disclosure,
            "reason": "No launch readiness was confirmed in the founder sources."}
        review = models.CapabilityReview.model_validate(review_payload(
            pipeline._grounding_prompt(plan, page), [finding]))
        pipeline.validate_grounding_review(review, plan, page)
        assert review.approved
        assert review.unsupported_claims == []
        assert next(item for item in review.field_reviews if item.field_id == finding["field_id"]).classification == "editorial"
        assert qa.honest_copy(disclosure, page) == []
        plan.brief.concept_disclosure = disclosure + " Trusted by 300 customers."
        finding["claim"] = plan.brief.concept_disclosure
        unsafe = models.CapabilityReview.model_validate(review_payload(
            pipeline._grounding_prompt(plan, page), [finding]))
        pipeline.validate_grounding_review(unsafe, plan, page)
        assert not unsafe.approved and unsafe.unsupported_claims
        assert qa.honest_copy(plan.brief.concept_disclosure, page)
        plan.brief.concept_disclosure = disclosure
        public = pipeline.public_plan_copy(plan)
        public["assets[0].alt"] = disclosure
        with monkeypatch.context() as isolated:
            isolated.setattr(pipeline, "public_plan_copy", lambda value: public)
            finding.update(field_id="assets[0].alt", claim=disclosure)
            art = models.CapabilityReview.model_validate(review_payload(
                pipeline._grounding_prompt(plan, page), [finding]))
            pipeline.validate_grounding_review(art, plan, page)
            assert not art.approved and art.unsupported_claims


@pytest.mark.parametrize("name,idea,offered,audience,invented", [
    ("RotorCheck", "A robotic inspection service for turbine operators using ultrasound to find internal cracks.",
        "inspection", "turbine operators", "internal cracks"),
    ("CycleCare", "A bicycle repair service for city commuters using a checklist to find worn brakes.",
        "bicycle repair", "city commuters", "worn brakes"),
])
def test_offer_as_a_service_restatement_needs_grounded_object_and_audience(modules, name, idea, offered, audience, invented):
    models, qa, _ = modules
    page = inputs(models).model_copy(update={"name": name, "idea": idea})
    sentence = f"{name} offers this {offered} as a service to {audience}."
    assert qa._confirmed_capability(sentence, page)
    assert qa.honest_copy(sentence, page) == []
    assert not qa._confirmed_capability(f"{name} offers this {invented} as a service to {audience}.", page)
    assert not qa._confirmed_capability(f"{name} offers this {offered} as a service to hospitals.", page)
    assert qa.honest_copy(sentence[:-1] + " with guaranteed results.", page)


@pytest.mark.parametrize("idea", [
    "A purchase-order organizer for independent furniture workshops.",
    "A recipe notebook for home cooks planning family meals.",
])
def test_launch_timing_requires_confirmed_source(modules, idea):
    from tests.unit.pagebuilder.test_pipeline import review_payload
    models, qa, pipeline = modules
    page = inputs(models).model_copy(update={"idea": idea})
    assert models.PageBrief.model_fields["concept_disclosure"].default == "Join the waitlist for early-access updates."
    for claim in ("Launches next month.", "Available tomorrow.", "Releasing this winter."):
        assert any(reason.startswith("unsupported_launch_timing:") for reason in qa.honest_copy(claim, page))
        confirmed = page.model_copy(update={"clarified_answers": {"launch": claim}})
        assert not any(reason.startswith("unsupported_launch_timing:") for reason in qa.honest_copy(claim, confirmed))
        negated = page.model_copy(update={"clarified_answers": {"launch": "Not " + claim}})
        assert any(reason.startswith("unsupported_launch_timing:") for reason in qa.honest_copy(claim, negated))
        plan = pipeline.fallback_plan(page)
        plan = plan.model_copy(update={"brief": plan.brief.model_copy(update={"concept_disclosure": claim})})
        review = models.CapabilityReview.model_validate(review_payload(pipeline._grounding_prompt(plan, page)))
        assert review.approved
        with pytest.raises(ValueError, match="unsupported_launch_timing"):
            pipeline.validate_grounding_review(review, plan, page)
    assert qa.honest_copy("Join the waitlist for early-access updates.", page) == []
    assert qa.honest_copy("Keep your notes ready for review.", page) == []
    assert qa.honest_copy("Use ingredients already available in your kitchen.", page) == []


@pytest.fixture(params=["b2b", "consumer"])
def tool_output(modules, monkeypatch, request):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    models, _, _ = modules
    fixtures = Path(__file__).with_name("fixtures")
    if request.param == "b2b":
        with tarfile.open(fixtures / "macpage_real_codex_output.tgz") as archive:
            files = {member.name.removeprefix("macpage/"): archive.extractfile(member).read()
                for member in archive.getmembers() if member.isfile() and member.name.startswith("macpage/")
                and not any(part.startswith("._") for part in Path(member.name).parts)}
        job = json.loads(files.pop("job_inputs.json"))
        page = models.PageInput.model_validate(job["input"])
        facts = json.loads((fixtures / "b2b_claims.json").read_text())
        files["page-plan.txt"] = files["page-plan.txt"].replace(page.name.encode(), facts["name"].encode())
        page = page.model_copy(update={"name": facts["name"]})
        draft = json.loads(files["page-plan.txt"])
        return page, draft, files, facts["claims"], facts["invented"]
    from tests.unit.pagebuilder.test_pipeline import customer_draft
    data, draft = customer_draft("consumer")
    facts = json.loads((fixtures / "consumer_claims.json").read_text())
    data.update(name=facts["name"], idea=facts["idea"],
        clarified_answers={"offer": facts["offer"], "audience": facts["audience"]})
    page = models.PageInput.model_validate(data)
    draft["brief"].update(promise=facts["claims"][0], proof_points=facts["claims"])
    draft["sections"][0]["body"] = facts["claims"][0]
    draft["sections"][2]["body"] = " ".join(facts["claims"])
    files = {"page-plan.txt": json.dumps(draft).encode()}
    return page, draft, files, facts["claims"], facts["invented"]


def test_tool_brief_explicitly_requires_palette_roles(modules, tool_output):
    from app.pagebuilder.tool import build_brief
    page = tool_output[0]
    brief = build_brief(page, None)
    assert "Required palette roles: background, text, primary, accent" in brief
    assert "primary is the button fill" in brief


def test_tool_reports_exact_scrubbed_validation_errors(modules, tool_output):
    from app.pagebuilder.tool import check_output
    page, draft, files, _, _ = tool_output
    draft["direction"]["palette"] = {"background": "#FFFFFF", "ink": "#152A35",
        "anchor": "#176B64", "accent": "#D85B42"}
    secret = "sk-proj-" + "test_only_not_a_real_key_1234567890"
    draft["direction"]["concept"] = secret * 30
    files["page-plan.txt"] = json.dumps(draft).encode()
    checked = check_output(files, page, None)
    failures = " ".join(checked["failures"])
    assert not checked["passed"] and "page_files_invalid" not in checked["failures"]
    assert "pagebuilder_palette_invalid" in failures and "missing primary, text" in failures
    assert "direction.concept" in failures and "500" in failures
    assert secret not in failures


def test_confirmed_capability_restatements_pass(modules, tool_output):
    models, qa, pipeline = modules
    page, draft, _, claims, _ = tool_output
    for claim in claims:
        assert not any(reason.startswith("capability_claim:") for reason in qa.honest_copy(claim, page)), claim
    assert qa.honest_copy(" ".join(claims), page) == []
    palette = draft["direction"]["palette"]
    if "ink" in palette:
        palette["text"], palette["primary"] = palette.pop("ink"), palette.pop("anchor")
    original = models.GenerationDraft.model_validate(draft)
    rewritten, _ = pipeline._rewrite_capability_copy(original, page)
    assert rewritten.brief.proof_points == original.brief.proof_points


def test_confirmed_idea_does_not_authorize_invented_facts(modules, tool_output):
    _, qa, _ = modules
    page, _, _, _, invented = tool_output
    for claim in invented:
        assert any(not reason.startswith("language:") for reason in qa.honest_copy(claim, page)), claim
    claim = f"{page.name} tracks shipping containers."
    unconfirmed = page.model_copy(update={"clarified_answers": {"open_questions": [claim], "risks": [claim]},
        "research": {"summary": claim}})
    assert any(reason.startswith("capability_claim:") for reason in qa.honest_copy(claim, unconfirmed))
    for source in ("A tool that tracks shipping containers is not the offer.",
            "We don't want a tool that tracks shipping containers."):
        negated = page.model_copy(update={"idea": source, "clarified_answers": {"offer": source}})
        assert any(reason.startswith("capability_claim:") for reason in qa.honest_copy(claim, negated))


def test_real_tool_output_reaches_independent_html_checks(modules, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from app.pagebuilder.tool import check_output
    fixtures = Path(__file__).with_name("fixtures")
    with tarfile.open(fixtures / "macpage_real_codex_output.tgz") as archive:
        files = {member.name.removeprefix("macpage/"): archive.extractfile(member).read()
            for member in archive.getmembers() if member.isfile() and member.name.startswith("macpage/")
            and not any(part.startswith("._") for part in Path(member.name).parts)}
    job = json.loads(files.pop("job_inputs.json"))
    draft = json.loads(files["page-plan.txt"])
    palette = draft["direction"]["palette"]
    palette["text"], palette["primary"] = palette.pop("ink"), palette.pop("anchor")
    files["page-plan.txt"] = json.dumps(draft).encode()
    checked = check_output(files, modules[0].PageInput.model_validate(job["input"]), job["tool_plan"])
    assert "contract" in checked, checked["failures"]
    assert "page_files_invalid" not in checked["failures"]
    assert "customer_copy:render_three_how_it_works_steps" in checked["failures"]


@pytest.mark.parametrize("files,expected", [({}, "Missing required page file or field: page-plan.txt"),
    ({"page-plan.txt": b"\xff"}, "Page file must be UTF-8: invalid start byte at bytes 0-1")])
def test_tool_file_errors_name_the_problem(modules, tool_output, files, expected):
    from app.pagebuilder.tool import check_output
    checked = check_output(files, tool_output[0], None)
    assert not checked["passed"] and checked["failures"] == [expected]


def test_confirmed_offer_paraphrases_keep_relationships_and_reject_inventions(modules, tool_output):
    page, _, _, _, invented = tool_output
    from app.pagebuilder.qa import honest_copy
    if page.name == "RotorProbe":
        restatements = ["The offer is a robotic crawler service using ultrasound to find internal blade cracks.",
            "Ultrasound detects internal cracks in blades."]
    else:
        restatements = ["The offer is a tool using barcode scanning to track the ingredients in your pantry.",
            "Pantry Path tracks ingredients in your pantry."]
    for sentence in restatements:
        assert honest_copy(sentence, page) == [], sentence
    for sentence in [*invented, "Trusted by Northstar Industries."]:
        assert honest_copy(sentence, page), sentence


@pytest.mark.parametrize("name,idea,restatements,invented", [
    ("RotorProbe", "A robotic inspection crawler for wind-turbine blades that finds internal cracks with ultrasound, sold as a service to wind farm operators", [
        "RotorProbe offers robotic ultrasound inspection of wind-turbine blades to find internal cracks.",
        "The offer is a service for wind farm operators.",
        "RotorProbe offers wind farm operators robotic ultrasound inspection of wind-turbine blades as a service.",
    ], [
        "RotorProbe cuts downtime by 40%.",
        "RotorProbe offers robotic inspection certified by DNV.",
        "RotorProbe offers robotic inspection used by Vestas.",
        "RotorProbe offers robotic inspection available today in our app.",
        "RotorProbe detects corrosion in wind-turbine blades.",
        "RotorProbe guarantees that ultrasound finds internal cracks.",
        "RotorProbe has already found internal cracks for wind farm operators.",
        "RotorProbe offers a live integration for wind farm operators.",
    ]),
    ("Pantry Path", "A meal-planning tool that tracks the ingredients in your pantry with barcode scanning, sold as a subscription to home cooks", [
        "Pantry Path offers a meal-planning tool to track pantry ingredients with barcode scanning.",
        "The offer is a subscription for home cooks.",
        "Pantry Path provides home cooks a meal-planning tool that tracks pantry ingredients with barcode scanning as a subscription.",
    ], [
        "Pantry Path cuts food waste by 40%.",
        "Pantry Path offers meal planning certified by the Academy of Nutrition and Dietetics.",
        "Pantry Path offers meal planning used by HelloFresh.",
        "Pantry Path offers meal planning available today in our app.",
        "Pantry Path detects food allergies.",
        "Pantry Path guarantees that it tracks pantry ingredients.",
        "Pantry Path has already tracked pantry ingredients for home cooks.",
        "Pantry Path offers a live integration for home cooks.",
    ]),
], ids=["b2b", "consumer"])
@pytest.mark.parametrize("case_index", range(11))
def test_capability_grounding_uses_confirmed_content_not_sentence_templates(
        modules, monkeypatch, name, idea, restatements, invented, case_index):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    models, qa, pipeline = modules
    page = inputs(models).model_copy(update={"name": name, "idea": idea})
    sentence = [*restatements, *invented][case_index]
    if case_index < len(restatements):
        assert qa._confirmed_capability(sentence, page), sentence
        assert qa.honest_copy(sentence, page) == [], sentence
        assert pipeline._raw_copy_diagnostics({"brief": {"promise": sentence}}, page) == []
    else:
        assert not qa._confirmed_capability(sentence, page), sentence
        assert qa.honest_copy(sentence, page), sentence


@pytest.mark.parametrize("specific", ["customer_usage", "percentage"])
def test_shared_words_do_not_authorize_unconfirmed_evidence(modules, tool_output, specific):
    _, qa, _ = modules
    page = tool_output[0]
    page = page.model_copy(update={"clarified_answers": {**page.clarified_answers,
        "audience": "Northstar Industries", "what_it_does": f"{page.name} tracks 40 items."}})
    sentence = ("Used by Northstar Industries." if specific == "customer_usage"
        else f"{page.name} tracks 40% of items.")
    assert not qa._confirmed_capability(sentence, page), sentence
    assert qa.honest_copy(sentence, page), sentence
