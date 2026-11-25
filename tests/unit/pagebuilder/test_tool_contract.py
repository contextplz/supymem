"""A tool can implement the check contract without knowing the old renderer."""
from html import escape
import json
from pathlib import Path
import re
import tarfile
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.pagebuilder.test_pipeline import customer_draft


@pytest.mark.parametrize("audience", ["warehouse dispatchers", "weekend gardeners"])
@pytest.mark.parametrize("failure", [
    "sections.0.headline: String should have at most 140 characters",
    'pagebuilder_plan_copy_rejected:[{"field_id":"brief.promise","reason":"Your claim is not supported"',
    "pagebuilder_qa_failed:honest_copy:reviewed_body_missing:hero",
])
def test_founder_check_reason_is_plain_and_actionable(audience, failure):
    load_company_os_app()
    from app.pagebuilder.local import failure_reason

    message = failure_reason(failure + ("" if "reason" in failure else ":" + audience))
    assert not re.search(r"field_id|brief\.promise|sections[.\[]|reviewed_body_missing|pagebuilder_|honest_copy|[{}]", message)
    if "140" in failure:
        assert "heading" in message.lower() and "140" in message
        assert message.endswith("In your venture chat, ask Charlie to build a new version with the corrected heading.")
    else:
        assert "Website" in message and "Retry" in message


@pytest.fixture(params=["b2b", "consumer"])
def page(monkeypatch, request):
    load_company_os_app()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from app.pagebuilder.models import GenerationDraft, PageInput
    if request.param == "b2b":
        fixtures = Path(__file__).with_name("fixtures")
        with tarfile.open(fixtures / "macpage_real_codex_output.tgz") as archive:
            inputs = json.load(archive.extractfile("macpage/job_inputs.json"))["input"]
        with tarfile.open(fixtures / "macpage2_real_codex_output.tgz") as archive:
            draft = json.load(archive.extractfile("macpage2/page-plan.txt"))
            receipt = json.load(archive.extractfile("macpage2/receipt_b504kr.json"))
        draft["sections"][2].update(headline="What it does", nav_label="What it does",
            purpose="Describe the confirmed capabilities and offer without a workflow.")
    else:
        inputs, draft = customer_draft("consumer")
        inputs.setdefault("clarified_answers", {})["workflow"] = draft["sections"][2]["body"]
        inputs["research"] = {"local_signup": {"mode": "mailto", "url": "mailto:owner@example.test",
            "notice": "Send your request in your email app."}}
        receipt = None
    for section in draft["sections"]:
        section["nav_label"] = None if section["id"] == "waitlist" else section.get("nav_label") or section["headline"]
    return PageInput.model_validate(inputs), GenerationDraft.model_validate(draft), receipt


def test_tool_brief_exposes_checker_contract_before_plan_prompt(page):
    from app.pagebuilder import pipeline, tool
    from app.pagebuilder.qa import tool_contract
    inputs, plan, _ = page
    for supplied_plan in (None, plan):
        brief = tool.build_brief(inputs, supplied_plan)
        contract = json.loads(brief.split("PAGE_CHECK_CONTRACT:\n", 1)[1].split("\n", 1)[0])
        assert contract == tool_contract(inputs, supplied_plan)
        assert brief.index("PAGE_CHECK_CONTRACT:") < brief.index("PLAN REQUIREMENTS" if supplied_plan is None else "REVIEWED_PLAN:")
        assert "page-plan.txt" in contract["copy_rule"]
    contract = tool_contract(inputs, plan)
    checked = pipeline._plan_contract(inputs, plan)
    assert contract["required_text"] == checked.required_text
    assert [item["id"] for item in contract["actions"]] == [item.id for item in checked.actions]
    assert contract["how_it_works_step_count"] == (None if plan.sections[2].headline == "What it does" else 3)


def test_brief_and_reviewer_share_exact_deterministic_copy_rules(page):
    from app.pagebuilder import pipeline, tool
    from app.pagebuilder.qa import HOW_IT_WORKS_RULE, tool_contract
    inputs, plan, _ = page
    assert 'headline and nav_label must each be exactly What it does' in HOW_IT_WORKS_RULE
    assert HOW_IT_WORKS_RULE in pipeline._grounding_prompt(plan, inputs)
    for supplied_plan in (None, plan):
        brief = tool.build_brief(inputs, supplied_plan)
        contract = tool_contract(inputs, supplied_plan)
        assert HOW_IT_WORKS_RULE in brief
        if supplied_plan:
            assert contract["sections"][2]["nav_label"] == plan.sections[2].nav_label
        else:
            assert next(action for action in contract["actions"] if action["id"] == "nav_2")["name"] == "sections[2].nav_label"
        for required in contract["required_text"]:
            assert json.dumps(required, ensure_ascii=False) in brief
        assert json.loads(brief.split("PAGE_CHECK_CONTRACT:\n", 1)[1].split("\n", 1)[0])["actions"] == contract["actions"]


