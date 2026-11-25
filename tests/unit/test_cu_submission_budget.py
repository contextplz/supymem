"""Original submit-attempt authority is persisted before observed form delivery."""

import threading

import pytest

from coding_agents.cu_ai_loop import Action, Caps, LoopState
from coding_agents.cu_exact_button_budget import exact_button_budgets
from desktop_runtime.cu_ai_cua import CuaError
from desktop_runtime.cu_ai_engine import Observation, run_loop
from desktop_runtime.cu_ai_retries import RetryGuard

GOAL = "Fill the owned workspace form and submit once. Report its assigned reference."


def elements():
    return [
        {
            "id": "b0",
            "ax_role": "input",
            "type": "text",
            "focused": False,
            "form_scope": "form:0:0",
        },
        {
            "id": "b1",
            "ax_role": "button",
            "label": "Save",
            "type": "",
            "submits_form": True,
            "form_scope": "form:0:0",
            "focused": True,
            "is_enabled": True,
        },
        {"id": "b2", "ax_role": "a", "label": "Read documentation"},
    ]


class FormMac:
    def __init__(self, failure=None):
        self.attempts = self.delivered = 0
        self.failure = failure

    def observe(self):
        obs = Observation(
            b"", elements(), "Chrome (browser)", "Owned form", "", False, None
        )
        obs.notes = (
            "PAGE URL: https://owned.example/form\nPAGE TEXT: No record committed"
            if self.delivered
            else "Owned form"
        )
        return obs

    def frontmost(self):
        return "Chrome (CDP)", 0

    def focused_role(self):
        return None

    def act(self, action, obs):
        self.attempts += 1
        if self.failure == "before" and self.attempts == 1:
            raise CuaError("known before dispatch", no_effect=True)
        self.delivered += 1
        if self.failure == "after":
            raise CuaError("unknown after dispatch")
        return "click ok (0.01s)"


def actor(payload):
    return {"actions": [{"op": "click", "element_id": "b1"}]}, {}


def run(mac, *, goal=GOAL, checkpoint=None, stop=None, verify=None):
    return run_loop(
        goal,
        actor,
        mac,
        stop or threading.Event(),
        checkpoint=checkpoint,
        verify=verify or (lambda *args: (False, "No accepted record")),
        caps=Caps(max_steps=8, max_seconds=60, explicit=True),
    )


def test_delivered_validation_rejection_consumes_attempt_and_cannot_be_replayed():
    mac = FormMac()
    result = run(mac)
    assert result.outcome != "done" and mac.delivered == 1
    assert any(r.get("outcome") == "exact_submission_limit_reached" for r in result.log)


def test_named_button_and_form_limits_are_both_reserved_before_delivery():
    saved = []
    ck = type(
        "CK",
        (),
        {
            "load": lambda self: None,
            "clear": lambda self: None,
            "save": lambda self, row: saved.append(row),
        },
    )()
    mac = FormMac("after")
    result = run(
        mac, goal="Click Save button exactly 1 times; submit once.", checkpoint=ck
    )
    assert result.reason == "effect_reconciliation_required" and mac.delivered == 1
    assert saved[-1]["pending_dispatch"] is True
    assert [b["used"] for b in saved[-1]["exact_button_budgets"]] == [1, 1]


def test_proven_pre_dispatch_refusal_restores_submission_quota_only():
    mac = FormMac("before")
    run(mac)
    assert mac.attempts == 2 and mac.delivered == 1


def test_used_attempt_survives_resume_even_after_rejected_business_result():
    saved = {
        **LoopState().snapshot(GOAL),
        "pending_dispatch": False,
        "route_retries": RetryGuard().snapshot(),
        "exact_button_budgets": [
            {"label": "__original_task_form_submission__", "limit": 1, "used": 1}
        ],
    }
    ck = type(
        "CK",
        (),
        {
            "load": lambda self: saved,
            "clear": lambda self: None,
            "save": lambda self, row: None,
        },
    )()
    mac = FormMac()
    assert run(mac, checkpoint=ck).outcome != "done" and mac.attempts == 0


