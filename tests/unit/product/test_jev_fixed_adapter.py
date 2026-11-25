"""Arm B (fixed) Jev adapter: state construction, floors, fallback. Pure functions, no network or DB."""
import json
import sys
from pathlib import Path

import pytest

for sub in ("packages", "services/company_os"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / sub))

from app.build import device_jev as jev
from coding_agents import jev_decision as d


def ctl(i, label, ops=("click",)):
    return {"id": i, "label": label, "operations": list(ops), "visible": True, "enabled": True}


def obs(controls=None, context="Harbor 12345 directory\nReference 0-0\nNext: Maple 22222", **kw):
    return d.Observation(revision="r", application="com.google.Chrome", location="https://x.test/", goal="visit pages",
        context=context, controls=controls if controls is not None else [ctl("c1", "Reference 0-0"), ctl("c2", "Maple 22222"), ctl("c3", "Reference 0-1")],
        permitted=True, confirmation_pending=False, prior_effect_uncertain=False, **kw)


def answer(body, picks, p=.8):
    out = {}
    for name, q in body["questions"].items():
        choice = picks[name]
        rest = (1 - p) / max(1, len(q["criteria"]) - 1)
        out[name] = {"type": "choice", "choice": choice,
                     "probabilities": {k: (p if k == choice else rest) for k in q["criteria"]}}
    return {"model": d.MODEL, "answers": out}


def test_default_state_unchanged_and_digest_stable():
    o = obs()
    body = d.request(o)
    assert "context" in body["state"] and "page_text" not in body["state"]
    assert "none" in body["questions"]["click_target"]["criteria"] and "blocked" in body["questions"]["operation"]["criteria"]
    assert o.digest() == obs(recent_actions=[]).digest() != obs(recent_actions=["click A"]).digest()


def test_fixed_state_has_text_actions_and_no_abstain_for_reversible_ops():
    body = d.request(obs(recent_actions=["click Harbor"]), fixed=True)
    assert "context" not in body["state"]
    assert "Maple 22222" in body["state"]["page_text"] and body["state"]["recent_actions"] == ["click Harbor"]
    assert set(body["questions"]["click_target"]["criteria"]) == {"c1", "c2", "c3"}
    assert "blocked" not in body["questions"]["operation"]["criteria"]


def test_fixed_keeps_abstain_without_controls_and_for_type():
    assert "blocked" in d.request(obs(controls=[]), fixed=True)["questions"]["operation"]["criteria"]
    typed = d.request(obs(controls=[ctl("c1", "Name", ("type",))], reviewed_text=["abc"]), fixed=True)
    assert "none" in typed["questions"]["type_target"]["criteria"]


def test_fixed_state_has_no_credential_text():
    text = "Welcome\npassword: hunter2\napi key sk-live\n" + "A" * 40 + "\nHarbor 1"
    body = d.request(obs(context=text, recent_actions=["click Next"]), fixed=True)
    blob = json.dumps(body)
    assert "hunter2" not in blob and "sk-live" not in blob and "A" * 40 not in blob and "Harbor 1" in blob


def test_page_text_capped_near_candidates():
    filler = " ".join(f"w{i}" for i in range(400))
    text = filler + " TARGET-LINK " + filler
    out = d.text_near(text, ["TARGET-LINK"])
    assert len(out) <= d.PAGE_TEXT_CAP and "TARGET-LINK" in out


def test_floors():
    o = obs()
    pick = {"operation": "click", "click_target": "c2"}
    assert d.parse(answer(d.request(o, fixed=True), pick, .6), o, fixed=True).target == "c2"
    assert d.parse(answer(d.request(o, fixed=True), pick, .45), o, fixed=True).operation == "blocked"
    assert d.parse(answer(d.request(o), {"operation": "click", "click_target": "c2"}, .6), o).operation == "blocked"  # arm A floor
    t = obs(controls=[ctl("c1", "Name", ("type",))], reviewed_text=["abc"])
    pk = {"operation": "type", "type_target": "c1", "type_value": "v0"}
    assert d.parse(answer(d.request(t, fixed=True), pk, .8), t, fixed=True).operation == "blocked"
    assert d.parse(answer(d.request(t, fixed=True), pk, .95), t, fixed=True).operation == "type"