def test_brief_supplies_pasteable_navigation_and_consent_limited_copy(page):
    from app.pagebuilder import tool
    from app.pagebuilder.qa import PageParser, tool_contract
    inputs, plan, _ = page
    for supplied_plan in (None, plan):
        brief = tool.build_brief(inputs, supplied_plan)
        snippet = brief.split("NAVIGATION_HTML:\n", 1)[1].split("\n\n", 1)[0]
        parser = PageParser()
        parser.feed(snippet)
        actual = [(attrs["id"], attrs["href"]) for tag, attrs, _ in parser.elements if tag == "a"]
        expected = [(action["id"], action["href"]) for action in tool_contract(inputs, supplied_plan)["actions"]
            if action["id"].startswith("nav_")]
        assert actual == expected
        assert brief.index("NAVIGATION_HTML:") < brief.index("PLAN REQUIREMENTS" if supplied_plan is None else "REVIEWED_PLAN:")
        assert "joining the waitlist or requesting early-access updates only" in brief
        assert 'Example: "Join the waitlist for early-access updates."' in brief
        assert "does not promise a discussion, consultation, booking or product access" in brief
        assert "Otherwise invite the visitor to discuss their" not in brief
        assert "Use a domain-specific pilot request" not in brief


def test_model_copy_repair_includes_reason_and_consent_limited_rewrite(page):
    from app.pagebuilder import tool
    inputs, plan, _ = page
    index = next(index for index, section in enumerate(plan.sections) if section.id == "waitlist")
    finding = {"field_id": f"sections[{index}].body", "claim": f"Join {inputs.name} to discuss your use case.",
        "reason": "Waitlist consent does not establish a use-case discussion; limit the invitation to joining or requesting updates."}
    message = tool.failure_explanations(["pagebuilder_qa_failed:honest_copy:" + json.dumps([finding])])[0]
    assert f"Reviewer reason for {finding['field_id']}: {finding['reason']}" in message
    assert f'Acceptable rewrite for {finding["field_id"]}: "Join the waitlist for early-access updates."' in message
    assert "page-plan.txt and its rendered HTML" in message


def test_review_reconciliation_depends_on_saved_spend_custody(page, monkeypatch):
    from app.pagebuilder import runtime, tool
    doc = {"_runtime": {"operations": {}}}
    monkeypatch.setattr(runtime, "read", lambda *_: SimpleNamespace(payload_json=json.dumps(doc)))
    job_id = page[0].venture_id
    assert tool.review_requires_reconciliation(job_id, "pagebuilder_check_failed") is False
    for state, settled, expected in [("completed", True, False), ("completed", False, True),
            ("calling", False, True), ("uncertain", False, True), ("provider_responded", False, True),
            ("responded", False, True), ("release_pending", False, True), ("reserving", False, False)]:
        doc["_runtime"]["operations"] = {"review": {"state": state, "usage": {"settled": settled}}}
        assert tool.review_requires_reconciliation(job_id, "pagebuilder_check_failed") is expected
    doc["_runtime"]["operations"] = {}
    assert tool.review_requires_reconciliation(job_id, "pagebuilder_outcome_uncertain") is True
    doc.clear()
    assert tool.review_requires_reconciliation(job_id, "pagebuilder_check_failed") is True


@pytest.mark.parametrize("field", ["headline", "nav_label"])
def test_text_repairs_include_the_complete_expected_copy(page, field):
    from app.pagebuilder import tool
    from app.pagebuilder.qa import check_page
    inputs, plan, _ = page
    plan = plan.model_copy(deep=True)
    required = " ".join([plan.brief.proof_points[0]] * 3)
    plan.brief.proof_points.append(required)
    files = fake_tool(tool.build_brief(inputs, plan), plan, inputs)
    plan = tool.tool_page_plan(inputs, None, files)
    assert len(required) > 80
    html = files["index.html"].decode().replace(escape(required), "")
    failures = check_page(html, files, inputs, plan)["failures"]
    assert "required_text_missing:" + required in failures
    finding = {"field_id": "sections[2]." + field, "claim": getattr(plan.sections[2], field),
        "reason": "HOW_IT_WORKS_RULE requires both headline and navigation label to be What it does."}
    message = tool.failure_explanations(["pagebuilder_qa_failed:honest_copy:" + json.dumps([finding])])[0]
    assert f'Expected text for sections[2].{field}: "What it does".' in message


