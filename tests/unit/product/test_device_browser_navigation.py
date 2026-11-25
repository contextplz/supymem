"""Closed typed-navigation witnesses never grant generic browser authority."""

import copy
import pytest
from tests.company_os_harness import load_company_os_app


@pytest.fixture
def nav():
    load_company_os_app()
    from app.build import device_browser_navigation

    return device_browser_navigation


def request_value():
    from coding_agents.jev_decision import Observation

    observation = Observation(
        revision="live",
        application="com.google.Chrome",
        location="https://example.test/",
        goal="Read page",
        context="Reviewed page",
        controls=[
            {
                "id": "c1",
                "label": "Next",
                "operations": ["click"],
                "visible": True,
                "enabled": True,
            }
        ],
        permitted=True,
        confirmation_pending=False,
        prior_effect_uncertain=False,
    )
    proof = {
        "qualified": True,
        "session_id": "a" * 32,
        "page_id": "e" * 32,
        "sequence": 1,
        "response": {
            "url": observation.location,
            "status": 200,
            "complete": True,
            "sequence": 1,
        },
        "location": observation.location,
        "document_id": "b" * 32,
        "ready_state": "complete",
        "visible_text": True,
        "visibility_scope": "bounded_dom_text_geometry",
        "observation_digest": observation.digest(),
    }
    return {
        "invocation_id": "nav_one",
        "observation": observation.model_dump(),
        "current_load": proof,
        "qualified_loads": [copy.deepcopy(proof)],
        "control_snapshots": [
            {
                "id": "c1",
                "tag": "A",
                "href": "https://example.test/next",
                "form": False,
                "visible": True,
                "enabled": True,
            }
        ],
    }


def saved():
    return {
        "manifest": {
            "task_id": "plt_test",
            "title": "Read approved page",
            "credit_ceiling_micro": 1000,
            "duration_seconds": 60,
            "applications": ["com.google.Chrome"],
            "origins": ["https://example.test"],
            "actions": ["browser.read", "browser.interact"],
            "instruction": "Read page",
            "browser_verification": {
                "expected_locations": [
                    "https://example.test/",
                    "https://example.test/next",
                ]
            },
        }
    }


def test_select_exact_reviewed_unique_link_without_provider(nav):
    candidate, summary = nav.select(
        saved(), nav.NavigationBody.model_validate(request_value())
    )
    assert candidate.operation == "click" and candidate.target == "c1"
    assert (
        summary["expected_location"] == "https://example.test/next"
        and summary["progress_count"] == 1
    )


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "wrong_href",
        "foreign_href",
        "form",
        "disabled",
        "missing",
        "digest",
        "order",
        "http_error",
        "blank",
        "pending",
        "foreign_history",
        "unqualified",
        "current_mismatch",
        "general",
        "native",
        "read_only",
    ],
)
def test_bad_witnesses_refuse_before_admission(nav, change):
    value = request_value()
    body = saved()
    if change == "duplicate":
        value["control_snapshots"] *= 2
    elif change == "wrong_href":
        value["control_snapshots"][0]["href"] = "https://example.test/decoy"
    elif change == "foreign_href":
        value["control_snapshots"][0]["href"] = "https://foreign.test/next"
    elif change == "form":
        value["control_snapshots"][0]["form"] = True
    elif change == "disabled":
        value["control_snapshots"][0]["enabled"] = False
    elif change == "missing":
        value["control_snapshots"] = []
    elif change == "digest":
        value["current_load"]["observation_digest"] = "0" * 64
    elif change == "order":
        value["qualified_loads"][0]["location"] = "https://example.test/next"
    elif change == "http_error":
        value["current_load"]["response"]["status"] = 404
    elif change == "blank":
        value["current_load"]["visible_text"] = False
    elif change == "pending":
        value["current_load"]["response"]["complete"] = False
    elif change == "foreign_history":
        value["qualified_loads"][0]["session_id"] = "c" * 32
    elif change == "unqualified":
        value["current_load"]["qualified"] = False
    elif change == "current_mismatch":
        value["qualified_loads"][0]["document_id"] = "c" * 32
    elif change == "general":
        body["manifest"]["general_browser_verification"] = {
            "operation": "general_goal_v1"
        }
    elif change == "native":
        body["manifest"]["desktop_verification"] = {}
    elif change == "read_only":
        body["manifest"]["actions"] = ["browser.read"]
    with pytest.raises(ValueError):
        nav.select(body, nav.NavigationBody.model_validate(value))