def test_missing_submission_checkpoint_cannot_reset_authority():
    saved = {
        **LoopState().snapshot(GOAL),
        "pending_dispatch": False,
        "route_retries": {},
    }
    ck = type("CK", (), {"load": lambda self: saved})()
    mac = FormMac()
    assert run(mac, checkpoint=ck).reason == "checkpoint_action_budget_unreconciled"
    assert mac.attempts == 0


def test_stop_before_delivery_restores_all_overlapping_reservations():
    stop, saved = threading.Event(), []

    def save(row):
        saved.append(row)
        if row["pending_dispatch"]:
            stop.set()

    ck = type(
        "CK",
        (),
        {
            "load": lambda self: None,
            "clear": lambda self: None,
            "save": lambda self, row: save(row),
        },
    )()
    mac = FormMac()
    result = run(
        mac,
        goal="Click Save button exactly 1 times; submit once.",
        checkpoint=ck,
        stop=stop,
    )
    assert result.outcome == "stopped" and mac.attempts == 0
    assert saved[-1]["pending_dispatch"] is False
    assert [b["used"] for b in saved[-1]["exact_button_budgets"]] == [0, 0]


@pytest.mark.parametrize(
    "chord", ["Enter", "Return", "CTRL+ENTER", " return ", "Space", "CMD+Space"]
)
def test_activation_aliases_share_one_form_attempt(chord):
    budget = exact_button_budgets(GOAL)[0]
    assert budget.admission(Action("key", chord=chord), elements()) == (1, None)
    budget.used = 1
    assert (
        budget.admission(Action("key", chord=chord), elements())[1]
        == "exact_submission_limit_reached"
    )


def test_implicit_enter_from_observed_form_field_consumes_attempt():
    rows = elements()
    rows[0]["focused"], rows[1]["focused"] = True, False
    assert exact_button_budgets(GOAL)[0].admission(
        Action("key", chord="Enter"), rows
    ) == (1, None)


@pytest.mark.parametrize(
    "action",
    [
        Action("double_click", element_id="b1"),
        Action("click", x=10, y=10),
        Action("script", text="opaque code"),
        Action("template", chord="opaque"),
    ],
)
def test_unbounded_or_opaque_delivery_is_refused_before_input(action):
    assert exact_button_budgets(GOAL)[0].admission(action, elements())[1]


def test_read_navigation_and_textarea_newline_do_not_spend_attempt():
    budget = exact_button_budgets(GOAL)[0]
    assert budget.admission(Action("click", element_id="b2"), elements()) == (0, None)
    assert budget.admission(
        Action("navigate", text="https://owned.example/read"), elements()
    ) == (0, None)
    rows = elements()
    rows[0].update(ax_role="textarea", focused=True)
    rows[1]["focused"] = False
    assert budget.admission(Action("key", chord="Enter"), rows) == (0, None)


def test_ambiguous_form_recipients_and_unknown_keyboard_focus_fail_closed():
    rows = elements()
    rows.append({**rows[1], "id": "b3", "form_scope": "form:0:1"})
    assert (
        exact_button_budgets(GOAL)[0].admission(Action("click", element_id="b1"), rows)[
            1
        ]
        == "exact_submission_recipient_ambiguous"
    )
    for row in rows:
        row["focused"] = False
    assert (
        exact_button_budgets(GOAL)[0].admission(Action("key", chord="Enter"), rows)[1]
        == "exact_submission_keyboard_recipient_unknown"
    )


def test_zero_authority_and_original_task_only_no_model_or_dom_authority():
    assert exact_button_budgets("Read the form and explain it.") == []
    budget = exact_button_budgets("Submit exactly 0 times.")[0]
    assert (
        budget.admission(Action("click", element_id="b1"), elements())[1]
        == "exact_submission_limit_reached"
    )
    with pytest.raises(ValueError):
        exact_button_budgets("Submit once; submit exactly 2 times.")


