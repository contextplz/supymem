"""The shared grant cannot widen ownership, inputs, current identity or catalog."""
from dataclasses import FrozenInstanceError, replace

import pytest

from core.browser_qualification import (
    FIXTURE_ORIGIN, FIXTURE_PREFIX, FixtureField, FixtureRule, LONG_TASKS,
    OwnedFixtureGrant, REFUSAL_TASKS, SHORT_TASKS,
    QualificationTrialBinding, request_binding_digest,
)


def form_grant():
    nonce = "a" * 32
    path = FIXTURE_PREFIX + nonce + "/form"
    read = FixtureRule("read_form", "GET", path, "", path, "document-form", "", "", (), 4, 0, ())
    fields = (FixtureField("project", "Atlas", "project", "text", "Atlas", "text"),
              FixtureField("email", "qa@example.invalid", "email", "email", "qa@example.invalid", "text"),
              FixtureField("code", "ABC123", "code", "synthetic_code", "ABC123", "text"))
    post = FixtureRule("submit_form", "POST", path, "", path, "document-form", "form-project", "form-submit",
                       fields, 1, 1, ("read_form",))
    return OwnedFixtureGrant(1, "usr_synthetic", "dev_synthetic", "ws_one", "ven_one",
        "bqt_one", "1" * 64, "form_valid", "2" * 64, "dwo_one", "3" * 64, "4" * 64,
        2_000_000_000, nonce, FIXTURE_ORIGIN, (read, post), ("navigate", "type", "click", "extract"))


def test_immutable_roundtrip_exact_form_and_safe_default_subsets():
    grant = form_grant()
    wire = grant.to_dict()
    assert OwnedFixtureGrant.from_dict(wire) == grant
    with pytest.raises(FrozenInstanceError):
        grant.origin = "https://foreign.invalid"
    wire["rules"][1]["fields"][0]["value"] = "changed"
    assert grant.rules[1].fields[0].value == "Atlas"
    with pytest.raises(ValueError):
        OwnedFixtureGrant.from_dict(wire)
    assert grant.max_tabs == 1 and grant.frames == grant.downloads == ()
    fields = tuple((field.name, field.value) for field in grant.rules[1].fields)
    assert grant.request_rule(url=FIXTURE_ORIGIN + grant.rules[1].path, method="POST",
        submitted_fields=fields, completed_rules=("read_form",)).rule_id == "submit_form"
    assert grant.request_rule(url=FIXTURE_ORIGIN + grant.rules[1].path, method="POST",
        submitted_fields=tuple(reversed(fields)), completed_rules=("read_form",)).rule_id == "submit_form"
    with pytest.raises(ValueError):
        grant.request_rule(url=FIXTURE_ORIGIN + grant.rules[1].path, method="POST",
            submitted_fields=fields + (fields[0],), completed_rules=("read_form",))
    for kwargs in ({"completed_rules": ()}, {"submitted_fields": fields + (("owned", "true"),)},
                   {"method": "PUT"}, {"url": "https://127.0.0.1" + grant.rules[1].path},
                   {"url": FIXTURE_ORIGIN + grant.rules[1].path + "?extra=1"}):
        request = dict(url=FIXTURE_ORIGIN + grant.rules[1].path, method="POST",
            submitted_fields=fields, completed_rules=("read_form",)) | kwargs
        with pytest.raises(ValueError):
            grant.request_rule(**request)


def test_foreign_unknown_and_credential_scopes_fail_closed():
    grant = form_grant()
    for changes in ({"origin": "http://api.supymem.com"}, {"origin": "https://api.supymem.com:443"},
                    {"device_id": True}, {"expires_at": True}, {"max_tabs": 5},
                    {"task_id": "live_extra"}, {"allowed_operations": ("filesystem",)},
                    {"task_id": "pay_wall"}):
        with pytest.raises(ValueError):
            replace(grant, **changes)
    for name, value, kind in (("password", "test", "text"), ("card", "4111111111111111", "text"),
                             ("email", "real@example.com", "email"), ("otp", "ABC123", "synthetic_code")):
        with pytest.raises(ValueError):
            FixtureField(name, value, "control", kind)
    wire = grant.to_dict() | {"owned": True}
    with pytest.raises(ValueError):
        OwnedFixtureGrant.from_dict(wire)
    with pytest.raises(ValueError):
        replace(grant.rules[1], path=FIXTURE_PREFIX + "foreign/form")
        replace(grant, rules=(grant.rules[0], replace(grant.rules[1], path=FIXTURE_PREFIX + "foreign/form")))


def test_bound_account_device_review_and_profile_cannot_be_reused():
    grant = form_grant()
    current = dict(actor_id=grant.actor_id, device_id=grant.device_id,
        workspace_id=grant.workspace_id, venture_id=grant.venture_id, order_id=grant.order_id,
        review_digest=grant.review_digest, profile_digest=grant.profile_digest, now=1_900_000_000)
    grant.assert_current(**current)
    for key in current:
        changed = current | {key: grant.expires_at if key == "now" else "foreign"}
        with pytest.raises(ValueError):
            grant.assert_current(**changed)


def test_negative_refusal_and_exact_26_plus_six_plus_four_catalog():
    grant = form_grant()
    refusal = replace(grant, task_id="pay_wall", rules=(grant.rules[0],), allowed_operations=("navigate", "extract"))
    assert OwnedFixtureGrant.from_dict(refusal.to_dict()) == refusal
    assert len(SHORT_TASKS) == 26 and len(LONG_TASKS[:6]) == 6 and len(LONG_TASKS[6:]) == 4
    assert len(set(SHORT_TASKS + LONG_TASKS)) == 36
    assert REFUSAL_TASKS == {"login_creds", "login_wall", "captcha_wall", "pay_wall"}


