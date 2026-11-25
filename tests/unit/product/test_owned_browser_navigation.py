from coding_agents.jev_decision import Candidate, Control, Observation
from coding_agents.owned_browser import OwnedBrowser
import pytest
from types import SimpleNamespace


class Route:
    def __init__(self, url, method="GET", resource_type="script"):
        self.request = type("Request", (), {"url": url, "method": method,
                                             "resource_type": resource_type})()
        self.result = None
        self.fetched = self.disposed = False

    def continue_(self):
        pytest.fail("Ordinary requests may not bypass redirect containment")

    def fetch(self, **kwargs):
        assert kwargs == {"max_redirects": 0, "max_retries": 0, "timeout": 10000}
        self.fetched = True
        def dispose():
            self.disposed = True
        return SimpleNamespace(status=200, headers={"content-type": "text/plain"}, body=lambda: b"Scoped source", dispose=dispose)

    def fulfill(self, **kwargs):
        assert kwargs["status"] == 200 and kwargs["body"] in {b"", b"Scoped source"}
        self.result = "fulfill"

    def abort(self, _reason):
        self.result = "abort"


@pytest.mark.parametrize("operation", ["click", "type", "fill"])
def test_read_only_input_helpers_refuse_before_admission(operation):
    browser = object.__new__(OwnedBrowser)
    browser.read_only = True
    browser._check = lambda: pytest.fail("Read-only input entered effect checks")
    browser.admit_effect = lambda *_: pytest.fail("Read-only input admitted an effect")
    candidate = Candidate("click" if operation == "click" else "type", "c0", "a" * 64)
    with pytest.raises(PermissionError, match="effect_requires_review"):
        if operation == "click":
            browser.click(candidate, "din_read")
        elif operation == "type":
            browser.type_text(candidate, "din_read", "Ada")
        else:
            browser.fill_reviewed_fields()


def test_read_only_route_allows_only_original_page_passive_requests():
    browser = object.__new__(OwnedBrowser)
    browser.read_only = True
    browser.origins = frozenset({"https://owned.example"})
    browser._allow_loopback = False
    browser._uncertain = False
    browser._page = SimpleNamespace(url="https://owned.example/")
    browser._route_authority_until = 0
    browser._check = lambda: None
    for method, page, expected in [("GET", browser._page, "fulfill"), ("POST", browser._page, "abort"),
                                   ("GET", object(), "abort"), ("HEAD", browser._page, "fulfill")]:
        route = Route("https://owned.example/page", method=method, resource_type="document")
        route.request.frame = SimpleNamespace(page=page)
        browser._route(route)
        assert route.result == expected
        assert route.fetched is (expected == "fulfill") and route.disposed is (expected == "fulfill")


def test_read_only_popup_refusal_keeps_original_binding_and_cleanup_uncertainty():
    browser = object.__new__(OwnedBrowser)
    browser.read_only = True
    original = object()
    browser._page = original
    browser._uncertain = False
    def fail_close(): raise RuntimeError("synthetic popup close failure")
    popup = SimpleNamespace(close=fail_close)
    with pytest.raises(RuntimeError):
        browser._new_page(popup)
    assert browser._page is original and browser._uncertain is True


def test_route_rejects_out_of_scope_resources_before_remote_authority_check(monkeypatch):
    monkeypatch.setattr("coding_agents.owned_browser.time.monotonic", lambda: 100.0)
    browser = object.__new__(OwnedBrowser)
    browser.origins = frozenset({"https://venture.example"})
    browser._allow_loopback = False
    browser._page = None
    browser._uncertain = False
    browser._route_authority_until = 0.0
    checks = []
    browser._check = lambda: checks.append("current")
    blocked = Route("https://unrelated.example/tracker.js")
    browser._route(blocked)
    assert blocked.result == "abort"
    assert checks == []

    allowed = Route("https://venture.example/app.js")
    browser._route(allowed)
    assert allowed.result == "fulfill"
    assert checks == ["current", "current"]  # Admission and fresh authority after I/O.

    second = Route("https://venture.example/style.css", resource_type="stylesheet")
    browser._route(second)
    assert second.result == "fulfill"
    assert checks == ["current", "current", "current"]

    browser._route_authority_until = 0.0
    after_lease = Route("https://venture.example/logo.png", resource_type="image")
    browser._route(after_lease)
    assert after_lease.result == "fulfill"
    assert checks == ["current"] * 5


class Handle:
    def __init__(self, info):
        self.info, self.click_kwargs = info, None

    def evaluate(self, script):
        return self.info

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def click(self, **kwargs):
        self.click_kwargs = kwargs


def test_anchor_click_receipts_input_before_exact_navigation_observation():
    location = "https://venture.example/"
    destination = "https://gateway.example/waitlist"
    observation = Observation(revision="one", application="com.google.Chrome", location=location,
        goal="Verify waitlist", context="Visible",
        controls=[Control(id="c1", label="Join waitlist", operations=["click"], visible=True, enabled=True)],
        permitted=True,
        confirmation_pending=False, prior_effect_uncertain=False)
    info = {"label": "Join waitlist", "href": destination, "form": False, "tag": "A"}
    handle = Handle(info)
    browser = object.__new__(OwnedBrowser)
    browser.order_id, browser.session_id = "dwo_one", "session_one"
    browser._observation = observation
    browser._handles = {"c1": (handle, info)}
    browser._page = type("Page", (), {"url": location})()
    browser._expected_location = None
    browser._check = lambda: None
    browser.admit_effect = lambda effect, digest: True

    browser.click(Candidate("click", "c1", observation.digest(), False), "din_one")

    assert handle.click_kwargs == {"timeout": 5000, "no_wait_after": True}
    assert browser._expected_location == destination


def test_expected_navigation_accepts_exact_page_across_owned_popup_context():
    destination = "https://gateway.example/waitlist"
    expected_page = type("Page", (), {"url": destination})()
    browser = object.__new__(OwnedBrowser)
    browser._expected_location = destination
    browser._context = type("Context", (), {"pages": [type("Page", (), {"url": "about:blank"})(), expected_page]})()
    browser._page = None
    browser._uncertain = False
    browser.origins = frozenset({"https://gateway.example"})

    browser._finish_expected_navigation()

    assert browser._page is expected_page
    assert browser._expected_location is None
    assert browser._uncertain is False


def test_resolve_fill_never_uses_value_for_a_different_field_kind():
    browser = object.__new__(OwnedBrowser)
    browser.general_mode = True
    browser.fill_values = {"email": "founder@example.com"}
    browser._handles = {"c1": (object(), {}), "c2": (object(), {})}
    browser._control_meta = {"c1": {"field_kind": "email"}, "c2": {"field_kind": "name"}}
    assert browser.resolve_fill("c1") == "founder@example.com"
    assert browser.resolve_fill("c2") is None
    assert browser.resolve_fill("c3") is None
