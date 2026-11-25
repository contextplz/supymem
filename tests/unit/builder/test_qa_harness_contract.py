"""A stale immutable browser image must fail before source upload or QA run."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from core.static_qa_contracts import Input
from tests.integration.test_postgres_auth_security import service_modules
from tests.qa_storage_fixtures import CONTRACT


@pytest.mark.parametrize("domain", ["procurement-workflow", "family-walks"])
def test_local_waitlist_actions_keep_loopback_and_public_route_boundaries(monkeypatch, domain):
    from core.static_qa_contracts import Action

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    values = {"id": "waitlist_cta", "kind": "external_link", "role": "link", "name": "Join updates"}
    with service_modules("company_os"):
        for host in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000"):
            url = f"http://{host}/public/waitlists/{domain}"
            assert Action(**values, target=url).target == url
            pricing = url + "/pricing/exp_" + "a" * 24 + "/standard"
            assert Action(**values, target=pricing).target == pricing
        for url in (f"http://outside.example/public/waitlists/{domain}", "http://localhost:8000/admin",
                    f"http://localhost:8000/public/waitlists/{domain}/../../admin",
                    f"http://user:password@localhost:8000/public/waitlists/{domain}",
                    f"http://localhost:0/public/waitlists/{domain}",
                    f"http://localhost:8000/public/waitlists/{domain}/price/standard",
                    f"http://localhost:8000/public/waitlists/{domain}/pricing/not-an-experiment/standard"):
            with pytest.raises(ValueError):
                Action(**values, target=url)
        monkeypatch.delenv("SUPYMEM_MODE")
        with pytest.raises(ValueError):
            Action(**values, target=f"http://localhost:8000/public/waitlists/{domain}")


@pytest.mark.parametrize("version,profiles,accepted", [
    (None, ["static_landing_v1"], False),
    (2, ["static_landing_v1"], False),
    (True, ["static_landing_v1"], False),
    (3, ["founder_product_v1"], False),
    (3, ["static_landing_v1"], True),
    (4, ["static_landing_v1"], False),
])
@pytest.mark.parametrize("product,offer", [("Dispatch Board", "Assign delivery routes"), ("Garden Journal", "Track home seedlings")])
def test_qa_image_case_contract_checked_before_any_mutation(monkeypatch, version, profiles, accepted, product, offer):
    request_input = Input(command_id=str(uuid4()), source_artifact_id="synthetic", source_sha256="a" * 64,
                          size_bytes=100, manifest=[{"path": "index.html", "size_bytes": 1,
                                                      "sha256": "b" * 64}], contract={**CONTRACT, "product_name": product, "required_text": [offer]})
    calls = []
    with service_modules("company_os"):
        from app.builder import qa_transport
        from app.builder.model_proxy import BuildModelError

        def fake_request(_binding, method, path, **_kwargs):
            calls.append((method, path))
            assert (method, path) == ("GET", "/health")
            return json.dumps({"status": "ok", "profiles": profiles,
                               **({} if version is None else {"case_contract_version": version})}).encode()

        def fake_state(_binding):
            calls.append(("GET", "/v1/qa/state"))
            return {"phase": "running", "input_digest": request_input.digest,
                    "command_id": request_input.command_id}

        monkeypatch.setattr(qa_transport, "request", fake_request)
        monkeypatch.setattr(qa_transport, "state", fake_state)
        if accepted:
            assert qa_transport.progress({}, request_input.model_dump(mode="json"),
                                         lambda: pytest.fail("source upload"),
                                         permit_execution=lambda: pytest.fail("execution admission"))["phase"] == "running"
            assert calls == [("GET", "/health"), ("GET", "/v1/qa/state")]
        else:
            with pytest.raises(BuildModelError, match="qa_harness_contract_mismatch"):
                qa_transport.progress({}, request_input.model_dump(mode="json"),
                                      lambda: pytest.fail("source upload"),
                                      permit_execution=lambda: pytest.fail("execution admission"))
            assert calls == [("GET", "/health")]
