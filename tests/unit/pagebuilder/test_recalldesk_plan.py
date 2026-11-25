"""Recorded raw copy fragments; clipped logs did not contain illustration poses."""
import json
import math
from pathlib import Path

import pytest

from tests.unit.pagebuilder.test_pipeline import fixture_draft, fixture_html, modules, review_payload, stub_render


RAW_COPY = json.loads((Path(__file__).parent / "fixtures/recalldesk_raw_copy.json").read_text())


@pytest.fixture(autouse=True)
def hosted_generation(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")


def recalldesk(modules, record="planrun2_repair"):
    models = modules["models"]
    inputs = models.PageInput(venture_id="ven_recalldesk", name="RecallDesk",
        idea="automatic SMS and WhatsApp follow-up to appointments marked no-show or cancelled in the practice management system; patients get an invitation to rebook by callback, self-schedule link or preferred-time reply",
        clarified_answers={"audience": "Independent dental clinics in the US with 1 to 3 dentists where the front-desk manager handles scheduling by phone"},
        waitlist_url="https://gateway.supymem.com/public/waitlists/ven_recalldesk")
    draft = fixture_draft()
    draft["brief"].update(RAW_COPY[record])
    draft["sections"][0]["body"] = draft["brief"]["promise"]
    for asset, asset_id, kinds in zip(draft["assets"], ["hero_illustration", "how_it_works_illustration"],
                                    [("calendar_page", "envelope"), ("calendar_page", "chat_bubble", "envelope")]):
        next(section for section in draft["sections"] if section["image_id"] == asset["id"])["image_id"] = asset_id
        asset["id"] = asset_id
        asset["scene"]["objects"] = [{"kind": kind, "x": 600, "y": 450, "scale": 1.8, "rotation": 45}
                                      for kind in kinds]
    return inputs, draft


def recalldesk_controls(modules):
    inputs, draft = recalldesk(modules)
    draft["brief"].update(audience="Independent dental clinics", problem="Missed visits leave front-desk follow-up work.",
        cta="Request early access", concept_disclosure="A proposed concept for appointment follow-up.")
    draft["sections"][0].update(headline="A follow-up path for missed visits", nav_label="Overview")
    draft["sections"][1].update(headline="At the front desk", nav_label="At the front desk",
        body="Review the appointment follow-up concept for independent dental clinics.")
    draft["sections"].insert(2, {"id": "follow_up", "purpose": "Describe the proposed patient follow-up path",
        "headline": "Follow-up path", "body": "Patients rebook via callback, self-schedule link, or time-preference reply.",
        "nav_label": "Follow-up path"})
    draft["sections"][-1].update(headline="Review early access", body="Review consent and request early access to the concept.")
    plan, _ = modules["pipeline"]._validated_plan(json.dumps(draft), inputs, (), rewrite_capabilities=True)
    contract = modules["qa"].qa_contract(inputs, plan)
    nav = "<nav>" + "".join(f'<a id="{action.id}" href="#{action.target}">{action.name}</a>'
                            for action in contract.actions if action.id.startswith("nav_")) + "</nav>"
    html = fixture_html(plan, inputs).replace("<main>", nav + "<main>")
    html = html.replace("</style>", '#hero_cta::after,#waitlist_cta::after{content:" ↗"}</style>')
    return inputs, plan, html


@pytest.mark.parametrize("decoration", ["css", "text", "hidden_span", "aria_override"])
def test_generated_recalldesk_control_names_are_pinned_before_render_and_manifest(modules, monkeypatch, decoration):
    pipeline, qa = modules["pipeline"], modules["qa"]
    inputs, plan, html = recalldesk_controls(modules)
    contract = qa.qa_contract(inputs, plan)
    if decoration != "css":
        for action in contract.actions[:2]:
            suffix = '<span aria-hidden="true"> ↗</span>' if decoration == "hidden_span" else " ↗"
            html = html.replace(action.name + "</a>", action.name + suffix + "</a>")
            if decoration == "aria_override":
                html = html.replace('id="' + action.id + '"', 'aria-labelledby="hero" aria-label="Overview" id="' + action.id + '"')
    monkeypatch.setattr(pipeline, "_prepare_grounded_plan", lambda *_: (plan, 0, [], {"reviews": []}))
    rendered = []
    def render(source, *_):
        parser = qa.PageParser()
        parser.feed(source)
        controls = [{"id": attrs["id"], "role": "link", "name": attrs["aria-label"],
                     "target": attrs["href"], "enabled": True} for tag, attrs, _ in parser.elements if tag == "a"]
        assert {control["id"] for control in controls} == {action.id for action in contract.actions}
        assert all(not attrs.get("aria-labelledby") for tag, attrs, _ in parser.elements if tag == "a")
        rendered.append(source)
        result = stub_render(source)
        for observation in result["observations"].values():
            observation["visibleControls"] = controls
        return result
    built = pipeline.generate_page(inputs, lambda *_: html, render=render)
    assert built.qa["passed"] and rendered == [built.files["index.html"]]
    assert built.manifest["qa_contract"] == contract.model_dump(mode="json")
    assert qa.pin_control_names(built.files["index.html"], contract) == built.files["index.html"]
    assert len(contract.actions) == 5 and "↗" in built.files["index.html"]


@pytest.mark.parametrize("defect", ["extra_anchor", "extra_button", "wrong_role", "wrong_target", "wrong_copy", "hidden_name", "disabled", "details", "duplicate"])
def test_pinning_recalldesk_names_does_not_approve_undeclared_or_broken_controls(modules, defect):
    qa = modules["qa"]
    inputs, plan, html = recalldesk_controls(modules)
    if defect == "extra_anchor":
        html = html.replace("</nav>", '<a href="#waitlist">Early access</a></nav>')
    elif defect == "extra_button":
        html = html.replace("</nav>", '<button>Early access</button></nav>')
    elif defect == "details":
        html = html.replace('<section id="waitlist">', '<details><summary>Early access</summary><section id="waitlist">').replace("</main>", "</details></main>")
    elif defect == "duplicate":
        html = html.replace("</main>", '<a id="hero_cta" href="#waitlist">Explore early access</a></main>')
    elif defect == "wrong_target":
        html = html.replace('id="hero_cta" href="#waitlist"', 'id="hero_cta" href="#hero"')
    elif defect == "wrong_copy":
        html = html.replace("Explore early access</a>", "Overview</a>")
    else:
        attribute = {"wrong_role": 'role="button"', "hidden_name": 'aria-hidden="true"', "disabled": 'aria-disabled="true"'}[defect]
        html = html.replace('id="hero_cta"', attribute + ' id="hero_cta"')
    html = qa.pin_control_names(html, qa.qa_contract(inputs, plan))
    result = qa.check_page(html, {asset.path: asset.svg for asset in plan.assets}, inputs, plan)
    assert not result["passed"]


def test_cta_contract_uses_the_final_deterministic_copy_rewrite(modules):
    pipeline, models, qa = (modules[key] for key in ("pipeline", "models", "qa"))
    inputs, draft = recalldesk(modules)
    draft["brief"]["cta"] = "RecallDesk sends SMS."
    final, corrections = pipeline._rewrite_capability_copy(models.GenerationDraft.model_validate(draft), inputs)
    assert any(item["field"] == "brief.cta" for item in corrections)
    contract = qa.qa_contract(inputs, pipeline.compile_draft_plan(inputs, final))
    assert contract.actions[1].name == final.brief.cta != draft["brief"]["cta"]
    html = qa.pin_control_names(f'<a id="waitlist_cta">{final.brief.cta}</a>', contract)
    assert 'aria-label="' + final.brief.cta + '"' in html


def test_recalldesk_html_repair_removes_undeclared_controls_and_pins_names_again(modules, monkeypatch):
    pipeline, qa = modules["pipeline"], modules["qa"]
    inputs, plan, html = recalldesk_controls(modules)
    monkeypatch.setattr(pipeline, "_prepare_grounded_plan", lambda *_: (plan, 0, [], {"reviews": []}))
    stages = []
    def call(stage, prompt):
        stages.append(stage)
        if stage == "code":
            return html.replace("</nav>", '<a href="#waitlist">Early access</a></nav>')
        assert stage == "repair_1" and '"unapproved_controls"' in prompt
        return html
    def render(source, *_):
        assert 'href="#waitlist">Early access</a>' not in source
        result = stub_render(source)
        parser = qa.PageParser()
        parser.feed(source)
        for observation in result["observations"].values():
            observation["visibleControls"] = [{"id": attrs["id"], "role": "link", "name": attrs["aria-label"],
                "target": attrs["href"], "enabled": True} for tag, attrs, _ in parser.elements if tag == "a"]
        return result
    built = pipeline.generate_page(inputs, call, render=render)
    assert built.qa["passed"] and stages == ["code", "repair_1"]


@pytest.mark.parametrize("record", RAW_COPY)
def test_recorded_copy_and_overlapping_decorations_recover_after_two_copy_repairs(modules, record):
    pipeline, images, qa = (modules[key] for key in ("pipeline", "images", "qa"))
    inputs, draft = recalldesk(modules, record)
    stages = []
    def call(stage, prompt):
        stages.append(stage)
        if stage != "brief_direction_plan":
            diagnostics = json.loads(prompt.split("RAW_PUBLIC_COPY_ERRORS:\n", 1)[1].split("\nReturn the complete", 1)[0])
            claim = next(item for item in diagnostics if item["field_id"] == "brief.promise")["capability_claims"][0]
            assert claim["rejected_sentence"] == draft["brief"]["promise"].split(". ")[0].rstrip(".") + "."
            assert "pre-launch offer/intent" in claim["rule"]
            assert "is built to" in claim["acceptable_rewrite"]
        return json.dumps(draft)
    plan, repairs, corrections = pipeline._prepare_plan(inputs, call, ())
    assert stages == ["brief_direction_plan", "plan_repair_1", "plan_repair_2"] and repairs == 2
    assert plan.brief.promise.startswith("RecallDesk is built to ")
    assert plan.sections[0].body == plan.brief.promise
    assert plan.sections[1] == modules["models"].Section.model_validate(draft["sections"][1])
    assert plan.brief.proof_points == draft["brief"]["proof_points"]
    assert len([item for item in corrections if item["provenance"] == "controller_prelaunch_offer_copy"]) == 2
    assert qa.honest_copy(plan.brief.promise, inputs) == []
    images.validate_compiled_assets(plan)
    assert pipeline.compile_draft_plan(inputs, pipeline.scene_draft(plan)) == plan
    files = {asset.path: asset.svg for asset in plan.assets}
    assert not any("capability_claim" in reason for reason in qa.check_page(fixture_html(plan, inputs), files, inputs, plan)["failures"])
    for asset in plan.assets:
        boxes = []
        for obj in asset.scene.objects:
            cosine, sine = abs(math.cos(math.radians(obj.rotation))), abs(math.sin(math.radians(obj.rotation)))
            radius_x, radius_y = obj.scale * (155 * cosine + 185 * sine), obj.scale * (185 * cosine + 155 * sine)
            box = (obj.x - radius_x, obj.y - radius_y, obj.x + radius_x, obj.y + radius_y)
            assert 0 <= box[0] < box[2] <= 1200 and 0 <= box[1] < box[3] <= 900
            assert all(min(box[2], prior[2]) <= max(box[0], prior[0]) or min(box[3], prior[3]) <= max(box[1], prior[1]) for prior in boxes)
            boxes.append(box)


def test_copy_repair_can_succeed_without_deterministic_rewrite(modules):
    pipeline = modules["pipeline"]
    inputs, draft = recalldesk(modules)
    stages = []
    def call(stage, prompt):
        stages.append(stage)
        if stage == "plan_repair_2":
            draft["brief"]["promise"] = draft["brief"]["promise"].replace("RecallDesk sends", "RecallDesk is built to send")
            draft["sections"][0]["body"] = draft["brief"]["promise"]
        return json.dumps(draft)
    _, repairs, corrections = pipeline._prepare_plan(inputs, call, ())
    assert repairs == 2 and len(stages) == 3
    assert not any(item["provenance"] == "controller_prelaunch_offer_copy" for item in corrections)


@pytest.mark.parametrize("claim", ["RecallDesk sends reminders to 10000 customers.", "RecallDesk provides HIPAA certification.",
    "RecallDesk integrates with Salesforce as a live partner.", "RecallDesk guarantees rebooking."])
def test_false_evidence_is_never_rewritten_and_error_mapping_stays_plan_rejected(modules, claim):
    pipeline = modules["pipeline"]
    inputs, draft = recalldesk(modules)
    draft["brief"]["promise"] = claim
    normalized, corrections = pipeline._rewrite_capability_copy(modules["models"].GenerationDraft.model_validate(draft), inputs)
    assert normalized.brief.promise == claim
    assert not any(item["field"] == "brief.promise" for item in corrections)
    with pytest.raises(ValueError, match="^pagebuilder_plan_rejected$"):
        pipeline.prepare_plan(inputs, lambda *_: json.dumps(draft))
    from app.tasks.contracts import blocker
    problem = blocker("pagebuilder_plan_rejected")
    assert "complete, usable page plan" in str(problem)


def test_fallback_copy_still_gets_an_independent_capability_review(modules):
    pipeline = modules["pipeline"]
    inputs, draft = recalldesk(modules)
    draft["brief"]["promise"] = "RecallDesk sends unconfirmed insurance reminders."
    draft["sections"][0]["body"] = "Request updates on automatic appointment follow-up."
    stages = []
    def call(stage, prompt):
        stages.append(stage)
        if stage.startswith("capability_review_"):
            assert "RecallDesk is built to send unconfirmed insurance reminders." in prompt
            return json.dumps(review_payload(prompt, [{"field_id": "brief.promise",
                "claim": "unconfirmed insurance reminders", "reason": "The founder only confirmed missed appointment follow-up."}]))
        if stage.startswith("plan_repair_") and int(stage.removeprefix("plan_repair_")) >= 3:
            draft["brief"]["promise"] = "RecallDesk is built to send unconfirmed insurance reminders."
        return json.dumps(draft)
    with pytest.raises(ValueError, match="pagebuilder_capability_grounding_rejected"):
        pipeline._prepare_grounded_plan(inputs, call, ())
    assert len(stages) <= pipeline.MAX_MODEL_CALLS
    assert "capability_review_3" in stages


def test_grounding_copy_repairs_use_extended_stages_without_exceeding_metered_budget(modules):
    pipeline = modules["pipeline"]
    inputs, draft = recalldesk(modules)
    draft["brief"]["promise"] = "RecallDesk sends unconfirmed insurance reminders."
    draft["sections"][0]["body"] = "Request updates on automatic appointment follow-up."
    stages = []
    def call(stage, prompt):
        stages.append(stage)
        if stage.startswith("capability_review_"):
            if stage == "capability_review_2":
                return json.dumps(review_payload(prompt))
            return json.dumps(review_payload(prompt, [{"field_id": "brief.promise",
                "claim": "unconfirmed insurance reminders", "reason": "Insurance reminders were not confirmed by the founder."}]))
        if stage == "plan_repair_5":
            draft["brief"]["promise"] = "RecallDesk sends appointment follow-up via SMS or WhatsApp."
        return json.dumps(draft)
    plan, repairs, _, grounding = pipeline._prepare_grounded_plan(inputs, call, ())
    assert grounding["passed"] and repairs == 5 and len(stages) + 1 <= pipeline.MAX_MODEL_CALLS
    assert stages[-2:] == ["plan_repair_5", "capability_review_2"]
    assert "insurance" not in plan.brief.promise


def test_extended_repair_receipt_requires_settled_successor_in_same_variant(modules):
    from app.pagebuilder.native_reference import _replaced_output
    stage = "plan_repair_4"
    successor = "variant_0:plan_repair_" + str(int(stage.removeprefix("plan_repair_")) + 1)
    operations = {successor: {"state": "completed", "usage": {"settled": True}, "response": {"content": "corrected draft"}}}
    assert _replaced_output(operations, "variant_0:" + stage)
    assert not _replaced_output(operations, "variant_1:" + stage)
    operations[successor]["usage"]["settled"] = False
    assert not _replaced_output(operations, "variant_0:" + stage)