def fake_tool(brief, plan, inputs):
    from tests.unit.pagebuilder.test_metadata import png
    contract = json.loads(brief.split("PAGE_CHECK_CONTRACT:\n", 1)[1].split("\n", 1)[0])
    if "headline" not in contract["sections"][0]:
        def sentences(value):
            return re.split(r"(?<=[.!?])\s+", value.strip())
        for index, section in enumerate(plan.sections):
            contract["sections"][index].update(headline=section.headline, body=section.body)
            if section.id == "how-it-works" and section.headline != "What it does":
                contract["sections"][index]["steps"] = sentences(section.body)
        for action in contract["actions"]:
            action["name"] = (plan.sections[int(action["id"].split("_")[1])].nav_label
                if action["id"].startswith("nav_") else plan.brief.cta)
        contract["required_text"] = [inputs.name, plan.brief.concept_disclosure,
            *[section.headline for section in plan.sections], *plan.brief.proof_points,
            *[sentence for section in plan.sections for sentence in sentences(section.body)]]
    files, sections = {}, []
    for asset in plan.assets:
        files[f"assets/{asset.id}.svg"] = (f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="900" viewBox="0 0 1200 900">'
            '<rect x="120" y="120" width="960" height="660" rx="80" fill="#14584D"/>'
            '<circle cx="600" cy="450" r="180" fill="#D9A441"/>'
            '<circle cx="600" cy="450" r="130" fill="#14584D"/>'
            '<circle cx="600" cy="450" r="80" fill="#D9A441"/></svg>').encode()
    def link(action):
        attributes = ' target="_blank" rel="noopener noreferrer"' if action["kind"] == "external_link" else ""
        return f'<a id="{action["id"]}" href="{escape(action["href"], quote=True)}"{attributes}>{escape(action["name"])}</a>'
    for section in contract["sections"]:
        heading = "h1" if section["id"] == "hero" else "h2"
        body = ('<ol>' + ''.join(f'<li>{escape(step)}</li>' for step in section["steps"]) + '</ol>'
            if section.get("steps") else f'<p>{escape(section["body"])}</p>')
        artwork = ''.join(f'<img id="{asset.id}" src="assets/{asset.id}.svg" width="1200" height="900" '
            f'alt="Original geometric illustration of {escape(asset.subject, quote=True)}">'
            for asset in plan.assets if asset.section_id == section["id"])
        cta = ''.join(link(action) for action in contract["actions"] if action["id"] in section["cta_ids"])
        sections.append(f'<section id="{section["id"]}"><{heading}>{escape(section["headline"])}</{heading}>{body}{artwork}{cta}</section>')
    nav = ''.join(link(action) for action in contract["actions"] if action["id"].startswith("nav_"))
    copy = ''.join(f'<p>{escape(text)}</p>' for text in contract["required_text"])
    social = json.loads(re.search(r"\nSOCIAL_PREVIEW_CONTRACT[^\n]*:\n([^\n]+)", brief)[1])
    meta = ''.join(f'<meta {"property" if name.startswith("og:") else "name"}="{name}" content="{escape(value, quote=True)}">'
        for name, value in social.items())
    files["index.html"] = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{escape(inputs.name)}</title>{meta}<meta http-equiv="Content-Security-Policy" '
        'content="default-src \'none\'; base-uri \'none\'; img-src \'self\'; style-src \'unsafe-inline\'; form-action \'none\'">'
        '<style>body{background:#fff;color:#142522;font:18px Arial;margin:0}section{padding:32px}img{max-width:100%;height:auto}'
        'a{display:inline-flex;min-height:44px;padding:12px;color:#fff;background:#14584D}a:focus-visible{outline:3px solid #D9A441}</style>'
        f'</head><body><header>{escape(inputs.name)}<nav aria-label="Primary navigation">{nav}</nav></header>'
        f'<main>{"".join(sections)}{copy}</main></body></html>').encode()
    signup = json.loads(re.search(r"\nSIGNUP_CONTRACT[^\n]*:\n([^\n]+)", brief)[1])
    fields = ''.join(f'<input name="{field["name"]}" type="{field["type"]}"'
        + ''.join(f' {key}="{escape(str(field[key]), quote=True)}"' for key in ("value", "pattern", "maxlength") if key in field)
        + (' required' if field.get("required") else '') + '>' for field in signup["fields"])
    fields = fields.replace('<input name="email"', '<label>Email<input name="email"').replace('maxlength="254" required>', 'maxlength="254" required></label>')
    fields = fields.replace('<input name="website"', '<input hidden name="website"')
    fields = fields.replace('<input name="consent"', f'<label>{escape(signup["consent"])}<input name="consent"').replace('value="yes" required>', 'value="yes" required></label>')
    enctype = f' enctype="{signup["enctype"]}"' if signup["enctype"] else ''
    files["waitlist.html"] = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{escape(inputs.name)}</title>'
        f'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; base-uri \'none\'; form-action {escape(signup["action"], quote=True)}">'
        f'</head><body><main><form method="post" action="{escape(signup["action"], quote=True)}"{enctype}>{fields}'
        '<button type="submit">Send request</button></form></main></body></html>').encode()
    files["page-plan.txt"] = plan.model_dump_json().encode()
    files["assets/social-preview.png"] = png()
    return files


@pytest.mark.parametrize("supplied_plan", [True, False])
def test_tool_following_brief_passes_first_round(page, supplied_plan, monkeypatch):
    import boto3

    from app.pagebuilder.tool import build_brief, check_output

    def no_aws(*args, **kwargs):
        pytest.fail("Checking connected-tool output must not use AWS")

    monkeypatch.setattr(boto3, "client", no_aws)
    inputs, plan, _ = page
    files = fake_tool(build_brief(inputs, plan if supplied_plan else None), plan, inputs)
    checked = check_output(files, inputs, None)
    assert checked["passed"], checked["failures"]


