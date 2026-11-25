import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app(reset=False)

from app.actuation.contract import ActuationMode
from app.actuation.registry import get
from app.actuation.tools.email_send import email_send


@pytest.mark.asyncio
async def test_email_send_is_simulated_without_resend_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    result = await email_send("ven_test", {"from": "hello@example.com", "to": ["a@example.com"], "subject": "Hi"})
    assert result.mode is ActuationMode.SIMULATED
    assert result.reason == "no_credential"


def test_email_send_is_registered() -> None:
    assert get("email.send") is not None
