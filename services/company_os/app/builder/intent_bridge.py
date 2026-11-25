"""Outside controller: read a bound task's persisted events, then invoke Core.

There is no shell, local SDK tool creation, task launch or provider key here.
The server-owned task binding must come from a verified runtime observation;
the sandbox's intent body cannot choose a URL, authority fence or credentials.
"""
from __future__ import annotations

import ipaddress
import json
import time
from urllib.parse import urlsplit

import httpx
from coding_agents.managed_controller import ManagedController
from coding_agents.managed_core import CoreAuthorityAdapter
from coding_agents.managed_protocol import ManagedInvocationError

from app.builder import sdk_authority
from app.builder.model_proxy import BuildModelError, _authority, _fence, _locked
from app.connectors.secrets import decrypt_at_rest, encrypt_at_rest
from app.state import db

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def _endpoint(value):
    try:
        url = urlsplit(value)
        address = ipaddress.ip_address(url.hostname)
        valid = (url.scheme == "http" and url.port == 8000 and url.path in {"", "/"}
                 and not url.username and not url.password and not url.query and not url.fragment
                 and any(address in network for network in PRIVATE_NETWORKS))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise BuildModelError("build_task_endpoint_invalid")
    return f"http://{address}:8000"


def install_task_binding(run_id, *, base_url, task_arn, task_control_token,
                         generation, controller_id, attempt):
    """Trusted runtime adapter only, after matching ECS task/run/ENI identity.

    This function and its input are not exposed to the founder or intent API.
    It does not assert AWS attestation by itself or enable an executable profile.
    The provider must first store the observed task ARN as sandbox_ref.
    """
    base_url = _endpoint(base_url)
    if not isinstance(task_control_token, str) or not 32 <= len(task_control_token) <= 200:
        raise BuildModelError("build_task_credential_invalid")
    with db()() as session:
        ctx = _locked(session, run_id)
        spec = _authority(session, ctx)
        _fence(ctx, spec, generation, controller_id, attempt)
        if not task_arn or ctx.run.sandbox_ref != task_arn or not task_arn.startswith("arn:aws:ecs:"):
            raise BuildModelError("build_task_identity_unverified")
        previous = spec.get("managed_task")
        binding = {"base_url": base_url, "task_arn": task_arn, "generation": generation}
        if previous:
            if any(previous.get(key) != value for key, value in binding.items()) or decrypt_at_rest(previous["control_token_ref"]) != task_control_token:
                raise BuildModelError("build_task_binding_conflict")
            return
        spec["managed_task"] = {**binding, "control_token_ref": encrypt_at_rest(task_control_token)}
        ctx.run.spec = json.dumps(spec, sort_keys=True)
        session.commit()


def _context(run_id, generation):
    with db()() as session:
        ctx = _locked(session, run_id)
        spec = _authority(session, ctx)
        fence = spec.get("controller_fence", {})
        _fence(ctx, spec, generation, fence.get("id"), fence.get("attempt"))
        binding = spec.get("managed_task", {})
        if (binding.get("generation") != generation or not binding.get("task_arn")
                or binding["task_arn"] != ctx.run.sandbox_ref):
            raise BuildModelError("build_task_binding_unavailable")
        endpoint = _endpoint(binding.get("base_url"))
        token = decrypt_at_rest(binding["control_token_ref"])
        return endpoint, token, {"generation": generation, "controller_id": fence["id"], "attempt": fence["attempt"]}


def _client():
    return httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0), follow_redirects=False, trust_env=False)