@pytest.mark.parametrize("opaque", [False, True])
def test_real_owned_native_submit_is_bounded_and_opaque_div_never_delivers(
    tmp_path, opaque
):
    from pathlib import Path
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from desktop_runtime.cu_ai_browser import BrowserMac
    from desktop_runtime.cu_browser.backend import BrowserBackend
    from desktop_runtime.cu_browser.chrome import CHROME_BIN, ChromeSession

    if not Path(CHROME_BIN).exists():
        pytest.skip("needs isolated installed Chrome")
    delivered = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            control = (
                b"<div tabindex=0 onclick='this.parentElement.requestSubmit()'>Create</div>"
                if opaque
                else b"<button>Save</button>"
            )
            self.wfile.write(
                b"<title>Owned form</title><form method=post><input name=name>"
                + control
                + b"</form>"
            )

        def do_POST(self):
            delivered.append(
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
            )
            self.do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    chrome = ChromeSession(tmp_path / "profile", headless=True)
    backend = None
    try:
        backend = BrowserBackend(
            chrome.start(), download_dir=tmp_path / "downloads", owned_context=True
        )
        url = f"http://127.0.0.1:{server.server_port}/form"
        backend.page.goto(url)
        mac = BrowserMac(backend)
        obs = mac.observe()
        submits = [e for e in obs.elements if e.get("submits_form") is True]
        if opaque:
            assert not submits and any(
                e.get("opaque_activation") is True for e in obs.elements
            )
        else:
            assert len(submits) == 1 and submits[0]["form_scope"]

        def repeat(payload):
            button = (
                next(
                    e
                    for e in payload["observation"].elements
                    if e.get("opaque_activation") is True
                )
                if opaque
                else next(
                    e
                    for e in payload["observation"].elements
                    if e.get("submits_form") is True
                )
            )
            return {"actions": [{"op": "click", "element_id": button["id"]}]}, {}

        result = run_loop(
            f"At {url} submit once.",
            repeat,
            mac,
            threading.Event(),
            verify=lambda *args: (False, "No record accepted"),
            caps=Caps(max_steps=5, max_seconds=30, explicit=True),
        )
        assert result.outcome != "done" and len(delivered) == (0 if opaque else 1)
    finally:
        try:
            if backend is not None:
                backend.close()
        finally:
            try:
                chrome.stop()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


def test_opaque_button_keyboard_activation_cannot_bypass_submit_limit():
    rows = elements()
    rows[1]["submits_form"] = False
    assert exact_button_budgets(GOAL)[0].admission(
        Action("key", chord="Spacebar"), rows
    )[1]
    rows[0].update(ax_role="textarea", focused=True)
    rows[1]["focused"] = False
    assert exact_button_budgets(GOAL)[0].admission(
        Action("key", chord="Ctrl+Enter"), rows
    )[1]


def test_time_cap_before_backend_delivery_restores_all_reserved_attempts(monkeypatch):
    from desktop_runtime import cu_ai_engine as engine

    saved, expired = [], [False]
    clock = engine.time.monotonic
    monkeypatch.setattr(
        engine.time, "monotonic", lambda: clock() + (120 if expired[0] else 0)
    )

    def save(row):
        saved.append(row)
        if row["pending_dispatch"]:
            expired[0] = True

    ck = type(
        "CK",
        (),
        {
            "load": lambda self: None,
            "clear": lambda self: None,
            "save": lambda self, row: save(row),
        },
    )()
    mac = FormMac()
    result = run(
        mac, goal="Click Save button exactly 1 times; submit once.", checkpoint=ck
    )
    assert result.outcome == "capped" and mac.attempts == 0
    assert saved[-1]["pending_dispatch"] is False
    assert [b["used"] for b in saved[-1]["exact_button_budgets"]] == [0, 0]


@pytest.mark.parametrize(
    "action",
    [
        Action("click", element_id="b0"),
        Action("type", element_id="b0", text="value"),
        Action("key", chord="Enter"),
        Action("key", chord="Space"),
        Action("key", chord="Tab"),
    ],
)
def test_observed_custom_handlers_cannot_bypass_quota_with_click_type_or_keys(action):
    rows = elements()
    rows[0].update(opaque_activation=True, focused=True)
    rows[1]["focused"] = False
    assert (
        exact_button_budgets(GOAL)[0].admission(action, rows)[1]
        == "exact_submission_opaque_activation_refused"
    )


def test_plain_native_field_selection_and_read_link_remain_available():
    budget = exact_button_budgets(GOAL)[0]
    rows = elements()
    assert budget.admission(Action("click", element_id="b0"), rows) == (0, None)
    assert budget.admission(Action("type", element_id="b0", text="value"), rows) == (
        0,
        None,
    )
    assert budget.admission(Action("click", element_id="b2"), rows) == (0, None)
