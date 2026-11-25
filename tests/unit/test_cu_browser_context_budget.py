"""Bounded source history must survive ordinary comparisons without claiming omissions."""

import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from coding_agents.cu_ai_loop import Action, Caps, build_user_text, verify_text
from coding_agents.cu_verification_context import MAX_CONTEXT_BYTES, compose
from desktop_runtime.cu_ai_browser import BrowserMac
from desktop_runtime.cu_ai_engine import Observation, run_loop
from desktop_runtime.cu_completion_evidence import validated_evidence


class Pages:
    def __init__(self):
        self.index, self.owned = 0, True
        self.url = "https://owned.example/constraints"
        self.text = "Constraint source: coast-71; region Northern; seats at least 4."

    def owned_tab_state(self):
        return (1, "a" * 32) if self.owned else None

    def observe(self):
        return SimpleNamespace(
            url=self.url,
            text=self.text,
            title="Owned source",
            elements=[],
            captcha=False,
            tabs=[{"i": 0, "title": "Owned source", "active": True}],
        )


def observed_sources():
    backend = Pages()
    mac = BrowserMac(backend)
    facts = ["coast-71"]
    mac.observe()
    for index in range(1, 8):
        backend.url = f"https://owned.example/source/{index}"
        fact = f"source-value-{index}"
        facts.append(fact)
        backend.text = f"Specification {index}: {fact}; price {11 + index}; region Northern; seats {index + 3}."
        mac.observe()
    return mac, backend, facts


def test_eight_small_sources_reach_verifier_including_early_constraints():
    mac, backend, facts = observed_sources()
    mac.did = ["redundant completed action " * 20] * 8
    mac.last_form_state = "redundant form state " * 30
    readback = mac.browser_readback()
    assert all(fact in readback for fact in facts)
    assert all(url in readback for url in mac.seen)
    assert readback.index(backend.url) < readback.index(
        "https://owned.example/constraints"
    )
    assert len(readback.encode()) <= 2500
    context = compose([], readback, "", "")
    assert all(fact in context for fact in facts)
    assert len(context.encode()) <= MAX_CONTEXT_BYTES


def test_twelve_short_sources_survive_navigation_without_model_memory():
    backend = Pages()
    mac = BrowserMac(backend)
    facts = []
    for index in range(12):
        backend.url = f"https://owned.example/specification/{index}"
        backend.text = f"Source {index}: specification-{index}-71; seats {index + 4}."
        facts.append(backend.text)
        mac.observe()
    readback = mac.browser_readback()
    context = compose([], readback, "", "")
    request = verify_text("Compare all twelve sources", "claim", "Chrome (browser)", "", [], context)
    assert all(fact in request for fact in facts)
    assert "older observed pages omitted" not in readback
    assert "historical" in readback and "not current state" in readback


def test_scroll_windows_remain_historical_source_evidence():
    backend = Pages()
    mac = BrowserMac(backend)
    first = backend.text
    mac.observe()
    backend.text = "Later viewport: second-source-value-83."
    mac.observe()
    readback = mac.browser_readback()
    assert first in readback and backend.text in readback
    assert "historical" in readback and "not current state" in readback


def test_long_unicode_viewport_keeps_both_ends_through_storage_and_verifier_budget():
    backend = Pages()
    backend.text = "Observed beginning: region Northern. " + "界" * 4000 + " Observed ending: seats 7."
    mac = BrowserMac(backend)
    mac.observe()
    readback = mac.browser_readback()
    request = verify_text("Read the source boundaries", "claim", "Chrome (browser)", "", [],
                          compose([], readback, "", ""))
    assert "Observed beginning: region Northern." in request
    assert "Observed ending: seats 7." in request
    assert "omitted content is not verified" in request
    assert len(mac.seen[backend.url].encode()) <= 2000
    assert len(readback.encode()) <= 2500


def test_extract_readback_survives_the_next_action_and_navigation():
    backend = Pages()
    mac = BrowserMac(backend)
    obs = mac.observe()
    queried_fact = "Observed matching passage: license-value-83."
    calls = []

    def extract(payload):
        calls.append(payload)
        return {"status": "ok", "data": queried_fact}

    backend.act = extract
    mac.act(Action(op="extract", text="license"), obs)
    mac.act(Action(op="wait", ms=1), obs)
    backend.url, backend.text = "https://owned.example/other", "Another page."
    mac.observe()
    readback = mac.browser_readback()
    context = compose([], readback, "", "")
    assert queried_fact in context
    assert "https://owned.example/constraints" in context
    assert calls[0] == {"action": "extract", "query": "license"}
    assert "historical" in readback and "not current state" in readback