@pytest.mark.parametrize("supplied_plan", [True, False])
def test_drawn_artwork_alt_is_not_a_grounded_product_claim(page, supplied_plan):
    from app.pagebuilder import pipeline, tool
    from tests.unit.pagebuilder.test_pipeline import review_payload

    inputs, draft, receipt = page
    brief = tool.build_brief(inputs, draft if supplied_plan else None)
    contract = json.loads(brief.split("PAGE_CHECK_CONTRACT:\n", 1)[1].split("\n", 1)[0])
    assert len(contract["artwork"]) == 2
    assert all(asset["width"] == 1200 and asset["height"] == 900 for asset in contract["artwork"])
    assert "aria-hidden" in contract["artwork_rule"] and "source quote" in contract["artwork_rule"]
    files = fake_tool(brief, draft, inputs)
    files["assets/" + draft.assets[0].id + ".svg"] = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 900">'
        '<rect width="1200" height="900" fill="#ffffff"/>'
        '<path d="M550 600 L610 270 L660 600 Z" fill="#14584D"/>'
        '<path d="M500 170 Q600 100 700 170" fill="none" stroke="#D9A441"/>'
        '<circle cx="560" cy="140" r="20" fill="#14584D"/></svg>').encode()
    alt = ("An illustrated turbine blade crossed by copper arcs." if receipt
        else "An illustrated pantry jar crossed by green leaves.")
    files["index.html"] = re.sub(rb'alt="[^"]*"', lambda match: ('alt="' + alt + '"').encode(), files["index.html"])
    checked = tool.check_output(files, inputs, None)
    assert checked["passed"], checked["failures"]
    plan = tool.tool_page_plan(inputs, None, files)
    public = pipeline.public_plan_copy(plan)
    assert not any(field.endswith(".alt") for field in public)
    prompt = pipeline._grounding_prompt(plan, inputs)
    svgs, _ = json.JSONDecoder().raw_decode(prompt.split("SANITIZED_SVG_BY_ASSET:\n", 1)[1])
    assert svgs == {}
    review = pipeline.CapabilityReview.model_validate(review_payload(prompt))
    pipeline.validate_grounding_review(review, plan, inputs)
    assert review.approved


@pytest.mark.parametrize("alt", ["Our product guarantees reliable results.", "Our product reduces costs.",
    "Our product delivers 40% faster results.", "Our product has 80 customers.", "Our product tracks your tasks.",
    "An illustration showing 50% reliability.", "An illustration showing 50 percent success.",
    "An illustration showing 50% reliable gradient.",
    "A picture with private-contact@example.test.", "A picture with fixture-private-artwork-token."])
def test_drawn_artwork_rejects_product_claims_and_private_data(page, monkeypatch, alt):
    from app.pagebuilder import tool
    from core import logging

    inputs, draft, _ = page
    inputs = inputs.model_copy(update={"clarified_answers": {**inputs.clarified_answers, "proof": alt}})
    monkeypatch.setattr(logging, "_local_secret_values", lambda: ["fixture-private-artwork-token"])
    files = fake_tool(tool.build_brief(inputs, draft), draft, inputs)
    files["index.html"] = re.sub(rb'alt="[^"]*"', lambda match: ('alt="' + alt + '"').encode(), files["index.html"])
    checked = tool.check_output(files, inputs, draft)
    assert not checked["passed"]
    message = " ".join(tool.failure_explanations(checked["failures"]))
    assert "pagebuilder_tool_artwork_invalid:" + draft.assets[0].id in message
    assert "alt" in message and "Replace" in message
    assert "private-contact@example.test" not in message and "fixture-private-artwork-token" not in message


@pytest.mark.parametrize("mode", ["decorative", "short", "numeric_picture", "numeric_opacity", "empty", "missing", "whitespace", "too_long"])
def test_drawn_artwork_alt_requires_text_or_explicit_decoration(page, mode):
    from app.pagebuilder import tool

    inputs, draft, _ = page
    files = fake_tool(tool.build_brief(inputs, draft), draft, inputs)
    replacement = {"decorative": b'alt="" aria-hidden="true"', "short": b'alt="A leaf"', "empty": b'alt=""',
        "numeric_picture": b'alt="A 50% green gradient with three circles."',
        "numeric_opacity": b'alt="An illustration at 25 percent opacity."',
        "missing": b"", "whitespace": b'alt="   "', "too_long": b'alt="' + b"A" * 161 + b'"'}[mode]
    files["index.html"] = re.sub(rb'alt="[^"]*"', replacement, files["index.html"])
    checked = tool.check_output(files, inputs, draft)
    if mode in {"decorative", "short", "numeric_picture", "numeric_opacity"}:
        assert checked["passed"], checked["failures"]
    else:
        assert not checked["passed"]
        message = " ".join(tool.failure_explanations(checked["failures"]))
        assert "pagebuilder_tool_artwork_invalid:" + draft.assets[0].id in message
        assert "alt" in message and ("160" if mode == "too_long" else "aria-hidden") in message


