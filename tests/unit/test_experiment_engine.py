from datetime import datetime, timedelta, timezone

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()


def test_ranking_scores_reasons_and_guardrails_across_domains():
    from app.credits.units import credits_to_micro
    from app.experiments import engine

    now = datetime(2026, 10, 26, tzinfo=timezone.utc)
    for venture_id, audience in (("ven_dispatch", "freight dispatchers"), ("ven_hiking", "weekend hikers")):
        inputs = {"venture_id": venture_id, "stage": "ideating", "direction": "current",
                  "understanding": {"assumptions": [f"Assumed: {audience} will pay for the offer."],
                                    "risks": [f"The message may not connect with {audience}."]}}
        ranking = engine.rank_assumptions(inputs)
        assert [row["question_code"] for row in ranking] == ["Q1", "Q3", "Q2", "Q5", "Q6", "Q4", "Q7", "Q8", "Q9"]
        assert all(row["uncertainty"] == 1 for row in ranking)
        assert next(row for row in ranking if row["question_code"] == "Q3")["assumption"].endswith("will pay for the offer.")
        evidence = [{"question": "Q1", "state": "concluded", "outcome": "supported", "direction": "current", "scan": "interviews"},
                    {"question": "Q1", "state": "concluded", "outcome": "supported", "direction": "current", "scan": "interviews"},
                    {"question": "Q3", "state": "concluded", "outcome": "supported", "direction": "old", "scan": "deposits"}]
        ranked = engine.rank_assumptions({**inputs, "experiments": evidence, "journey": {"reachable_leads": [{}]},
                                         "volume": {"replies": 1}})
        by_code = {row["question_code"]: row for row in ranked}
        assert by_code["Q1"]["uncertainty"] == .3
        assert by_code["Q3"]["uncertainty"] == 1
        assert by_code["Q4"]["uncertainty"] == by_code["Q5"]["uncertainty"] == .6
        assert ranked[0]["question_code"] == "Q3"
        evidence.append({**evidence[0], "scan": "public_complaints"})
        assert next(row for row in engine.rank_assumptions({**inputs, "experiments": evidence})
                    if row["question_code"] == "Q1")["uncertainty"] == .1
        assert next(row for row in engine.rank_assumptions({**inputs, "stage": "operating"})
                    if row["question_code"] == "Q9")["impact"] == 5
        assert next(row for row in engine.rank_assumptions({**inputs, "stage": "live"})
                    if row["question_code"] == "Q9")["impact"] == 5
        weak = engine.rank_assumptions({**inputs, "journey": {"need_signals": [{}]}})
        assert next(row for row in weak if row["question_code"] == "Q1")["uncertainty"] == .6
        experiment = {"id": "candidate", "venture_id": venture_id, "direction": "current", "question": "Q3",
                      "hypothesis": f"{audience} will pay for the offer", "icp": {"who": audience},
                      "info": .7, "founder_minutes": 30, "ceiling_micro": credits_to_micro(50),
                      "days": 14, "sample_unit": "views", "min_sample": 50, "variable": "price"}
        assert engine.score(experiment, ranked, {"views": 49}) == pytest.approx(5 * .7 * .2 / (3 * 3))
        assert engine.score(experiment, ranked, {"views": 50}) == pytest.approx(5 * .7 / (3 * 3))
        assert engine.score({"spec": experiment}, ranked, {"views": 50}) == engine.score(experiment, ranked, {"views": 50})
        assert engine.score({"scan": "demand"}, ranking, {}) == pytest.approx(5 * .5 / (1 + 1 / 7))
        assert engine.score({"scan": "public_posts", "question": "Q1", "sample_unit": "pages", "min_sample": 6}, ranking, {}) == pytest.approx(5 * .5 / (1 + 1 / 7))
        assert engine.reason(experiment, ranked).startswith("Biggest open risk:")
        assert "about 2 weeks" in engine.reason(experiment, ranked)
        assert "your offer" in engine.reason(experiment, ranked) and "—" not in engine.reason(experiment, ranked)
        with pytest.raises(ValueError):
            engine.score({**experiment, "info": 2}, ranked, {})
        running = [{**experiment, "id": str(index), "state": "running", "variable": "channel"} for index in range(3)]
        assert "3 experiments" in engine.blocked_reason(experiment, running, now)
        assert not engine.blocked_reason(experiment, [{**row, "venture_id": "other"} for row in running], now)
        assert "variable and audience" in engine.blocked_reason(experiment, [{**running[0], "variable": "price"}], now)
        assert not engine.blocked_reason(experiment, [{**running[0], "variable": "price", "icp": {"who": "different"}}], now)
        done = {**experiment, "state": "concluded", "ended_at": now - timedelta(days=13)}
        assert "14 days" in engine.blocked_reason(experiment, [done], now)
        assert "14 days" in engine.blocked_reason({**experiment, "window_end": "tomorrow", "fingerprint": "daily"}, [done], now)
        assert not engine.blocked_reason({**experiment, "icp": {"who": "different"}}, [done], now)
        assert not engine.blocked_reason({**experiment, "angles": [{"replies": 1}]}, [done], now)
        assert not engine.blocked_reason({**experiment, "input_fingerprint": "new"}, [{**done, "input_fingerprint": "old"}], now)
        assert not engine.blocked_reason(experiment, [{**done, "ended_at": now - timedelta(days=14)}], now)