def test_six_whole_extracts_share_budget_before_bulky_page_chrome():
    backend = Pages()
    mac = BrowserMac(backend)
    records = []
    for index in range(6):
        backend.url = f"https://owned.example/detail/{index}"
        backend.text = "Unrelated navigation and explanatory chrome. " * 60
        record = "Observed source passage. " * 14 + f"Exact terminal fact: value-{index}-83."
        records.append(record)
        backend.act = lambda payload, record=record: {"status": "ok", "data": record}
        mac.act(Action(op="extract", text="required fact"), mac.observe())
    readback = mac.browser_readback()
    request = verify_text("Compare six source facts", "claim", "Chrome (browser)", "", [],
                          compose([], readback, "", ""))
    assert all(record in request for record in records)
    assert len(readback.encode()) <= 4200
    assert "historical" in readback and "not current state" in readback


def test_overbudget_extracts_are_whole_or_explicitly_omitted():
    backend = Pages()
    mac = BrowserMac(backend)
    records = []
    for index in range(4):
        backend.url = f"https://owned.example/detail/{index}"
        backend.text = "Unrelated viewport."
        record = f"record-start-{index}: " + "界" * 340 + f" record-end-{index}"
        records.append(record)
        backend.act = lambda payload, record=record: {"status": "ok", "data": record}
        mac.act(Action(op="extract", text="required fact"), mac.observe())
    readback = mac.browser_readback()
    assert any(record in readback for record in records)
    assert any(record not in readback for record in records)
    for index, record in enumerate(records):
        assert record in readback or f"record-start-{index}" not in readback
    assert "omitted content is not verified" in readback
    assert len(readback.encode()) <= 4200


def test_actor_current_page_has_priority_and_keeps_available_early_sources():
    mac, backend, facts = observed_sources()
    obs = mac.observe()
    assert all(fact in obs.notes for fact in facts)
    assert obs.notes.index("PAGE TEXT:") < obs.notes.index("PRIOR PAGE READBACK")
    assert "historical" in obs.notes and "not current state" in obs.notes
    assert len(obs.notes.encode()) <= 3700
    rendered = build_user_text(
        "task",
        [],
        0,
        "",
        [],
        obs.frontmost,
        obs.window_title,
        obs.elements,
        notes=obs.notes,
    )
    assert all(fact in rendered for fact in facts)


def test_overcapacity_history_is_marked_and_never_claimed_complete():
    mac, backend, facts = observed_sources()
    for index in range(8, 24):
        backend.url, backend.text = f"https://owned.example/source/{index}", f"Source {index}."
        mac.observe()
    backend.url, backend.text = "https://owned.example/last", "Latest current fact."
    obs = mac.observe()
    readback = mac.browser_readback()
    assert facts[0] not in readback
    assert "omitted content is not verified" in readback
    assert "omitted content is not verified" in obs.notes
    assert "Latest current fact." in readback and len(readback.encode()) <= 4200
    mac.reset_completion_evidence()
    assert mac.browser_readback() == ""
    assert "omitted content is not verified" not in mac.observe().notes


def test_page_and_extract_history_are_byte_bounded_and_reset_together():
    backend = Pages()
    mac = BrowserMac(backend)
    backend.text = "界" * 4000
    mac.observe()
    backend.act = lambda payload: {"status": "ok", "data": "界" * 4000}
    mac.act(Action(op="extract", text="fact"), mac.last)
    assert len(mac.seen[backend.url].encode()) <= 2000
    assert len("\n".join(mac._reads[backend.url]).encode()) <= 2000
    assert "omitted content is not verified" in mac.browser_readback()
    mac.reset_completion_evidence()
    assert not mac._reads and not mac.seen


def test_byte_overcapacity_retains_current_source_and_marks_truncated_excerpt():
    mac, backend, _ = observed_sources()
    mac.seen = {
        f"https://owned.example/long/{i}": "value-" + str(i) + "界" * 300
        for i in range(8)
    }
    backend.url = list(mac.seen)[-1]
    backend.text = "Current source: live-83."
    obs = mac.observe()
    assert "live-83" in obs.notes and "omitted content is not verified" in obs.notes
    readback = mac.browser_readback()
    assert backend.url in readback and "live-83" in readback
    assert "omitted content is not verified" in readback
    # eight long pages is a multi-page task: its readback budget widens to 4200 bytes (cu_ai_browser.browser_readback)
    assert len(readback.encode()) <= 4200 and len(obs.notes.encode()) <= 3700