def test_new_popup_document_may_reset_source_page_sequence(nav):
    value = request_value()
    first = copy.deepcopy(value["current_load"])
    first["sequence"] = first["response"]["sequence"] = 2
    first["document_id"] = "d" * 32
    first["page_id"] = "f" * 32
    value["qualified_loads"] = [first, copy.deepcopy(value["current_load"])]
    candidate, _ = nav.select(saved(), nav.NavigationBody.model_validate(value))
    assert candidate.target == "c1"


@pytest.mark.parametrize(
    "kind",
    [
        "decreasing",
        "same_sequence_new_document",
        "same_document_new_sequence",
        "cross_page_document",
    ],
)
def test_source_page_history_cannot_reorder_or_rebind_document(nav, kind):
    value = request_value()
    first = copy.deepcopy(value["current_load"])
    if kind == "decreasing":
        first["sequence"] = first["response"]["sequence"] = 2
        first["document_id"] = "d" * 32
    elif kind == "same_sequence_new_document":
        first["document_id"] = "d" * 32
    elif kind == "same_document_new_sequence":
        value["current_load"]["sequence"] = value["current_load"]["response"][
            "sequence"
        ] = 2
    else:
        first["page_id"] = "f" * 32
    value["qualified_loads"] = [first, copy.deepcopy(value["current_load"])]
    with pytest.raises(ValueError):
        nav.select(saved(), nav.NavigationBody.model_validate(value))


def test_same_page_new_document_increases_sequence_and_exact_duplicate_is_safe(nav):
    value = request_value()
    first = copy.deepcopy(value["current_load"])
    first["document_id"] = "d" * 32
    value["current_load"]["sequence"] = value["current_load"]["response"][
        "sequence"
    ] = 2
    value["qualified_loads"] = [
        first,
        copy.deepcopy(value["current_load"]),
        copy.deepcopy(value["current_load"]),
    ]
    assert (
        nav.select(saved(), nav.NavigationBody.model_validate(value))[0].target == "c1"
    )


def test_reserved_probe_marker_matches_without_mutating_the_actual_href(nav):
    value = request_value()
    raw = "https://example.test/next?supymem_check=1"
    value["control_snapshots"][0]["href"] = raw
    body = nav.NavigationBody.model_validate(value)
    candidate, summary = nav.select(saved(), body)
    assert (
        candidate.target == "c1"
        and summary["expected_location"] == "https://example.test/next"
    )
    assert body.control_snapshots[0].href == raw


@pytest.mark.parametrize(
    "suffix",
    [
        "?unknown=1&supymem_check=1",
        "?Supymem_check=1",
        "?supymem_check=1#fragment",
        "?supymem_check=1&",
        "?other=a+b&supymem_check=1",
    ],
)
def test_marker_does_not_authorize_other_url_bytes(nav, suffix):
    value = request_value()
    value["control_snapshots"][0]["href"] = "https://example.test/next" + suffix
    with pytest.raises(ValueError):
        nav.select(saved(), nav.NavigationBody.model_validate(value))


def test_marker_removal_preserves_exact_other_encoding_order_and_fragment(nav):
    raw = "https://example.test/next?a=%20&supymem_check=1&b=x+y&a=%2f#keep"
    assert nav.strip_marker(raw) == "https://example.test/next?a=%20&b=x+y&a=%2f#keep"
    assert (
        nav.strip_marker("https://example.test/next?a=%20")
        == "https://example.test/next?a=%20"
    )
    value = request_value()
    value["control_snapshots"][0]["href"] = (
        "https://example.test/next?a=+&supymem_check=1"
    )
    body = saved()
    body["manifest"]["browser_verification"]["expected_locations"][1] = (
        "https://example.test/next?a=%20"
    )
    with pytest.raises(ValueError):
        nav.select(body, nav.NavigationBody.model_validate(value))


def test_marked_and_unmarked_duplicate_anchors_remain_ambiguous(nav):
    from coding_agents.jev_decision import Observation

    value = request_value()
    second = {**value["observation"]["controls"][0], "id": "c2"}
    value["observation"]["controls"].append(second)
    digest = Observation.model_validate(value["observation"]).digest()
    value["current_load"]["observation_digest"] = digest
    value["qualified_loads"][0]["observation_digest"] = digest
    value["control_snapshots"].append(
        {
            **value["control_snapshots"][0],
            "id": "c2",
            "href": "https://example.test/next?supymem_check=1",
        }
    )
    with pytest.raises(ValueError):
        nav.select(saved(), nav.NavigationBody.model_validate(value))
