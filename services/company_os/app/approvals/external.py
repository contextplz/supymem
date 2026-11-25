"""Fail-closed, one-time approvals for real external side effects.

The regular approval workflow records a founder decision.  This module binds
that decision to one rendered external action and consumes it atomically before
the provider is called.  Drafting and dry-run simulation never need this
token; live email, posts, replies, invitations, and provisioning do.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from sqlalchemy import select

from app.audit.writer import audit
from app.core.local_mode import is_local
from app.core.local_capabilities import enabled as capability_enabled, blocked_message
from app.credits.accounts import account_for_workspace
from app.credits.errors import CreditError
from app.credits.policy import get_policy
from app.credits.reserve import Hold, release, reserve, settle
from app.credits.units import usd_to_micro
from app.db.models import Approval, CreditReservation
from app.state import db
from core.clock import as_utc, utcnow


class OutboundExecutionError(RuntimeError):
    """A stable, founder-visible reason no external action was attempted."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code.replace("_", " "))


class DefinitiveProviderFailure(RuntimeError):
    """A provider conclusively rejected a request before it accepted it."""


class ProviderReconnectRequired(DefinitiveProviderFailure):
    """No side effect occurred; the same approved action can resume after OAuth."""


@dataclass(frozen=True)
class OutboundRate:
    action: str
    provider: str
    version: str
    source: str
    amount_micro: int


@dataclass(frozen=True)
class OutboundAttempt:
    approval_id: str
    workspace_id: str
    action: str
    digest: str
    rate: OutboundRate
    hold: Hold


@dataclass(frozen=True)
class OutboundExecution:
    status: str  # succeeded | failed_definitive | recovery_required
    code: str
    provider_result: Any | None = None
    reservation_id: str | None = None


