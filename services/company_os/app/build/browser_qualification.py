"""Default-off trial custody in existing documents/account/order transactions.

These hooks never call a provider, execute a browser, approve an order, or move
credits. The normal device hold and invocation journal remain the billing owner.
"""
from __future__ import annotations
from app.credits.accounts import account_can_spend

import json
import os
import re
import secrets
from dataclasses import asdict, replace
from datetime import datetime

from sqlalchemy import select

from app.build import device_budget, device_orders as orders
from app.builder.access import authorize
from app.db.models import CreditReservation, VentureDocument
from app.db.supymem_models import DesktopDevice
from core.browser_qualification import (
    FIXTURE_ORIGIN, FIXTURE_PREFIX, OwnedFixtureGrant, QualificationTrialBinding, TRIAL_CEILING_MICRO_USD,
    request_binding_digest,
)
from core.canonical import digest, sha256_hex
from core.ids import new_id

KIND = "browser_qualification_trial"
FIXTURE_KIND = "browser_qualification_fixture"
ENABLED_ENV = "DEVICE_BROWSER_QUALIFICATION_ENABLED"
ALLOWLIST_ENV = "DEVICE_BROWSER_QUALIFICATION_PERSON_IDS"
UNAVAILABLE = "browser_qualification_unavailable"
INVALID = "browser_qualification_binding_invalid"


def enabled(actor: str) -> bool:
    return os.getenv(ENABLED_ENV, "").lower() in {"1", "true"} and actor in {
        value.strip() for value in os.getenv(ALLOWLIST_ENV, "").split(",") if value.strip()}


def _device_current(session, actor, device_id) -> None:
    # Select only public custody columns; refresh credentials are never read.
    device = session.execute(select(DesktopDevice.person_id, DesktopDevice.platform, DesktopDevice.revoked_at)
        .where(DesktopDevice.id == device_id)).one_or_none()
    if not device or device.person_id != actor or device.platform != "macos" or device.revoked_at is not None:
        raise ValueError(UNAVAILABLE)


def _save(row, trial):
    row.provenance_json, row.updated_at = json.dumps(trial, sort_keys=True, separators=(",", ":")), orders.utcnow()


def _trial(session, trial_id, *, workspace_id, venture_id, actor_id, device_id, lock=True):
    row = session.get(VentureDocument, trial_id, with_for_update=lock, populate_existing=True)
    if row is None or row.kind != KIND or row.venture_id != venture_id:
        raise KeyError("browser_qualification_not_found")
    trial = json.loads(row.provenance_json or "{}")
    if tuple(trial.get(key) for key in ("workspace_id", "venture_id", "actor_id", "device_id")) != (
            workspace_id, venture_id, actor_id, device_id):
        raise KeyError("browser_qualification_not_found")
    return row, trial


def _amount(value):
    if type(value) is not int or not 0 <= value <= TRIAL_CEILING_MICRO_USD:
        raise ValueError(INVALID)
    return value


def totals(trial) -> dict:
    known = sum(value["actual_micro_usd"] for value in trial["attempts"].values() if value["state"] == "known")
    protected = sum(value["protected_micro_usd"] for value in trial["attempts"].values() if value["state"] != "known")
    allocated = sum(value["remaining_micro_usd"] for value in trial["allocations"].values())
    return {"ceiling_micro_usd": TRIAL_CEILING_MICRO_USD, "known_micro_usd": known,
        "protected_unknown_micro_usd": protected, "allocated_remaining_micro_usd": allocated,
        "remaining_micro_usd": max(0, TRIAL_CEILING_MICRO_USD - known - protected - allocated),
        "attempted_calls": len(trial["attempts"]), "budget_breached": trial.get("budget_breached", False),
        "provider_invoice_verified": False}


