"""Contract-specific negotiation and report validation without browser execution."""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import create_model

from core.artifact_snapshot import canonical
from core.static_qa_contracts import Contract, Input, Strict
from tests.integration.test_postgres_auth_security import service_modules
from tests.qa_storage_fixtures import POLICY, report_fixture

requires_qa_image = pytest.mark.skipif(
    not (Path(__file__).resolve().parents[3] / "services/qa_runner/app").is_dir(),
    reason="Hosted QA runner is not in the public local release",
)


@pytest.mark.parametrize("venture_id", ["equipment", "pottery"])
def test_local_qa_external_links_allow_only_loopback_http(monkeypatch, venture_id):
    from core.static_qa_contracts import Action

    with service_modules("company_os"):
        monkeypatch.setenv("SUPYMEM_MODE", "local")
        from app.core import local_mode

        assert local_mode.is_local()
        values = {"id": "waitlist_cta", "kind": "external_link", "role": "link", "name": "Join the waitlist"}
        route = "/public/waitlists/" + venture_id
        for base in ("http://127.0.0.1:3300", "http://localhost:3300/api", "http://[::1]:3300"):
            assert Action(**values, target=base + route)
        for target in ("http://your-site.test" + route, "http://127.0.0.1.evil.test" + route,
                       "http://user:password@localhost:3300" + route, "https://user:password@your-site.test" + route,
                       "http://localhost:3300/local/settings", "http://localhost:3300" + route + "#other",
                       "http://localhost:0" + route, "http://localhost:65536" + route):
            with pytest.raises(ValueError, match="external_target_invalid"):
                Action(**values, target=target)
        monkeypatch.setenv("SUPYMEM_MODE", "hosted")
        for base in ("http://127.0.0.1:3300", "http://localhost:3300", "http://[::1]:3300"):
            with pytest.raises(ValueError, match="external_target_invalid"):
                Action(**values, target=base + route)
        assert Action(**values, target="https://your-site.test" + route)


@pytest.mark.parametrize("product,offer", [("Dispatch Board", "Assign delivery routes"), ("Garden Journal", "Track home seedlings")])
@pytest.mark.parametrize("managed,versions,accepted", [
    (False, [3], True), (False, [3, 4], True), (True, [3], False),
    (True, [4], True), (True, [3, 4], True), (True, [True, 4], False),
])
@requires_qa_image
def test_contract_specific_versions_preserve_v3_wire_and_admission(monkeypatch, product, offer, managed, versions, accepted):
    from services.qa_runner.app.static_qa_api import Config, create_app
    health_route = next(route for route in create_app(Config(str(uuid4()), str(uuid4()), 1, "x" * 40), None).routes if route.path == "/health")
    advertised = health_route.endpoint()
    assert advertised["case_contract_version"] == 3
    assert advertised["case_contract_versions"] == [3, 4]
    with service_modules("company_os"):
        from app.builder import qa_transport
        from app.builder.model_proxy import BuildModelError
        from app.experiments.wizard_tests import RequestForm, form_html

        legacy = Contract(profile="static_landing_v1", product_name=product, required_text=[offer])
        legacy_wire = {"profile": "static_landing_v1", "product_name": product, "required_text": [offer], "actions": []}
        assert legacy.model_dump(mode="json") == legacy_wire
        v3_contract = create_model("V3Contract", __base__=Strict, **{
            name: (Contract.model_fields[name].annotation, Contract.model_fields[name])
            for name in ("profile", "product_name", "required_text", "actions", "visuals")})
        assert v3_contract.model_validate(legacy_wire).product_name == product
        assert canonical(legacy.model_dump(mode="json")) == canonical(legacy_wire)
        contract = legacy
        if managed:
            url = "https://forms.example.test/public/waitlists/venture"
            form = RequestForm(fields=[{"key": "task", "label": offer, "kind": "short_text"},
                {"key": "address", "label": "Email address", "kind": "email"}])
            contract = Contract.model_validate({**legacy_wire, "managed_request_form": {
                "target": url, "html": form_html(form, url, "exp_" + "a" * 24, "Contact me about my request.", "v1")}})
            with pytest.raises(ValueError):
                v3_contract.model_validate(contract.model_dump(mode="json"))
        assert contract.case_contract_version == (4 if managed else 3)
        request = Input(command_id=str(uuid4()), source_artifact_id="synthetic", source_sha256="a" * 64,
            size_bytes=100, manifest=[{"path": "index.html", "size_bytes": 1, "sha256": "b" * 64}], contract=contract)
        if not managed:
            v3_input = create_model("V3Input", __base__=Strict, **{
                name: (v3_contract if name == "contract" else field.annotation, field)
                for name, field in Input.model_fields.items()})
            assert v3_input.model_validate(request.model_dump(mode="json")).contract.product_name == product
        calls = []

        def health(_binding, method, path, **_kwargs):
            calls.append((method, path))
            assert (method, path) == ("GET", "/health")
            return json.dumps({"status": "ok", "profiles": ["static_landing_v1"],
                "case_contract_version": max(versions), "case_contract_versions": versions}).encode()

        monkeypatch.setattr(qa_transport, "request", health)
        monkeypatch.setattr(qa_transport, "state", lambda _binding: {"phase": "running",
            "command_id": request.command_id, "input_digest": request.digest})
        if accepted:
            assert qa_transport.progress({}, request.model_dump(mode="json"), lambda: pytest.fail("source upload"),
                permit_execution=lambda: pytest.fail("execution"))["phase"] == "running"
        else:
            with pytest.raises(BuildModelError, match="qa_harness_contract_mismatch"):
                qa_transport.progress({}, request.model_dump(mode="json"), lambda: pytest.fail("source upload"),
                    permit_execution=lambda: pytest.fail("execution"))
        assert calls == [("GET", "/health")]


