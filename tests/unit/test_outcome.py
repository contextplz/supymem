"""Outcome thresholds require measured evidence, including for negative results."""
import pytest

from tests.unit.test_experiment_scan_recipes import research_snapshot, venture


@pytest.mark.parametrize("audience,metric,value,display", [
    ("Freight operations teams", "signup_rate_ratio", 2.5, "Measured signup rate ratio: 2.5"),
    ("Families arranging neighborhood walks", "referrer_share", .1, "10% of your sample"),
    ("Independent agency owners", "referrer_share", .004, "0.4% of your sample"),
])
def test_outcome_summary_displays_plain_metric_and_unknowns(research_snapshot, audience, metric, value, display):
    from app.experiments.autonomous import _outcome_line

    spec = {"scan": "page_test", "icp": {"who": audience}, "metric": metric}
    summary = _outcome_line(spec, {"events": 30, metric: value}, verdict="supported")
    assert display in summary and "Next:" in summary
    for missing in (None, float("nan"), float("inf"), True):
        unknown = _outcome_line(spec, {"events": 0, metric: missing}, verdict="inconclusive")
        assert "not measurable yet" in unknown and "Measured" not in unknown


def test_outcome_threshold_table(research_snapshot):
    from app.experiments.autonomous import outcome

    spec = {"icp": research_snapshot["journey"]["icp"], "metric": "signal_share", "min_sample": 5,
            "success_threshold": {"op": ">=", "value": .4, "min_events": 5},
            "failure_threshold": {"op": "<", "value": .15, "min_events": 5}}
    cases = [
        ({"signal_share": .4, "events": 5}, "supported"),
        ({"signal_share": .39, "events": 5}, "inconclusive"),
        ({"signal_share": .14, "events": 5}, "refuted"),
        ({"signal_share": .15, "events": 5}, "inconclusive"),
        ({"signal_share": .8, "events": 4}, "inconclusive"),
        ({"signal_share": 0, "events": 0}, "inconclusive"),
        ({"signal_share": 0, "events": 5}, "refuted"),
        ({"signal_share": None, "events": 5}, "inconclusive"),
        ({"events": 5}, "inconclusive"),
        ({"signal_share": float("nan"), "events": 5}, "inconclusive"),
        ({"signal_share": float("inf"), "events": 5}, "inconclusive"),
        ({"signal_share": True, "events": 5}, "inconclusive"),
        ({"signal_share": .8, "events": False}, "inconclusive"),
        ({"signal_share": .8, "events": -1}, "inconclusive"),
        ({"signal_share": .8, "events": float("inf")}, "inconclusive"),
    ]
    for measured, expected in cases:
        assert outcome(spec, measured) == expected, measured
    for operation, target, value in ((">", 1, 2), ("<=", 1, 1), ("==", 1, 1), ("!=", 1, 2)):
        assert outcome({**spec, "success_threshold": {"op": operation, "value": target}},
                       {"signal_share": value, "events": 5}) == "supported"
    assert outcome({**spec, "success_threshold": {"op": "invalid", "value": .4}},
                   {"signal_share": .8, "events": 5}) == "inconclusive"
    split_metric = {**spec, "failure_threshold": {"op": "<", "value": .15, "min_events": 5, "metric": "other_share"}}
    assert outcome(split_metric, {"signal_share": .3, "other_share": .1, "events": 5}) == "refuted"
    assert outcome(split_metric, {"signal_share": .3, "other_share": .2, "events": 5}) == "inconclusive"
