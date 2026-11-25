from types import SimpleNamespace

from desktop_runtime.cu_browser.action_cache import ActionCache, cacheable


def payload(text="Hello", history=(), url="https://x.test/a?q=1", els=None):
    obs = SimpleNamespace(notes=f"PAGE URL: {url}\nTABS: t\nPAGE TEXT: {text}",
                          elements=els or [{"id": "b0", "ax_role": "a", "label": "Next"}, {"id": "b1", "ax_role": "button", "label": "Buy"}])
    return {"goal": "g", "subgoal": 0, "history": list(history), "observation": obs}


def run(cache, p, reply):
    calls = []

    def decide(_):
        calls.append(1)
        return reply, {"in": 5, "out": 5}
    return cache.wrap(decide)(p), len(calls)


NAV = {"actions": [{"op": "click", "element_id": "b0"}]}


def test_commits_only_after_page_changed_then_replays_exact_situation():
    c = ActionCache()
    assert run(c, payload(), NAV)[1] == 1
    # same situation again before any change: still a miss (nothing committed)
    assert run(c, payload(), NAV)[1] == 1
    run(c, payload(text="Page two"), {"actions": [{"op": "wait", "ms": 1}]})   # page changed -> first reply committed
    (reply, usage), calls = run(c, payload(), {"actions": []})
    assert calls == 0 and reply == NAV and usage["cached"] and c.hits == 1


def test_any_difference_is_a_miss():
    c = ActionCache()
    run(c, payload(), NAV)
    run(c, payload(text="B"), NAV)
    for p in (payload(text="Hello changed"), payload(history=["1. x"]), payload(url="https://x.test/b")):
        assert run(c, p, NAV)[1] == 1


def test_never_caches_submits_typing_or_done():
    obs = payload()["observation"]
    for reply in ({"actions": [{"op": "click", "element_id": "b1"}]}, {"actions": [{"op": "type", "element_id": "b0", "text": "x"}]},
                  {"actions": [{"op": "done", "summary": "s"}]}, {"actions": [{"op": "key", "chord": "return"}]}):
        assert not cacheable(reply, obs)
    assert cacheable(NAV, obs) and cacheable({"actions": [{"op": "navigate", "text": "https://a.b/c"}]}, obs)


def test_timing_in_history_does_not_break_replay(tmp_path):
    c = ActionCache(tmp_path / "c.json")
    run(c, payload(history=["1. navigate -> navigate ok (0.031s)"]), NAV)
    run(c, payload(text="B"), NAV)
    assert ActionCache(tmp_path / "c.json").items   # persisted
    assert run(ActionCache(tmp_path / "c.json"), payload(history=["1. navigate -> navigate ok (0.120s)"]), {"actions": []})[1] == 0