@pytest.mark.parametrize("missing", ["file", "image", "dimensions", "unsafe", "encoding", "viewbox"])
def test_artwork_repairs_identify_exact_file_image_and_size(page, missing):
    from app.pagebuilder import tool

    inputs, draft, _ = page
    asset = draft.assets[0]
    path = "assets/" + asset.id + ".svg"
    files = fake_tool(tool.build_brief(inputs, draft), draft, inputs)
    if missing == "file":
        for image in draft.assets:
            del files["assets/" + image.id + ".svg"]
    elif missing == "image":
        files["index.html"] = files["index.html"].replace(('id="' + asset.id + '"').encode(), b'id="wrong_artwork_id"')
    elif missing == "dimensions":
        files["index.html"] = files["index.html"].replace(b'width="1200" height="900"', b'width="300" height="200"')
    elif missing == "unsafe":
        files[path] = files[path].replace(b"</svg>", b"<script>alert(1)</script></svg>")
    elif missing == "encoding":
        files[path] = b"\xff"
    else:
        files[path] = files[path].replace(b'viewBox="0 0 1200 900"', b'viewBox="0 0 300 200"')
    checked = tool.check_output(files, inputs, draft)
    assert not checked["passed"]
    message = " ".join(tool.failure_explanations(checked["failures"]))
    assert path in message and asset.id in message
    if missing in {"file", "image", "dimensions"}:
        assert 'width="1200" height="900"' in message
    if missing == "file":
        assert all("assets/" + image.id + ".svg" in message for image in draft.assets)
    assert any(step in message for step in ("Create", "Set", "Replace"))


def test_tool_rechecks_cached_artwork_reviews_after_the_contract_changes(page, monkeypatch):
    from app.pagebuilder import api, pipeline, runtime, tool
    from tests.unit.pagebuilder.test_pipeline import review_payload

    inputs, draft, _ = page
    files = fake_tool(tool.build_brief(inputs, draft), draft, inputs)
    plan = tool.tool_page_plan(inputs, draft, files)
    doc = {"_actor_subject": "owner", "tool_build": {"source_review": {
        "stage": "capability_review_1", "plan_sha256": pipeline.plan_digest(plan)}},
        "_runtime": {"operations": {"variant_0:capability_review_1": {}}}}
    row = SimpleNamespace(workspace_id="workspace", venture_id=inputs.venture_id)
    monkeypatch.setattr(runtime, "validate_inputs", lambda *args: None)
    monkeypatch.setattr(api, "authority", lambda *args, **kwargs: (row.workspace_id, "owner"))
    monkeypatch.setattr(runtime, "mutate", lambda job_id, payload, callback: callback(row, doc, None))
    stages = []
    def review(stage, prompt):
        stages.append(stage)
        return json.dumps(review_payload(prompt))
    monkeypatch.setattr(runtime, "job_model_call", lambda *args: review)
    for attempt in range(2):
        assert tool.review_tool_source("page_job", inputs, plan, {"reviews": []})["passed"]
    assert stages == ["capability_review_2", "capability_review_2"]


@pytest.mark.parametrize("workflow_confirmed", [False, True])
def test_workflow_form_matches_confirmed_facts_without_blocking_the_page(page, workflow_confirmed):
    from app.pagebuilder import pipeline, tool
    from app.pagebuilder.models import GenerationDraft, PageInput
    from app.pagebuilder.qa import check_page, tool_contract
    from tests.unit.pagebuilder.test_pipeline import review_payload

    inputs, draft, receipt = page
    domain = "b2b" if receipt else "consumer"
    if workflow_confirmed:
        data, raw = customer_draft(domain)
        data.setdefault("clarified_answers", {})["workflow"] = raw["sections"][2]["body"]
        data["research"] = inputs.research
        for section in raw["sections"]:
            section["nav_label"] = None if section["id"] == "waitlist" else section["headline"]
        inputs, draft = PageInput.model_validate(data), GenerationDraft.model_validate(raw)
    else:
        fixtures = Path(__file__).with_name("fixtures")
        facts = json.loads((fixtures / (domain + "_claims.json")).read_text())
        inputs = inputs.model_copy(update={"name": facts["name"]})
        if domain == "consumer":
            inputs = inputs.model_copy(update={"idea": facts["idea"],
                "clarified_answers": {"offer": facts["offer"], "audience": facts["audience"]}})
        raw = draft.model_dump(mode="json")
        raw["brief"].update(promise=facts["claims"][0], proof_points=facts["claims"][:3],
            cta="Join the early-access list", concept_disclosure="Join the waitlist for early-access updates.")
        raw["sections"][0].update(headline=("Inspection for your turbine blades" if domain == "b2b"
            else "Ingredient tracking for your pantry"), body=facts["claims"][0])
        raw["sections"][1]["body"] = "What matters on your next list?"
        raw["sections"][2].update(headline="What it does", nav_label="What it does",
            purpose="Describe the confirmed capabilities and offer without a workflow.",
            body=facts["claims"][0] + " " + facts["claims"][2])
        raw["sections"][3]["body"] = ("Wind farm operators." if domain == "b2b" else "Home cooks.")
        raw["sections"][4]["body"] = "Early-access details are to be confirmed."
        raw["sections"][5]["body"] = "Join the waitlist for early-access updates."
        draft = GenerationDraft.model_validate(raw)

    for supplied_plan in (None, draft):
        brief = tool.build_brief(inputs, supplied_plan)
        files = fake_tool(brief, draft, inputs)
        checked = tool.check_output(files, inputs, None)
        assert checked["passed"], checked["failures"]
    plan = tool.tool_page_plan(inputs, None, files)
    contract = tool_contract(inputs, plan)
    rule = contract["how_it_works_rule"]
    assert rule in pipeline.plan_prompt(inputs)
    prompt = pipeline._grounding_prompt(plan, inputs)
    assert rule in prompt and rule in brief
    review = pipeline.CapabilityReview.model_validate(review_payload(prompt))
    pipeline.validate_grounding_review(review, plan, inputs)
    assert review.approved
    if workflow_confirmed:
        assert contract["how_it_works_step_count"] == 3
        assert len(contract["sections"][2]["steps"]) == 3
        broken = files["index.html"].decode().replace("<ol>", "<div>").replace("</ol>", "</div>").replace("<li>", "<p>").replace("</li>", "</p>")
        assert "customer_copy:render_three_how_it_works_steps" in check_page(broken, files, inputs, plan)["failures"]
    else:
        assert contract["how_it_works_step_count"] is None
        assert "steps" not in contract["sections"][2]
        body = contract["sections"][2]["body"]
        broken = files["index.html"].decode().replace(f"<p>{escape(body)}</p>", f"<ol><li>{escape(body)}</li></ol>")
        assert "customer_copy:what_it_does_must_not_be_ordered_steps" in check_page(broken, files, inputs, plan)["failures"]


