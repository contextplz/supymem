"""GitHub fake adapter — the one used in local mode and by every test (39 §7).

It shares the real adapter's declarations, so a scope or capability added on one
side shows up on the other, and it drives the same event writer, so the trust
receipt is byte-for-byte the same shape.
"""

from __future__ import annotations

from typing import Any

from app.connectors.fakes import FakeMixin, fake_state, load_fixture
from app.connectors.github import webhooks as gh_webhooks
from app.connectors.github.adapter import GitHubConnector
from app.connectors.protocol import (
    ConnectionView,
    ConnectorError,
    Health,
    WebhookEvent,
    WebhookRef,
)
from app.connectors.webhooks import WebhookRejected, sign_hex


class GitHubFakeConnector(FakeMixin, GitHubConnector):
    provider = "github"

    def __init__(self, manifest: dict[str, Any] | None = None) -> None:
        super().__init__(manifest, simulated=True)

    def auth_complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Accepts the fixture token so local mode needs no real account."""
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
            id=f"fake_hook_{con.id}",
            secret_ref="",
            events=tuple(events or gh_webhooks.EVENTS),
        )

    def verify_webhook(
        self, raw: bytes, headers: dict[str, str], secret: str
    ) -> WebhookEvent | None:
        try:
            return gh_webhooks.verify(raw, headers, secret)
        except WebhookRejected:
            return None

    def simulate_delivery(
        self,
        secret: str,
        *,
        event: str = "pull_request",
        delivery_id: str = "fake-delivery-1",
        body: bytes | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        """Build a correctly signed delivery so the ingress path can be tested."""
        if body is None:
            fixture = load_fixture(self.provider, "webhook")
            import json

            body = json.dumps(fixture).encode("utf-8")
        headers = {
            gh_webhooks.SIGNATURE_HEADER: f"sha256={sign_hex(secret, body)}",
            gh_webhooks.EVENT_HEADER: event,
            gh_webhooks.DELIVERY_HEADER: delivery_id,
        }
        return body, headers

    def revoke(self, con: ConnectionView) -> None:
        st = fake_state(self.provider)
        # Dropping our hook is ours to drop; their repositories are untouched.
        st.created.append({"operation": "revoke", "connection_id": con.id})

    def _repository(self, con: ConnectionView) -> str:
        for grant in con.resource_grants:
            if "/" in grant:
                return grant
        raise ConnectorError("no repository has been granted on this connection")
