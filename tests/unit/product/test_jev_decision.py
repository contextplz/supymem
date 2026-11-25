from copy import deepcopy

import pytest

from coding_agents.jev_decision import MODEL, Candidate, Observation, parse, request, verify_current


def observation(**updates):
    return Observation.model_validate({"revision": "snapshot-1", "application": "com.example.fixture",
        "location": "https://fixture.example", "goal": "Fill Company and continue", "context": "Company name supplied: Example",
        "permitted": True, "confirmation_pending": False, "prior_effect_uncertain": False,
        "controls": [{"id": "c1", "label": "Company", "operations": ["type"], "visible": True, "enabled": True},
                     {"id": "c2", "label": "Next", "operations": ["click"], "visible": True, "enabled": True}], **updates})


def response(obs, **choices):
    questions = request(obs)["questions"]
    return {"model": MODEL, "answers": {name: {"type": "choice", "choice": choices.get(name, "none"),
        "probabilities": {key: float(key == choices.get(name, "none")) for key in question["criteria"]}}
        for name, question in questions.items()}}


@pytest.mark.parametrize("operation", ["blocked", "wait", "done"])
def test_non_mutating_answers_ignore_irrelevant_targets(operation):
    obs = observation()
    result = parse(response(obs, operation=operation, click_target="c2", type_target="c1"), obs)
    assert result.operation == operation and result.target is None
    assert result.requires_outcome_verification is (operation == "done")


def test_only_compatible_target_can_be_selected():
    obs = observation()
    result = parse(response(obs, operation="type", type_target="c1", click_target="c2"), obs)
    assert result.operation == "type" and result.target == "c1"
    invalid = response(obs, operation="type", type_target="c1")
    invalid["answers"]["type_target"]["choice"] = "c2"
    with pytest.raises(ValueError, match="answer_invalid"):
        parse(invalid, obs)


def test_desktop_type_selects_only_founder_reviewed_literal():
    obs = observation(reviewed_text=["google.com", "websites that sell pets"])
    candidate = parse(response(obs, operation="type", type_target="c1",
                               click_target="c2", type_value="v1"), obs)
    assert candidate.value_index == 1
    verify_current(candidate, obs)
    changed = Observation.model_validate({**obs.model_dump(), "reviewed_text": ["different text"]})
    with pytest.raises(ValueError, match="observation_changed"):
        verify_current(candidate, changed)


def test_scroll_is_a_scoped_observed_control():
    obs = observation(controls=[{"id": "c1", "label": "Scroll the selected window down",
                                "operations": ["scroll"], "visible": True, "enabled": True}])
    candidate = parse(response(obs, operation="scroll", scroll_target="c1"), obs)
    assert candidate.operation == "scroll" and candidate.target == "c1"
    verify_current(candidate, obs)


@pytest.mark.parametrize("field", ["confirmation_pending", "prior_effect_uncertain", "permitted"])
def test_deterministic_review_gates_precede_provider_request(field):
    obs = observation(**{field: field != "permitted"})
    with pytest.raises(ValueError, match="requires_review"):
        request(obs)


def test_hidden_and_disabled_controls_are_absent_from_the_paid_request():
    obs = observation()
    data = obs.model_dump()
    data["controls"][0]["visible"] = False
    data["controls"][1]["enabled"] = False
    body = request(Observation.model_validate(data))
    assert body["state"]["controls"] == []
    questions = body["questions"]
    assert set(questions) == {"operation"}
    assert set(questions["operation"]["criteria"]) == {"blocked", "wait", "done"}


def test_operation_question_receives_control_semantics_in_shared_state():
    body = request(observation())
    assert body["state"]["controls"][0]["label"] == "Company"
    assert body["state"]["controls"][0]["operations"] == ["type"]


def test_low_probability_on_relevant_head_abstains_without_guessing():
    obs = observation()
    value = response(obs, operation="click", click_target="c2")
    value["answers"]["click_target"]["probabilities"] = {"c2": .6, "none": .4}
    assert parse(value, obs).operation == "blocked"


@pytest.mark.parametrize("mutation", ["model", "missing_head", "extra_head", "nan", "bool", "wrong_winner"])
def test_malformed_provider_response_is_not_an_action(mutation):
    obs = observation()
    value = response(obs, operation="click", click_target="c2")
    if mutation == "model":
        value["model"] = "unverified-alias"
    elif mutation == "missing_head":
        del value["answers"]["type_target"]
    elif mutation == "extra_head":
        value["answers"]["execute_command"] = {}
    elif mutation in {"nan", "bool"}:
        value["answers"]["click_target"]["probabilities"]["c2"] = float("nan") if mutation == "nan" else True
    else:
        value["answers"]["click_target"]["probabilities"] = {"c2": .1, "none": .9}
    with pytest.raises(ValueError):
        parse(value, obs)


def test_snapshot_changes_invalidate_a_decision():
    obs = observation()
    candidate = parse(response(obs, operation="click", click_target="c2"), obs)
    verify_current(candidate, obs)
    changed = deepcopy(obs.model_dump())
    changed["revision"] = "snapshot-2"
    with pytest.raises(ValueError, match="observation_changed"):
        verify_current(candidate, Observation.model_validate(changed))
    with pytest.raises(ValueError, match="requires_review"):
        verify_current(candidate, observation(confirmation_pending=True))


@pytest.mark.parametrize("operation,target,verify", [("terminal", "c1", False), ("click", "c1", False),
    ("click", "none", False), ("done", None, False), ("blocked", "c2", False)])
def test_current_validation_rejects_invalid_or_repurposed_candidates(operation, target, verify):
    obs = observation()
    with pytest.raises(ValueError):
        verify_current(Candidate(operation, target, obs.digest(), verify), obs)