def test_real_receipt_failures_have_actionable_explanations(page):
    from app.pagebuilder.tool import failure_explanations
    _, _, receipt = page
    failures = ([failure for iteration in receipt["iteration_receipts"] for failure in iteration["diagnosis"]["failures"]]
        if receipt else ["required_text_missing:Keep your recipes together.", "approved_action_id_missing:nav_2"])
    messages = failure_explanations(failures)
    assert len(messages) == len(failures)
    assert all(message.startswith(failure + ": ") and "\n" not in message for failure, message in zip(failures, messages))
    assert not any("unknown" in message.lower() for message in messages)


def test_three_fix_rounds_deliver_all_remaining_failures(page, monkeypatch, tmp_path):
    from desktop_runtime import coding_loop, landing_loop
    from app.pagebuilder import tool
    inputs, plan, _ = page
    workspace = tmp_path / "page"
    workspace.mkdir()
    (workspace / ".supymem-venture").write_text(inputs.venture_id)
    brief = tool.build_brief(inputs, plan)
    files = fake_tool(brief, plan, inputs)
    bad = dict(files)
    bad["index.html"] = files["index.html"].replace(b'</main>',
        b''.join(f'<p>Unreviewed sentence {index}.</p>'.encode() for index in range(35)) + b'</main>')
    remaining = tool.check_output(bad, inputs, None)["failures"]
    assert len(json.dumps(remaining).encode()) > 1500
    prompts = []
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(landing_loop, "time", SimpleNamespace(monotonic=lambda: clock.now, time=lambda: clock.now))
    original_check = tool.check_output
    def check(*args):
        clock.now += 45
        return original_check(*args)
    monkeypatch.setattr(tool, "check_output", check)
    def run(prompt, deadline):
        prompts.append(prompt)
        clock.now = deadline - 1
        if len(prompts) > 1:
            assert f"Fix round {len(prompts) - 1} of 3" in prompt
            assert all(message in prompt for message in tool.failure_explanations(remaining))
        for name, body in (files if len(prompts) == 4 else bad).items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        return {"outcome": "pass", "workspace_marker_verified": True}
    coding = SimpleNamespace(provider="codex", _workspace=lambda: workspace, authorize=lambda: True,
        tracker=SimpleNamespace(stop_all=lambda **kwargs: True), run=run)
    worker = SimpleNamespace(last={}, cancelled=SimpleNamespace(is_set=lambda: False),
        loop_saved={"task_snapshot": {"page_input": inputs.model_dump(mode="json"), "page_plan": None}},
        progress=lambda state, **values: worker.last.update(state=state, **values))
    monkeypatch.setattr(coding_loop, "_checkpoint", lambda *args: "a" * 40)
    monkeypatch.setattr(coding_loop, "_quiescent", lambda *args: True)
    manifest = {"coding_loop_verification": {"max_iterations": 4, "max_wall_seconds": 3600, "per_call_seconds": 900},
        "instruction": brief}
    from app.build.device_orders import CodingLoopVerification
    from pydantic import ValidationError
    caps = {**manifest["coding_loop_verification"], "operation": "workspace_loop_v1", "provider": "codex",
        "workspace_slug": "checked-page", "profile": "static_landing_v1", "check_recipe": "static_landing_checks_v1",
        "phase": "implement", "network": "none", "read_scope": "venture_workspace", "write_scope": "venture_workspace"}
    assert CodingLoopVerification.model_validate(caps).max_iterations == 4
    with pytest.raises(ValidationError, match="device_coding_loop_scope_invalid"):
        CodingLoopVerification.model_validate({**caps, "max_iterations": 5})
    result = landing_loop.run(worker, manifest,
        SimpleNamespace(order_id="dwo_page", iteration_receipt=lambda *_: None), coding, float("inf"))
    assert result["task_verified"] is True and result["iterations"] == 4
    assert clock.now <= manifest["coding_loop_verification"]["max_wall_seconds"]


