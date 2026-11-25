"""Distinct model/intent expiring run credentials; never provider credentials."""
import hashlib
import hmac
import json
import secrets
from datetime import timedelta

from sqlalchemy import select

from app.builder.model_proxy import BuildModelError, _authority, _fence, _locked
from app.connectors.secrets import decrypt_at_rest, encrypt_at_rest
from app.credits._txn import naive_utc
from app.db.supymem_models import BuildRun
from app.state import db


def issue_model_credential(run_id, *, generation, controller_id, attempt):
    return _issue(run_id, audience="model", generation=generation, controller_id=controller_id, attempt=attempt)


def issue_intent_credential(run_id, *, generation, controller_id, attempt):
    return _issue(run_id, audience="intent", generation=generation, controller_id=controller_id, attempt=attempt)


def _issue(run_id, *, audience, generation, controller_id, attempt):
    """Trusted controller only. A replay gets the same encrypted stored token."""
    with db()() as session:
        ctx = _locked(session, run_id)
        spec = _authority(session, ctx)
        if spec.get("builder_admission_closed_at"):
            raise BuildModelError("build_builder_admission_closed")
        # Credentials may be minted before the task exists; authenticated model
        # execution still independently requires a running, attested task.
        _fence(ctx, spec, generation, controller_id, attempt, provisioning=True)
        prior = spec.get(audience + "_credential", {})
        if prior.get("generation") == generation and prior.get("token_ref"):
            return decrypt_at_rest(prior["token_ref"])
        token = "b" + audience + "_" + secrets.token_urlsafe(32)
        expires = min(naive_utc(ctx.pool.expires_at), naive_utc() + timedelta(hours=1))
        spec[audience + "_credential"] = {"generation": generation, "audience": "build." + audience, "expires_at": expires.isoformat(),
            "hash": hashlib.sha256(token.encode()).hexdigest(), "token_ref": encrypt_at_rest(token)}
        ctx.run.spec = json.dumps(spec, sort_keys=True)
        session.commit()
        return token


def authenticate_model(run_id, authorization):
    return _authenticate(run_id, authorization, audience="model")


def authenticate_intent(run_id, authorization):
    return _authenticate(run_id, authorization, audience="intent")


def _authenticate(run_id, authorization, *, audience):
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix) or not 20 <= len(authorization) <= 200:
        raise BuildModelError("build_" + audience + "_credential_invalid")
    token_hash = hashlib.sha256(authorization[len(prefix):].encode()).hexdigest()
    with db()() as session:
        run = session.scalar(select(BuildRun).where(BuildRun.id == run_id, BuildRun.runtime_version == 1))
        if not run:
            raise BuildModelError("build_" + audience + "_credential_invalid")
        spec = json.loads(run.spec or "{}")
        credential = spec.get(audience + "_credential", {})
        expected = credential.get("hash", "")
        if not expected or not hmac.compare_digest(expected, token_hash):
            raise BuildModelError("build_" + audience + "_credential_invalid")
        from datetime import datetime
        try:
            expires = datetime.fromisoformat(credential["expires_at"])
        except (KeyError, ValueError):
            raise BuildModelError("build_" + audience + "_credential_invalid") from None
        if credential.get("audience") != "build." + audience or credential.get("generation") != spec.get("generation") or expires <= naive_utc() or spec.get("credentials_revoked") or spec.get("builder_admission_closed_at") or run.closed_at:
            raise BuildModelError("build_" + audience + "_credential_revoked")
        return credential["generation"]
