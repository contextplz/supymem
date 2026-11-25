"""Linear fake adapter — local mode and tests (39 §7)."""

from __future__ import annotations

from typing import Any

from app.connectors.fakes import FakeMixin, fake_state, load_fixture
from app.connectors.linear import webhooks as lin_webhooks
from app.connectors.linear.adapter import LinearConnector
from app.connectors.protocol import ConnectionView, Health, WebhookEvent, WebhookRef
from app.connectors.webhooks import WebhookRejected, sign_hex


class LinearFakeConnector(FakeMixin, LinearConnector):
    provider = "linear"

    def __init__(self, manifest: dict[str, Any] | None = None) -> None:
        super().__init__(manifest, simulated=True)

    def auth_complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = str(payload.get("token") or "") or str(
            load_fixture(self.provider, "account")["token"]
        )
        account = str(payload.get("external_account") or "") or str(
            load_fixture(self.provider, "account")["external_account"]
        )
        return {"secret_value": token, "external_account": account}

    def health(self, con: ConnectionView) -> Health:
        st = fake_state(self.provider)
        if st.expired:
            return Health("down", "access expired")
        return Health(con.health or "ok")

    async def _perform_register_webhook(
        self,
        con: ConnectionView,
        events: list[str],
        *,
        secret: str,
        callback_url: str,
    ) -> WebhookRef | None:
        self._precheck()
        return WebhookRef(
            id=f"fake_lin_hook_{con.id}",
            secret_ref="",
            events=tuple(events or lin_webhooks.EVENTS),
        )

    def verify_webhook(
        self, raw: bytes, headers: dict[str, str], secret: str
    ) -> WebhookEvent | None:
        try:
            return lin_webhooks.verify(raw, headers, secret)
        except WebhookRejected:
            return None

    def simulate_delivery(
        self,
        secret: str,
        *,
        event: str = "Issue",
        delivery_id: str = "fake-lin-1",
        body: bytes | None = None,
        sent_ms: float | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        import json
        import time

        if body is None:
            fixture = load_fixture(self.provider, "webhook")
            body = json.dumps(fixture).encode("utf-8")
        ts = sent_ms if sent_ms is not None else time.time() * 1000
        headers = {
            "Linear-Signature": sign_hex(secret, body),
            "Linear-Delivery-Timestamp": str(ts),
            "Linear-Delivery": delivery_id,
            "Linear-Event": event,
        }
        return body, headers
