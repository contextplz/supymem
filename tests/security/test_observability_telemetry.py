from uuid import uuid4

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()


def _body(event="ui.dead_end"):
    return {"event": event, "event_id": str(uuid4()), "venture_id": "ven_obs",
            "props": {"feature": "idea", "route": "/v/{id}/idea", "code": "ui_error_shown"}}


@pytest.mark.parametrize("extra", [{"text": "private"}, {"email": "founder@example.com"},
    {"route": "/v/{id}/private-product"}, {"route": "/v/ven_private/idea"},
    {"route": "/gtm?token=secret"}, {"code": "private_error"}, {"step": "idea"},
    {"error_text": "private"}, {"session_id": str(uuid4())}])
def test_telemetry_observability_rejects_content_and_arbitrary_routes(extra):
    from app.api.events_ingest import TelemetryBody
    from pydantic import ValidationError

    raw = _body()
    TelemetryBody.model_validate(raw)
    with pytest.raises(ValidationError):
        TelemetryBody.model_validate({**raw, "props": {**raw["props"], **extra}})


def test_telemetry_journey_requires_venture_and_no_error_content():
    from app.api.events_ingest import TelemetryBody
    from pydantic import ValidationError

    raw = _body("journey.step_entered")
    raw["props"] = {"feature": "idea", "route": "/v/{id}/idea", "step": "clarify"}
    TelemetryBody.model_validate(raw)
    for change in ({"venture_id": None}, {"props": {**raw["props"], "code": "action_failed"}},
                   {"venture_id": "private@example.com"}, {"workspace_id": "founder content"},
                   {"props": {**raw["props"], "step": "private"}}):
        with pytest.raises(ValidationError):
            TelemetryBody.model_validate({**raw, **change})