def test_flag_default_on_with_kill_switch(monkeypatch):
    monkeypatch.delenv(jev.FIXED_ENV, raising=False)
    assert jev.fixed_enabled() is True
    monkeypatch.setenv(jev.FIXED_ENV, "0")
    assert jev.fixed_enabled() is False
    monkeypatch.setenv(jev.FIXED_ENV, "1")
    assert jev.fixed_enabled() is True


def test_fallback_accepts_first_json_object_only():
    o = obs()
    fb = lambda r: jev.fallback_candidate(r, o)  # noqa: E731
    assert fb('{"operation":"click","target":"c2"}\n{"operation":"click","target":"c1"} trailing').target == "c2"
    assert fb('{"operation":"click","target":"c2"} junk}}').target == "c2"
    for bad in ('{"operation":"click","target":"c2"', 'junk {"operation":"click","target":"c2"}', '[{"operation":"wait"}]', ""):
        assert fb(bad).operation == "blocked"


def test_fallback_model_env_and_effort(monkeypatch):
    from app.build import device_ai_step as step
    from coding_agents import cu_ai_loop as loop
    monkeypatch.delenv("SUPYMEM_CU_FALLBACK_MODEL", raising=False)
    assert step.fallback_model() == "gpt-6.1-sol"
    assert loop.reasoning_for("gpt-6.1-sol", 200) == ({"effort": "low"}, 1400)
    monkeypatch.setenv("SUPYMEM_CU_FALLBACK_MODEL", "gpt-6-luna")
    assert step.fallback_model() == "gpt-6-luna" and loop.reasoning_for("gpt-6-luna", 200) == ({"effort": "none"}, 200)
    monkeypatch.setenv("SUPYMEM_CU_FALLBACK_MODEL", "evil-model")
    assert step.fallback_model() == "gpt-6.1-sol"


def test_step_model_env(monkeypatch):
    from app.build import device_ai_step as step
    monkeypatch.delenv("SUPYMEM_CU_STEP_MODEL", raising=False)
    assert step.step_model() == "gpt-5.6-luna"
    monkeypatch.setenv("SUPYMEM_CU_STEP_MODEL", "gpt-6.1-sol")
    assert step.step_model() == "gpt-6.1-sol"


def test_pricing_rows_for_new_models():
    from app.credits.estimate import estimate_llm_micro
    from app.llm.pricing import pricing_table
    t = pricing_table()
    assert (t["gpt-6.1-sol"]["input_per_mtok"], t["gpt-6.1-sol"]["output_per_mtok"]) == (2.0, 10.0)
    assert (t["gpt-6-luna"]["input_per_mtok"], t["gpt-6-luna"]["output_per_mtok"]) == (0.1, 0.5)
    assert estimate_llm_micro("gpt-6.1-sol", prompt_tokens=1000, max_tokens=1000) > estimate_llm_micro("gpt-6-luna", prompt_tokens=1000, max_tokens=1000) > 0


def test_fallback_mapping_is_closed():
    o = obs()
    fb = lambda r, **k: jev.fallback_candidate(r, o, **k)  # noqa: E731
    assert (fb('{"operation":"click","target":"c2"}').operation, fb('{"operation":"click","target":"c2"}').target) == ("click", "c2")
    assert fb('{"operation":"click","target":"c9"}').operation == "blocked"
    assert fb('{"operation":"type","target":"c1"}').operation == "blocked"
    assert fb("not json").operation == "blocked"
    assert fb('{"operation":"done"}').operation == "blocked"
    assert fb('{"operation":"done"}', has_browser_verifier=True).requires_outcome_verification
    d.verify_current(fb('{"operation":"click","target":"c2"}'), o)


