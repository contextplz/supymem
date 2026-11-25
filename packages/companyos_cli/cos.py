"""Company OS CLI — thin HTTP client over the same service core (41 §5)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

DEFAULT_BASE = "http://127.0.0.1:3300"

# Exit codes (41 §5)
EXIT_OK = 0
EXIT_BLOCKED = 2
EXIT_DENIED = 3
EXIT_FAILED = 4


def _base() -> str:
    return (os.environ.get("COS_BASE_URL") or DEFAULT_BASE).rstrip("/")


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    token = os.environ.get("COS_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _workspace_id(explicit: str | None = None) -> str | None:
    return explicit or os.environ.get("COS_WORKSPACE_ID")


def _client() -> httpx.Client:
    return httpx.Client(base_url=_base(), headers=_headers(), timeout=30.0)


def _print(data: Any, *, as_json: bool) -> None:
    if as_json or not sys.stdout.isatty():
        print(json.dumps(data, indent=2, default=str))
    elif isinstance(data, dict):
        print(json.dumps(data, indent=2, default=str))
    else:
        print(data)


def cmd_status(args: argparse.Namespace) -> int:
    ws = _workspace_id(args.workspace)
    with _client() as client:
        if ws and os.environ.get("COS_TOKEN"):
            r = client.get(f"/api/v1/workspaces/{ws}/status")
        elif ws:
            r = client.get(f"/workspaces/{ws}")
            if r.status_code == 200:
                # Enrich with MCP status tool for open work.
                st = client.post(
                    "/mcp/tools/get_status",
                    json={"name": "get_status", "arguments": {"workspace_id": ws}},
                )
                body = r.json()
                if st.status_code == 200:
                    body["status"] = st.json()
                _print(body, as_json=args.json)
                return EXIT_OK
        else:
            r = client.get("/me")
        if r.status_code == 401 or r.status_code == 403:
            _print(r.json(), as_json=True)
            return EXIT_DENIED
        if r.status_code >= 400:
            _print(r.json() if r.content else {"error": r.status_code}, as_json=True)
            return EXIT_FAILED
        _print(r.json(), as_json=args.json)
        return EXIT_OK


def cmd_do(args: argparse.Namespace) -> int:
    ws = _workspace_id(args.workspace)
    if not ws:
        print("COS_WORKSPACE_ID or --workspace is required", file=sys.stderr)
        return EXIT_FAILED
    text = args.text
    with _client() as client:
        if os.environ.get("COS_TOKEN"):
            import uuid

            r = client.post(
                "/api/v1/engagements",
                headers={"Idempotency-Key": str(uuid.uuid4())},
                json={"workspace_id": ws, "request": text},
            )
        else:
            r = client.post(
                "/mcp/tools/request_work",
                json={
                    "name": "request_work",
                    "arguments": {"workspace_id": ws, "text": text},
                },
            )
        if r.status_code in {401, 403}:
            _print(r.json(), as_json=True)
            return EXIT_DENIED
        if r.status_code >= 400:
            _print(r.json() if r.content else {"error": r.status_code}, as_json=True)
            return EXIT_FAILED
        body = r.json()
        _print(body, as_json=args.json)
        status = str(body.get("status") or "")
        if status == "blocked":
            return EXIT_BLOCKED
        if status == "failed":
            return EXIT_FAILED
        return EXIT_OK


def cmd_approve(args: argparse.Namespace) -> int:
    approval_id = args.approval_id
    with _client() as client:
        if os.environ.get("COS_TOKEN"):
            r = client.post(
                f"/api/v1/approvals/{approval_id}/decide",
                json={"decision": args.decision},
            )
        else:
            r = client.post(
                f"/approvals/{approval_id}/decide",
                json={
                    "decision": args.decision,
                    "decided_by": os.environ.get("COS_PERSON_ID", "per_local000000"),
                },
            )
        if r.status_code in {401, 403}:
            _print(r.json(), as_json=True)
            return EXIT_DENIED
        if r.status_code >= 400:
            _print(r.json() if r.content else {"error": r.status_code}, as_json=True)
            return EXIT_FAILED
        _print(r.json(), as_json=args.json)
        return EXIT_OK


def cmd_workspaces(args: argparse.Namespace) -> int:
    with _client() as client:
        r = client.post(
            "/mcp/tools/list_workspaces",
            json={"name": "list_workspaces", "arguments": {}},
        )
        if r.status_code >= 400:
            # Fall back to /me desk listing if MCP path unavailable.
            r = client.get("/me")
        if r.status_code in {401, 403}:
            _print(r.json(), as_json=True)
            return EXIT_DENIED
        if r.status_code >= 400:
            _print(r.json() if r.content else {"error": r.status_code}, as_json=True)
            return EXIT_FAILED
        _print(r.json(), as_json=args.json)
        return EXIT_OK


def cmd_site(args: argparse.Namespace) -> int:
    ws = _workspace_id(args.workspace)
    if not ws:
        print("COS_WORKSPACE_ID or --workspace is required", file=sys.stderr)
        return EXIT_FAILED
    with _client() as client:
        r = client.get(f"/workspaces/{ws}/site")
        if r.status_code == 404:
            _print({"error": "no_site", "message": "No first look yet"}, as_json=True)
            return EXIT_FAILED
        if r.status_code >= 400:
            _print(r.json() if r.content else {"error": r.status_code}, as_json=True)
            return EXIT_FAILED
        body = r.json()
        _print(body, as_json=args.json)
        if args.open and body.get("url"):
            import webbrowser

            webbrowser.open(str(body["url"]))
        return EXIT_OK


def cmd_ops_agents(args: argparse.Namespace) -> int:
    with _client() as client:
        r = client.get("/api/v1/agents/status")
        if r.status_code >= 400:
            _print(r.json() if r.content else {"error": r.status_code}, as_json=True)
            return EXIT_FAILED
        _print(r.json(), as_json=args.json)
        return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cos", description="Company OS CLI")
    p.add_argument("--workspace", help="Workspace id (or COS_WORKSPACE_ID)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="Always print JSON")
    common.add_argument("--workspace", help="Workspace id (or COS_WORKSPACE_ID)")
    sub = p.add_subparsers(dest="command", required=True)

    st = sub.add_parser("status", parents=[common], help="Workspace summary")
    st.set_defaults(func=cmd_status)

    ws = sub.add_parser("workspaces", parents=[common], help="List workspaces")
    ws.set_defaults(func=cmd_workspaces)

    site = sub.add_parser("site", parents=[common], help="Show first-look site for a workspace")
    site.add_argument("--open", action="store_true", help="Open the site URL")
    site.set_defaults(func=cmd_site)

    agents = sub.add_parser("ops-agents", parents=[common], help="List Polsia-compatible ops agents")
    agents.set_defaults(func=cmd_ops_agents)

    do = sub.add_parser("do", parents=[common], help="Create an engagement from text")
    do.add_argument("text", help="What you want done")
    do.set_defaults(func=cmd_do)

    ap = sub.add_parser("approve", parents=[common], help="Approve a pending item")
    ap.add_argument("approval_id", help="Approval id")
    ap.add_argument(
        "--decision",
        default="approved",
        choices=["approved", "rejected"],
    )
    ap.set_defaults(func=cmd_approve)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
