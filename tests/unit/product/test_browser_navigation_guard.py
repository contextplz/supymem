from desktop_runtime.device_worker import DeviceWorker, _navigation_unproven as unproven


def _safari_run(tmp_path, monkeypatch, title_after_return):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver
    from PIL import Image

    image_path = tmp_path / "window.png"
    Image.new("RGB", (120, 80), "white").save(image_path)
    app = AppChoice("Safari", "com.apple.Safari", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    effects, reviews = [], []

    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                returned = any(e["action"] == "key" for e in effects)
                return {"snapshot_id": f"ps1_{len(effects) + 1}", "screenshot_path": str(image_path),
                        "window_title": title_after_return if returned else "ClubCheck",
                        "elements": [{"id": "elem_1", "role": "AXTextField", "label": "smart search field",
                                      "enabled": True, "has_point": True, "actionable": True,
                                      "text_entry": True, "browser_address": True}]}
            if arguments["action"] == "wait":
                return {"waited_ms": 1}
            effects.append(arguments)
            return {"effect": "unverifiable"}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_mac", "ven_ours", None
        decisions = 0
        claim_attempted = False
        def claim(self): self.execution_id, self.claim_attempted = "dex_mac", True
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            self.decisions += 1
            if self.decisions == 1:
                return Candidate("type", "c1", observation.digest(), value_index=0)
            if self.decisions == 2:
                target = next(c for c in observation.controls if c.operations == ["key"])
                return Candidate("key", target.id, observation.digest())
            return Candidate("done", None, observation.digest(), True)
        def review_desktop_image(self, _invocation, snapshot_id, _encoded, observation=None):
            reviews.append(snapshot_id)
            return {"authority": "observation_only", "snapshot_id": snapshot_id,
                    "image_sha256": "a" * 64, "report": {"verdict": "done", "next_step": "",
                        "visible_facts": ["Safari is displaying fixture-grooming.supymem.com",
                                          "The visible page title is ClubCheck"]}}
        def admit_effect(self, *_args): return True
        def effect_receipt(self, *_args): return None
        def stop_receipt(self, *_args, **_kwargs): return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Open fixture-grooming.supymem.com in Safari", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Safari", "delivery_mode": "background", "max_steps": 5,
            "reviewed_text": ["fixture-grooming.supymem.com"], "visual_review": True}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)
    return worker, effects, reviews


def test_unchanged_title_is_not_done_and_return_is_not_preempted_by_review(tmp_path, monkeypatch):
    worker, effects, reviews = _safari_run(tmp_path, monkeypatch, "ClubCheck")
    # The typed address is not reviewed before Return has run.
    assert [e["action"] for e in effects] == ["type", "key"]
    assert len(reviews) == 1
    assert worker.last["outcome"] == "attention_required"
    review = worker.last["evidence"]["visual_review"]
    assert review["verdict"] == "blocked"
    assert "Navigation not confirmed" in review["visible_facts"][-1]


def test_changed_title_still_needs_result_review(tmp_path, monkeypatch):
    worker, _effects, _reviews = _safari_run(tmp_path, monkeypatch, "KennelDesk")
    assert worker.last["outcome"] == "visual_result_review_required"
    assert worker.last["task_verified"] is False


def test_navigation_unproven_needs_ax_title_change_or_matching_url():
    goal = ["Open fixture-grooming.supymem.com"]
    assert unproven("com.apple.Safari", goal, "A", "A", []) is not None
    assert unproven("com.apple.Safari", goal, "A", "", []) is not None
    assert unproven("com.apple.Safari", goal, "A", "B", []) is None
    assert unproven("com.apple.Safari", goal, "A", "A",
                    ["https://fixture-grooming.supymem.com/x"]) is None
    assert unproven("com.apple.Safari", goal, "A", "A", ["https://fixture-fitness.supymem.com/"]) is not None
    assert unproven("com.apple.Calculator", goal, "A", "A", []) is None
    assert unproven("com.apple.Safari", ["Read the page"], "A", "A", []) is None