def read_events(run_id, generation, endpoint, token):
    try:
        deadline = time.monotonic() + 30
        with _client() as client, client.stream("GET", endpoint + "/v1/events", headers={
            "Authorization": "Bearer " + token, "X-Supymem-Build-ID": run_id,
            "X-Supymem-Generation": str(generation), "Accept": "application/json", "Accept-Encoding": "identity"}) as response:
            if (time.monotonic() > deadline or response.status_code != 200 or response.headers.get("content-type", "").split(";", 1)[0] != "application/json"
                    or response.headers.get("content-encoding", "identity").lower() != "identity"):
                raise BuildModelError("build_task_event_read_failed")
            chunks, count = [], 0
            length = response.headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_SNAPSHOT_BYTES):
                raise BuildModelError("build_task_event_snapshot_limit")
            for chunk in response.iter_raw():
                count += len(chunk)
                if count > MAX_SNAPSHOT_BYTES or time.monotonic() > deadline:
                    raise BuildModelError("build_task_event_snapshot_limit")
                chunks.append(chunk)
            if time.monotonic() > deadline or length is not None and int(length) != count:
                raise BuildModelError("build_task_event_snapshot_limit")
            value = json.loads(b"".join(chunks))
        if (not isinstance(value, dict) or value.get("build_run_id") != run_id
                or type(value.get("generation")) is not int or value["generation"] != generation):
            raise BuildModelError("build_task_event_scope_mismatch")
        events = value.get("events")
        if not isinstance(events, list) or len(events) > 500 or any(not isinstance(event, str) for event in events):
            raise BuildModelError("build_task_event_snapshot_invalid")
        return events
    except BuildModelError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise BuildModelError("build_task_event_read_failed") from exc


def _controller(run_id, generation):
    endpoint, token, fence = _context(run_id, generation)
    return ManagedController(CoreAuthorityAdapter(sdk_authority, run_id, **fence),
                             lambda: read_events(run_id, generation, endpoint, token))


def synchronize(run_id, *, generation):
    try:
        result = _controller(run_id, generation).synchronize()
        return {"cursor": result.cursor, "consumed_invocations": sorted(result.consumed_invocations)}
    except (ManagedInvocationError, json.JSONDecodeError) as exc:
        raise BuildModelError("build_task_event_validation_failed") from exc


def final_receipts(run_id, *, generation, controller_id, attempt):
    """Trusted terminal drain; never invokes a model or executes an SDK tool."""
    from sqlalchemy import select
    from app.db.supymem_models import BuildModelCall
    from coding_agents.managed_controller import metadata_chain
    from .model_proxy import digest
    endpoint, token, fence = _context(run_id, generation)
    if fence != {"generation": generation, "controller_id": controller_id, "attempt": attempt}:
        raise BuildModelError("build_controller_fence_lost")
    with db()() as session:
        ctx = _locked(session, run_id)
        if not json.loads(ctx.run.spec).get("builder_admission_closed_at"):
            raise BuildModelError("build_final_admission_not_closed")
    raw = read_events(run_id, generation, endpoint, token)
    try:
        chain = metadata_chain(raw)
        actions = {row["event_id"] for row in chain if row["event_type"] == "ActionEvent"}
        observed = {row.get("action_id") for row in chain if row["event_type"] == "ObservationEvent"}
        if not chain or actions != observed or any(row["event_type"] in {"AgentErrorEvent", "ConversationErrorEvent"} for row in chain):
            raise BuildModelError("build_final_event_chain_incomplete")
        result = ManagedController(CoreAuthorityAdapter(sdk_authority, run_id, **fence), lambda: raw).synchronize()
    except ManagedInvocationError as exc:
        raise BuildModelError("build_final_event_chain_invalid") from exc
    with db()() as session:
        ctx = _locked(session, run_id)
        spec = _authority(session, ctx)
        _fence(ctx, spec, generation, controller_id, attempt)
        calls = session.scalars(select(BuildModelCall).where(BuildModelCall.build_run_id == run_id).order_by(BuildModelCall.sequence)).all()
        if (not calls or any(row.generation != generation or row.state != "settled" or row.error_code or not row.consumed_event_id for row in calls)
                or {row.id for row in calls} != result.consumed_invocations or spec.get("agent_cursor") != result.cursor):
            raise BuildModelError("build_final_model_receipts_unresolved")
        return {"cursor": result.cursor, "snapshot_digest": digest(chain), "consumed_sequence": spec["consumed_sequence"],
                "calls": [{"id": row.id, "response_digest": row.response_digest, "consumed_event_id": row.consumed_event_id} for row in calls]}


def prepare(run_id, *, generation, invocation_key, input_event_id, request):
    try:
        return _controller(run_id, generation).prepare(invocation_key=invocation_key, input_event_id=input_event_id, request=request)
    except (ManagedInvocationError, json.JSONDecodeError) as exc:
        raise BuildModelError("build_task_event_validation_failed") from exc