def test_tool_repairs_the_mac_receipt_mistakes_from_exact_messages(page, monkeypatch, tmp_path):
    from contextlib import contextmanager
    from app.core.operation_receipts import OperationJournal
    from app.credits import estimate, gate
    from app.pagebuilder import api, runtime, tool
    from app.research import metered
    from desktop_runtime import coding_loop, landing_loop
    from tests.unit.pagebuilder.test_pipeline import review_payload
    inputs, plan, _ = page
    plan = plan.model_copy(deep=True)
    waitlist = next(section for section in plan.sections if section.id == "waitlist")
    waitlist.body = f"Join the {inputs.name} waitlist to discuss your use case."
    workspace = tmp_path / "page"
    workspace.mkdir()
    (workspace / ".supymem-venture").write_text(inputs.venture_id)
    brief = tool.build_brief(inputs, None)
    prompts, reviews = [], []
    missing_ids = set(range(5))
    missing_copy = set(plan.brief.proof_points)
    def run(prompt, deadline):
        prompts.append(prompt)
        if 'Acceptable rewrite for sections[5].body: "Join the waitlist for early-access updates."' in prompt:
            waitlist.body = "Join the waitlist for early-access updates."
        files = fake_tool(brief, plan, inputs)
        missing_ids.difference_update(index for index in list(missing_ids)
            if f"approved_action_id_missing:nav_{index}" in prompt)
        missing_copy.difference_update(proof for proof in list(missing_copy)
            if "required_text_missing:" + proof in prompt)
        for index in missing_ids:
            files["index.html"] = files["index.html"].replace(f' id="nav_{index}"'.encode(), b"")
        for proof in missing_copy:
            files["index.html"] = files["index.html"].replace(escape(proof).encode(), b"")
        for name, body in files.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        return {"outcome": "pass", "workspace_marker_verified": True}
    doc = {"_actor_subject": "owner", "tool_build": {"grounding": {"reviews": []}}, "_runtime": {"operations": {}}}
    row = SimpleNamespace(workspace_id="workspace", venture_id=inputs.venture_id)
    journal = OperationJournal(read=lambda *_: row, document=lambda *_: doc,
        mutate=lambda job_id, payload, callback: callback(row, doc, None), before_call=lambda *args: None, prefix="pagebuilder")
    settled = []
    @contextmanager
    def charge(**kwargs):
        yield SimpleNamespace(passthrough=True, hold=None, task_invocation=None,
            protect_outcome=lambda: None, record=lambda **counts: None, record_micro=lambda *args, **kwargs: None)
    def settle(usage, binding):
        assert usage["actual_micro"] == 3 and usage["source"] == "byok"
        settled.append(binding["request_id"])
        return 0
    def review_call(job_id, payload, *, deadline=None):
        def call(stage, prompt):
            public = json.loads(prompt.split("PUBLIC_COPY_BY_FIELD:\n", 1)[1].split("\nSANITIZED_SVG_BY_ASSET:", 1)[0])
            body = public["sections[5].body"]
            reviews.append(body)
            findings = [] if body == "Join the waitlist for early-access updates." else [{"field_id": "sections[5].body", "claim": body,
                "reason": "Waitlist consent does not establish a use-case discussion; limit the invitation to joining or requesting updates."}]
            response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1, total_tokens=3),
                choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(review_payload(prompt, findings))))])
            return metered.perform(job_id, "variant_0:" + stage, {"_job_attempt": 1, "_worker_id": "worker"},
                purpose="pagebuilder." + stage, provider="test", model="test", estimate=10, journal=journal,
                callback=lambda: response, parse=lambda content: {"content": content}, prefix="pagebuilder", require_stop=True, byok=True)["content"]
        return call
    monkeypatch.setattr(gate, "charge_sync", charge)
    monkeypatch.setattr(gate, "reconcile_receipt", settle)
    monkeypatch.setattr(estimate, "actual_llm_micro", lambda *args, **kwargs: 3)
    monkeypatch.setattr(runtime, "job_model_call", review_call)
    monkeypatch.setattr(runtime, "validate_inputs", lambda *args: None)
    monkeypatch.setattr(api, "authority", lambda *args, **kwargs: (row.workspace_id, "owner"))
    monkeypatch.setattr(runtime, "mutate", journal.mutate)
    monkeypatch.setattr(runtime, "read", lambda *_: SimpleNamespace(payload_json=json.dumps(doc)))
    monkeypatch.setattr(coding_loop, "_checkpoint", lambda *args: "a" * 40)
    monkeypatch.setattr(coding_loop, "_quiescent", lambda *args: True)
    coding = SimpleNamespace(provider="codex", _workspace=lambda: workspace, authorize=lambda: True,
        tracker=SimpleNamespace(stop_all=lambda **kwargs: True), run=run)
    worker = SimpleNamespace(last={}, cancelled=SimpleNamespace(is_set=lambda: False),
        loop_saved={"task_snapshot": {"page_input": inputs.model_dump(mode="json"), "page_plan": None,
            "page_brief": brief, "pagebuilder_job_id": "page_job"}})
    worker.progress = lambda state, **values: worker.last.update(state=state, **values)
    result = landing_loop.run(worker, {"instruction": brief, "coding_loop_verification": {
        "max_iterations": 4, "max_wall_seconds": 3600, "per_call_seconds": 900}},
        SimpleNamespace(order_id="dwo_page", iteration_receipt=lambda *_: None), coding, float("inf"))
    assert result["task_verified"] is True and result["iterations"] == 3
    assert len(worker.last["iteration_receipts"]) == 3 and reviews[-1] == "Join the waitlist for early-access updates."
    assert [receipt["outcome"] for receipt in worker.last["iteration_receipts"]] == ["tests_failed", "tests_failed", "pass"]
    assert all(receipt["outcome"] != "execution_requires_reconciliation" for receipt in worker.last["iteration_receipts"])
    assert len(set(settled)) == 2
    assert all(operation["state"] == "completed" and operation["usage"]["settled"] is True
        for operation in doc["_runtime"]["operations"].values())