def start_trial(venture_id, caller, *, catalog_digest, seed_digest, expires_at):
    """Controller-only entry; no API accepts caller-authored trials or grants."""
    wid, actor, device = orders._scope(venture_id, caller)
    if not enabled(actor):
        raise ValueError(UNAVAILABLE)
    from core.browser_qualification_catalog import CATALOG_DIGEST
    if (catalog_digest != CATALOG_DIGEST or not isinstance(seed_digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", seed_digest) is None
            or type(expires_at) is not int or not int(orders.utcnow().timestamp()) < expires_at <= int(orders.utcnow().timestamp()) + 86400):
        raise ValueError(INVALID)
    with orders.db()() as session:
        account = device_budget.lock_account(session, wid, venture_id)
        orders._lock_venture(session, wid, venture_id)
        authorize(session, workspace_id=wid, venture_id=venture_id, actor=actor, mutate=True)
        _device_current(session, actor, device)
        if account is None or account.status != "active":
            raise ValueError(UNAVAILABLE)
        previous = session.scalars(select(VentureDocument).where(VentureDocument.venture_id == venture_id,
            VentureDocument.kind == KIND).with_for_update())
        for saved in previous:
            prior = json.loads(saved.provenance_json or "{}")
            # Expiry/restart/new process never erases protected USD or creates a reset button.
            if prior.get("state") == "active" or totals(prior)["protected_unknown_micro_usd"]:
                if (prior.get("actor_id"), prior.get("device_id"), prior.get("catalog_digest"), prior.get("seed_digest")) == (
                        actor, device, catalog_digest, seed_digest):
                    return trial_view(prior)
                raise ValueError("browser_qualification_previous_trial_unresolved")
        tid = new_id("bqt")
        trial = {"schema_version": 1, "trial_id": tid, "workspace_id": wid, "venture_id": venture_id,
            "actor_id": actor, "device_id": device, "catalog_digest": catalog_digest, "seed_digest": seed_digest,
            "expires_at": expires_at, "state": "active", "allocations": {}, "attempts": {}, "grants": {},
            "requests": {}, "profiles": {}, "bindings": {}, "task_seeds": {}}
        now = orders.utcnow()
        session.add(VentureDocument(id=tid, venture_id=venture_id, kind=KIND, title="Synthetic browser qualification",
            provenance_json=json.dumps(trial), created_at=now, updated_at=now))
        session.commit()
        return trial_view(trial)


def trial_view(trial):
    return {key: trial[key] for key in ("schema_version", "trial_id", "workspace_id", "venture_id", "actor_id",
        "device_id", "catalog_digest", "seed_digest", "expires_at", "state")} | totals(trial)


def status(venture_id, caller, trial_id):
    wid, actor, device = orders._scope(venture_id, caller)
    if not enabled(actor):
        raise KeyError("browser_qualification_not_found")
    with orders.db()() as session:
        _device_current(session, actor, device)
        _, trial = _trial(session, trial_id, workspace_id=wid, venture_id=venture_id, actor_id=actor, device_id=device, lock=False)
        return trial_view(trial)


def scope_policy(binding):
    """All immutable authority except the two seal values derived after review."""
    return {key: value for key, value in binding.to_dict().items()
            if key not in {"review_digest", "grant_digest", "binding_digest"}}


def _authority(session, account, body, grant, *, receipt_only=False):
    if (not isinstance(grant, (OwnedFixtureGrant, QualificationTrialBinding))
            or body["review_digest"] != orders._digest(body) or body.get("browser_qualification_scope") != scope_policy(grant)):
        raise ValueError(INVALID)
    grant.assert_current(actor_id=body["actor_id"], device_id=body["device_id"], workspace_id=body["workspace_id"],
        venture_id=body["venture_id"], order_id=body["id"], review_digest=body["review_digest"],
        profile_digest=digest((body["manifest"].get("general_browser_verification") or {}).get("execution_profile")),
        now=0 if receipt_only else int(orders.utcnow().timestamp()))
    row, trial = _trial(session, grant.trial_id, workspace_id=grant.workspace_id, venture_id=grant.venture_id,
        actor_id=grant.actor_id, device_id=grant.device_id)
    saved = trial["grants"] if isinstance(grant, OwnedFixtureGrant) else trial["bindings"]
    if (trial["catalog_digest"] != grant.catalog_digest or trial["task_seeds"].get(grant.task_id) != grant.seed_digest
            or saved.get(grant.order_id) != grant.to_dict() or account is None
            or (not receipt_only and not account_can_spend(session, account, grant.workspace_id, grant.venture_id))
            or grant.expires_at > min(trial["expires_at"], int(datetime.fromisoformat(body["expires_at"]).timestamp()))):
        raise ValueError(INVALID)
    if not receipt_only:
        if not enabled(grant.actor_id) or account.status != "active" or trial["state"] != "active" or int(orders.utcnow().timestamp()) >= trial["expires_at"]:
            raise ValueError(UNAVAILABLE)
        authorize(session, workspace_id=grant.workspace_id, venture_id=grant.venture_id, actor=grant.actor_id, mutate=True)
        _device_current(session, grant.actor_id, grant.device_id)
    return row, trial


def _seal(session, account, body, grant):
    """Persist a producer-derived full grant outside the unchanged order review hash.

    Root's controller produces rules only from pinned seeded fixture metadata;
    this function is not exposed as a caller-authored grant endpoint.
    """
    if not enabled(body["actor_id"]) or body["state"] != "pending" or body["review_digest"] != orders._digest(body):
        raise ValueError(UNAVAILABLE)
    row, trial = _trial(session, grant.trial_id, workspace_id=body["workspace_id"], venture_id=body["venture_id"],
        actor_id=body["actor_id"], device_id=body["device_id"])
    prior_seed = trial["task_seeds"].get(grant.task_id)
    if prior_seed is not None and prior_seed != grant.seed_digest:
        raise ValueError("browser_qualification_task_seed_changed")
    trial["task_seeds"][grant.task_id] = grant.seed_digest
    saved = trial["grants"] if isinstance(grant, OwnedFixtureGrant) else trial["bindings"]
    if grant.order_id in saved and saved[grant.order_id] != grant.to_dict():
        raise ValueError("browser_qualification_grant_changed")
    previous = saved.get(grant.order_id)
    saved[grant.order_id] = grant.to_dict()
    _save(row, trial)
    session.flush()
    _authority(session, account, body, grant)
    if previous is not None:
        return False
    return True


def seal_order_grant(session, account, body, grant: OwnedFixtureGrant):
    if not isinstance(grant, OwnedFixtureGrant):
        raise ValueError(INVALID)
    changed = _seal(session, account, body, grant)
    if changed:
        now = orders.utcnow()
        session.add(VentureDocument(id="bqf_" + grant.nonce, venture_id=grant.venture_id, kind=FIXTURE_KIND,
            title="Isolated synthetic fixture", provenance_json=json.dumps({"trial_id": grant.trial_id,
                "workspace_id": grant.workspace_id, "actor_id": grant.actor_id, "device_id": grant.device_id,
                "order_id": grant.order_id}), created_at=now, updated_at=now))
    return changed


def seal_trial_binding(session, account, body, binding: QualificationTrialBinding):
    if not isinstance(binding, QualificationTrialBinding):
        raise ValueError(INVALID)
    return _seal(session, account, body, binding)


def allocate_order(session, account, body, grant: OwnedFixtureGrant | QualificationTrialBinding):
    """Run inside normal approval's account-first transaction, before its credit hold."""
    row, trial = _authority(session, account, body, grant)
    amount = _amount(body["manifest"]["credit_ceiling_micro"])
    previous = trial["allocations"].get(body["id"])
    if previous:
        if previous["ceiling_micro_usd"] != amount:
            raise ValueError(INVALID)
        return False
    if trial.get("budget_breached") or amount > totals(trial)["remaining_micro_usd"]:
        raise ValueError("browser_qualification_usd_limit")
    trial["allocations"][body["id"]] = {"ceiling_micro_usd": amount, "remaining_micro_usd": amount}
    _save(row, trial)
    return True


def _hold(session, account, body):
    hold = session.get(CreditReservation, body.get("reservation_id"), with_for_update=True)
    if (hold is None or hold.is_pool or hold.account_id != account.id or hold.workspace_id != body["workspace_id"]
            or hold.request_id != body["id"] or hold.purpose != "device.work_order"
            or hold.amount_micro != body["manifest"]["credit_ceiling_micro"] or hold.status != "reconciling"):
        raise ValueError(INVALID)


def reserve_attempt(session, account, body, grant: OwnedFixtureGrant | QualificationTrialBinding, *, invocation_id):
    """Join existing admission atomically before I/O; root's caller commits once."""
    row, trial = _authority(session, account, body, grant)
    _hold(session, account, body)
    if body.get("state") != "claimed" or not body.get("execution_id"):
        raise ValueError("browser_qualification_order_not_executing")
    call = body.get("invocations", {}).get(invocation_id)
    if (not call or call.get("source_kind") != "browser_cuai_text_v1" or call.get("state") != "calling"
            or call.get("profile_digest") != grant.profile_digest or call.get("phase") not in {"plan", "actor", "verify"}):
        raise ValueError(INVALID)
    quote = _amount(call["quote_micro"])
    key = body["id"] + ":" + invocation_id
    previous = trial["attempts"].get(key)
    binding = {"order_id": body["id"], "invocation_id": invocation_id, "request_digest": call["request_digest"],
        "profile_digest": grant.profile_digest, "quote_micro_usd": quote}
    if previous:
        if any(previous[name] != value for name, value in binding.items()):
            raise ValueError(INVALID)
        return False
    allocation = trial["allocations"].get(body["id"])
    if not allocation or trial.get("budget_breached") or quote < 1 or quote > allocation["remaining_micro_usd"]:
        raise ValueError("browser_qualification_usd_limit")
    # Same conversion as normal trusted cost_micro/credit accounting (1e6 per USD).
    # Capture the actual configured profile/rates; do not import QA provider defaults.
    from app.build import device_browser_ai as browser_ai
    from app.llm.pricing import pricing_table
    profile = (body["manifest"]["general_browser_verification"])["execution_profile"]
    checked = browser_ai.assert_configuration(profile)
    roles = (checked.roles.plan, checked.roles.actor, checked.roles.verify)
    providers = {role.provider for role in roles}
    rate_keys = {key for role in roles for key in (role.model, browser_ai.pricing_key(role.model, role.provider))}
    capture = {"provider": next(iter(providers)) if len(providers) == 1 else "mixed",
        "profile": checked.model_dump(), "pricing_digest": browser_ai.pricing_digest(checked),
        "rates": {key: pricing_table()[key] for key in rate_keys}}
    old_capture = trial["profiles"].get(grant.profile_digest)
    if old_capture is not None and old_capture != capture:
        raise ValueError("browser_qualification_pricing_changed")
    trial["profiles"][grant.profile_digest] = capture
    allocation["remaining_micro_usd"] -= quote
    trial["attempts"][key] = binding | {"state": "unknown", "protected_micro_usd": quote}
    _save(row, trial)
    return True


def settle_attempt(session, account, body, grant: OwnedFixtureGrant | QualificationTrialBinding, *, invocation_id):
    """Project trusted existing usage into USD only; never charge credits twice."""
    row, trial = _authority(session, account, body, grant, receipt_only=True)
    key = body["id"] + ":" + invocation_id
    attempt = trial["attempts"].get(key)
    call = body.get("invocations", {}).get(invocation_id)
    if not attempt or not call or call.get("request_digest") != attempt["request_digest"]:
        raise ValueError(INVALID)
    if call.get("unknown_provider_micro"):
        return False
    actual = call.get("provider_actual_micro")
    if call.get("state") != "completed" or type(actual) is not int or actual < 0 or not call.get("usage"):
        return False  # Unknown/absent/malformed usage keeps the entire pre-attempt ceiling.
    if attempt["state"] == "known":
        if attempt["actual_micro_usd"] != actual:
            raise ValueError(INVALID)
        return False
    quote = attempt["quote_micro_usd"]
    if actual > quote or body.get("inference_reconciliation_required"):
        attempt["protected_micro_usd"] = max(quote, actual)
        trial["budget_breached"] = True
    else:
        attempt.update(state="known", actual_micro_usd=actual)
        trial["allocations"][body["id"]]["remaining_micro_usd"] += quote - actual
    _save(row, trial)
    return True


def release_order_allocation(session, account, body, grant: OwnedFixtureGrant | QualificationTrialBinding):
    """Release only unused USD after the existing durable order settlement proof."""
    row, trial = _authority(session, account, body, grant, receipt_only=True)
    if body.get("state") != "stopped" or not body.get("settlement_ledger_id"):
        raise ValueError("browser_qualification_cleanup_unconfirmed")
    allocation = trial["allocations"].get(body["id"])
    if allocation:
        allocation["remaining_micro_usd"] = 0
        _save(row, trial)


def cancel_order_allocation(session, account, body, binding):
    """Release only an unclaimed cancelled order's unused allocation after normal hold cancellation."""
    row, trial = _authority(session, account, body, binding, receipt_only=True)
    if body.get("state") != "cancelled" or body.get("claimed_at") or any(
            attempt["order_id"] == body["id"] for attempt in trial["attempts"].values()):
        raise ValueError("browser_qualification_cancel_requires_reconciliation")
    if body.get("reservation_id"):
        hold = session.get(CreditReservation, body["reservation_id"], with_for_update=True)
        if (hold is None or hold.is_pool or hold.account_id != account.id or hold.request_id != body["id"]
                or hold.workspace_id != body["workspace_id"] or hold.purpose != "device.work_order"
                or hold.status not in {"released", "expired"}):
            raise ValueError(INVALID)
    allocation = trial["allocations"].get(body["id"])
    if allocation:
        allocation["remaining_micro_usd"] = 0
        _save(row, trial)


def request_status(venture_id, caller, trial_id, request_id):
    wid, actor, device = orders._scope(venture_id, caller)
    if not enabled(actor):
        raise KeyError("browser_qualification_not_found")
    with orders.db()() as session:
        _device_current(session, actor, device)
        _, trial = _trial(session, trial_id, workspace_id=wid, venture_id=venture_id, actor_id=actor, device_id=device, lock=False)
        receipt = trial["requests"].get(request_id)
        if receipt is None:
            raise KeyError("browser_qualification_not_found")
        return {key: value for key, value in receipt.items() if not key.startswith("_")}


def completed_rules(grant, receipts, *, safe_read=False):
    """Prerequisites close transitively; transaction proof permits only a following passive read."""
    eligible = set()
    for receipt in receipts:
        rule = next((rule for rule in grant.rules if rule.rule_id == receipt["rule_id"]), None)
        known = receipt.get("server_response_sent") is True
        transaction = safe_read and (receipt["state"] == "committed" and receipt["mutated"] is True
            or receipt["state"] == "rejected_before_mutation" and receipt["status_code"] in {200, 400}
            and receipt["mutated"] is False and rule and rule.rejected_retry_limit == 1)
        if rule and (known or transaction) and (receipt["state"] == "committed"
                or receipt["state"] == "rejected_before_mutation" and receipt["status_code"] in {200, 400}
                and receipt["mutated"] is False and rule.rejected_retry_limit == 1):
            eligible.add(rule.rule_id)
    completed = set()
    for rule in grant.rules:
        if rule.rule_id in eligible and set(rule.prerequisites) <= completed:
            completed.add(rule.rule_id)
    return completed


def _validate_source_artifacts(body,grant,trial,context,notes,current_load=None):
    """Optional native CSV inputs must rejoin the exact delivered source and returned actor input."""
    from app.build.device_browser_ai import SourceNotesQualification, strict_json
    context._source_artifacts_digest=None
    if not notes:
        return
    value=strict_json(notes)
    if not isinstance(value,dict) or not ({'download','failed_download'} & set(value)):
        return
    source=SourceNotesQualification.model_validate(value)
    def read_receipt(request_id,url,status,sha,size,mime,state):
        receipt=trial['requests'].get(request_id)
        rule=next((rule for rule in grant.rules if rule.method=='GET' and url==grant.origin+rule.path
            + ('?'+rule.query if rule.query else '')),None)
        contract=next((item for item in grant.downloads if rule and item.rule_id==rule.rule_id),None)
        if receipt is None or rule is None or contract is None:
            raise ValueError(INVALID)
        expected={key:getattr(grant,key) for key in ('trial_id','order_id','workspace_id','venture_id','actor_id','device_id')}
        expected.update(grant_digest=grant.grant_digest,execution_id=body['execution_id'],request_id=request_id,
            rule_id=rule.rule_id,request_digest=request_binding_digest(grant,rule,request_id),method='GET',
            path=rule.path,query=rule.query,state=state,mutated=False,server_response_sent=True,status_code=status,
            response_digest=sha,response_size_bytes=size,response_mime_type=mime)
        if (any(receipt.get(key)!=item for key,item in expected.items())
                or receipt.get('mutated') is not False or receipt.get('server_response_sent') is not True
                or type(receipt.get('status_code')) is not int or type(receipt.get('response_size_bytes')) is not int):
            raise ValueError(INVALID)
        return contract,rule
    if source.failed_download is not None:
        failed=source.failed_download
        read_receipt(failed.request_id,failed.url,failed.status,failed.sha256,failed.size_bytes,failed.mime_type,'failed_read')
        if (current_load is not None and current_load.qualified is False
                and failed.request_id==current_load.response.request_id and failed.model_dump()!=current_load.response.model_dump()):
            raise ValueError(INVALID)
    if source.download is not None:
        import csv
        import io
        from core.browser_qualification_catalog import fixture_preview
        download=source.download
        contract,rule=read_receipt(download.request_id,download.url,200,download.sha256,download.size_bytes,download.media_type,'committed')
        if (orders.general_browser_read_only(body['manifest']) or 'download' not in grant.allowed_operations
                or (download.rule_id,download.filename,download.media_type,download.size_bytes,download.sha256)
                    != (rule.rule_id,contract.filename,contract.media_type,contract.max_bytes,contract.sha256)):
            raise ValueError(INVALID)
        snapshot=trial['task_states'][grant.task_id]['current']
        response=fixture_preview(snapshot,rule.path.removeprefix(snapshot['base_path']),rule.query)
        if (response.status!=200 or sha256_hex(response.body)!=download.sha256 or len(response.body)!=download.size_bytes
                or response.media_type.split(';',1)[0].strip().lower()!=download.media_type
                or download.rows_excluding_header!=max(0,len(list(csv.reader(io.StringIO(response.body.decode('utf-8')))))-1)):
            raise ValueError(INVALID)
        child=body.get('invocations',{}).get(download.invocation_id,{})
        parent=body.get('invocations',{}).get(child.get('parent_invocation_id'),{})
        effect=child.get('effect') or {}
        if (child.get('source_kind')!='browser_cuai_actor_effect_v1' or child.get('state')!='completed'
                or child.get('no_provider_io') is not True or child.get('profile_digest')!=grant.profile_digest
                or child.get('execution_id')!=body['execution_id'] or child.get('qualification_read_url')!=download.url
                or any(type(child.get(key)) is not int or child[key]!=0 for key in ('actual_micro','provider_actual_micro'))
                or child.get('proposal',{}).get('operation') not in {'click','navigate'}
                or child.get('proposal',{}).get('observation_digest')!=download.observation_digest
                or parent.get('source_kind')!='browser_cuai_text_v1' or parent.get('phase')!='actor'
                or parent.get('state')!='completed' or parent.get('profile_digest')!=grant.profile_digest
                or effect.get('state')!='returned' or effect.get('digest')!=download.effect_digest):
            raise ValueError(INVALID)
    context._source_artifacts_digest=sha256_hex(notes.encode())


def validate_runtime_context(session, account, body, context, *, observation, current_url, current_load=None,source_notes=''):
    """Rejoin untrusted runtime metadata to the current saved, sealed fixture scope.

    The caller holds the normal account/order locks. No runtime completion claim
    authorizes a rule: the normalized context uses only durable sent receipts.
    """
    binding = orders._qualification_binding(body)
    if binding is None or not isinstance(binding, OwnedFixtureGrant):
        if context is not None or current_load is not None and current_load.qualified is not True:
            raise ValueError(INVALID)
        if binding is not None:
            _authority(session, account, body, binding)
        return None
    if context is None:
        raise ValueError(INVALID)
    grant = binding
    _, trial = _authority(session, account, body, grant)
    from app.build import device_invocations as calls
    calls._check_executing(session, body["venture_id"], body, account, orders.utcnow())
    if trial.get("budget_breached"):
        raise ValueError(UNAVAILABLE)
    if context.grant_digest != grant.grant_digest or current_url != observation.location:
        raise ValueError(INVALID)
    receipts = [row for row in trial["requests"].values() if row["order_id"] == body["id"]]
    if any(row["state"] in {"pending", "unknown"} or row.get("server_response_sent") is not True
           for row in receipts):
        raise ValueError("browser_qualification_request_protected")
    _validate_source_artifacts(body,grant,trial,context,source_notes,current_load)
    completed = completed_rules(grant, receipts)
    if (len(set(context.completed_rules)) != len(context.completed_rules)
            or not set(context.completed_rules) <= completed):
        raise ValueError(INVALID)
    context.completed_rules = sorted(completed)

    def read_rule(url):
        try:
            return grant.request_rule(url=url, method="GET", completed_rules=completed)
        except ValueError:
            raise ValueError(INVALID) from None

    current = read_rule(current_url)
    recovery = current_load is not None and current_load.qualified is not True
    if recovery:
        response = current_load.response
        receipt = trial["requests"].get(response.request_id)
        source_rule = next((rule for rule in grant.rules if receipt and rule.rule_id == receipt.get("rule_id")), None)
        failed_read = source_rule is not None and source_rule.method == "GET" and receipt.get("state") == "failed_read"
        rejected_post = (source_rule is not None and source_rule.method == "POST" and source_rule.rejected_retry_limit == 1
            and receipt.get("state") == "rejected_before_mutation" and response.status == 400)
        if not (failed_read or rejected_post):
            raise ValueError(INVALID)
        expected = {"grant_digest": grant.grant_digest, "trial_id": grant.trial_id, "order_id": body["id"],
            "execution_id": body["execution_id"], "workspace_id": grant.workspace_id, "venture_id": grant.venture_id,
            "actor_id": grant.actor_id, "device_id": grant.device_id, "rule_id": source_rule.rule_id,
            "request_id": response.request_id, "request_digest": request_binding_digest(grant, source_rule, response.request_id),
            "method": source_rule.method, "path": source_rule.path, "query": source_rule.query,
            "mutated": False, "server_response_sent": True, "status_code": response.status,
            "response_digest": response.sha256, "response_size_bytes": response.size_bytes,
            "response_mime_type": response.mime_type}
        if (getattr(current_load, "kind", None) != "qualification_error" or current_load.qualified is not False
                or receipt is None or any(receipt.get(key) != value for key, value in expected.items())
                or not 400 <= response.status <= 599 or response.complete is not True
                or response.sequence != current_load.sequence
                or current_load.location != current_url or response.url != current_url
                or current_url != grant.origin + source_rule.path + ("?" + source_rule.query if source_rule.query else "")
                or current_load.observation_digest != observation.digest()
                or response.mime_type != response.mime_type.split(";", 1)[0].strip().lower()
                or context.controls or context.links or context.frames or observation.controls or observation.reviewed_text):
            raise ValueError(INVALID)
        if rejected_post:
            _effect_current(body, grant, invocation_id=receipt.get("invocation_id"), execution_id=body["execution_id"],
                effect_digest=receipt.get("effect_digest"), expected={key: expected[key] for key in (
                    "grant_digest", "rule_id", "request_digest", "request_id")}, known_receipt=True)
        # A known delivered failed read permits only bounded recovery. It never
        # becomes a successful page, prerequisite, completion, or effect grant.
    context._recovery_only = recovery
    eligible = [rule for rule in grant.rules if set(rule.prerequisites) <= completed
        and sum(row["rule_id"] == rule.rule_id for row in receipts) < rule.max_requests
        and (not rule.max_commits or sum(row["rule_id"] == rule.rule_id and row["mutated"] for row in receipts) < rule.max_commits)]
    observed = {control.id: control for control in observation.controls}
    if (len({control.id for control in context.controls}) != len(context.controls)
            or {control.id for control in context.controls} != set(observed)):
        raise ValueError(INVALID)
    source_ids = set()
    for control in context.controls:
        target = observed[control.id]
        document = [rule for rule in grant.rules if (rule.document_path, rule.document_id) ==
            (control.document_path, control.document_id)]
        if (not target.visible or not target.enabled or not document
                or control.document_path != current.document_path and not any(
                    frame.document_path == control.document_path and frame.parent_document_path == current.document_path
                    for frame in grant.frames)):
            raise ValueError(INVALID)
        identity = (control.document_path, control.document_id, control.container_id, control.control_id)
        if control.control_id and identity in source_ids:
            raise ValueError(INVALID)
        source_ids.add(identity)
        rules = [rule for rule in eligible if (rule.document_path, rule.document_id, rule.form_id) ==
            (control.document_path, control.document_id, control.container_id)]
        fields = [field for rule in rules for field in rule.fields if field.control_id == control.control_id]
        if control.kind is not None or control.input_value is not None or control.select_value is not None:
            matching = []
            for field in fields:
                select_value = field.value if field.kind in {"select", "radio", "checkbox"} else None
                if (control.kind, control.input_value, control.select_value) != (field.kind, field.input_value, select_value):
                    continue
                if field.kind == "select":
                    valid = control.tag == "SELECT" and control.input_type == "select-one" and "select" in grant.allowed_operations
                elif field.kind in {"radio", "checkbox"}:
                    valid = control.tag == "INPUT" and control.input_type == field.kind and "check" in grant.allowed_operations
                else:
                    valid = (field.input_value is not None and control.tag == ("TEXTAREA" if field.input_type == "textarea" else "INPUT")
                        and control.input_type == field.input_type and "type" in grant.allowed_operations)
                if valid:
                    matching.append(field)
            if not matching:
                raise ValueError(INVALID)
            allowed = {"click"} if control.kind in {"radio", "checkbox"} else {"type"}
            if not set(target.operations) <= allowed:
                raise ValueError(INVALID)
        elif "type" in target.operations or control.tag in {"INPUT", "TEXTAREA", "SELECT"}:
            raise ValueError(INVALID)
        elif control.container_id and control.tag == "BUTTON":
            if ("click" not in grant.allowed_operations or not any(rule.control_id == control.control_id for rule in rules)
                    or control.input_type not in {"button", "submit", None}):
                raise ValueError(INVALID)
        elif control.tag not in {"A", "BUTTON", "SUMMARY", "DETAILS", "DIV", "SPAN", "TD", "TR", "LABEL"}:
            raise ValueError(INVALID)
    values = list(dict.fromkeys(control.select_value if control.kind == "select" else control.input_value
        for control in context.controls if "type" in observed[control.id].operations))
    if observation.reviewed_text != values:
        raise ValueError(INVALID)
    if len(context.tabs) > grant.max_tabs or len({tab.id for tab in context.tabs}) != len(context.tabs):
        raise ValueError(INVALID)
    for tab in context.tabs:
        read_rule(tab.url)
    if (len({(frame.document_path, frame.parent_document_path, frame.frame_id) for frame in context.frames}) != len(context.frames)
            or any(frame.model_dump() not in [asdict(saved) for saved in grant.frames] for frame in context.frames)):
        raise ValueError(INVALID)
    if len({link.id for link in context.links}) != len(context.links):
        raise ValueError(INVALID)
    for link in context.links:
        control = next((control for control in context.controls if control.id == link.id), None)
        if control is None or control.tag != "A" or "click" not in observed[link.id].operations:
            raise ValueError(INVALID)
        rule = read_rule(link.href)
        if rule not in eligible:
            raise ValueError(INVALID)
    context._eligible_rule_ids = tuple(rule.rule_id for rule in eligible if not recovery or rule.method == "GET")
    return grant


def order_status(venture_id, caller, trial_id, order_id):
    from app.build import device_invocations as calls
    with orders.db()() as session:
        _, body, account = calls._locked(session, venture_id, caller, order_id)
        grant = OwnedFixtureGrant.from_dict(body.get("browser_qualification_grant"))
        if grant.trial_id != trial_id:
            raise KeyError("browser_qualification_not_found")
        _, trial = _authority(session, account, body, grant)
        calls._check_executing(session, venture_id, body, account, orders.utcnow())
        rows = [value for value in trial["requests"].values() if value["order_id"] == order_id]
        completed = completed_rules(grant, rows)
        return {key: getattr(grant, key) for key in ("actor_id", "device_id", "workspace_id", "venture_id", "trial_id",
            "order_id", "catalog_digest", "task_id", "seed_digest", "review_digest", "profile_digest", "expires_at")} | {
            "grant_digest": grant.grant_digest, "completed_rules": sorted(completed),
            "read_prerequisites": sorted(completed_rules(grant, rows, safe_read=True)), "rules": {
                rule.rule_id: {"request_count": sum(row["rule_id"] == rule.rule_id for row in rows),
                    "committed_count": sum(row["rule_id"] == rule.rule_id and row["mutated"] for row in rows),
                    "protected": any(row["rule_id"] == rule.rule_id and (row["state"] in {"pending", "unknown"}
                        or row.get("server_response_sent") is not True) for row in rows)} for rule in grant.rules}}


def _effect_current(body, grant, *, invocation_id, execution_id, effect_digest, expected, known_receipt=False):
    call = body.get("invocations", {}).get(invocation_id)
    effect = call.get("effect") if call else None
    parent = body.get("invocations", {}).get(call.get("parent_invocation_id")) if call else None
    if (execution_id != body.get("execution_id") or not call or call.get("state") != "completed"
            or call.get("source_kind") != "browser_cuai_actor_effect_v1" or not parent
            or not known_receipt and call.get("parent_invocation_id") != body.get("browser_ai_latest_invocation_id")
            or parent.get("state") != "completed" or parent.get("phase") != "actor"
            or parent.get("source_kind") != "browser_cuai_text_v1" or parent.get("profile_digest") != grant.profile_digest
            or call.get("profile_digest") != grant.profile_digest or not effect
            or effect.get("state") not in ({"admitted", "returned"} if known_receipt else {"admitted"})
            or effect.get("digest") != effect_digest or effect.get("qualification") != expected):
        raise ValueError("browser_qualification_effect_unavailable")


def _task_entry(trial, task_id):
    """One controller-created seed/fact snapshot per task, never a local reset."""
    from core.browser_qualification_catalog import CATALOG_DIGEST, catalog_task, new_fixture_state
    catalog_task(task_id)
    if trial["catalog_digest"] != CATALOG_DIGEST:
        raise ValueError("browser_qualification_catalog_changed")
    entry = trial.setdefault("task_states", {}).get(task_id)
    if entry is None:
        nonce = secrets.token_hex(24)
        snapshot = new_fixture_state(task_id, secrets.token_hex(8), FIXTURE_PREFIX + nonce)
        entry = {"nonce": nonce, "initial": snapshot, "current": json.loads(json.dumps(snapshot))}
        trial["task_states"][task_id] = entry
        prior = trial["task_seeds"].get(task_id)
        if prior is not None and prior != snapshot["seed_digest"]:
            raise ValueError("browser_qualification_task_seed_changed")
        trial["task_seeds"][task_id] = snapshot["seed_digest"]
    return entry


def _selected_trial(session, account, body, spec):
    if not isinstance(spec, dict) or set(spec) != {"trial_id", "task_id"}:
        raise ValueError(INVALID)
    if any(not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None for value in spec.values()):
        raise ValueError(INVALID)
    if account is None or account.status != "active" or not enabled(body["actor_id"]):
        raise ValueError(UNAVAILABLE)
    _device_current(session, body["actor_id"], body["device_id"])
    authorize(session, workspace_id=body["workspace_id"], venture_id=body["venture_id"], actor=body["actor_id"], mutate=True)
    row, trial = _trial(session, spec["trial_id"], workspace_id=body["workspace_id"], venture_id=body["venture_id"],
        actor_id=body["actor_id"], device_id=body["device_id"])
    if trial["state"] != "active" or int(orders.utcnow().timestamp()) >= trial["expires_at"] or trial.get("budget_breached"):
        raise ValueError(UNAVAILABLE)
    return row, trial


def prepare_order_scope(session, account, body, qualification_spec):
    """Normal prepare hook BEFORE its unchanged hash; caller already holds account/venture locks.

    Returns a typed draft with an intentionally unset review seal. The draft
    never leaves the controller or grants execution. Root's normal prepare is
    the only caller and hashes ``browser_qualification_scope`` before sealing.
    """
    from core.browser_qualification_catalog import fixture_contract
    row, trial = _selected_trial(session, account, body, qualification_spec)
    task_id = qualification_spec["task_id"]
    entry = _task_entry(trial, task_id)
    contract = fixture_contract(entry["initial"])
    previous_id = trial.setdefault("task_orders", {}).get(task_id)
    if previous_id:
        previous = session.get(VentureDocument, previous_id, with_for_update=True)
        saved = json.loads(previous.provenance_json or "{}") if previous else None
        if (saved and saved.get("state") == "pending" and saved.get("actor_id") == body["actor_id"]
                and saved.get("device_id") == body["device_id"] and saved.get("manifest") == body["manifest"]
                and saved.get("task_snapshot") == body.get("task_snapshot")
                and saved.get("review_digest") == orders._digest(saved)):
            return {"existing_order": saved}
        raise ValueError("browser_qualification_task_already_prepared")
    identity = {key: body[key] for key in ("actor_id", "device_id", "workspace_id", "venture_id")}
    identity.update(schema_version=1, trial_id=trial["trial_id"], catalog_digest=trial["catalog_digest"], task_id=task_id,
        seed_digest=entry["initial"]["seed_digest"], order_id=body["id"], review_digest="0" * 64,
        profile_digest=digest(body["manifest"]["general_browser_verification"]["execution_profile"]),
        expires_at=min(trial["expires_at"], int(datetime.fromisoformat(body["expires_at"]).timestamp())))
    if contract["fixture_supported"]:
        draft = OwnedFixtureGrant(**identity, nonce=entry["nonce"], origin=FIXTURE_ORIGIN,
            rules=tuple(contract["rules"]), allowed_operations=tuple(contract["allowed_operations"]),
            max_tabs=contract["max_tabs"], frames=tuple(contract["frames"]), downloads=tuple(contract["downloads"]))
    else:
        draft = QualificationTrialBinding(**identity)
    body["browser_qualification_scope"] = scope_policy(draft)
    _save(row, trial)
    return draft


def seal_prepared_order(session, account, body, draft):
    """Normal prepare hook AFTER review hashing and BEFORE its single commit."""
    if not isinstance(draft, (OwnedFixtureGrant, QualificationTrialBinding)):
        raise ValueError(INVALID)
    sealed = replace(draft, review_digest=body["review_digest"])
    if isinstance(sealed, OwnedFixtureGrant):
        seal_order_grant(session, account, body, sealed)
        body["browser_qualification_grant"] = sealed.to_dict()
    else:
        seal_trial_binding(session, account, body, sealed)
        body["browser_qualification_trial_binding"] = sealed.to_dict()
    row, trial = _trial(session, sealed.trial_id, workspace_id=sealed.workspace_id, venture_id=sealed.venture_id,
        actor_id=sealed.actor_id, device_id=sealed.device_id)
    trial.setdefault("task_orders", {})[sealed.task_id] = body["id"]
    _save(row, trial)
    return sealed


def prepare_task(venture_id, caller, trial_id, task_id):
    """Internal producer: choose catalog data, then create one normal pending review.

    No endpoint accepts rules, seeds, credentials or grant values. Approval and
    every execution step remain the existing founder-click/order lifecycle.
    """
    from app.build import device_browser_ai as browser_ai
    from core.browser_qualification_catalog import catalog_task, fixture_contract, fixture_goal
    wid, actor, device = orders._scope(venture_id, caller)
    selected = {"trial_id": trial_id, "task_id": task_id}
    with orders.db()() as session:
        account = device_budget.lock_account(session, wid, venture_id)
        orders._lock_venture(session, wid, venture_id)
        row, trial = _selected_trial(session, account, {"workspace_id": wid, "venture_id": venture_id,
            "actor_id": actor, "device_id": device}, selected)
        previous_id = trial.get("task_orders", {}).get(task_id)
        if previous_id:
            _, existing = orders._saved_order(session, venture_id, previous_id, wid, actor, device, lock=True)
            if existing["state"] == "pending" and existing["review_digest"] == orders._digest(existing):
                browser_ai.assert_configuration(existing["manifest"]["general_browser_verification"]["execution_profile"])
                field = "browser_qualification_grant" if "browser_qualification_grant" in existing else "browser_qualification_trial_binding"
                binding = (OwnedFixtureGrant if field.endswith("grant") else QualificationTrialBinding).from_dict(existing[field])
                _authority(session, account, existing, binding)
                return existing
            raise ValueError("browser_qualification_task_already_prepared")
        entry = _task_entry(trial, task_id)
        task = catalog_task(task_id)
        contract = fixture_contract(entry["initial"])
        goal = fixture_goal(entry["initial"])
        _save(row, trial)
        session.commit()
    try:
        profile = orders.BrowserAIProfile.model_validate(browser_ai.strict_json(os.getenv("DEVICE_BROWSER_CUAI_PROFILE_JSON", "")))
        browser_ai.assert_configuration(profile)
    except (ValueError, TypeError):
        raise ValueError(UNAVAILABLE) from None
    owned = contract["fixture_supported"]
    origins = [FIXTURE_ORIGIN] if owned else ["https://pypi.org", "https://github.com", "https://files.pythonhosted.org"]
    start_url = contract["start_url"]
    interacts = owned and bool(set(contract["allowed_operations"]) - {"navigate", "scroll", "extract", "wait"})
    title = f"Synthetic fixture qualification · {task_id}" if owned else f"Public source qualification · {task_id}"
    manifest = orders.Manifest(task_id=f"founder_browser_{venture_id}", title=title,
        instruction=goal, applications=["com.google.Chrome"], origins=origins,
        actions=["browser.read", "browser.interact"] if interacts else ["browser.read"], credit_ceiling_micro=500_000,
        duration_seconds=task.max_seconds, general_browser_verification={"operation": "general_goal_v1", "start_url": start_url,
            "max_steps": task.max_steps, "allow_type": owned and any(field.input_value is not None
                for rule in contract["rules"] for field in rule.fields), "fill_values": {},
            "read_urls": list(contract.get("read_urls", ())), "execution_profile": profile.model_dump()})
    # Root adds this controller-only optional argument to normal prepare. No alternate order engine.
    return orders.prepare(venture_id, caller, manifest, qualification_spec=selected)


def receive_fixture_request(nonce, *, path, query, method, submitted_fields=(), body_kind=None, raw_body_digest=None, request_id=None,
                            invocation_id=None, effect_digest=None, execution_id=None):
    """Only a saved current grant may read/mutate isolated synthetic fixture state.

    An effect proposal/header does not create authority. POST joins the already
    admitted Core effect and exact request scope; duplicates never mutate again.
    """
    if not isinstance(nonce, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", nonce) is None:
        raise KeyError("browser_qualification_not_found")
    from app.build import device_invocations as calls
    from core.browser_qualification_catalog import fixture_transition
    with orders.db()() as session:
        routing = session.get(VentureDocument, "bqf_" + nonce)
        if routing is None or routing.kind != FIXTURE_KIND:
            raise KeyError("browser_qualification_not_found")
        lookup = json.loads(routing.provenance_json)
        account = device_budget.lock_account(session, lookup["workspace_id"], routing.venture_id)
        orders._lock_venture(session, lookup["workspace_id"], routing.venture_id)
        order_row, body = orders._saved_order(session, routing.venture_id, lookup["order_id"],
            lookup["workspace_id"], lookup["actor_id"], lookup["device_id"], lock=True)
        trial_row, trial = _trial(session, lookup["trial_id"], workspace_id=lookup["workspace_id"],
            venture_id=routing.venture_id, actor_id=lookup["actor_id"], device_id=lookup["device_id"])
        grant = OwnedFixtureGrant.from_dict(trial["grants"].get(body["id"]))
        _authority(session, account, body, grant)
        calls._check_executing(session, routing.venture_id, body, account, orders.utcnow())
        completed = completed_rules(grant, [receipt for receipt in trial["requests"].values()
            if receipt["order_id"] == body["id"]], safe_read=method in {"GET", "HEAD"})
        rule = grant.request_rule(url=FIXTURE_ORIGIN + path + ("?" + query if query else ""), method=method,
            submitted_fields=submitted_fields, completed_rules=completed)
        if method == "POST" and body_kind != rule.body_kind:
            raise ValueError(INVALID)
        prior = [receipt for receipt in trial["requests"].values() if receipt["order_id"] == body["id"] and receipt["rule_id"] == rule.rule_id]
        if len(prior) >= rule.max_requests or any(receipt["state"] in {"pending", "unknown"} for receipt in prior):
            raise ValueError("browser_qualification_request_protected")
        if method == "POST":
            if not isinstance(request_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id) is None:
                raise ValueError(INVALID)
            if request_id in trial["requests"]:
                raise ValueError("browser_qualification_request_already_attempted")
            expected = {"grant_digest": grant.grant_digest, "rule_id": rule.rule_id,
                        "request_digest": request_binding_digest(grant, rule, request_id), "request_id": request_id}
            _effect_current(body, grant, invocation_id=invocation_id, execution_id=execution_id,
                            effect_digest=effect_digest, expected=expected)
            if any(receipt.get("effect_digest") == effect_digest for receipt in trial["requests"].values()):
                raise ValueError("browser_qualification_effect_unavailable")
            if not rule.max_commits and not rule.rejected_retry_limit:
                raise ValueError(INVALID)
        else:
            request_id = new_id("bqr")
            expected = {"grant_digest": grant.grant_digest, "rule_id": rule.rule_id,
                        "request_digest": request_binding_digest(grant, rule, request_id), "request_id": request_id}
        receipt = expected | {"trial_id": grant.trial_id, "order_id": body["id"], "execution_id": body["execution_id"],
            "workspace_id": grant.workspace_id, "venture_id": grant.venture_id, "actor_id": grant.actor_id, "device_id": grant.device_id,
            "invocation_id": invocation_id, "effect_digest": effect_digest, "method": method, "path": path, "query": query,
            "body_digest": raw_body_digest or digest(submitted_fields), "state": "pending", "status_code": None,
            "mutated": False, "server_response_sent": False}
        trial["requests"][request_id] = receipt
        _save(trial_row, trial)
        # Persist one-use custody before the handler. Crash/error after delivery
        # leaves a protected pending request even if no response reached native.
        session.commit()
        account = device_budget.lock_account(session, lookup["workspace_id"], routing.venture_id)
        orders._lock_venture(session, lookup["workspace_id"], routing.venture_id)
        _, body = orders._saved_order(session, routing.venture_id, lookup["order_id"],
            lookup["workspace_id"], lookup["actor_id"], lookup["device_id"], lock=True)
        trial_row, trial = _authority(session, account, body, grant)
        calls._check_executing(session, routing.venture_id, body, account, orders.utcnow())
        receipt = trial["requests"][request_id]
        if method == "POST":
            try:
                _effect_current(body, grant, invocation_id=invocation_id, execution_id=execution_id,
                                effect_digest=effect_digest, expected=expected)
            except ValueError as exc:
                receipt.update(state="unknown", status_code=409, error_code="browser_qualification_effect_retired")
                _save(trial_row, trial)
                session.commit()
                raise ValueError("browser_qualification_effect_retired") from exc
        try:
            entry = trial["task_states"][grant.task_id]
            transition = fixture_transition(entry["current"], rule,
                dict(submitted_fields) if method == "POST" else None)
            result = transition.response
        except Exception as exc:
            receipt.update(state="unknown", status_code=500, handler_error=True)
            _save(trial_row, trial)
            session.commit()
            raise ValueError("browser_qualification_fixture_failed") from exc
        mutated = result.state_change == "commit"
        if mutated and sum(item["mutated"] for item in prior) >= rule.max_commits:
            raise ValueError("browser_qualification_commit_limit")
        if result.state_change == "rejected_before_mutation" and not rule.rejected_retry_limit:
            raise ValueError("browser_qualification_rejection_unreviewed")
        # Pending admission released the lock; order only the applied source
        # transitions here, under the reacquired account/venture lock.
        source_sequence = 1 + sum(item["order_id"] == body["id"] and item["state"] in {
            "committed", "rejected_before_mutation", "failed_read"} for item in trial["requests"].values())
        receipt.update(state="failed_read" if method in {"GET", "HEAD"} and 400 <= result.status <= 599 else
                       "committed" if mutated or method in {"GET", "HEAD"} else "rejected_before_mutation",
                       status_code=result.status, mutated=mutated, response_digest=sha256_hex(result.body),
                       response_size_bytes=len(result.body), response_mime_type=result.media_type.split(";", 1)[0].strip().lower(),
                       source_sequence=source_sequence)
        if transition.request_metadata:
            receipt["_source_request_id"] = transition.request_metadata["request_id"]
        entry["current"] = transition.state
        _save(trial_row, trial)
        session.commit()
        return result, request_id


def record_response_delivery(nonce, request_id):
    """ASGI body-send completion only; native outcome and effect receipts remain separate."""
    with orders.db()() as session:
        routing = session.get(VentureDocument, "bqf_" + nonce)
        if routing is None or routing.kind != FIXTURE_KIND:
            return
        lookup = json.loads(routing.provenance_json)
        device_budget.lock_account(session, lookup["workspace_id"], routing.venture_id)
        orders._lock_venture(session, lookup["workspace_id"], routing.venture_id)
        row, trial = _trial(session, lookup["trial_id"], workspace_id=lookup["workspace_id"], venture_id=routing.venture_id,
            actor_id=lookup["actor_id"], device_id=lookup["device_id"])
        receipt = trial["requests"].get(request_id)
        if receipt and receipt["state"] in {"committed", "rejected_before_mutation", "failed_read"}:
            receipt["server_response_sent"] = True
            entry = trial.get("task_states", {}).get(next((key for key, order in trial.get("task_orders", {}).items()
                if order == receipt["order_id"]), ""))
            if entry and receipt["method"] == "POST":
                post_receipts = entry["current"]["state"].get("post_receipts", [])
                source = next((item for item in post_receipts if item["request_id"] == receipt.get("_source_request_id")), None)
                if source:
                    source["response"] = {"status": receipt["status_code"], "size_bytes": receipt["response_size_bytes"],
                        "sha256": receipt["response_digest"], "complete": True}
            _save(row, trial)
            session.commit()