def test_lost_owned_page_custody_cannot_supply_historical_completion_facts():
    mac, backend, facts = observed_sources()
    backend.owned = False
    readback = mac.browser_readback()
    assert "custody" in readback.lower() and "unavailable" in readback.lower()
    assert not any(fact in readback for fact in facts)
    assert backend.index == 0  # Readback never creates a replacement page.


@pytest.mark.parametrize(
    "change", ["snapshot", "url", "document", "document_error", "tab"]
)
def test_live_owned_tab_does_not_authorize_cached_current_page_after_identity_change(
    change,
):
    mac, backend, facts = observed_sources()
    backend.snapshot_id = "observed-snapshot"
    backend.page = SimpleNamespace(url=backend.url)
    backend.completion_snapshot_current = lambda snapshot, url: (
        snapshot == backend.snapshot_id and url == backend.page.url
    )
    mac.observe()
    if change == "snapshot":
        backend.snapshot_id = "new-document-snapshot"
    elif change == "url":
        backend.page.url = "https://owned.example/redirected"
    elif change == "document":
        backend.completion_snapshot_current = lambda *args: False
    elif change == "document_error":

        def unavailable(*args):
            raise RuntimeError("private bridge body")

        backend.completion_snapshot_current = unavailable
    else:
        backend.owned_tab_state = lambda: (1, "b" * 32)
    readback = mac.browser_readback()
    assert not any(fact in readback for fact in facts)
    assert "historical content is not completion proof" in readback


def test_backend_current_completion_snapshot_checks_same_url_document_identity_read_only():
    from desktop_runtime.cu_browser.backend import BrowserBackend

    backend = object.__new__(BrowserBackend)
    backend.snapshot_id = "source-observed-snapshot"
    backend.page = SimpleNamespace(url="https://owned.example/current")
    backend.owned_tab_state = lambda: (1, "a" * 32)
    checks = []
    backend._assert_page_scope = lambda: checks.append("document-and-generation-read")
    assert (
        backend.completion_snapshot_current(
            "source-observed-snapshot", backend.page.url
        )
        is True
    )
    assert checks == ["document-and-generation-read"]

    def replacement_document():
        checks.append("replacement-document-read")
        backend.snapshot_id = None

    backend._assert_page_scope = replacement_document
    assert (
        backend.completion_snapshot_current(
            "source-observed-snapshot", backend.page.url
        )
        is False
    )
    assert checks[-1] == "replacement-document-read"