def test_decide_falls_back_when_jev_under_floor(monkeypatch):
    o = obs()
    saved = {"manifest": {"instruction": "visit pages", "applications": ["com.google.Chrome"], "origins": ["https://x.test"],
                          "actions": ["browser.read", "browser.interact"], "duration_seconds": 60, "title": "t",
                          "task_id": "t1", "credit_ceiling_micro": 8000},
             "review_digest": "a" * 64}
    monkeypatch.setenv(jev.FIXED_ENV, "1")
    monkeypatch.setenv("JEV_DEVICE_DECISIONS_ENABLED", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "x")
    seen = []
    monkeypatch.setattr(jev.orders, "retrieve", lambda *a: saved)
    monkeypatch.setattr(jev.calls, "admit", lambda *a, **k: seen.append(k["invocation_id"]) or {"admitted_now": True})
    monkeypatch.setattr(jev.calls, "record_result", lambda *a, **k: None)
    monkeypatch.setattr(jev.calls, "check_current", lambda *a: None)
    monkeypatch.setattr(jev.device_effects, "save_proposal", lambda *a, **k: None)
    body = d.request(o, fixed=True)
    value = answer(body, {"operation": "click", "click_target": "c2"}, .45)
    value["usage"] = {"input_tokens": 10, "output_tokens": 1}
    monkeypatch.setattr(jev, "_provider_call", lambda b: value)
    from app.build import device_ai_step as step
    sent = []
    monkeypatch.setattr(step, "_admit", lambda v, c, o_, i, *a, **k: seen.append(i))
    monkeypatch.setattr(step, "_settle", lambda *a, **k: None)
    monkeypatch.setattr(step, "_openai", lambda b: sent.append(b) or {"status": "completed", "usage": {"input_tokens": 5, "output_tokens": 5},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"operation":"click","target":"c1"}'}]}]})
    c = jev.decide_browser("v", "caller", "o", invocation_id="din_1", observation=o)
    assert (c.operation, c.target) == ("click", "c1") and seen == ["din_1", "din_1-fb"]
    assert sent[0]["model"] == "gpt-6.1-sol" and sent[0]["reasoning"] == {"effort": "low"}


def test_core_reply_json_first_object_strict():
    from app.build import device_browser_ai as core
    assert core.reply_json('{"passed":true,"reason":"x"} extra {"a":1}') == {"passed": True, "reason": "x"}
    for bad in ('{"passed":true', '{"a":1,"a":2}', '{"a":NaN}', "nope", '[{"a":1}]'):
        with pytest.raises(ValueError):
            core.reply_json(bad)
    assert core.strict_json('{"a":1}') == {"a": 1}
    with pytest.raises(ValueError):
        core.strict_json('{"a":1} extra')  # provider bodies and config stay strict
    assert {"gpt-6.1-sol", "gpt-6-luna"} <= core.SUPPORTED_MODELS
    rates = {"input_per_mtok": 2, "output_per_mtok": 10}
    assert core.cost_micro("gpt-6.1-sol", 272001, 0, rates=rates) > 1.9 * core.cost_micro("gpt-6.1-sol", 272000, 0, rates=rates)


def test_fixed_done_without_verifier_keeps_terminal_floor():
    o = obs(controls=[])
    body = d.request(o, fixed=True)
    hi, lo = answer(body, {"operation": "done"}, .95), answer(body, {"operation": "done"}, .7)
    assert jev._candidate(hi, o, has_browser_verifier=False, fixed=True).operation == "done"
    assert jev._candidate(lo, o, has_browser_verifier=False, fixed=True).operation == "blocked"
    assert jev._candidate(lo, o, has_browser_verifier=True, fixed=True).operation == "done"
