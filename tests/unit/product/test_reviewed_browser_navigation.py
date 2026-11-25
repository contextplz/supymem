"""Exact reviewed-URL offers and ordinary Core effect protocol, no providers."""

from __future__ import annotations

import copy
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from coding_agents.jev_decision import Candidate
from coding_agents.owned_browser import OwnedBrowser, strip_check_marker
from desktop_runtime.device_gateway import UpstreamRejected, WorkOrderGateway
from desktop_runtime.device_worker import DeviceWorker


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            body = '<h1>Source page</h1><a href="/next">An unrelated label</a><button>Other</button>'
            if path == "/duplicate":
                body += '<a href="/next">Second exact URL</a>'
            if path == "/form":
                body = '<h1>Source page</h1><form><a href="/next">Go</a></form>'
            if path == "/next":
                body = "<h1>Final source page</h1>"
            if path == "/popup":
                body = '<h1>Source page</h1><a href="/next" target="_blank">Popup</a>'
            if path == "/marked":
                body = '<h1>Source page</h1><a href="/next?supymem_check=1">Marked</a>'
            if path == "/marked-encoding":
                body = '<h1>Source page</h1><a href="/next?q=a%20b&supymem_check=1#review">Marked</a>'
            if path == "/marked-duplicate":
                body = '<h1>Source page</h1><a href="/next">One</a><a href="/next?supymem_check=1">Two</a>'
            raw = body.encode()
            self.send_response(404 if path == "/error" else 200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def browser(site, tmp_path):
    pytest.importorskip("playwright.sync_api")
    value = OwnedBrowser(
        order_id="dwo_reviewed",
        goal="Read the reviewed URLs",
        origins=[site],
        authorize=lambda *_: True,
        admit_effect=lambda *_: True,
        profile_dir=tmp_path / "profile",
        allow_loopback_for_tests=True,
    )
    yield value
    value.close()


def test_actual_unique_exact_anchor_offer_without_label_matching(browser, site):
    browser.start(site + "/")
    observation = browser.observe()
    offer = browser.reviewed_navigation_offer(observation, site + "/next")
    assert offer is not None
    assert len(offer["control_snapshots"]) == len(observation.controls)
    selected = next(x for x in offer["control_snapshots"] if x["id"] == offer["target"])
    assert (
        selected["tag"] == "A"
        and selected["form"] is False
        and selected["href"] == site + "/next"
    )
    assert offer["current_load"]["qualified"] is True
    assert offer["current_load"]["observation_digest"] == observation.digest()
    assert "label" not in selected
    assert (
        browser.reviewed_navigation_offer(observation, site + "/next?different=1")
        is None
    )
    assert (
        browser.reviewed_navigation_offer(observation, "https://foreign.example/next")
        is None
    )


def test_reserved_marker_preserves_other_raw_url_bytes():
    original = 'https://example.test/a?q=a%20b&empty=&x=%2f&supymem_check=1&x=last#raw%20hash'
    expected = 'https://example.test/a?q=a%20b&empty=&x=%2f&x=last#raw%20hash'
    assert strip_check_marker(original) == expected
    assert DeviceWorker.strip_check_marker(original) == expected
    assert strip_check_marker(expected) == expected
    assert strip_check_marker('https://example.test/?supymem_check=1#') == 'https://example.test/#'
    assert strip_check_marker('https://example.test/?supymem_check_extra=1') == 'https://example.test/?supymem_check_extra=1'
    assert strip_check_marker('https://example.test/?%73upymem_check=1&x=%20') == 'https://example.test/?x=%20'
    marked = DeviceWorker.with_check_marker(expected)
    assert marked == expected.partition('#')[0] + '&supymem_check=1#raw%20hash'
    assert strip_check_marker(marked) == expected
    assert DeviceWorker.with_check_marker(original) == marked
    assert strip_check_marker(DeviceWorker.with_check_marker('https://example.test/?q=a+b')) == 'https://example.test/?q=a+b'
    assert strip_check_marker(DeviceWorker.with_check_marker('https://example.test/?%73upymem_check=0&x=%20#z')) == 'https://example.test/?x=%20#z'


def test_actual_marker_offer_retains_raw_href_for_input(browser, site):
    browser.start(site + '/marked')
    observation = browser.observe()
    offer = browser.reviewed_navigation_offer(observation, site + '/next')
    assert offer is not None
    selected = next(item for item in offer['control_snapshots'] if item['id'] == offer['target'])
    assert selected['href'] == site + '/next?supymem_check=1'
    inputs = []
    browser.admit_effect = lambda effect, _digest: (inputs.append(effect) or True)
    browser.click(Candidate('click', offer['target'], observation.digest()), 'din_marker')
    current = browser.observe()
    assert current.location == selected['href'] and browser.verified_load(current)
    assert inputs[0]['control']['href'] == selected['href']


def test_actual_marker_does_not_waive_encoding_query_or_fragment(browser, site):
    browser.start(site + '/marked-encoding')
    observation = browser.observe()
    assert browser.reviewed_navigation_offer(observation, site + '/next?q=a%20b#review') is not None
    for target in ('/next?q=a+b#review', '/next?q=a%20b', '/next?q=a%20b&unknown=1#review'):
        assert browser.reviewed_navigation_offer(observation, site + target) is None


def test_actual_marker_duplicate_canonical_controls_remain_ambiguous(browser, site):
    browser.start(site + '/marked-duplicate')
    observation = browser.observe()
    assert browser.reviewed_navigation_offer(observation, site + '/next') is None


@pytest.mark.parametrize("path", ["/duplicate", "/form", "/error"])
def test_actual_ambiguous_form_or_unqualified_load_has_no_offer(browser, site, path):
    browser.start(site + path)
    observation = browser.observe()
    assert browser.reviewed_navigation_offer(observation, site + "/next") is None


def test_offer_cannot_reuse_old_document_or_general_observation(browser, site):
    browser.start(site + "/")
    observation = browser.observe()
    assert browser.reviewed_navigation_offer(observation, site + "/next") is not None
    browser.general_mode = True
    assert browser.reviewed_navigation_offer(observation, site + "/next") is None
    browser.general_mode = False
    browser._page.reload(wait_until="load")
    assert browser.reviewed_navigation_offer(observation, site + "/next") is None


def test_page_identity_is_stable_on_reload_and_rejects_mismatched_receipt(
    browser, site
):
    browser.start(site + "/")
    prior = browser.observe()
    before = copy.deepcopy(browser.last_load_evidence)
    browser._page.reload(wait_until="load")
    current = browser.observe()
    after = copy.deepcopy(browser.last_load_evidence)
    assert before["page_id"] == after["page_id"]
    assert before["document_id"] != after["document_id"]
    assert before["sequence"] < after["sequence"]
    assert browser.verified_load(prior) is False
    browser.last_load_evidence.pop("page_id")
    assert browser.verified_load(current) is False
    browser.last_load_evidence["page_id"] = "0" * 32
    assert browser.verified_load(current) is False
    assert browser.reviewed_navigation_offer(current, site + "/next") is None


def test_actual_popup_has_new_page_identity_even_when_generation_resets(browser, site):
    browser.start(site + "/popup")
    prior = browser.observe()
    before = copy.deepcopy(browser.last_load_evidence)
    target = next(control for control in prior.controls if control.label == "Popup")
    browser.click(Candidate("click", target.id, prior.digest()), "din_popup")
    current = browser.observe()
    after = browser.last_load_evidence
    assert browser.verified_load(current) is True
    assert before["session_id"] == after["session_id"]
    assert before["page_id"] != after["page_id"]
    assert before["sequence"] == after["sequence"] == 1
    assert browser.verified_load(prior) is False


def test_existing_click_refuses_changed_href_before_effect_admission(browser, site):
    admitted = []
    browser.admit_effect = lambda *args: admitted.append(args) or True
    browser.start(site + "/")
    observation = browser.observe()
    offer = browser.reviewed_navigation_offer(observation, site + "/next")
    browser._page.locator("a").evaluate("el => el.href='/other'")
    with pytest.raises(PermissionError, match="browser_observation_stale"):
        browser.click(
            Candidate("click", offer["target"], observation.digest()), "din_changed"
        )
    assert admitted == []


def test_next_reviewed_url_requires_ordered_qualified_prefix_and_typed_descriptor():
    manifest = {
        "browser_verification": {
            "expected_locations": ["https://example.test/", "https://example.test/next"]
        }
    }
    assert DeviceWorker.next_reviewed_location(manifest, []) is None
    assert (
        DeviceWorker.next_reviewed_location(manifest, ["https://example.test/next"])
        is None
    )
    assert (
        DeviceWorker.next_reviewed_location(
            manifest, ["https://example.test/?supymem_check=1"]
        )
        == "https://example.test/next"
    )
    assert (
        DeviceWorker.next_reviewed_location(
            manifest, ["https://example.test/", "https://example.test/next"]
        )
        is None
    )
    assert (
        DeviceWorker.next_reviewed_location(
            {**manifest, "general_browser_verification": {}}, ["https://example.test/"]
        )
        is None
    )


@pytest.mark.parametrize("origin,goal", [
    ("https://supply.example.test", "Read the published inventory integration guide"),
    ("https://recipes.example.test", "Read the published dinner recipe"),
])
def test_reviewed_progress_refuses_detours_and_changed_targets(origin, goal):
    expected = [origin + "/", origin + "/next?q=a%20b#review"]
    manifest = {"instruction": goal, "browser_verification": {"expected_locations": expected}}
    assert DeviceWorker.next_reviewed_location(manifest, [expected[0], expected[0]]) == expected[1]
    assert DeviceWorker.verification_satisfied(manifest, [expected[0], expected[0], expected[1]])
    for history in (
        [origin + "/other", expected[0]],
        [expected[0], origin + "/other"],
        [expected[0], origin + "/other", expected[1]],
        [expected[1], expected[0], expected[1]],
        [expected[0], expected[1].replace("%20", "+")],
    ):
        assert DeviceWorker.next_reviewed_location(manifest, history) is None
        assert not DeviceWorker.verification_satisfied(manifest, history)


@pytest.mark.parametrize("origin,goal", [
    ("https://supply.example.test", "Read the published inventory integration guide"),
    ("https://recipes.example.test", "Read the published dinner recipe"),
])
def test_exact_reviewed_navigation_rejects_changed_targets(origin, goal):
    from tests.company_os_harness import load_company_os_app
    from tests.unit.product.test_device_browser_navigation import request_value, saved
    from coding_agents.jev_decision import Observation

    load_company_os_app()
    from app.build import device_browser_navigation as navigation

    order, value = saved(), request_value()
    order["manifest"].update(origins=[origin], instruction=goal)
    order["manifest"]["browser_verification"]["expected_locations"] = [origin + "/", origin + "/next"]
    value["observation"].update(location=origin + "/", goal=goal)
    value["control_snapshots"][0]["href"] = origin + "/next"
    observation = Observation.model_validate(value["observation"])
    value["current_load"].update(location=observation.location, observation_digest=observation.digest())
    value["current_load"]["response"]["url"] = observation.location
    value["qualified_loads"] = [copy.deepcopy(value["current_load"])]
    assert navigation.select(order, navigation.NavigationBody.model_validate(value))[0].target == "c1"
    for target in (origin + "/other", origin + "/next?changed=1", origin + "/next#changed", "https://foreign.example.test/next"):
        value["control_snapshots"][0]["href"] = target
        with pytest.raises(ValueError, match=navigation.UNAVAILABLE):
            navigation.select(order, navigation.NavigationBody.model_validate(value))


@pytest.mark.parametrize("origin", ["https://supply.example.test", "https://recipes.example.test"])
@pytest.mark.parametrize("retired", ["cancelled", "closed", "expired"])
def test_retired_browser_pause_cannot_continue_or_reveal(origin, retired):
    from types import SimpleNamespace

    worker = DeviceWorker(SimpleNamespace(order_id="dwo_pause"), lambda _receipt: None)
    worker.deadline = time.monotonic() + 60
    worker.last.update(state="needs_you", location=origin + "/sign-in")
    revealed = []
    worker._reveal = lambda: revealed.append(True) or True
    assert worker.continue_founder_handoff()
    worker.continue_event.clear()
    if retired == "cancelled":
        worker.cancel()
        worker.continue_event.clear()
    elif retired == "closed":
        worker.closed = True
    else:
        worker.server_deadline = time.time() - 1
    assert worker.continue_founder_handoff() is False
    assert worker.request_founder_handoff() is False
    assert not worker.continue_event.is_set()
    assert revealed == []


@pytest.mark.parametrize("origin,goal", [
    ("https://supply.example.test", "Read the published inventory integration guide"),
    ("https://recipes.example.test", "Read the published dinner recipe"),
])
def test_private_profile_survives_tasks_without_copying_existing_tabs(tmp_path, monkeypatch, origin, goal):
    import stat
    from types import SimpleNamespace
    from desktop_runtime import cu_browser
    from desktop_runtime.cu_browser import chrome

    executable = tmp_path / "unused-chrome"
    executable.touch()
    monkeypatch.setattr(chrome, "CHROME_BIN", str(executable))
    monkeypatch.setenv("SUPYMEM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SUPYMEM_CU_CHROME_PROFILE_EMAIL", "must-not-copy@example.test")
    monkeypatch.delenv("SUPYMEM_CU_BROWSER_MODE", raising=False)
    profiles = []

    def session(profile, *, headless):
        assert headless is False
        profiles.append(profile)
        return SimpleNamespace(start=lambda: "unused-endpoint", stop=lambda: None)

    monkeypatch.setattr(cu_browser, "ChromeSession", session)
    monkeypatch.setattr(cu_browser, "prepare_profile_copy", lambda *_args: pytest.fail("Existing tabs must not be copied"))
    for order_id in ("dwo_first", "dwo_second"):
        worker = DeviceWorker(SimpleNamespace(order_id=order_id, account_id="usr_profile"), lambda _receipt: None)
        worker.last.update(location=origin, instruction=goal)
        factory = worker.chrome_factory()
        assert factory is not None and factory.profile_email == ""
        assert factory.start_endpoint() == "unused-endpoint"
        sentinel = profiles[-1] / "saved-session"
        if order_id == "dwo_first":
            sentinel.write_text("private session")
        factory.stop()
        assert sentinel.read_text() == "private session"
        assert stat.S_IMODE(profiles[-1].stat().st_mode) == 0o700
    assert profiles[0] == profiles[1]
    other = cu_browser.persistent_profile_dir(tmp_path / "browser", "usr_other")
    assert other != profiles[0] and not (other / "saved-session").exists()
    assert DeviceWorker(SimpleNamespace(order_id="dwo_legacy"), lambda _receipt: None).chrome_factory() is None


class Server:
    """Explicit protocol fake; real Core controller/admission has separate PG tests."""

    def __init__(self, manifest, *, capability=True, refusal=False, wrong_target=False):
        self.manifest, self.capability, self.refusal, self.wrong_target = (
            manifest,
            capability,
            refusal,
            wrong_target,
        )
        self.state = "approved"
        self.proposals, self.effects, self.receipts = {}, {}, {}
        self.model_calls = 0
        self.proposal_calls = []
        self.general_marker = None

    def saved(self):
        return {
            "id": "dwo_reviewed",
            "venture_id": "ven_reviewed",
            "device_id": "dev_reviewed",
            "review_digest": "a" * 64,
            "state": self.state,
            "execution_id": "dex_reviewed",
            "manifest": self.manifest,
        }

    def request(self, method, path, body):
        if method == "GET" and path.endswith("/capabilities"):
            return {
                "available": True,
                **({"typed_navigation_proposal_v1": True} if self.capability else {}),
                **(
                    {"general_browser_execution_v1": self.general_marker}
                    if self.general_marker is not None
                    else {}
                ),
            }
        if method == "GET" and path.endswith("/authority"):
            return {"current": self.state == "claimed"}
        if method == "GET":
            return self.saved()
        if path.endswith("/claim"):
            self.state = "claimed"
            return self.saved()
        if path.endswith("/browser-navigation-proposal"):
            self.proposal_calls.append(copy.deepcopy(body))
            if self.refusal:
                raise UpstreamRejected(409, "device_typed_navigation_unavailable")
            expected = self.manifest["browser_verification"]["expected_locations"][1]
            target = next(
                x["id"] for x in body["control_snapshots"] if x["href"] == expected
            )
            if self.wrong_target:
                target = "c_wrong"
            candidate = Candidate(
                "click", target, body["current_load"]["observation_digest"]
            )
            self.proposals[body["invocation_id"]] = candidate
            return {
                "authority": "proposal_only",
                "source_kind": "typed_reviewed_url_v1",
                "invocation_id": body["invocation_id"],
                "expected_location": expected,
                "candidate": vars(candidate),
            }
        if path.endswith("/browser-decision"):
            self.model_calls += 1
            from coding_agents.jev_decision import Observation

            observation = Observation.model_validate(body["observation"])
            return {
                "authority": "proposal_only",
                "candidate": vars(Candidate("blocked", None, observation.digest())),
            }
        if path.endswith("/effects/admit"):
            proposal = self.proposals.get(body["invocation_id"])
            assert (
                proposal is not None
                and proposal.observation_digest == body["observation_digest"]
            )
            assert body["invocation_id"] not in self.effects
            self.effects[body["invocation_id"]] = body["effect_digest"]
            return {"admitted_now": True}
        if path.endswith("/effects/receipt"):
            assert self.effects[body["invocation_id"]] == body["effect_digest"]
            self.receipts[body["invocation_id"]] = body["outcome"]
            return {"recorded": True}
        if path.endswith("/stop-receipt"):
            self.state = "stopped"
            return {"state": "stopped"}
        raise AssertionError("Unsupported QA protocol route")


@pytest.mark.parametrize("marker", [None, False, "true", True])
def test_general_claim_requires_explicit_current_core_marker(marker):
    server = Server({"general_browser_verification": {"operation": "general_goal_v1"}})
    server.general_marker = marker
    gateway = WorkOrderGateway(
        venture_id="ven_reviewed",
        order_id="dwo_reviewed",
        device_id="dev_reviewed",
        request=server.request,
    )
    if marker is True:
        gateway.claim()
        assert gateway.claimed is True and server.state == "claimed"
    else:
        with pytest.raises(PermissionError, match="native_execution_unavailable"):
            gateway.claim()
        assert gateway.claimed is False and gateway.claim_attempted is False
        assert server.state == "approved" and server.model_calls == 0
        assert server.effects == server.proposals == {}


@pytest.mark.parametrize(
    "case,capability,refusal,wrong_target,verified,model_calls,proposal_calls",
    [
        ("/", True, False, False, True, 0, 1),
        ("/duplicate", True, False, False, False, 1, 0),
        ("/", False, False, False, False, 1, 0),
        ("/", True, True, False, False, 0, 1),
        ("/", True, False, True, False, 0, 1),
    ],
)
def test_actual_worker_requires_server_proposal_and_ordinary_effect_protocol(
    site,
    monkeypatch,
    case,
    capability,
    refusal,
    wrong_target,
    verified,
    model_calls,
    proposal_calls,
):
    pytest.importorskip("playwright.sync_api")
    from desktop_runtime import device_worker as worker_module

    manifest = {
        "applications": ["com.google.Chrome"],
        "origins": [site],
        "actions": ["browser.read", "browser.interact"],
        "duration_seconds": 30,
        "instruction": "Read these exact reviewed URLs in order",
        "browser_verification": {"expected_locations": [site + case, site + "/next"]},
    }
    # HTTP start is the explicit existing localhost-only test capability.
    monkeypatch.setattr(
        DeviceWorker, "browser_start_url", staticmethod(lambda _m: site + case)
    )
    actual = worker_module.OwnedBrowser
    monkeypatch.setattr(
        worker_module,
        "OwnedBrowser",
        lambda **kwargs: actual(**kwargs, allow_loopback_for_tests=True),
    )
    monkeypatch.setattr(worker_module, "report_error", lambda *_args, **_kwargs: None)
    server = Server(
        manifest, capability=capability, refusal=refusal, wrong_target=wrong_target
    )
    gateway = WorkOrderGateway(
        venture_id="ven_reviewed",
        order_id="dwo_reviewed",
        device_id="dev_reviewed",
        request=server.request,
    )
    worker = DeviceWorker(gateway, lambda _value: None)
    worker._notify_terminal = lambda *_args, **_kwargs: None
    worker.run()
    assert worker.last.get("task_verified") is verified
    assert (
        server.model_calls == model_calls
        and len(server.proposal_calls) == proposal_calls
    )
    assert len(server.effects) == int(verified)
    assert all(value == "returned" for value in server.receipts.values())
    assert worker.last["evidence"]["processes_stopped"] is True
    if verified:
        assert worker.last["outcome"] == "verified"
        body = server.proposal_calls[0]
        assert body["qualified_loads"][-1] == body["current_load"]
        assert len(body["control_snapshots"]) == len(body["observation"]["controls"])
    else:
        assert worker.last["outcome"] != "verified"
