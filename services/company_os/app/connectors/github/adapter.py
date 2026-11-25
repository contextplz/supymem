"""GitHub adapter (39 §4, provider 1).

Reads repositories, trees, files, commits, pull requests, issues and CI runs;
writes branches, commits, draft pull requests and comments. Never merges, never
deploys, never touches settings — those permissions are not requested at all.
"""

from __future__ import annotations

import base64
from typing import Any

from connectors.http import Reply, request_json, retry_after_seconds

from app.connectors.base import ConnectorBase
from app.connectors.github import scopes as gh_scopes
from app.connectors.github import webhooks as gh_webhooks
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

API = "https://api.github.com"
API_VERSION = "2022-11-28"


class GitHubConnector(ConnectorBase):
    provider = "github"
    auth_kind = "pat"
    baseline_scope = gh_scopes.METADATA_READ
    scopes = gh_scopes.SCOPES
    read_specs = gh_scopes.READS
    write_capabilities = gh_scopes.CAPABILITIES

    # ── auth ────────────────────────────────────────────────────────────────
    def auth_start(self, workspace_id: str, scopes: list[str]) -> AuthChallenge:
        setup = self.manifest.get("auth") or {}
        return AuthChallenge(
            kind=self.auth_kind,
            requested_scopes=tuple(scopes or (self.baseline_scope,)),
            url=str(setup.get("url") or ""),
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

    # ── http ────────────────────────────────────────────────────────────────
    def _headers(self, con: ConnectionView) -> dict[str, str]:
        if not con.secret_ref:
            raise ConnectorError("connection has no stored credential")
        token = store().get(con.secret_ref, requester=f"github:{con.id}")
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }

    def _check(self, reply: Reply) -> Reply:
        if reply.status == 429 or (
            reply.status == 403
            and reply.headers.get("x-ratelimit-remaining") == "0"
        ):
            raise RateLimited(
                retry_after_seconds(reply.headers), provider=self.provider
            )
        if reply.status in (401, 403):
            raise AuthExpired(self.provider, reply.status)
        if not reply.ok:
            raise ConnectorError(f"github returned {reply.status}")
        return reply

    async def _get(
        self, con: ConnectionView, path: str, params: dict[str, Any] | None = None
    ) -> Reply:
        return self._check(
            await request_json(
                "GET", f"{API}{path}", headers=self._headers(con), params=params
            )
        )

    async def _post(
        self, con: ConnectionView, path: str, body: dict[str, Any]
    ) -> Reply:
        return self._check(
            await request_json(
                "POST", f"{API}{path}", headers=self._headers(con), json=body
            )
        )

    # ── reads ───────────────────────────────────────────────────────────────
    async def _perform_list(
        self, con: ConnectionView, kind: str, cursor: str | None
    ) -> Page:
        page = _page_number(cursor)
        params: dict[str, Any] = {"per_page": 50, "page": page}
        if kind == "repo":
            reply = await self._get(con, f"/orgs/{con.external_account}/repos", params)
            rows = [
                Resource("repo", str(r["full_name"]), str(r["full_name"]),
                         {"private": bool(r.get("private"))})
                for r in _rows(reply)
            ]
        else:
            repo = _repo_of(con)
            path = {
                "commit": f"/repos/{repo}/commits",
                "pull_request": f"/repos/{repo}/pulls",
                "issue": f"/repos/{repo}/issues",
                "check_run": f"/repos/{repo}/actions/runs",
            }.get(kind)
            if path is None:
                raise ConnectorError(f"github cannot list '{kind}'")
            reply = await self._get(con, path, params)
            rows = [
                Resource(
                    kind,
                    f"{repo}#{r.get('number', r.get('sha', r.get('id', '')))}",
                    str(r.get("title") or r.get("name") or r.get("sha") or ""),
                    {"state": r.get("state") or r.get("status") or ""},
                )
                for r in _rows(reply)
            ]
        has_next = 'rel="next"' in (reply.headers.get("link") or "")
        return Page(
            items=tuple(rows),
            cursor=f"p{page + 1}" if has_next else None,
            exhausted=not has_next,
            bytes_in=reply.bytes_in,
        )

    async def _perform_read(
        self, con: ConnectionView, ref: ResourceRef
    ) -> ReadResult:
        repo, _, tail = ref.ref.partition(":")
        if ref.kind == "tree":
            reply = await self._get(
                con, f"/repos/{repo}/git/trees/{tail or 'HEAD'}", {"recursive": 1}
            )
            entries = (reply.body or {}).get("tree") or []
            return ReadResult(
                ref=ref.ref,
                kind=ref.kind,
                items=tuple(
                    Resource("path", str(e.get("path")), str(e.get("path")))
                    for e in entries
                ),
                bytes_in=reply.bytes_in,
            )
        if ref.kind == "file":
            reply = await self._get(con, f"/repos/{repo}/contents/{tail}")
            body = reply.body or {}
            raw = base64.b64decode(body.get("content") or "").decode(
                "utf-8", "replace"
            )
            return ReadResult(
                ref=ref.ref, kind=ref.kind, content=raw, bytes_in=reply.bytes_in
            )
        raise ConnectorError(f"github cannot read '{ref.kind}'")

    # ── writes ──────────────────────────────────────────────────────────────
    async def _perform_write(
        self, con: ConnectionView, action: Action, cap: Capability
    ) -> ActionResult:
        repo, _, tail = action.target.partition("#")
        payload = action.payload
        if cap.operation == "create_branch":
            reply = await self._post(
                con,
                f"/repos/{repo}/git/refs",
                {
                    "ref": f"refs/heads/{payload.get('branch')}",
                    "sha": payload.get("base_sha"),
                },
            )
            return _result(reply, str(payload.get("branch")), action)
        if cap.operation == "commit_file":
            reply = self._check(
                await request_json(
                    "PUT",
                    f"{API}/repos/{repo}/contents/{payload.get('path')}",
                    headers=self._headers(con),
                    json={
                        "message": payload.get("message"),
                        "content": base64.b64encode(
                            str(payload.get("content") or "").encode("utf-8")
                        ).decode("ascii"),
                        "branch": payload.get("branch"),
                        "sha": payload.get("sha"),
                    },
                )
            )
            return _result(reply, str(payload.get("path")), action)
        if cap.operation in ("open_draft_pr", "open_pr"):
            reply = await self._post(
                con,
                f"/repos/{repo}/pulls",
                {
                    "title": payload.get("title"),
                    "head": payload.get("head"),
                    "base": payload.get("base"),
                    "body": payload.get("body") or "",
                    "draft": cap.operation == "open_draft_pr",
                },
            )
            number = (reply.body or {}).get("number")
            return _result(reply, f"{repo}#{number}", action)
        if cap.operation in ("comment_pr", "comment_issue"):
            reply = await self._post(
                con,
                f"/repos/{repo}/issues/{tail}/comments",
                {"body": payload.get("body")},
            )
            return _result(reply, f"{repo}#{tail}", action)
        raise ConnectorError(f"github cannot {cap.operation}")

    async def _perform_register_webhook(
        self,
        con: ConnectionView,
        events: list[str],
        *,
        secret: str,
        callback_url: str,
    ) -> WebhookRef | None:
        repo = _repo_of(con)
        reply = await self._post(
            con,
            f"/repos/{repo}/hooks",
            {
                "config": {
                    "url": callback_url,
                    "secret": secret,
                    "content_type": "json",
                },
                "events": list(events or gh_webhooks.EVENTS),
                "active": True,
            },
        )
        return WebhookRef(
            id=str((reply.body or {}).get("id") or ""),
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


def _rows(reply: Reply) -> list[dict[str, Any]]:
    body = reply.body
    if isinstance(body, dict):
        for key in ("workflow_runs", "items"):
            if key in body:
                return list(body[key])
        return []
    return list(body or [])


def _repo_of(con: ConnectionView) -> str:
    for grant in con.resource_grants:
        if "/" in grant:
            return grant
    raise ConnectorError("no repository has been granted on this connection")


def _page_number(cursor: str | None) -> int:
    if not cursor:
        return 1
    try:
        return max(1, int(str(cursor).lstrip("p")))
    except ValueError:
        return 1


def _result(reply: Reply, display: str, action: Action) -> ActionResult:
    body = reply.body if isinstance(reply.body, dict) else {}
    external = body.get("number") or body.get("id") or body.get("sha") or display
    return ActionResult(
        external_id=str(external),
        display=display,
        url=str(body.get("html_url") or "") or None,
        bytes_out=reply.bytes_in,
        simulated=False,
    )