@pytest.mark.parametrize("code", ["pagebuilder_call_budget_exhausted", "pagebuilder_outcome_uncertain",
    "pagebuilder_settlement_pending", "pagebuilder_operation_in_progress", "pagebuilder_provider_rejected",
    "pagebuilder_local_spend_limit", "private_provider_detail"])
def test_review_stop_saves_the_real_reason_and_current_iteration(page, monkeypatch, tmp_path, code):
    from app.core.jobs import FatalError
    from app.pagebuilder import runtime, tool
    from desktop_runtime import coding_loop, landing_loop
    inputs, plan, _ = page
    workspace = tmp_path / "page"
    workspace.mkdir()
    (workspace / ".supymem-venture").write_text(inputs.venture_id)
    brief = tool.build_brief(inputs, plan)
    files = fake_tool(brief, plan, inputs)
    def run(prompt, deadline):
        for name, body in files.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        return {"outcome": "pass", "workspace_marker_verified": True}
    def review(*args, **kwargs):
        raise FatalError(code) from TimeoutError("private provider body must not reach the receipt")
    monkeypatch.setattr(tool, "review_tool_source", review)
    monkeypatch.setattr(runtime, "read", lambda *_: SimpleNamespace(payload_json=json.dumps({"tool_build": {"grounding": {"reviews": []}},
        "_runtime": {"operations": {"previous_review": {"state": "completed", "usage": {"settled": True}}}}})))
    monkeypatch.setattr(runtime, "fail", lambda *args: None)
    monkeypatch.setattr(coding_loop, "_checkpoint", lambda *args: "a" * 40)
    monkeypatch.setattr(coding_loop, "_quiescent", lambda *args: True)
    coding = SimpleNamespace(provider="codex", _workspace=lambda: workspace, authorize=lambda: True,
        tracker=SimpleNamespace(stop_all=lambda **kwargs: True), run=run)
    worker = SimpleNamespace(last={}, cancelled=SimpleNamespace(is_set=lambda: False), loop_saved={"task_snapshot": {
        "page_input": inputs.model_dump(mode="json"), "page_plan": plan.model_dump(mode="json"), "pagebuilder_job_id": "page_job"}})
    worker.progress = lambda state, **values: worker.last.update(state=state, **values)
    result = landing_loop.run(worker, {"instruction": brief, "coding_loop_verification": {
        "max_iterations": 4, "max_wall_seconds": 3600, "per_call_seconds": 900}},
        SimpleNamespace(order_id="dwo_page", iteration_receipt=lambda *_: None), coding, float("inf"))
    uncertain = code in {"pagebuilder_outcome_uncertain", "pagebuilder_settlement_pending", "pagebuilder_operation_in_progress"}
    assert result["task_verified"] is False
    assert result["outcome"] == ("execution_requires_reconciliation" if uncertain else "checks_unavailable")
    expected = {"private_provider_detail": "pagebuilder_check_failed",
        "pagebuilder_local_spend_limit": "pagebuilder_spend_limit"}.get(code, code)
    assert result["stop_reason"] == result["failure_code"] == expected
    assert result["check_failure"]["category"] == "timeout"
    assert worker.last["iteration_receipts"][0]["diagnosis"]["failure_code"] == expected
    assert worker.last["iteration_receipts"][0]["diagnosis"]["next_action"] == "stop"
    assert "private provider body" not in json.dumps(worker.last)
    assert "private_provider_detail" not in json.dumps(worker.last)
    if code == "pagebuilder_local_spend_limit":
        assert "Monthly limit (USD)" in worker.last["message"]


def test_review_call_stops_before_dialing_after_the_build_deadline(page, monkeypatch):
    import time
    from app.core.jobs import FatalError
    from app.pagebuilder import runtime
    monkeypatch.setattr(runtime, "journal", SimpleNamespace(mutate=lambda *args: None, recover=lambda *args: None))
    with pytest.raises(FatalError, match="pagebuilder_check_timeout"):
        runtime.job_model_call("page_job", {}, deadline=time.monotonic() - 1)("capability_review_1", "Review the confirmed copy.")