def test_real_owned_chrome_redirect_same_url_document_and_history_changes_require_readback(
    tmp_path,
):
    from desktop_runtime.cu_browser.backend import BrowserBackend
    from desktop_runtime.cu_browser.chrome import CHROME_BIN, ChromeSession

    if not Path(CHROME_BIN).exists():
        pytest.skip("needs installed Chrome for isolated owned source identity test")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = ("<html><title>Owned identity</title><body><p>owned-fact-71</p>"
                    + "<p>Ordinary filler</p>" * 400
                    + "<p>An uncommon article phrase source-license-83 ends this reference.</p></body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    chrome = ChromeSession(tmp_path / "profile", headless=True)
    backend = None
    try:
        backend = BrowserBackend(
            chrome.start(), download_dir=tmp_path / "downloads", owned_context=True
        )
        backend.page.goto(f"http://127.0.0.1:{server.server_port}/current")
        mac = BrowserMac(backend)
        first = mac.observe()
        assert "owned-fact-71" in mac.browser_readback()
        assert "source-license-83" not in first.notes
        mac.act(Action(op="extract", text="uncommon article"), first)
        backend.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        mac.observe()
        assert "source-license-83" in mac.browser_readback()
        for change in (
            lambda: backend.page.goto(
                f"http://127.0.0.1:{server.server_port}/redirected"
            ),
            lambda: backend.page.reload(),
            lambda: backend.page.evaluate("history.replaceState(null, '', '/changed')"),
        ):
            change()
            assert backend.owned_tab_state() is not None  # Existence alone still holds.
            assert "owned-fact-71" not in mac.browser_readback()
            mac.observe()
            assert "owned-fact-71" in mac.browser_readback()
            assert "source-license-83" in mac.browser_readback()
        backend.page.close()
        assert "owned-fact-71" not in mac.browser_readback()
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
                server_thread.join(timeout=2)


def test_verification_rebalances_whole_receipts_without_exceeding_global_bound():
    row = {
        "seq": 1,
        "page_scope": "a" * 32,
        "action": "type",
        "status": "ok",
        "element_index": 1,
        "before": {"field_type": "text", "checked": None},
        "after": {"field_type": "text", "checked": None},
        "action_input_matched": True,
    }
    response = {
        "action_seq": 1,
        "page_scope": "a" * 32,
        "method": "POST",
        "status": 202,
        "resource": "fetch",
    }
    envelope = {
        "v": 1,
        "source": "owned_browser_backend",
        "actions": [dict(row, seq=i + 1) for i in range(16)],
        "responses": [dict(response, action_seq=i + 1) for i in range(16)],
    }
    facts = "x" * 1900 + " independently-observed-early-source-97 " + "x" * 500
    context = compose(
        ["plan" * 1000], facts, "\n" + json.dumps(envelope), "note" * 1000
    )
    receipt = json.loads(context.splitlines()[1])
    assert validated_evidence(receipt) == receipt
    assert receipt["actions"][-1]["seq"] == receipt["responses"][-1]["action_seq"] == 16
    assert "independently-observed-early-source-97" in context
    assert "older whole browser receipts omitted" in context
    assert len(context.encode()) <= MAX_CONTEXT_BYTES
    request = verify_text(
        "goal" * 1000,
        "claim" * 1000,
        "Chrome (browser)",
        "current",
        [],
        context,
        "page" * 1000,
    )
    assert len(request.encode()) <= 10240


def test_varied_maximum_source_envelopes_never_overflow_global_context():
    rng = random.Random(71)
    for _ in range(64):
        rows = []
        for seq in range(1, 17):
            row = {"seq": seq, "page_scope": "a" * 32, "action": "type", "status": "ok"}
            if rng.choice([True, False]):
                row.update(
                    element_index=rng.randrange(1, 1_000_000_000),
                    action_input_matched=True,
                )
            if rng.choice([True, False]):
                row["before"] = {"field_type": "textarea", "checked": None}
            if rng.choice([True, False]):
                row["after"] = {"field_type": "text", "checked": False}
            rows.append(row)
        envelope = {
            "v": 1,
            "source": "owned_browser_backend",
            "actions": rows,
            "responses": [],
        }
        assert validated_evidence(envelope) == envelope
        context = compose(
            ["plan" * 1000], "界" * 2000, "\n" + json.dumps(envelope), "note" * 1000
        )
        receipt = json.loads(context.splitlines()[1])
        assert (
            validated_evidence(receipt) == receipt
            and receipt["actions"][-1]["seq"] == 16
        )
        assert len(context.encode()) <= MAX_CONTEXT_BYTES
        assert "file evidence truncated; omitted content is not verified" in context


@pytest.mark.parametrize(
    "frontmost,fact_offset,page_limit",
    [("Chrome (browser)", 1500, 3800), ("TextEdit", 1000, 2200)],
)
def test_terminal_answer_uses_rebalanced_sources_without_reopening_mutations(
    frontmost, fact_offset, page_limit
):
    class Counter:
        def __init__(self):
            self.inputs = 0

        def frontmost(self):
            return frontmost, 0

        def focused_role(self):
            return None

        def observe(self):
            obs = Observation(
                str(self.inputs).encode(),
                [{"id": "b0", "ax_role": "button", "label": "Advance"}],
                frontmost,
                "Owned counter",
                "",
                False,
                None,
            )
            obs.notes = "PAGE TEXT: Current count " + str(self.inputs)
            return obs

        def browser_readback(self):
            return (
                "x" * fact_offset
                + " Earlier authorized source fact: coast-71 "
                + "x" * 600
            )

        def act(self, action, obs):
            self.inputs += 1
            return "click ok (0.01s)"

    mac, calls, claims = Counter(), [], []

    def actor(payload):
        calls.append(payload)
        if mac.inputs < 2:
            return {"actions": [{"op": "click", "element_id": "b0"}]}, {}
        assert "TERMINAL_ANSWER_ONLY" in payload["hint"]
        assert "coast-71" in payload["observation"].notes
        assert len(payload["observation"].notes.encode()) <= page_limit
        assert "Current count 2" in payload["observation"].notes
        return {"actions": [{"op": "done", "summary": "Count 2, source coast-71."}]}, {}

    def verify(claim, obs, context):
        claims.append(claim)
        assert "coast-71" in context
        return True, "current source evidence"

    result = run_loop(
        "Press the 'Advance' button exactly 2 times. Give count and source fact.",
        actor,
        mac,
        threading.Event(),
        caps=Caps(max_steps=8, max_seconds=60, explicit=True),
        verify=verify,
    )
    assert result.outcome == "done" and result.summary == "Count 2, source coast-71.", (
        result.reason,
        result.log,
    )
    assert mac.inputs == 2 and len(calls) == 3 and len(claims) == 2