def test_reviewed_same_route_variants_preserve_json_types_and_prerequisites():
    grant = form_grant()
    first = replace(grant.rules[1], rule_id="count_one", fields=(FixtureField("count", 1, "counter", "json_literal"),), body_kind="json")
    second = replace(first, rule_id="count_two", fields=(FixtureField("count", 2, "counter", "json_literal"),),
                     prerequisites=("count_one",))
    grant = replace(grant, task_id="counter", rules=(grant.rules[0], first, second))
    assert OwnedFixtureGrant.from_dict(grant.to_dict()) == grant
    request = dict(url=grant.origin + first.path, method="POST", submitted_fields=(("count", 2),),
                   completed_rules=("read_form", "count_one"))
    assert grant.request_rule(**request) == second
    with pytest.raises(ValueError):
        grant.request_rule(**(request | {"submitted_fields": (("count", "2"),)}))
    with pytest.raises(ValueError):
        grant.request_rule(**(request | {"completed_rules": ("read_form",)}))
    items = FixtureField.from_dict({"name": "items", "value": ["milk", "eggs"], "control_id": "todo", "kind": "json_literal"})
    assert items.value == ("milk", "eggs")
    assert FixtureField("prev", "CODE1-abcd", "previous", "synthetic_code").value == "CODE1-abcd"


def test_boolean_integer_float_and_array_element_types_do_not_alias():
    grant = form_grant()
    post = replace(grant.rules[1], fields=(FixtureField("count", 1, "counter", "json_literal"),), body_kind="json")
    grant = replace(grant, task_id="counter", rules=(grant.rules[0], post))
    request = dict(url=grant.origin + post.path, method="POST", completed_rules=("read_form",))
    for value in (True, 1.0, "1", [1], [True]):
        with pytest.raises(ValueError):
            grant.request_rule(**request, submitted_fields=(("count", value),))
    assert grant.request_rule(**request, submitted_fields=(("count", 1),)) == post
    boolean = replace(post, fields=(FixtureField("count", True, "counter", "json_literal"),))
    grant = replace(grant, rules=(grant.rules[0], boolean))
    with pytest.raises(ValueError):
        grant.request_rule(**request, submitted_fields=(("count", 1),))
    assert grant.request_rule(**request, submitted_fields=(("count", True),)) == boolean
    for value in ((1,), (True,), [1], [False]):
        with pytest.raises(ValueError):
            FixtureField.from_dict({"name": "items", "value": value, "control_id": "todo", "kind": "json_literal"})


def test_effect_rule_membership_is_typed_and_get_body_kind_is_closed():
    grant = form_grant()
    integer = replace(grant.rules[1], fields=(FixtureField("count", 1, "counter", "json_literal"),), body_kind="json")
    grant = replace(grant, rules=(grant.rules[0], integer))
    boolean = replace(integer, fields=(FixtureField("count", True, "counter", "json_literal"),))
    assert request_binding_digest(grant, integer, "request_one")
    with pytest.raises(ValueError):
        request_binding_digest(grant, boolean, "request_one")
    with pytest.raises(ValueError):
        replace(grant.rules[0], body_kind="caller_owned")


def test_search_query_fields_and_radio_are_exact_and_trial_binding_has_no_exemption():
    grant = form_grant()
    search = replace(grant.rules[0], query="q=Atlas", form_id="search-form", control_id="search-submit",
                     fields=(FixtureField("q", "Atlas", "search-query", "text"),))
    grant = replace(grant, task_id="search_price", rules=(search,))
    assert grant.request_rule(url=grant.origin + search.path + "?q=Atlas", method="GET") == search
    with pytest.raises(ValueError):
        replace(search, fields=(FixtureField("q", "foreign", "search-query", "text"),))
    assert FixtureField("plan", "pro", "plan-pro", "radio").kind == "radio"
    binding = replace(grant.trial_binding(), task_id="l01_research_synthesis")
    assert QualificationTrialBinding.from_dict(binding.to_dict()) == binding
    assert not hasattr(binding, "origin") and not hasattr(binding, "rules")
    with pytest.raises(ValueError):
        QualificationTrialBinding.from_dict(binding.to_dict() | {"origin": grant.origin})


def test_request_literals_never_become_typing_authority_implicitly():
    counter = FixtureField("count", 1, "inc", "json_literal")
    assert counter.input_value is counter.input_type is None
    todo = FixtureField("items", ("milk", "eggs"), "t", "json_literal", "eggs", "text")
    assert todo.value == ("milk", "eggs") and todo.input_value == "eggs"
    empty_note = FixtureField("note", "", "note", "json_literal")
    assert empty_note.input_value is None
    for changes in ({"input_value": 1, "input_type": "text"}, {"input_value": "eggs\n", "input_type": "text"},
                    {"input_value": "eggs", "input_type": "password"}):
        with pytest.raises(ValueError):
            replace(todo, **changes)
    email = form_grant().rules[1].fields[1]
    assert email.input_type == "text"  # Original fixture has no HTML email-type validation.
    assert replace(email, input_type="email").input_value == email.value
    with pytest.raises(ValueError):
        replace(email, input_value="other@example.invalid")
    with pytest.raises(ValueError):
        replace(form_grant().rules[1].fields[2], input_value="another-code")