def execution_enabled(action: str = "gtm_email_send") -> bool:
    """Live external execution requires an explicit deployment opt-in.

    Connector credentials or ``CHANNELS_DRY_RUN=false`` must never turn on
    real communication by themselves.
    """

    if is_local():
        return capability_enabled(_action_capability(action))
    return os.environ.get("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _action_capability(action: str) -> str:
    return "send_email" if action in {"gtm_email_send", "gtm_test_email"} else "connected_apps"


def action_digest(action: str, target: dict[str, Any]) -> str:
    """Stable digest of the exact content/recipient/action being approved."""
    target = _email_sender_target(action, target)

    payload = json.dumps(
        {"action": action, "target": target},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _email_sender_target(action: str, target: dict[str, Any], *, workspace_id: str | None = None) -> dict[str, Any]:
    from app.connections import mailboxes
    if action not in {"gtm_email_send", "gtm_test_email"} or not mailboxes.enabled():
        return target
    from app.autopilot import outreach
    from app.db.models import GtmContact, GtmEmailAccount, GtmEmailSendQueue
    with db()() as session:
        if target.get("queue_id"):
            job = session.get(GtmEmailSendQueue, target["queue_id"])
            contact = session.get(GtmContact, job.contact_id) if job else None
            if not job or not contact or workspace_id is not None and contact.workspace_id != workspace_id:
                raise mailboxes.MailboxUnavailable("mailbox_unavailable")
            sender = outreach.draft_mailbox(contact.workspace_id, None, {"contact_id": contact.id,
                "day": job.sequence_day, "mailbox_id": job.email_account_id}, session=session)
        elif action == "gtm_test_email" and target.get("mailbox_id"):
            account = session.get(GtmEmailAccount, target["mailbox_id"])
            if account is None:
                raise mailboxes.MailboxUnavailable("mailbox_unavailable")
            sender = outreach._mailbox(workspace_id or account.workspace_id, mailbox_id=account.id, session=session)
        else:
            return target
        if target.get("sender") is not None and target["sender"] != sender:
            raise mailboxes.MailboxUnavailable("mailbox_binding_changed")
        return target | {"sender": sender}


def outbound_rate(action: str, provider: str) -> OutboundRate:
    """Read an administrator-supplied, auditable outbound provider rate.

    Provider invoices and contract prices are not generally queryable at send
    time.  The platform therefore refuses the action until an administrator
    sets the ``outbound_provider_rates`` credit policy, including the quoted
    price *and* its version/source.  A missing or zero rate is never guessed.
    """

    if is_local() and action == "content_publish" and provider == "smtp":
        raise OutboundExecutionError("outbound_provider_mismatch",
            "Open Connected apps, connect your publishing account, then return to Content and publish your draft.")
    if is_local() and action in {"gtm_email_send", "gtm_test_email", "content_publish"}:
        return OutboundRate(action, provider, "local-smtp" if _action_capability(action) == "send_email" else "local-connection", "Your own connection", 0)
    raw = get_policy("outbound_provider_rates", {})
    if not isinstance(raw, dict):
        raise OutboundExecutionError("outbound_rate_configuration_missing")
    version = raw.get("version")
    rates = raw.get("rates")
    if not isinstance(version, str) or not version.strip() or not isinstance(rates, dict):
        raise OutboundExecutionError("outbound_rate_configuration_missing")
    row = rates.get(f"{action}:{provider}")
    if not isinstance(row, dict):
        raise OutboundExecutionError("outbound_rate_unconfigured")
    if row.get("action") != action or row.get("provider") != provider:
        raise OutboundExecutionError("outbound_rate_configuration_invalid")
    source = row.get("source")
    if not isinstance(source, str) or not source.strip():
        raise OutboundExecutionError("outbound_rate_source_missing")
    if row.get("currency", "USD") != "USD":
        raise OutboundExecutionError("outbound_rate_currency_unsupported")
    try:
        usd = Decimal(str(row.get("price_usd")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OutboundExecutionError("outbound_rate_configuration_invalid") from exc
    if not usd.is_finite() or usd <= 0:
        raise OutboundExecutionError("outbound_rate_must_be_positive")
    try:
        amount_micro = usd_to_micro(usd)
    except (TypeError, ValueError) as exc:
        raise OutboundExecutionError("outbound_rate_configuration_invalid") from exc
    if amount_micro < 1:
        raise OutboundExecutionError("outbound_rate_must_be_positive")
    return OutboundRate(
        action=action,
        provider=provider,
        version=version.strip(),
        source=source.strip(),
        amount_micro=amount_micro,
    )


def outbound_quote(action: str, provider: str) -> dict[str, Any]:
    """Founder-visible status, with no invented price for missing config."""

    try:
        rate = outbound_rate(action, provider)
    except OutboundExecutionError as exc:
        return {
            "credit_estimate_micro": None,
            "credit_status": exc.code,
            "rate_version": None,
            "rate_source": None,
        }
    return {
        "credit_estimate_micro": rate.amount_micro,
        "credit_status": "configured",
        "rate_version": rate.version,
        "rate_source": rate.source,
    }


def _detail(row: Approval) -> dict[str, Any]:
    try:
        detail = json.loads(row.detail_json or "{}")
    except json.JSONDecodeError as exc:
        raise OutboundExecutionError("external_approval_invalid") from exc
    if not isinstance(detail, dict):
        raise OutboundExecutionError("external_approval_invalid")
    return detail


def _validate_row(
    row: Approval | None, *, workspace_id: str, approval_id: str | None, action: str, digest: str
) -> tuple[Approval, dict[str, Any]]:
    if not approval_id:
        raise OutboundExecutionError("external_approval_required")
    if row is None:
        raise OutboundExecutionError("external_approval_not_found")
    if row.workspace_id != workspace_id:
        raise OutboundExecutionError("external_approval_workspace_mismatch")
    if row.kind != "external_comms" or row.status != "approved":
        raise OutboundExecutionError("external_approval_not_approved")
    if row.expires_at and as_utc(row.expires_at) <= utcnow():
        raise OutboundExecutionError("external_approval_expired")
    detail = _detail(row)
    if detail.get("external_action") != action:
        raise OutboundExecutionError("external_approval_action_mismatch")
    if detail.get("execution_digest") != digest:
        raise OutboundExecutionError("external_approval_content_mismatch")
    return row, detail


def _execution_request_id(approval_id: str, digest: str) -> str:
    return f"outbound:{approval_id}:{digest}"


def _find_active_hold(account_id: str, request_id: str) -> Hold | None:
    with db()() as session:
        row = session.scalars(
            select(CreditReservation)
            .where(
                CreditReservation.account_id == account_id,
                CreditReservation.request_id == request_id,
                CreditReservation.status.in_(("active", "open")),
            )
            .order_by(CreditReservation.created_at.desc())
        ).first()
        if row is None:
            return None
        return Hold(reservation_id=row.id, account_id=row.account_id, amount_micro=row.amount_micro)


def _set_execution(approval_id: str, update: dict[str, Any]) -> None:
    with db()() as session:
        row = session.execute(
            select(Approval).where(Approval.id == approval_id).with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise OutboundExecutionError("external_approval_not_found")
        detail = _detail(row)
        execution = detail.get("external_execution")
        if not isinstance(execution, dict):
            execution = {}
        execution.update(update)
        detail["external_execution"] = execution
        row.detail_json = json.dumps(detail, sort_keys=True, default=str)
        session.commit()


def begin_metered_external_execution(
    *, workspace_id: str, approval_id: str | None, action: str, provider: str, target: dict[str, Any]
) -> OutboundAttempt:
    """Validate the exact approval and reserve a priced provider action.

    The approval is marked ``sending`` *before* the provider is called.  A
    crash or timeout therefore cannot be retried into a duplicate message; it
    is surfaced as recovery-required instead.
    """

    if not (execution_enabled(action) if is_local() else execution_enabled()):
        raise OutboundExecutionError("external_execution_disabled",
            blocked_message(_action_capability(action)) if is_local() else None)
    rate = outbound_rate(action, provider)
    quote = target.get("credit_quote")
    if quote is not None and (not isinstance(quote, dict) or quote.get("rate_version") != rate.version
                              or quote.get("credit_estimate_micro") != rate.amount_micro):
        raise OutboundExecutionError("outbound_rate_version_changed")
    if target.get("venture_id"):
        from app.credits.accounts import get_account
        from app.db.models import Venture
        with db()() as session:
            venture = session.get(Venture, target["venture_id"])
            if not venture or venture.workspace_id != workspace_id or venture.archived_at:
                raise OutboundExecutionError("outbound_venture_scope_mismatch")
        account = get_account(venture.id)
        if account is not None and ((account.scope != "person" and (account.scope != "venture" or account.workspace_id != workspace_id or account.venture_id != venture.id))):
            raise OutboundExecutionError("outbound_credit_scope_mismatch")
    else:
        account = account_for_workspace(workspace_id)
    if account is None:
        raise OutboundExecutionError("outbound_credit_account_unavailable")
    digest = action_digest(action, target)
    if not approval_id:
        raise OutboundExecutionError("external_approval_required")
    request_id = _execution_request_id(approval_id, digest)

    with db()() as session:
        row = session.execute(
            select(Approval).where(Approval.id == approval_id).with_for_update()
        ).scalar_one_or_none()
        _, detail = _validate_row(
            row, workspace_id=workspace_id, approval_id=approval_id, action=action, digest=digest
        )
        execution = detail.get("external_execution")
        if isinstance(execution, dict):
            state = str(execution.get("state") or "")
            if state == "succeeded" or detail.get("execution_consumed_at"):
                raise OutboundExecutionError("external_approval_already_consumed")
            if state in {"sending", "unknown"}:
                raise OutboundExecutionError("external_execution_recovery_required")
            if state == "failed_definitive":
                raise OutboundExecutionError("external_approval_requires_new_review")
            if execution.get("rate_version") not in {None, rate.version}:
                raise OutboundExecutionError("outbound_rate_version_changed")
        else:
            execution = {}
        execution.update(
            {
                "state": "reserving",
                "request_id": request_id,
                "rate_version": rate.version,
                "rate_source": rate.source,
                "amount_micro": rate.amount_micro,
                "provider": provider,
                "started_at": utcnow().isoformat(),
            }
        )
        detail["external_execution"] = execution
        row.detail_json = json.dumps(detail, sort_keys=True, default=str)
        session.commit()

    hold = _find_active_hold(account.id, request_id)
    if hold is None:
        try:
            hold = reserve(
                account_id=account.id,
                amount_micro=rate.amount_micro,
                purpose=f"outbound:{action}",
                provider=provider,
                workspace_id=workspace_id,
                request_id=request_id,
            )
        except CreditError as exc:
            _set_execution(approval_id, {"state": "reserve_failed", "error": exc.code})
            raise OutboundExecutionError(exc.code, str(exc)) from exc

    _set_execution(
        approval_id,
        {"state": "sending", "reservation_id": hold.reservation_id, "provider_attempted_at": utcnow().isoformat()},
    )
    from app.credits.reserve import protect_outcome
    protect_outcome(hold)
    return OutboundAttempt(
        approval_id=approval_id,
        workspace_id=workspace_id,
        action=action,
        digest=digest,
        rate=rate,
        hold=hold,
    )


def _finish_success(attempt: OutboundAttempt, provider_result: Any) -> OutboundExecution:
    try:
        settled = settle(attempt.hold, actual_micro=attempt.rate.amount_micro)
    except Exception:  # The provider may have accepted; never retry.
        _set_execution(
            attempt.approval_id,
            {"state": "unknown", "error": "credit_settlement_failed", "recovery_required_at": utcnow().isoformat()},
        )
        audit(
            "outbound.execution_unknown", workspace_id=attempt.workspace_id, actor_id="external_execution",
            actor_kind="system", target_ref=attempt.action, approval_id=attempt.approval_id,
            outcome="error", denied_reason="credit_settlement_failed", payload={"reservation_id": attempt.hold.reservation_id},
        )
        return OutboundExecution("recovery_required", "credit_settlement_failed", reservation_id=attempt.hold.reservation_id)
    _set_execution(
        attempt.approval_id,
        {
            "state": "succeeded",
            "execution_consumed_at": utcnow().isoformat(),
            "settlement_ledger_id": settled.ledger_id,
            "settled_micro": settled.actual_micro,
        },
    )
    # Keep the legacy top-level field so re-review code cannot revive this action.
    with db()() as session:
        row = session.execute(select(Approval).where(Approval.id == attempt.approval_id).with_for_update()).scalar_one()
        detail = _detail(row)
        detail["execution_consumed_at"] = utcnow().isoformat()
        row.detail_json = json.dumps(detail, sort_keys=True, default=str)
        session.commit()
    audit(
        "approval.external_consumed", workspace_id=attempt.workspace_id, actor_id="external_execution",
        actor_kind="system", target_ref=attempt.action, approval_id=attempt.approval_id,
        payload={"execution_digest": attempt.digest, "reservation_id": attempt.hold.reservation_id, "ledger_id": settled.ledger_id, "rate_version": attempt.rate.version},
    )
    return OutboundExecution("succeeded", "approved", provider_result=provider_result, reservation_id=attempt.hold.reservation_id)


def resolve_outreach_execution(
    *, workspace_id: str, venture_id: str, draft_id: str, approval_id: str | None,
    outcome: str, actor_subject: str | None,
) -> None:
    """Finish only the original draft's accounting, without invoking a provider."""
    with db()() as session:
        rows = session.scalars(select(Approval).where(Approval.workspace_id == workspace_id,
            Approval.id == approval_id if approval_id else Approval.kind == "external_comms")).all()
        matches = []
        for row in rows:
            detail = _detail(row)
            target = detail.get("target") or {}
            if (detail.get("external_action") == "gtm_email_send"
                    and target.get("venture_id") == venture_id and target.get("draft_id") == draft_id):
                matches.append((row.id, detail))
        if not matches:
            if approval_id:
                raise OutboundExecutionError("outreach_delivery_approval_mismatch")
            return
        if len(matches) != 1:
            matches = [(ident, detail) for ident, detail in matches
                       if (detail.get("external_execution") or {}).get("state") in {"sending", "unknown", "succeeded"}]
        if len(matches) != 1:
            raise OutboundExecutionError("outreach_delivery_approval_mismatch")
        ident, detail = matches[0]
        execution = detail.get("external_execution") or {}
        attestation = execution.get("delivery_resolution")
        if attestation and attestation.get("outcome") != outcome:
            raise OutboundExecutionError("outreach_delivery_resolution_conflict")
        if execution.get("state") not in {"sending", "unknown", "succeeded", "failed_definitive"}:
            raise OutboundExecutionError("outreach_delivery_not_resolvable")
        if execution.get("state") == "failed_definitive" and not attestation:
            raise OutboundExecutionError("outreach_delivery_not_resolvable")
        if outcome not in {"sent", "not_sent"}:
            raise OutboundExecutionError("outreach_delivery_outcome_invalid")
        reservation = session.get(CreditReservation, execution["reservation_id"]) if execution.get("reservation_id") else None
        if is_local() and execution.get("provider") == "smtp" and not execution.get("reservation_id"):
            from app.credits import local_meter
            if execution.get("amount_micro") != 0 or execution.get("request_id") != _execution_request_id(ident, detail.get("execution_digest")):
                raise OutboundExecutionError("outreach_delivery_credit_mismatch")
            with local_meter._database() as connection:
                receipt = connection.execute("SELECT estimate_usd_micro, cost_usd_micro FROM calls WHERE request_id = ? "
                    "AND workspace_id = ? AND venture_id = ? AND provider = ? AND purpose = ? ORDER BY rowid DESC LIMIT 1",
                    (execution["request_id"], workspace_id, venture_id, "smtp", "outbound:gtm_email_send")).fetchone()
                if receipt is None or receipt["estimate_usd_micro"] != 0 or receipt["cost_usd_micro"] not in {None, 0}:
                    raise OutboundExecutionError("outreach_delivery_credit_mismatch")
            hold = None
        else:
            if (reservation is None or reservation.workspace_id != workspace_id
                    or reservation.request_id != execution.get("request_id")
                    or reservation.provider != "composio_gmail"
                    or reservation.purpose != "outbound:gtm_email_send"):
                raise OutboundExecutionError("outreach_delivery_credit_mismatch")
            hold = Hold(reservation.id, reservation.account_id, reservation.amount_micro)
        amount = execution["amount_micro"]
    attestation = attestation or {"outcome": outcome, "evidence": "founder_attested",
        "actor": actor_subject, "attested_at": utcnow().isoformat()}
    if outcome == "sent":
        settled = settle(hold, actual_micro=amount) if hold else None
        update = {"state": "succeeded", "execution_consumed_at": utcnow().isoformat(),
            "settlement_ledger_id": settled.ledger_id if settled else None, "settled_micro": settled.actual_micro if settled else 0}
    else:
        if execution.get("state") == "succeeded" or reservation and reservation.status == "settled":
            raise OutboundExecutionError("outreach_delivery_already_charged")
        if hold:
            release(hold, definitive=True)
        update = {"state": "failed_definitive", "error": "founder_attested_not_delivered", "failed_at": utcnow().isoformat()}
    update["delivery_resolution"] = attestation
    _set_execution(ident, update)
    if outcome == "sent":
        with db()() as session:
            row = session.get(Approval, ident, with_for_update=True)
            detail = _detail(row)
            detail["execution_consumed_at"] = update["execution_consumed_at"]
            row.detail_json = json.dumps(detail, sort_keys=True, default=str)
            session.commit()
    audit("outbound.delivery_resolved", workspace_id=workspace_id, actor_id=actor_subject,
        actor_kind="person", target_ref=draft_id, approval_id=ident,
        payload={"outcome": outcome, "evidence": "founder_attested", "reservation_id": hold.reservation_id if hold else None})


def execute_metered_external_action(
    *, workspace_id: str, approval_id: str | None, action: str, provider: str,
    target: dict[str, Any], invoke: Callable[[], Any],
) -> OutboundExecution:
    """Reserve → provider → settle/refund, without retrying ambiguous sends."""

    if is_local():
        return _execute_local_action(workspace_id=workspace_id, approval_id=approval_id, action=action,
            provider=provider, target=target, invoke=invoke)

    attempt = begin_metered_external_execution(
        workspace_id=workspace_id, approval_id=approval_id, action=action, provider=provider, target=target
    )
    from app.connections.composio_pacing import AdmissionLimited
    try:
        provider_result = invoke()
    except AdmissionLimited:
        try:
            release(attempt.hold, definitive=True)
        except Exception:
            _set_execution(attempt.approval_id, {"state": "unknown", "error": "credit_release_failed"})
            raise OutboundExecutionError("credit_release_failed")
        _set_execution(attempt.approval_id, {"state": "retry_wait", "error": "composio_rate_limited"})
        raise
    except DefinitiveProviderFailure as exc:
        try:
            release(attempt.hold, definitive=True)
        except Exception:  # failed refund leaves an explicit manual recovery state
            _set_execution(attempt.approval_id, {"state": "unknown", "error": "credit_release_failed"})
            return OutboundExecution("recovery_required", "credit_release_failed", reservation_id=attempt.hold.reservation_id)
        paused = isinstance(exc, ProviderReconnectRequired)
        _set_execution(
            attempt.approval_id,
            {"state": "paused_reconnect" if paused else "failed_definitive", "error": str(exc)[:500], "failed_at": utcnow().isoformat()},
        )
        audit(
            "outbound.execution_failed", workspace_id=attempt.workspace_id, actor_id="external_execution",
            actor_kind="system", target_ref=attempt.action, approval_id=attempt.approval_id,
            outcome="denied", denied_reason="provider_definitive_failure", payload={"reservation_id": attempt.hold.reservation_id},
        )
        return OutboundExecution("paused" if paused else "failed_definitive", str(exc) if paused else "provider_definitive_failure", reservation_id=attempt.hold.reservation_id)
    except Exception as exc:  # Timeout/network outcome may have reached the provider.
        _set_execution(
            attempt.approval_id,
            {"state": "unknown", "error": str(exc)[:500], "recovery_required_at": utcnow().isoformat()},
        )
        audit(
            "outbound.execution_unknown", workspace_id=attempt.workspace_id, actor_id="external_execution",
            actor_kind="system", target_ref=attempt.action, approval_id=attempt.approval_id,
            outcome="error", denied_reason="provider_outcome_unknown", payload={"reservation_id": attempt.hold.reservation_id},
        )
        return OutboundExecution("recovery_required", "provider_outcome_unknown", reservation_id=attempt.hold.reservation_id)
    return _finish_success(attempt, provider_result)


def _execute_local_action(
    *, workspace_id: str, approval_id: str | None, action: str, provider: str,
    target: dict[str, Any], invoke: Callable[[], Any],
) -> OutboundExecution:
    from app.credits import metered
    from app.credits.errors import CreditsUnavailable
    from app.connections.composio_pacing import AdmissionLimited
    from app.db.models import Venture
    from app.db.session import serialize_writes
    from app.gtm.supymem.composio_gmail import DefiniteFailure

    if not execution_enabled(action):
        raise OutboundExecutionError("external_execution_disabled", blocked_message(_action_capability(action)))
    provider = "smtp" if _action_capability(action) == "send_email" else provider
    rate = outbound_rate(action, provider)
    quote = target.get("credit_quote")
    if quote is not None and (not isinstance(quote, dict) or quote.get("rate_version") != rate.version
            or quote.get("credit_estimate_micro") != rate.amount_micro):
        raise OutboundExecutionError("outbound_rate_version_changed")
    digest = action_digest(action, target)
    request_id = _execution_request_id(approval_id, digest)
    with db()() as session:
        serialize_writes(session, key=f"external:{approval_id}")
        row, detail = _validate_row(session.get(Approval, approval_id), workspace_id=workspace_id,
            approval_id=approval_id, action=action, digest=digest)
        if target.get("venture_id"):
            venture = session.get(Venture, target["venture_id"])
            if not venture or venture.workspace_id != workspace_id or venture.archived_at:
                raise OutboundExecutionError("outbound_venture_scope_mismatch")
        execution = detail.get("external_execution") or {}
        if not isinstance(execution, dict):
            raise OutboundExecutionError("external_approval_invalid")
        if execution.get("state") == "succeeded" or detail.get("execution_consumed_at"):
            raise OutboundExecutionError("external_approval_already_consumed")
        if execution.get("state") in {"reserving", "sending", "unknown"}:
            raise OutboundExecutionError("external_execution_recovery_required",
                "Open your Sent mailbox and check this email's delivery before trying again."
                if _action_capability(action) == "send_email" else
                "Open the connected app and check whether your content was published before trying again.")
        if execution.get("state") == "failed_definitive":
            raise OutboundExecutionError("external_approval_requires_new_review")
        detail["external_execution"] = {"state": "reserving", "request_id": request_id,
            "provider": provider, "amount_micro": rate.amount_micro, "rate_version": rate.version,
            "started_at": utcnow().isoformat()}
        row.detail_json = json.dumps(detail, sort_keys=True)
        session.commit()
    attempted = False

    try:
        with metered.charge_sync(workspace_id=workspace_id, venture_id=target.get("venture_id"),
                purpose=f"outbound:{action}", estimate_micro=rate.amount_micro, provider=provider, payer="founder", request_id=request_id) as meter:
            _set_execution(approval_id, {"state": "sending", "provider_attempted_at": utcnow().isoformat()})
            attempted = True
            meter.protect_outcome()
            try:
                result = invoke()
            except (AdmissionLimited, DefinitiveProviderFailure, DefiniteFailure):
                meter.confirm_unspent()
                raise
            meter.record_micro(rate.amount_micro)
    except AdmissionLimited:
        _set_execution(approval_id, {"state": "retry_wait", "error": "composio_rate_limited"})
        raise
    except (DefinitiveProviderFailure, DefiniteFailure) as exc:
        paused = isinstance(exc, ProviderReconnectRequired)
        _set_execution(approval_id, {"state": "paused_reconnect" if paused else "failed_definitive",
            "error": "provider_definitive_failure", "failed_at": utcnow().isoformat()})
        return OutboundExecution("paused" if paused else "failed_definitive", "provider_definitive_failure")
    except CreditsUnavailable as exc:
        if not attempted:
            _set_execution(approval_id, {"state": "reserve_failed", "error": exc.code})
            raise OutboundExecutionError(exc.code, str(exc)) from exc
        _set_execution(approval_id, {"state": "unknown", "error": "provider_outcome_unknown"})
        return OutboundExecution("recovery_required", "provider_outcome_unknown")
    except Exception as exc:
        if not attempted:
            code = getattr(exc, "code", "outbound_credit_account_unavailable")
            _set_execution(approval_id, {"state": "reserve_failed", "error": code})
            message = str(exc) + " Nothing was sent." if isinstance(exc, CreditError) else "Open Keys and connections > Your monthly spend cap and check Monthly limit (USD), then try your reviewed action again. Nothing was sent."
            raise OutboundExecutionError(code, message) from None
        _set_execution(approval_id, {"state": "unknown", "error": "provider_outcome_unknown"})
        return OutboundExecution("recovery_required", "provider_outcome_unknown")
    with db()() as session:
        serialize_writes(session, key=f"external:{approval_id}")
        row = session.get(Approval, approval_id)
        detail = _detail(row)
        detail["execution_consumed_at"] = utcnow().isoformat()
        detail["external_execution"].update(state="succeeded", settled_micro=rate.amount_micro,
            execution_consumed_at=detail["execution_consumed_at"], completed_at=utcnow().isoformat())
        row.detail_json = json.dumps(detail, sort_keys=True)
        session.commit()
    audit("approval.external_consumed", workspace_id=workspace_id, actor_id="external_execution",
        actor_kind="system", target_ref=action, approval_id=approval_id, payload={"execution_digest": digest})
    audit("outbound.execution_succeeded", workspace_id=workspace_id, actor_id="external_execution",
        actor_kind="system", target_ref=action, approval_id=approval_id, outcome="ok", payload={})
    return OutboundExecution("succeeded", "approved", provider_result=result)


def consume_external_approval(
    *,
    workspace_id: str,
    approval_id: str | None,
    action: str,
    target: dict[str, Any],
) -> tuple[bool, str]:
    """Compatibility guard for retired unmetered external execution paths.

    Every provider call must use :func:`execute_metered_external_action`.
    Leaving this helper able to consume an approval would let imported
    reply/invite paths bypass credit reservation and ambiguous-outcome
    recovery, so it deliberately blocks until those paths are migrated.
    """

    return False, "external_execution_requires_metered_executor"


def propose_external_approval(
    *,
    workspace_id: str,
    subject_ref: str,
    action: str,
    target: dict[str, Any],
    summary: str,
    review: dict[str, Any],
    risk: str = "high",
) -> dict[str, Any]:
    """Create/reuse a review from server-resolved action data.

    Callers supply a resource identifier, never a digest.  The resource owner
    resolves the real recipient/copy before this function calculates the
    digest stored with the generic approval record.
    """

    from app.approvals import service

    target = _email_sender_target(action, target, workspace_id=workspace_id)
    digest = action_digest(action, target)
    for status in ("pending", "approved"):
        for existing in service.list_approvals(workspace_id, status=status):
            if existing.kind != "external_comms" or existing.subject_ref != subject_ref:
                continue
            if (
                existing.detail.get("external_action") == action
                and existing.detail.get("execution_digest") == digest
                and not existing.detail.get("execution_consumed_at")
            ):
                state = str((existing.detail.get("external_execution") or {}).get("state") or "")
                # An indeterminate provider outcome is deliberately sticky:
                # a fresh approval must not turn a timeout into a duplicate.
                if state not in {"failed_definitive"}:
                    return existing.to_dict()

    view = service.request_approval(
        workspace_id=workspace_id,
        kind="external_comms",
        subject_ref=subject_ref,
        summary=summary,
        # A workflow agent proposes the action; the founder can therefore make
        # the independent approval through the ordinary role-aware service.
        requested_by="agt_outbound_review",
        risk=risk,
        detail={
            "external_action": action,
            "execution_digest": digest,
            "review": review,
            "target": target,
            "one_time": True,
        },
    )
    return view.to_dict()


def gtm_email_review(workspace_id: str, queue_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a queued email and its exact current copy for founder review."""

    from app.db.models import GtmContact, GtmEmailAccount, GtmEmailSendQueue
    from app.gtm.relationships.suppression import email_suppressed

    with db()() as session:
        job = session.get(GtmEmailSendQueue, queue_id)
        if job is None or job.status != "queued":
            raise ValueError("outbound_queue_not_reviewable")
        account = session.get(GtmEmailAccount, job.email_account_id)
        contact = session.get(GtmContact, job.contact_id)
        from app.autopilot.guard import mailbox_send_max
        from app.gtm.supymem.pipeline import _gmail_workspace_ids
        from app.gtm.supymem.composio_gmail import mailbox_slots_used_today
        if (account is None or contact is None or contact.workspace_id != workspace_id
                or account.workspace_id not in _gmail_workspace_ids(session, workspace_id)):
            raise ValueError("outbound_queue_not_found")
        if email_suppressed(session, workspace_id, contact):
            raise ValueError("outbound_recipient_suppressed")
        from app.connections import mailboxes
        if mailboxes.enabled():
            from app.autopilot import outreach
            sender = outreach.draft_mailbox(workspace_id, None, {"contact_id": contact.id,
                "day": job.sequence_day, "mailbox_id": job.email_account_id}, session=session)
        try:
            sequence = json.loads(contact.sequence_state_json or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("outbound_draft_missing") from exc
        message = sequence.get(str(job.sequence_day))
        if not isinstance(message, dict) or not message.get("subject") or not message.get("body"):
            raise ValueError("outbound_draft_missing")
        from app.gtm.relationships.email_compliance import complete_body
        message["body"] = complete_body(session, workspace_id, contact, message["body"], account.email or "")
        contact.sequence_state_json = json.dumps(sequence)
        session.commit()
        target = {
            "queue_id": job.id,
            "recipient": contact.email or "",
            "sequence_day": job.sequence_day,
            "subject": str(message["subject"]),
            "body": str(message["body"]),
        }
        review = {
            "channel": "email",
            "recipient": target["recipient"],
            "subject": target["subject"],
            "body": target["body"],
            "sequence_day": target["sequence_day"],
            "mailbox": account.email or "",
            "daily_send_limit": mailbox_send_max(session, account),
            "sent_today": mailbox_slots_used_today(session, account),
            **outbound_quote("gtm_email_send", "composio_gmail"),
        }
        if mailboxes.enabled():
            target["sender"] = sender
            review["mailbox_id"] = sender["mailbox_id"]
    return target, review


def local_content_draft_review(workspace_id: str, post_id: str) -> dict[str, Any]:
    from app.content.engine import _post_view
    from app.db.models import ContentPost

    with db()() as session:
        post = session.get(ContentPost, post_id)
        if post is None or post.workspace_id != workspace_id:
            raise ValueError("Open Content in your startup and choose the draft you want to review.")
        if post.state not in {"draft", "refused", "approved", "scheduled", "failed"}:
            raise ValueError("Open Content and choose an unpublished draft to review.")
        return _post_view(post)


def content_post_review(workspace_id: str, post_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve an unpublished content post and exact rendered fields."""

    from app.db.models import ContentAccount, ContentPost

    with db()() as session:
        post = session.get(ContentPost, post_id)
        if post is None or post.workspace_id != workspace_id or post.state not in {"scheduled", "approved"}:
            raise ValueError("content_post_not_reviewable")
        account = session.get(ContentAccount, post.account_id or "")
        if account is None or account.status != "active":
            raise ValueError("content_account_not_active")
        try:
            account_extra = json.loads(account.extra_json or "{}")
        except json.JSONDecodeError:
            account_extra = {}
        provider = (
            f"composio_{post.platform}"
            if isinstance(account_extra, dict) and account_extra.get("auth_via") == "composio"
            else f"native_{post.platform}"
        )
        target = {
            "post_id": post.id,
            "platform": post.platform,
            "target": post.target or "",
            "title": post.title or "",
            "body": post.body or "",
            "visual_path": post.visual_path or "",
        }
        review = {
            "channel": post.platform,
            "destination": post.target or post.platform,
            "title": post.title or "",
            "body": post.body or "",
            "visual_path": post.visual_path or "",
            "provider": provider,
            **outbound_quote("content_publish", provider),
        }
    return target, review


__all__ = [
    "action_digest",
    "consume_external_approval",
    "content_post_review",
    "DefinitiveProviderFailure",
    "execute_metered_external_action",
    "execution_enabled",
    "gtm_email_review",
    "local_content_draft_review",
    "propose_external_approval",
    "OutboundExecutionError",
    "outbound_quote",
    "outbound_rate",
]
