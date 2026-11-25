"""Linear adapter (39 §4, provider 2). Read teams/issues; write issues and comments."""

from __future__ import annotations

from typing import Any

from connectors.http import Reply, request_json, retry_after_seconds

from app.connectors.base import ConnectorBase
from app.connectors.linear import scopes as lin_scopes
from app.connectors.linear import webhooks as lin_webhooks
from app.connectors.protocol import (
    Action,
    ActionResult,
    AuthChallenge,
    AuthExpired,
    Capability,
    ConnectionView,
    ConnectorError,
    Health,
    Page,
    RateLimited,
    ReadResult,
    Resource,
    ResourceRef,
    WebhookEvent,
    WebhookRef,
)
from app.connectors.secrets import store
from app.connectors.webhooks import WebhookRejected

API = "https://api.linear.app/graphql"


class LinearConnector(ConnectorBase):
    provider = "linear"
    auth_kind = "api_key"
    baseline_scope = lin_scopes.TEAMS_READ
    scopes = lin_scopes.SCOPES
    read_specs = lin_scopes.READS
    write_capabilities = lin_scopes.CAPABILITIES

    def auth_start(self, workspace_id: str, scopes: list[str]) -> AuthChallenge:
        setup = self.manifest.get("auth") or {}
        return AuthChallenge(
            kind=self.auth_kind,
            requested_scopes=tuple(scopes or (self.baseline_scope,)),
            url=str(setup.get("url") or "https://linear.app/settings/api"),
            instructions_key=str(setup.get("instructions_key") or ""),
            concierge_task=str(setup.get("concierge_task") or "") or None,
        )

    def auth_complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = str(payload.get("token") or "").strip()
        if not token:
            raise ConnectorError("no credential supplied")
        account = str(payload.get("external_account") or "").strip()
        if not account:
            raise ConnectorError("no account supplied")
        return {"secret_value": token, "external_account": account}

    def health(self, con: ConnectionView) -> Health:
        return Health(con.health or "unknown", con.last_error or "")

    def _headers(self, con: ConnectionView) -> dict[str, str]:
        if not con.secret_ref:
            raise ConnectorError("connection has no stored credential")
        token = store().get(con.secret_ref, requester=f"linear:{con.id}")
        return {
            "Authorization": token,
            "Content-Type": "application/json",
        }

    def _check(self, reply: Reply) -> Reply:
        if reply.status == 429:
            raise RateLimited(retry_after_seconds(reply.headers), provider=self.provider)
        if reply.status in (401, 403):
            raise AuthExpired(self.provider, reply.status)
        if not reply.ok:
            raise ConnectorError(f"linear returned {reply.status}")
        return reply

    async def _gql(self, con: ConnectionView, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        reply = self._check(
            await request_json(
                "POST",
                API,
                headers=self._headers(con),
                json={"query": query, "variables": variables or {}},
            )
        )
        body = reply.body or {}
        if body.get("errors"):
            raise ConnectorError("linear graphql error")
        data = body.get("data") or {}
        data["_bytes_in"] = reply.bytes_in
        return data

    async def _perform_list(
        self, con: ConnectionView, kind: str, cursor: str | None
    ) -> Page:
        if kind == "team":
            data = await self._gql(con, "{ teams { nodes { id name } } }")
            nodes = ((data.get("teams") or {}).get("nodes") or [])
            rows = [Resource("team", str(n["id"]), str(n.get("name") or n["id"])) for n in nodes]
        elif kind == "issue":
            data = await self._gql(
                con,
                "{ issues { nodes { id identifier title state { name } } } }",
            )
            nodes = ((data.get("issues") or {}).get("nodes") or [])
            rows = [
                Resource(
                    "issue",
                    str(n["id"]),
                    str(n.get("title") or n.get("identifier") or n["id"]),
                    {"state": ((n.get("state") or {}).get("name") or "")},
                )
                for n in nodes
            ]
        else:
            raise ConnectorError(f"linear cannot list '{kind}'")
        return Page(items=tuple(rows), cursor=None, exhausted=True, bytes_in=int(data.get("_bytes_in") or 0))

    async def _perform_read(self, con: ConnectionView, ref: ResourceRef) -> ReadResult:
        if ref.kind != "issue":
            raise ConnectorError(f"linear cannot read '{ref.kind}'")
        data = await self._gql(
            con,
            "query($id: String!) { issue(id: $id) { id identifier title description } }",
            {"id": ref.ref},
        )
        issue = data.get("issue") or {}
        return ReadResult(
            ref=ref.ref,
            kind=ref.kind,
            content=str(issue.get("description") or issue.get("title") or ""),
            bytes_in=int(data.get("_bytes_in") or 0),
        )

    async def _perform_write(
        self, con: ConnectionView, action: Action, cap: Capability
    ) -> ActionResult:
        payload = action.payload
        if cap.operation == "create_issue":
            data = await self._gql(
                con,
                "mutation($title: String!, $teamId: String!) { issueCreate(input: { title: $title, teamId: $teamId }) { issue { id identifier } } }",
                {"title": payload.get("title"), "teamId": action.target},
            )
            issue = ((data.get("issueCreate") or {}).get("issue") or {})
            return ActionResult(
                external_id=str(issue.get("id") or ""),
                display=str(issue.get("identifier") or action.target),
                url=None,
                bytes_out=len(str(payload)),
                simulated=self.simulated,
            )
        if cap.operation == "comment_issue":
            data = await self._gql(
                con,
                "mutation($id: String!, $body: String!) { commentCreate(input: { issueId: $id, body: $body }) { comment { id } } }",
                {"id": action.target, "body": payload.get("body")},
            )
            comment = ((data.get("commentCreate") or {}).get("comment") or {})
            return ActionResult(
                external_id=str(comment.get("id") or ""),
                display=action.target,
                url=None,
                bytes_out=len(str(payload)),
                simulated=self.simulated,
            )
        raise ConnectorError(f"linear cannot '{cap.operation}'")

    def verify_webhook(self, raw: bytes, headers: dict[str, str], secret: str) -> WebhookEvent | None:
        try:
            return lin_webhooks.verify(raw, headers, secret)
        except WebhookRejected:
            return None

    async def _perform_register_webhook(
        self,
        con: ConnectionView,
        events: list[str],
        *,
        secret: str,
        callback_url: str,
    ) -> WebhookRef | None:
        return WebhookRef(
            id=f"lin_hook_{con.id}",
            secret_ref="",
            events=tuple(events or lin_webhooks.EVENTS),
        )