class Controls:
    def __init__(self, items):
        self.items = items

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class Control:
    def __init__(self, attrs):
        self.attrs = attrs

    def get_attribute(self, name):
        return "" if name in self.attrs and self.attrs[name] is None else self.attrs.get(name)

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def fill(self, value):
        self.value = value

    def evaluate(self, script, *args):
        return True


@pytest.mark.parametrize("product,offer", [("Dispatch Board", "Assign delivery routes"), ("Garden Journal", "Track home seedlings")])
@pytest.mark.parametrize("managed", [False, True])
@requires_qa_image
def test_runtime_cases_validate_for_both_contract_versions(tmp_path, product, offer, managed):
    from services.qa_runner.app.static_qa_runner import Harness

    with service_modules("company_os"):
        from app.builder.model_proxy import BuildModelError
        from app.builder.qa_validation import validate
        from app.experiments.wizard_tests import RequestForm, form_html

        url = "https://forms.example.test/public/waitlists/venture"
        form = RequestForm(fields=[{"key": "task", "label": offer, "kind": "short_text"},
            {"key": "details", "label": "Details", "kind": "long_text"},
            {"key": "address", "label": "Email address", "kind": "email"}])
        html = form_html(form, url, "exp_" + "a" * 24, "Contact me about my request.", "v1")
        contract = Contract(profile="static_landing_v1", product_name=product, required_text=[offer],
            managed_request_form={"html": html, "target": url} if managed else None)
        request = Input(command_id=str(uuid4()), source_artifact_id="synthetic", source_sha256="a" * 64,
            size_bytes=100, manifest=[{"path": "index.html", "size_bytes": 1, "sha256": "b" * 64}], contract=contract)
        qa_id, build_id = str(uuid4()), "synthetic-build"
        blob, evidence = report_fixture({"id": qa_id, "input": request.model_dump(mode="json")}, build_id)
        document = json.loads(blob)
        if managed:
            controls = Controls([Control(attrs) for attrs in contract.managed_request_form.controls])
            submit = Control({"id": "request_submit"})
            native = SimpleNamespace(is_visible=lambda: True,
                locator=lambda selector: Controls([submit]) if selector == "[id='request_submit']" else controls,
                evaluate=lambda script, *args: html == args[0] if "DOMParser" in script else "HTML:0/BODY:0/FORM:0")
            page = SimpleNamespace(locator=lambda _selector: Controls([native]))
            harness = Harness({"input": request.model_dump(mode="json")}, tmp_path, tmp_path)
            for viewport in ("desktop", "mobile"):
                observed = set()
                harness.forms(page, viewport + "-0", observed)
                harness.forms(page, viewport + "-0", observed)
            document["cases"].extend(case.model_dump(mode="json") for case in harness.cases)
            document["coverage"]["observed_cases"] = len(document["cases"])
            assert len({case.id for case in harness.cases}) == len(harness.cases)
        assert validate(canonical(document), request=request, build_id=build_id, qa_id=qa_id,
            generation=1, policy=POLICY, evidence=evidence)[0].outcome == "passed"
        if managed:
            native.is_visible = lambda: False
            controls.items[0].is_visible = lambda: False
            submit.is_enabled = lambda: False
            hidden = Harness({"input": request.model_dump(mode="json")}, tmp_path, tmp_path)
            for viewport in ("desktop", "mobile"):
                hidden.forms(page, viewport + "-0", set())
            failed = {**document, "outcome": "failed", "cases": [item for item in document["cases"]
                if ":form:request_form:" not in item["id"]] + [case.model_dump(mode="json") for case in hidden.cases]}
            failed["coverage"] = {**document["coverage"], "observed_cases": len(failed["cases"])}
            assert validate(canonical(failed), request=request, build_id=build_id, qa_id=qa_id,
                generation=1, policy=POLICY, evidence=evidence)[0].outcome == "failed"
            for case in [item for item in document["cases"] if ":form:request_form:" in item["id"]]:
                incomplete = {**document, "cases": [item for item in document["cases"] if item != case]}
                incomplete["coverage"] = {**document["coverage"], "observed_cases": len(incomplete["cases"])}
                with pytest.raises(BuildModelError, match="qa_report_cases_missing"):
                    validate(canonical(incomplete), request=request, build_id=build_id, qa_id=qa_id,
                        generation=1, policy=POLICY, evidence=evidence)
            undeclared = {**document, "cases": [{**item, "id": item["id"].replace("request_form", "abc123abc123")}
                if item["id"].endswith(":backend") else item for item in document["cases"]]}
            with pytest.raises(BuildModelError, match="qa_report_cases_missing"):
                validate(canonical(undeclared), request=request, build_id=build_id, qa_id=qa_id,
                    generation=1, policy=POLICY, evidence=evidence)
