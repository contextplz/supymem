"""Founder-authorized durable landing-page jobs; no provider I/O in HTTP requests."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import zipfile
import zlib
from bisect import bisect_right

from fastapi import HTTPException
from sqlalchemy import func, or_, select

from app.authz.workspace import assert_venture_access, subject_person_id
from app.core.jobs import enqueue, publish_wake
from app.core.local_mode import is_local
from app.db.models import CreditLedgerEntry, CreditReservation, Job, Venture, Workspace
from app.db.supymem_models import IdeaRevision, IdeaSession
from app.state import db

KIND = "pagebuilder.build"
MAX_INPUT_BYTES = 48_000
MAX_PROMPT_TOKENS = 18000
MAX_OUTPUT_TOKENS = 16384  # Includes DeepSeek thinking tokens as well as visible output.
CALLS_PER_VARIANT = 10  # text calls; image calls are quoted separately below.
IMAGE_CALLS_PER_VARIANT = 3  # hero plus up to two section artworks
IMAGE_MAX_PROMPT_TOKENS = 1200
IMAGE_TIMEOUT_SECONDS = 180
IMAGE_MAX_OUTPUT_TOKENS = 4000  # image tokens; high 1536x1024 measured ~1.4K
CALL_TIMEOUT_SECONDS = 300
REQUEST_POLICY = "scoped_grounding_v19"
MAX_PAGE_MICRO = int(os.environ.get("PAGEBUILDER_MAX_PAGE_MICRO", "250000"))  # 25 credits default; fail closed if the configured model exceeds it.
BUILDS_PER_REVISION = 3
ATTEMPTS_PER_REVISION = 8
FAILED_QUEUE = {"failed_dead", "failed", "poison", "cancelled"}


def dump(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


OPENAI_KEY_REF = "env:PAGEBUILDER_OPENAI_API_KEY"


def resolve_route(workspace_id):
    """Founder BYOK first, then the optional landing-only OpenAI route, then the platform default."""
    if is_local():
        from app.pagebuilder.local import route
        return route(workspace_id)
    from app.llm.founder_provider import ResolvedLLMConfig, resolve_llm_config
    config = resolve_llm_config(workspace_id)
    model = os.environ.get("PAGEBUILDER_OPENAI_MODEL", "").strip()
    if config.source == "founder_override" or not model or not os.environ.get("PAGEBUILDER_OPENAI_API_KEY", "").strip():
        return config
    # The OpenAI-compatible route may point at an Azure OpenAI v1 endpoint (same Sol model, platform Azure key).
    base_url = os.environ.get("PAGEBUILDER_OPENAI_BASE_URL", "").strip().rstrip("/") or "https://api.openai.com/v1"
    return ResolvedLLMConfig(workspace_id=workspace_id, provider="openai", base_url=base_url,
        model=model, api_key_ref=OPENAI_KEY_REF, source="platform_default")


def provider_config(resolved):
    """Gateway config for the landing-only OpenAI route; None means use the existing paths."""
    if resolved.api_key_ref != OPENAI_KEY_REF:
        return None
    from pydantic import SecretStr
    from llm_gateway.provider_foundry import ProviderConfig
    return ProviderConfig(provider="openai", base_url=resolved.base_url,
        api_key=SecretStr(os.environ["PAGEBUILDER_OPENAI_API_KEY"].strip()), deployment_pro=resolved.model,
        deployment_flash=resolved.model, reasoning_effort="", source="platform_default")


def image_quote(resolved, text_ceiling_micro):
    """Optional artwork on the platform's landing-only OpenAI route; None keeps native SVG art."""
    from app.credits.estimate import _lookup_model_row, estimate_llm_micro
    model = os.environ.get("PAGEBUILDER_IMAGE_MODEL", "gpt-image-2.5-flare").strip()
    if (resolved.api_key_ref != OPENAI_KEY_REF or not model or os.environ.get("PAGEBUILDER_IMAGES", "1") == "0"
            or not _lookup_model_row(model)):
        return None
    ceiling = estimate_llm_micro(model, prompt_tokens=IMAGE_MAX_PROMPT_TOKENS, max_tokens=IMAGE_MAX_OUTPUT_TOKENS)
    if text_ceiling_micro * CALLS_PER_VARIANT + ceiling * IMAGE_CALLS_PER_VARIANT > MAX_PAGE_MICRO:
        return None  # Never let artwork push a page over the hard per-page cap.
    return {"provider": "openai", "model": model, "provider_identity": "openai:pagebuilder_images",
        "calls": IMAGE_CALLS_PER_VARIANT, "call_ceiling_micro": ceiling, "max_prompt_tokens": IMAGE_MAX_PROMPT_TOKENS,
        "max_output_tokens": IMAGE_MAX_OUTPUT_TOKENS, "pricing": _lookup_model_row(model)}


def calls_per_variant(quote):
    return CALLS_PER_VARIANT + ((quote.get("image") or {}).get("calls", 0))


def reasoning_policy(provider, model):
    if provider == "openai":
        from app.llm.pricing import load_pricing
        canonical = (load_pricing().get("aliases") or {}).get(model, model)
        if canonical in {"gpt-6-sol", "gpt-6.1-sol"}:
            return {"planning": "low", "reviewer": "low", "generation": "low"}  # Sol has no "none"; low keeps HTML within the output cap.
    deepseek = provider in {"azure_foundry", "deepseek_direct"} and model.lower().replace("_", "-").startswith("deepseek-v4")
    return {"planning": "low", "reviewer": "low", "generation": "none"} if deepseek else {
        "planning": None, "reviewer": None, "generation": None}


def enabled():
    if is_local():
        from app.pagebuilder.local import enabled as local_enabled
        return local_enabled()
    return os.environ.get("SUPYMEM_PAGEBUILDER_ENABLED", "0").lower() in {"1", "true", "yes", "on"}


def require_enabled():
    if not enabled():
        if is_local():
            from app.workspaces.resolve import local_person
            from app.pagebuilder.tool import require_connected
            from app.core.local_capabilities import capability
            if capability("landing_pages")["state"] != "off":
                require_connected(local_person().id)
            try:
                from app.core.local_capabilities import blocked_message
                message = blocked_message("landing_pages")
            except ImportError:
                message = "Save your API key and planning model in Settings > Keys and connections > Your AI provider, then choose Retry on your landing page."
            raise HTTPException(409, detail={"code": "capability_unavailable", "message": message})
        raise HTTPException(404, detail={"code": "pagebuilder_disabled"})


def authority(venture_id, caller, *, write=False):
    if not is_local():
        require_enabled()
    actor = caller if isinstance(caller, str) else getattr(caller, "sub", None)
    if not isinstance(actor, str) or not actor.strip() or actor in {"system", "anonymous"}:
        raise HTTPException(401, detail={"code": "unauthenticated"})
    _, wid = assert_venture_access(venture_id, actor)
    with db()() as session:
        venture = session.get(Venture, venture_id)
        workspace = session.get(Workspace, wid)
        if not venture or venture.archived_at or venture.stage == "purged" or not workspace or workspace.archived_at:
            raise HTTPException(404, detail={"code": "not_found"})
    if write:
        from app.authz.roles import role_allows
        from app.workspaces.service import role_of
        if not role_allows(role_of(wid, subject_person_id(actor)), "manage_apps"):
            raise HTTPException(403, detail={"code": "pagebuilder_manage_apps_required"})
    return wid, subject_person_id(actor)


def _fit_local_research(inputs):
    remaining = inputs["research"].copy()
    markdown = remaining.pop("dossier_markdown")
    fitted = {key: remaining.pop(key) for key in ("local_signup", "experiment_id") if key in remaining}
    inputs["research"] = fitted
    if len(dump(inputs).encode()) > MAX_INPUT_BYTES:
        raise HTTPException(409, detail="Open your venture's Idea screen, shorten your description and answers, then ask me to build your page again.")

    def add_section(key, value):
        fitted[key] = value
        if len(dump(inputs).encode()) > MAX_INPUT_BYTES:
            del fitted[key]

    for key in ("research_skipped", "limitations", "gap_items", "existing_site", "summary"):
        if key in remaining:
            add_section(key, remaining.pop(key))
    add_section("dossier_markdown", "")
    if "dossier_markdown" in fitted:
        room = MAX_INPUT_BYTES - len(dump(inputs).encode()) + len(dump("").encode())
        end = bisect_right(range(len(markdown) + 1), room, key=lambda size: len(dump(markdown[:size]).encode())) - 1
        fitted["dossier_markdown"] = markdown[:end]
    for key in sorted(remaining, key=lambda key: (key != "fast_scan", key != "deep_research", key)):
        add_section(key, remaining[key])


def snapshot(session, venture_id, workspace_id, *, experiment_id=None):
    from app.conversion.waitlist import VERSION, consent_text, public_url
    from app.research.dossier import load_validation_dossier
    venture = session.get(Venture, venture_id)
    if not venture or venture.workspace_id != workspace_id:
        raise HTTPException(404, detail={"code": "venture_not_found"})
    query = select(IdeaSession).where(IdeaSession.venture_id == venture_id, IdeaSession.workspace_id == workspace_id)
    query = (query.where(IdeaSession.state != "abandoned") if is_local()
             else query.where(IdeaSession.confirmed_revision_id.is_not(None)))
    idea = session.scalar(query.order_by(IdeaSession.updated_at.desc()).limit(1))
    revision_id = (idea.current_revision_id or idea.confirmed_revision_id if is_local()
                   else idea.confirmed_revision_id) if idea else None
    revision = session.get(IdeaRevision, revision_id) if revision_id else None
    if ((revision and (revision.workspace_id, revision.venture_id, revision.session_id)
         != (workspace_id, venture_id, idea.id)) or (not revision and (not is_local() or revision_id))):
        raise HTTPException(409, detail={"code": "pagebuilder_confirmed_revision_required"})
    markdown, research = load_validation_dossier(venture_id)
    try:
        answers = json.loads(revision.structured or "{}") if revision else {}
    except ValueError:
        raise HTTPException(409, detail={"code": "pagebuilder_idea_invalid"}) from None
    summary = (answers.get("summary") or "").strip() if is_local() else (revision.summary if revision else "")
    summary = summary or venture.raw_idea
    if is_local() and len((summary or "").strip()) < 10:
        raise HTTPException(422, detail="Open your venture's Idea screen and describe what your page should offer, then ask me to build it again.")
    inputs = {"venture_id": venture_id, "name": venture.name, "idea": summary,
        "clarified_answers": answers, "research": {**research, "dossier_markdown": (markdown or "")[:16000]},
        "waitlist_url": public_url(venture_id), "waitlist_consent_text": consent_text(venture.name),
        "waitlist_consent_version": VERSION}
    from app.experiments.page_tests import page_input
    page_test = page_input(session, venture_id, workspace_id, experiment_id=experiment_id)
    if experiment_id and not page_test:
        raise HTTPException(409, detail="Open your venture chat and prepare this experiment again for your current idea.")
    if page_test:
        inputs["page_test"] = page_test
    elif is_local():
        from app.pagebuilder.local_publish import waitlist_destination
        # Private snapshots use saved settings; export and publication still verify the public endpoint.
        inputs["research"]["local_signup"] = waitlist_destination(venture_id, venture.name, deployed=False, verify=False)
    if len(dump(inputs).encode()) > MAX_INPUT_BYTES:
        if not is_local():
            raise HTTPException(409, detail={"code": "pagebuilder_input_too_large"})
        _fit_local_research(inputs)
    return revision.id if revision else digest(inputs), inputs


def resources(workspace_id, venture_id):
    if is_local():
        from app.pagebuilder.local import resources as local_resources
        return local_resources(workspace_id, venture_id)
    from app.conversion.waitlist import VERSION
    from app.credits.accounts import get_account
    from app.credits.estimate import _lookup_model_row, estimate_llm_micro
    from app.llm.founder_provider import resolved_provider_identity
    config = resolve_route(workspace_id)
    account = get_account(venture_id)
    from app.db.supymem_models import VentureWaitlist
    with db()() as session:
        waitlist = session.get(VentureWaitlist, venture_id)
        waitlist_error = ("pagebuilder_waitlist_scope_invalid" if waitlist and waitlist.workspace_id != workspace_id
            else "pagebuilder_waitlist_disabled" if waitlist and not waitlist.enabled
            else "pagebuilder_waitlist_consent_upgrade_required" if waitlist and waitlist.consent_version != VERSION else None)
    # Creation of a missing enabled row preserves an acknowledged quote.
    waitlist_policy = ["eligible" if waitlist_error is None else waitlist_error, VERSION]
    if not is_local() and not _lookup_model_row(config.model):
        raise HTTPException(503, detail={"code": "pagebuilder_pricing_unavailable"})
    # The existing receipt gateway handles BYOK attribution without hiding it.
    byok = config.source == "founder_override"
    # The reviewed shared gateway preserves captured phase effort on BYOK too.
    provider_error = None
    ceiling = estimate_llm_micro(config.model, prompt_tokens=MAX_PROMPT_TOKENS, max_tokens=MAX_OUTPUT_TOKENS)
    identity = resolved_provider_identity(config)
    reasoning = reasoning_policy(config.provider, config.model)
    image = image_quote(config, ceiling)
    variant_ceiling = ceiling * CALLS_PER_VARIANT + (image["call_ceiling_micro"] * image["calls"] if image else 0)
    if not is_local() and variant_ceiling > MAX_PAGE_MICRO:
        provider_error = "pagebuilder_model_exceeds_limit"
    return {"provider": config.provider, "model": config.model, "payer": "founder" if byok else "platform_credits",
        "provider_identity": identity, "account_id": account.id if account else None,
        "call_timeout_seconds": CALL_TIMEOUT_SECONDS, "max_output_tokens": MAX_OUTPUT_TOKENS,
        "request_policy": REQUEST_POLICY, "reasoning_policy": reasoning,
        "call_ceiling_micro": ceiling, "ceiling_micro": variant_ceiling, "image": image,
        "resource_digest": digest([identity, account.id if account else None, _lookup_model_row(config.model), ceiling,
            REQUEST_POLICY, reasoning, CALLS_PER_VARIANT, CALL_TIMEOUT_SECONDS, MAX_OUTPUT_TOKENS, waitlist_policy,
            *([image] if image else [])]),
        "waitlist_error": waitlist_error, "provider_error": provider_error,
        "ready": bool(config.key_is_set and (is_local() or os.environ.get("LLM_OFFLINE", "0").lower() not in {"1", "true", "yes"})
            and waitlist_error is None and provider_error is None and (is_local() or variant_ceiling <= MAX_PAGE_MICRO) and
            (byok or (account and (account.scope == "person" or (account.scope == "venture" and account.workspace_id == workspace_id and account.venture_id == venture_id)) and account.status == "active"))),
        "available_micro": account.granted_micro - account.spent_micro - account.reserved_micro if account else 0}


def _quote_fingerprint(workspace_id, actor, venture_id, revision_id, inputs, resources, variants):
    return digest([workspace_id, actor, venture_id, revision_id, digest(inputs), resources["resource_digest"],
                   variants, resources["ceiling_micro"] * variants])


def price(venture_id: str, caller) -> dict:
    """The one-variant build ceiling before the idea is confirmed; the exact quote needs the confirmed revision."""
    wid, _ = authority(venture_id, caller, write=True)
    q = resources(wid, venture_id)
    if is_local():
        return {"ready": q["ready"], "reason": q["waitlist_error"], "model": q["model"],
                "image_method": "original_contextual_illustration", "renderer_verification": "local_html"}
    ready = q["ready"] and (q["payer"] == "founder" or q["available_micro"] >= q["ceiling_micro"])
    return {"credits": q["ceiling_micro"] / 10000, "ceiling_micro": q["ceiling_micro"], "ready": ready,
        "reason": None if ready else "pagebuilder_insufficient_credits" if q["ready"] else q["waitlist_error"] or q["provider_error"] or "pagebuilder_provider_or_credits_unavailable"}


def profile(venture_id: str, caller, job_id: str | None = None):
    """The founder card and chat share the existing quote and native eligibility."""
    from datetime import timezone
    from app.build import device_orders
    from app.pagebuilder.native_reference import resolve_candidate
    from app.pagebuilder.health import current as renderer_health
    from app.pagebuilder.tool import connected_provider
    from app.pagebuilder_admission import inputs, publication
    wid, actor = authority(venture_id, caller, write=True)
    with db()() as session:
        revision_id, _inputs = snapshot(session, venture_id, wid)
        try:
            task = device_orders._coding_task_snapshot(session, wid, venture_id, "Build the approved landing page with Codex")
            device_orders._coding_offline_profile(task)
        except (KeyError, ValueError):
            task = {}  # A plan is optional: the server builder only needs the confirmed idea.
        query = select(Job).where(Job.kind == KIND, Job.workspace_id == wid, Job.venture_id == venture_id)
        if job_id:
            query = query.where(Job.id == job_id)
        rows = session.scalars(query.order_by(func.coalesce(Job.scheduled_for, Job.created_at).desc(),
            Job.created_at.desc()).limit(20)).all()
        if job_id and not rows:
            raise HTTPException(404, detail={"code": "not_found"})
        # Reusing a historical free failure keeps its creation identity. Its
        # current execution timestamps decide which build the founder sees.
        def activity(row):
            times = [value for value in (row.created_at, row.scheduled_for, row.started_at, row.finished_at) if value]
            return max(value if value.tzinfo else value.replace(tzinfo=timezone.utc) for value in times)
        rows.sort(key=lambda row: (row.status in {"pending", "scheduled", "retry_wait", "leased", "running"}
            and json.loads(row.payload_json)["_runtime"]["state"] not in {"ready", "failed"},
            activity(row)), reverse=True)
        matching = rows if job_id else [row for row in rows if json.loads(row.payload_json).get("revision_id") == revision_id]
        row = matching[0] if matching else None
        consecutive_failures = 0
        for candidate in matching:
            if _view(candidate)["state"] != "failed":
                break
            consecutive_failures += 1
        if row:
            session.expunge(row)
    current = None
    if row:
        doc = json.loads(row.payload_json)
        current = {**_view(row), "queue_state": row.status, "queue_attempt": row.attempts,
            "consecutive_failures": consecutive_failures,
            "original_creator_required": doc.get("_actor_subject") != actor,
            "native_ready": False, "candidates": [],
            "server_publish_ready": False, "server_candidates": [], "publication": None}
        if current["state"] == "ready" and not current["original_creator_required"]:
            for variant, data in enumerate(doc["result"]["bundles"]):
                try:
                    if is_local():
                        from app.pagebuilder.native_reference import _completed_candidate
                        with db()() as session:
                            candidate = session.get(Job, row.id)
                            if (not candidate or candidate.status != "succeeded"
                                    or (candidate.kind, candidate.workspace_id, candidate.venture_id) != (KIND, wid, venture_id)):
                                raise ValueError("pagebuilder_candidate_not_found")
                            checked = _completed_candidate(session, candidate)
                        frozen = checked["_runtime"]["input"]
                        if checked.get("_actor_subject") != actor or checked["revision_id"] != revision_id or any(
                                frozen.get(field) != _inputs.get(field) for field in ("name", "idea", "clarified_answers")):
                            raise ValueError("pagebuilder_inputs_changed")
                    else:
                        resolve_candidate(venture_id, caller, {"job_id": row.id, "variant": variant})
                except (HTTPException, KeyError, ValueError):
                    pass
                else:
                    current["candidates"].append({"variant": variant, "direction": data["direction"]})
                selection = {"job_id": row.id, "variant": variant,
                    "plan_id": task.get("plan_id"), "plan_task_id": task.get("task_id")}
                try:
                    saved = publication.current(venture_id, caller, selection)
                    if saved and current["publication"] is None:
                        current["publication"] = saved
                    with db()() as session:
                        inputs.current(session, venture_id, caller, selection,
                            active_run_id=saved["run_id"] if saved else None)
                except (HTTPException, KeyError, PermissionError, ValueError):
                    continue
                candidate = {"variant": variant, "direction": data["direction"]}
                if saved and current["publication"]["run_id"] == saved["run_id"]:
                    current["server_candidates"].insert(0, candidate)
                    current["publication"] = saved
                else:
                    current["server_candidates"].append(candidate)
            current["native_ready"] = bool(current["candidates"])
            current["server_publish_ready"] = bool(current["server_candidates"])
    return {"task_id": task.get("task_id"), "plan_id": task.get("plan_id"), "artifact_profile": "static_landing_v1",
        "revision_id": revision_id, "current": current, "renderer_ready": renderer_health()["ready"],
        "server_admission_enabled": os.getenv("SUPYMEM_PAGEBUILDER_ADMISSION_ENABLED", "").lower() in {"1", "true"},
        "quote": _estimate(venture_id, caller, variants=1) if current is None and (not is_local() or connected_provider(actor)) else None}



def estimate(venture_id: str, caller) -> dict:
    return _estimate(venture_id, caller, variants=1)


def _estimate(venture_id, caller, *, variants):
    wid, actor = authority(venture_id, caller, write=True)
    if is_local():
        from app.pagebuilder.tool import require_connected
        require_enabled()
        require_connected(actor)
    if type(variants) is not int or not 1 <= variants <= 3:
        raise HTTPException(422, detail={"code": "pagebuilder_variants_invalid"})
    with db()() as session:
        revision_id, inputs = snapshot(session, venture_id, wid)
    q = resources(wid, venture_id)
    ceiling = q["ceiling_micro"] * variants
    sufficient = q["payer"] == "founder" or q["available_micro"] >= ceiling
    if is_local():
        return {"seconds": CALL_TIMEOUT_SECONDS * variants, "variant_count": variants, "ready": q["ready"],
            "reason": q["waitlist_error"], "revision_id": revision_id, "input_digest": digest(inputs),
            "quote_fingerprint": _quote_fingerprint(wid, actor, venture_id, revision_id, inputs, q, variants),
            "model": q["model"], "payer": "founder", "renderer_verification": "local_html",
            "image_method": "illustrative_ai_artwork_with_native_svg_fallback" if q["image"] else "original_contextual_illustration"}
    return {"credits": ceiling / 10000, "seconds": CALL_TIMEOUT_SECONDS * variants, "ceiling_micro": ceiling, "variant_count": variants,
        "ready": q["ready"] and sufficient, "reason": None if q["ready"] and sufficient else "pagebuilder_insufficient_credits" if q["ready"] else q["waitlist_error"] or q["provider_error"] or "pagebuilder_provider_or_credits_unavailable",
        "revision_id": revision_id, "input_digest": digest(inputs), "model": q["model"], "payer": q["payer"],
        "quote_fingerprint": _quote_fingerprint(wid, actor, venture_id, revision_id, inputs, q, variants),
        "image_cost_credits": (q["image"]["call_ceiling_micro"] * q["image"]["calls"] * variants / 10000) if q["image"] else 0,
        "image_method": "illustrative_ai_artwork_with_native_svg_fallback" if q["image"] else "original_contextual_illustration",
        "renderer_verification": "worker_preflight_before_charging",
        "basis": "Up to ten metered text calls per variant, including capability grounding and original SVG artwork"
            + (", plus up to three metered illustrative image calls (native SVG art is used if one fails)" if q["image"] else "")
            + ". Generation starts after checking that previews can render. Only confirmed usage is consumed."}


def start(venture_id: str, caller, *, variants: int = 1) -> dict:
    return _start(venture_id, caller, variants=variants)


def build(venture_id: str, caller, *, new_version_of=None) -> dict:
    """One-click build: the founder's click is the approval of the capped server-side quote."""
    return _start(venture_id, caller, variants=1, retry_failed=True, new_version_of=new_version_of)


def rewrite(venture_id, caller, *, new_version_of, instruction):
    if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 600:
        raise HTTPException(422, detail={"code": "pagebuilder_rewrite_instruction_invalid"})
    if is_local():
        return _start(venture_id, caller, variants=1, retry_failed=True, new_version_of=new_version_of,
            rewrite_instruction=instruction.strip())
    from app.pagebuilder.qa import literal_copy_edit
    wid, actor = authority(venture_id, caller, write=True)
    with db()() as session:
        source = session.get(Job, new_version_of)
        if (source and (source.kind, source.workspace_id, source.venture_id) == (KIND, wid, venture_id)
                and json.loads(source.payload_json).get("_actor_subject") == actor and _view(source)["state"] == "ready"):
            doc = json.loads(source.payload_json)
            before = base64.b64decode(doc["result"]["bundles"][0]["files"]["index.html"], validate=True)
            try:
                edit = literal_copy_edit(before, instruction.strip())
            except ValueError as exc:
                raise HTTPException(422, detail={"code": str(exc)}) from exc
            if edit and edit[2] == before:
                return _view(source)
    return _start(venture_id, caller, retry_failed=True, new_version_of=new_version_of,
                  rewrite_instruction=instruction.strip())


def _retry_tool_request(session, row, doc):
    from app.build.device_orders import KIND as ORDER_KIND
    from app.db.models import VentureDocument
    from app.pagebuilder.tool import require_connected, result_pending

    saved = doc.get("tool_build") or {}
    if order_id := saved.get("order_id"):
        order_row = session.get(VentureDocument, order_id)
        order = json.loads(order_row.provenance_json or "{}") if order_row and (
            order_row.kind, order_row.venture_id) == (ORDER_KIND, row.venture_id) else {}
        if (order.get("actor_id") != doc["_actor_subject"]
                or order.get("workspace_id") != row.workspace_id
                or (order.get("task_snapshot") or {}).get("pagebuilder_job_id") != row.id
                or order.get("state") not in {"stopped", "cancelled", "failed", "expired"}
                or order.get("stop_reconciliation_required") or order.get("inference_reconciliation_required")
                or ((order.get("claimed_at") or order.get("execution_id"))
                    and (order.get("native_stop") or {}).get("processes_stopped") is not True)):
            raise HTTPException(409, detail={"code": "pagebuilder_outcome_uncertain", "message":
                "Choose Watch your tool in Build activity. If you see Check Stop, choose it before retrying your page."})
        if result_pending(order, row, doc):
            raise HTTPException(409, detail={"code": "pagebuilder_result_pending", "message":
                "Your tool has a checked result. Open Website and choose Retry saving before building again."})
    provider = saved.get("provider") or doc.get("coding_provider")
    if not provider:
        raise HTTPException(409, detail={"code": "pagebuilder_retry_tool_missing", "message":
            "This attempt did not save a coding tool. In your venture chat, ask Charlie to rebuild your landing page with the tool you want."})
    require_connected(doc["_actor_subject"], provider=provider)
    return provider, {key: saved[key] for key in ("provider", "plan", "grounding", "brief") if key in saved} if "brief" in saved else None


def reject_secret_instruction(*instructions):
    from app.secrets.detect import looks_like_secret

    if any(isinstance(text, str) and looks_like_secret(text) for text in instructions):
        raise HTTPException(409, detail={"code": "pagebuilder_build_request_contains_secret", "message":
            "Remove credentials from your page request. Save keys in Settings > Keys and connections, then retry without credentials."})


def _start(venture_id, caller, *, variants=1, expected_quote=None, require_quote=False, retry_failed=False, new_version_of=None,
           rewrite_instruction=None, provider=None, instruction=None, retry_job_id=None, experiment_id=None):
    from app.ventures import page_choice
    wid, actor = authority(venture_id, caller, write=True)
    connected_tool = None
    if is_local():
        from app.pagebuilder.tool import require_connected
        if instruction is not None and (not isinstance(instruction, str) or not instruction.strip() or len(instruction.encode()) > 16000):
            raise HTTPException(422, detail={"code": "pagebuilder_build_instruction_invalid", "message":
                "Describe the page you want in your venture chat, then send your build request."})
        reject_secret_instruction(instruction, rewrite_instruction)
        require_enabled()
        if retry_job_id is not None:
            if not isinstance(retry_job_id, str) or not retry_job_id:
                raise HTTPException(422, detail={"code": "pagebuilder_new_version_unavailable", "message":
                    "Open Build activity and choose Retry on your stopped landing page."})
            new_version_of = retry_job_id
        else:
            connected_tool = require_connected(actor, provider=provider) if provider else require_connected(actor)
        retry_failed = True
    if type(variants) is not int or not 1 <= variants <= 3:
        raise HTTPException(422, detail={"code": "pagebuilder_variants_invalid"})
    with db()() as session:
        # Venture lock serializes concurrent requests for the same idea version.
        session.get(Venture, venture_id, with_for_update=True)
        if is_local() and new_version_of:
            source = session.get(Job, new_version_of)
            if source and (source.kind, source.workspace_id, source.venture_id) == (KIND, wid, venture_id):
                experiment_id = json.loads(source.payload_json).get("experiment_build_id")
        revision_id, inputs = (snapshot(session, venture_id, wid, experiment_id=experiment_id) if experiment_id
            else snapshot(session, venture_id, wid))
        q = resources(wid, venture_id)
        if expected_quote and expected_quote != _quote_fingerprint(wid, actor, venture_id, revision_id, inputs, q, variants):
            raise HTTPException(409, detail={"code": "pagebuilder_quote_changed"})
        if q["waitlist_error"]:
            from app.pagebuilder.local import failure_reason
            raise HTTPException(409, detail={"code": q["waitlist_error"],
                **({"message": failure_reason(q["waitlist_error"])} if is_local() else {})})
        if q["provider_error"]:
            raise HTTPException(503, detail={"code": q["provider_error"]})
        base_key = key = "pagebuilder:" + digest([wid, venture_id, revision_id] + ([experiment_id] if experiment_id else []))
        old = session.scalar(select(Job).where(Job.dedupe_key == key))
        retry_doc = None
        saved_tool = None
        if retry_failed:
            slot_filter = (or_(Job.dedupe_key == base_key, Job.dedupe_key.startswith(base_key + ":")) if is_local()
                else Job.dedupe_key.in_([base_key, *(f"{base_key}:{attempt}" for attempt in range(2, ATTEMPTS_PER_REVISION + 1))]))
            slots = session.scalars(select(Job).where(slot_filter)
                .order_by(func.coalesce(Job.finished_at, Job.started_at, Job.scheduled_for, Job.created_at).desc(),
                    Job.created_at.desc())).all()
            if any((row.kind, row.workspace_id, row.venture_id) != (KIND, wid, venture_id) for row in slots):
                raise HTTPException(409, detail={"code": "pagebuilder_idempotency_conflict"})
            if old and json.loads(old.payload_json).get("_actor_subject") != actor:
                if new_version_of:
                    raise HTTPException(409, detail={"code": "pagebuilder_new_version_unavailable"})
                return _view(old)
            previous = next((row for row in slots if row.id == new_version_of), None) if new_version_of else None
            if new_version_of:
                source_doc = json.loads(previous.payload_json) if previous else {}
                source_state = _view(previous)["state"] if source_doc.get("_actor_subject") == actor else None
                if retry_job_id is not None and source_state != "failed":
                    raise HTTPException(409, detail={"code": "pagebuilder_new_version_unavailable", "message":
                        "Open Build activity and choose Retry on a stopped landing page for your current idea."})
                if source_state not in {"ready", "failed", "queued", "running", "critiquing"} or (rewrite_instruction and source_state != "ready"):
                    raise HTTPException(409, detail={"code": "pagebuilder_new_version_unavailable"})
                if is_local() and source_state == "failed" and (retry_job_id or not (instruction or rewrite_instruction)):
                    retry_job_id, retry_doc = previous.id, source_doc
                if (source_state != "ready" and any(operation.get("state") in {"calling", "uncertain"}
                        for operation in source_doc["_runtime"]["operations"].values())):
                    raise HTTPException(409, detail={"code": "pagebuilder_outcome_uncertain", **({"message":
                        "Open Website and choose Refresh status to check your last request before retrying your page."} if retry_doc else {})})
            if retry_doc is not None:
                if digest(retry_doc["_runtime"]["input"]) != digest(inputs):
                    raise HTTPException(409, detail={"code": "pagebuilder_inputs_changed", "message":
                        "Your idea or page settings changed. Ask Charlie to rebuild your landing page with your current details."})
                provider, saved_tool = _retry_tool_request(session, previous, retry_doc)
                instruction = retry_doc.get("build_instruction")
            if rewrite_instruction:
                for row in slots:
                    current = json.loads(row.payload_json)
                    if current.get("rewrite") and any(operation.get("state") in {"calling", "uncertain"}
                            for operation in current["_runtime"]["operations"].values()):
                        raise HTTPException(409, detail={"code": "pagebuilder_outcome_uncertain"})
            active = next((row for row in slots if row.status not in FAILED_QUEUE | {"succeeded", "done"}
                and not (is_local() and _view(row)["state"] == "failed")), None)
            if not active and not new_version_of and slots and _view(slots[0])["state"] == "ready":
                active = slots[0]
            if active:
                active_doc = json.loads(active.payload_json)
                if retry_doc is not None and (active_doc.get("retry_of") != retry_job_id or active_doc.get("_actor_subject") != actor):
                    raise HTTPException(409, detail={"code": "pagebuilder_tool_build_active", "message":
                        "Open Build activity and finish or stop your current page build before retrying this one."})
                active_provider = active_doc.get("coding_provider") or active_doc.get("tool_build", {}).get("provider")
                if provider and active_provider and provider != active_provider and _view(active)["state"] != "ready":
                    raise HTTPException(409, detail={"code": "pagebuilder_tool_build_active", "message":
                        "Your page is already building with another tool; choose Show status here in your chat, stop that build, then resend this build request."})
                if rewrite_instruction and json.loads(active.payload_json).get("rewrite") != {
                        "source_job_id": new_version_of, "instruction": rewrite_instruction}:
                    raise HTTPException(409, detail={"code": "pagebuilder_new_version_unavailable"})
                view = _view(active)
                if view["state"] == "failed":
                    raise HTTPException(409, detail={"code": view.get("error", "pagebuilder_job_failed")})
                if previous and active.id == previous.id and view["state"] != "ready":
                    return {**view, "requeued": True}
                if previous and active.created_at <= previous.created_at:
                    raise HTTPException(409, detail={"code": "pagebuilder_new_version_unavailable"})
                page_choice.building(session, venture_id)
                session.commit()
                return view
            if new_version_of:
                if slots[0].id != previous.id and slots[0].created_at > previous.created_at and _view(slots[0])["state"] != "failed":
                    newer = json.loads(slots[0].payload_json)
                    if retry_doc is not None and (newer.get("retry_of") != retry_job_id or newer.get("_actor_subject") != actor):
                        raise HTTPException(409, detail={"code": "pagebuilder_new_version_unavailable", "message":
                            "Open Build activity and review your newer page before asking Charlie to build another version."})
                    if rewrite_instruction and newer.get("rewrite") != {
                            "source_job_id": new_version_of, "instruction": rewrite_instruction}:
                        raise HTTPException(409, detail={"code": "pagebuilder_new_version_unavailable"})
                    return _view(slots[0])
            delivered = 0
            for row in slots:
                doc = json.loads(row.payload_json)
                # ponytail: legacy jobs lack delivery markers, backfill receipts if historical accounting needs precision.
                if (doc.get("result", {}).get("ready_delivered") is True
                        or row.status in {"succeeded", "done"} and _state(row, doc)[0] == "ready"):
                    delivered += 1
            if not is_local() and delivered >= BUILDS_PER_REVISION:
                raise HTTPException(409, detail={"code": "pagebuilder_build_limit"})
            for row in [previous] if previous else slots:
                if retry_doc is None and instruction is None and _requeue_free_failure(session, row, actor=actor, revision_id=revision_id,
                        inputs=inputs, resource_digest=q["resource_digest"]):
                    page_choice.building(session, venture_id)
                    session.commit()
                    publish_wake()
                    return {**_view(row), "requeued": True}
            if not is_local() and len(slots) >= ATTEMPTS_PER_REVISION:
                raise HTTPException(409, detail={"code": "pagebuilder_attempt_limit"})
            if slots:
                key, old = f"{base_key}:{len(slots) + 1}", None
        if old:
            if (old.kind, old.workspace_id, old.venture_id) != (KIND, wid, venture_id):
                raise HTTPException(409, detail={"code": "pagebuilder_idempotency_conflict"})
            if old.status not in FAILED_QUEUE:
                page_choice.building(session, venture_id)
                session.commit()
            return _view(old)
        if not is_local() and require_quote and not expected_quote:
            raise HTTPException(409, detail={"code": "pagebuilder_quote_required"})
        if not q["ready"]:
            raise HTTPException(503, detail={"code": "pagebuilder_provider_or_credits_unavailable"})
        q = {**q, "ceiling_micro": q["ceiling_micro"] * variants, "call_budget": calls_per_variant(q) * variants}
        if q["payer"] != "founder" and q["available_micro"] < q["ceiling_micro"]:
            raise HTTPException(402, detail={"code": "pagebuilder_insufficient_credits"})
        payload = {"_actor_subject": actor, "revision_id": revision_id, "variant_count": variants,
            "_runtime": {"input": inputs, "quote": q, "operations": {}, "calls_used": 0, "state": "queued", "stage": "queued", "progress": 0},
            "result": {}}
        if experiment_id:
            payload["experiment_build_id"] = experiment_id
        if is_local() and (provider or connected_tool):
            payload["coding_provider"] = provider or connected_tool
        if is_local() and instruction:
            payload["build_instruction"] = instruction
        if rewrite_instruction:
            payload["rewrite"] = {"source_job_id": new_version_of, "instruction": rewrite_instruction}
        if retry_doc is not None:
            if is_local():
                reject_secret_instruction(retry_doc.get("build_instruction"),
                    retry_doc.get("rewrite", {}).get("instruction"), (saved_tool or {}).get("brief"))
            payload["retry_of"] = retry_job_id
            if saved_tool:
                payload["tool_build"] = saved_tool
            if retry_doc.get("rewrite"):
                payload["rewrite"] = retry_doc["rewrite"]
        from app.tenancy import bind
        with bind(workspace_id=wid, venture_id=venture_id, actor=actor):
            jid = enqueue(session, KIND, payload, workspace_id=wid, venture_id=venture_id, dedupe_key=key,
                effect_key=key, max_attempts=3)
        session.flush()
        row = session.get(Job, jid)
        row.timeout_s = max(900, variants * (CALLS_PER_VARIANT * CALL_TIMEOUT_SECONDS + 120
            + (IMAGE_CALLS_PER_VARIANT * IMAGE_TIMEOUT_SECONDS if q["image"] else 0)))
        view = _view(row)
        page_choice.building(session, venture_id)
        session.commit()
    if is_local():
        from app.pagebuilder.tool import prepare_job
        prepare_job(jid, caller)
        view = status(venture_id, caller, jid)
    publish_wake()
    return view


def _candidate_current(doc):
    """A generated review flag alone is not a durable, metered approval receipt."""
    from app.pagebuilder.pipeline import _json, fallback_plan, plan_digest, validate_grounding_review
    from app.pagebuilder.models import CapabilityReview, PageInput, PagePlan
    from app.pagebuilder.images import (RASTER_EXTRA_ALT, SUPPORTED_COMPILER_VERSIONS, raster_alt,
                                        validate_compiled_assets, validate_svg, webp_size)
    from app.pagebuilder.models import RasterImage
    try:
        runtime = doc["_runtime"]
        quote = runtime["quote"]
        if not is_local() and (quote.get("request_policy") != REQUEST_POLICY
                or quote.get("call_budget") != calls_per_variant(quote) * doc["variant_count"]
                or quote.get("call_timeout_seconds") != CALL_TIMEOUT_SECONDS
                or quote.get("max_output_tokens") != MAX_OUTPUT_TOKENS
                or quote.get("reasoning_policy") != reasoning_policy(quote["provider"], quote["model"])):
            return False
        inputs = PageInput.model_validate(runtime["input"])
        bundles = doc["result"]["bundles"]
        if len(bundles) != doc["variant_count"]:
            return False
        for variant, bundle in enumerate(bundles):
            qa = bundle["qa"]
            rendered = qa["render"]
            native_recovery = is_local() and bundle.get("manifest", {}).get("repair_fallback") == {
                "stage": "qa", "reason": "pagebuilder_qa_failed"}
            native_layout = is_local() and bundle.get("manifest", {}).get("local_native_layout") is True
            if native_layout and inputs.research.get("local_signup", {}).get("mode") != "setup":
                return False
            local_preview = (is_local() and rendered.get("browser_rendered") is False
                and rendered.get("screenshot_qa") == "not_run" and rendered.get("preview_method") == "local_html"
                and bundle.get("manifest", {}).get("preview_method") == "local_html")
            if (qa.get("passed") is not True or rendered.get("passed") is not True
                    or (not local_preview and rendered.get("browser_rendered") is not True) or rendered.get("failures") != []):
                return False
            for viewport in (() if local_preview else ("desktop", "mobile")):
                observation = rendered["observations"][viewport]
                if (any(observation["checks"].get(check) is not True for check in
                        ("contrast", "image_clipping", "hero_cta_initial"))
                        or observation.get("contrastSkipped") != 0
                        or observation.get("contrastUnresolved") != []
                        or observation.get("imageClippingFailures") != []
                        or observation["initialHeroCta"].get("visible") is not True
                        or observation["initialHeroCta"].get("withinViewport") is not True
                        or observation["initialHeroCta"].get("hitCoverage") is not True):
                    return False
            grounding = qa["capability_grounding"]
            plan = PagePlan.model_validate(bundle["plan"])
            manifest = bundle["manifest"]
            tool_artwork = is_local() and manifest.get("built_with_coding_tool") is True
            if tool_artwork:
                if (manifest.get("image_provenance") != "connected_coding_tool" or native_recovery or native_layout
                        or manifest.get("repair_fallback") or any(asset.scene is not None or asset.compiler_version is not None for asset in plan.assets)):
                    return False
            elif (not isinstance(manifest, dict) or manifest.get("image_provenance") != "model_scene_native_svg"
                    or manifest.get("image_compiler_version") not in SUPPORTED_COMPILER_VERSIONS
                    or {asset.compiler_version for asset in plan.assets} != {manifest["image_compiler_version"]}):
                return False
            if not tool_artwork:
                validate_compiled_assets(plan)
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(bundle["source_zip"], validate=True))) as archive:
                names = archive.namelist()
                files = {name: base64.b64decode(body, validate=True) for name, body in bundle["files"].items()}
                if (len(names) != len(files) or set(names) != set(files) or any(
                        archive.getinfo(name).file_size != len(body) or archive.read(name) != body
                        for name, body in files.items())):
                    return False
                if is_local() and (local_preview or native_recovery or inputs.research.get("local_signup")):
                    from app.pagebuilder.qa import check_page
                    if not check_page(files["index.html"].decode("utf-8"), files, inputs, plan,
                            [RasterImage.model_validate(image) for image in bundle.get("imagery", [])])["passed"]:
                        return False
                if native_recovery or native_layout:
                    from app.experiments.page_tests import native_sections
                    from app.pagebuilder.pipeline import _html, _fallback_html as fallback_html
                    from app.pagebuilder.qa import pin_control_names, qa_contract
                    imagery = [RasterImage.model_validate(image) for image in bundle.get("imagery", [])]
                    expected_html = pin_control_names(_html(native_sections(fallback_html(plan, inputs, imagery),
                        inputs, plan)), qa_contract(inputs, plan, imagery))
                    if social := manifest.get("social_preview"):
                        from app.pagebuilder.metadata import add_social_preview
                        expected_html, _ = add_social_preview(expected_html, files[social["path"]], inputs.name)
                    if files["index.html"] != expected_html.encode():
                        return False
                for asset in plan.assets:
                    validate_svg(asset.svg)
                    expected = asset.svg.encode() if tool_artwork else validate_svg(asset.svg).encode()
                    if (base64.b64decode(bundle["files"][asset.path], validate=True) != expected
                            or names.count(asset.path) != 1 or archive.getinfo(asset.path).file_size != len(expected)
                            or archive.read(asset.path) != expected):
                        return False
                # Illustrative artwork: exact settled receipt, exact bytes, controller-derived alt and placement.
                imagery = [RasterImage.model_validate(image) for image in bundle.get("imagery", [])]
                assets = {asset.id: asset for asset in plan.assets}
                if (len({image.id for image in imagery}) != len(imagery) or (imagery and not quote.get("image"))
                        or manifest.get("illustrative_imagery", []) != [image.model_dump(mode="json", include={
                            "id", "path", "sha256", "model", "provenance", "operation", "replaces"}) for image in imagery]):
                    return False
                for image in imagery:
                    body = base64.b64decode(bundle["files"][image.path], validate=True)
                    source = assets.get(image.replaces)
                    expected_alt = raster_alt(source) if source else RASTER_EXTRA_ALT
                    receipt = runtime["operations"][f"variant_{variant}:{image.operation}"]
                    if (hashlib.sha256(body).hexdigest() != image.sha256 or webp_size(body) != (image.width, image.height)
                            or names.count(image.path) != 1 or archive.read(image.path) != body
                            or image.alt != expected_alt or image.model != quote["image"]["model"]
                            or (image.replaces is not None and (source is None or source.section_id != image.section_id))
                            or receipt.get("state") != "completed" or receipt.get("error_code")
                            or receipt.get("usage", {}).get("settled") is not True
                            or base64.b64decode(receipt["response"]["image_b64"], validate=True) != body):
                        return False
            final_digest = plan_digest(plan)
            fallback = manifest.get("repair_fallback")
            if native_recovery:
                generation = [runtime["operations"].get(f"variant_{variant}:code", {}),
                    *[operation for key, operation in runtime["operations"].items()
                        if key.startswith(f"variant_{variant}:repair_")]]
                if any(operation.get("state") != "completed" or operation.get("error_code")
                        or operation.get("usage", {}).get("settled") is not True for operation in generation):
                    return False
            elif fallback:
                stage, reason = fallback["stage"], fallback["reason"]
                from app.pagebuilder.pipeline import IMAGE_FALLBACK_CODES
                operation = runtime["operations"].get(f"variant_{variant}:{stage}", {})
                if (not stage.startswith(("plan_repair_", "repair_")) or reason not in IMAGE_FALLBACK_CODES
                        or (operation.get("error_code") != reason and not (
                            operation.get("state") == "completed" and not operation.get("error_code")
                            and operation.get("usage", {}).get("settled") is True))
                        or operation.get("state") not in {"uncertain", "failed", "retryable", "completed"}):
                    return False
            if grounding.get("policy") == "deterministic_early_access_v1":
                from app.pagebuilder.pipeline import scene_draft
                if (not fallback or not fallback["stage"].startswith("plan_repair_")
                        or (scene_draft(plan) if is_local() else plan) != fallback_plan(inputs, plan.direction)
                        or grounding != {"policy": "deterministic_early_access_v1", "passed": True,
                            "plan_sha256": final_digest, "reviews": [], "scope": "generation_only",
                            "independent_qa_attestation": False}):
                    return False
                continue
            review = grounding["reviews"][-1]
            stage = review["stage"]
            from app.pagebuilder.local import CODING_TOOL_REVIEW
            tool_review = is_local() and stage == CODING_TOOL_REVIEW
            if (grounding.get("policy") != "semantic_capability_v4" or grounding.get("passed") is not True
                    or grounding.get("scope") != "generation_only" or grounding.get("independent_qa_attestation") is not False
                    or not tool_review and stage not in {"capability_review_1", "capability_review_2", "capability_review_3", "capability_review_4", "capability_review_5", "capability_review_6"}
                    or grounding.get("plan_sha256") != final_digest or review.get("plan_sha256") != final_digest
                    or review["review"].get("approved") is not True or review["review"].get("unsupported_claims") != []):
                return False
            source_review = (doc.get("tool_build") or {}).get("source_review") or {}
            batches = source_review.get("batches")
            if is_local() and batches:
                from app.pagebuilder.pipeline import tool_page_plan
                from app.pagebuilder.tool import checked_review_batches
                stages = sorted(batches)
                if (manifest.get("built_with_coding_tool") is not True or quote.get("provider") != "openai_compatible"
                        or stage != stages[-1] or source_review.get("stage") != stage
                        or source_review.get("plan_sha256") != final_digest
                        or any(name not in {f"capability_review_{index}" for index in range(1, 7)} for name in stages)
                        or source_review.get("prompt_sha256") != digest([batches[name] for name in stages])
                        or plan_digest(tool_page_plan(inputs, doc["tool_build"]["plan"], files)) != final_digest):
                    return False
                def read_batch(name, _prompt):
                    operation = runtime["operations"][f"variant_{variant}:{name}"]
                    if (operation.get("state") != "completed" or operation.get("error_code")
                            or operation.get("usage", {}).get("settled") is not True):
                        raise ValueError("pagebuilder_grounding_review_invalid")
                    return operation["response"]["content"]
                actual_review = checked_review_batches(plan, inputs, batches, read_batch)
            elif tool_review:
                from app.pagebuilder.pipeline import _grounding_prompt, tool_page_plan
                saved = doc["tool_build"]["source_review"]
                source_plan = tool_page_plan(inputs, doc["tool_build"]["plan"], files)
                if (manifest.get("built_with_coding_tool") is not True or quote.get("provider") != CODING_TOOL_REVIEW
                        or saved["stage"] != stage or saved["plan_sha256"] != final_digest or plan_digest(source_plan) != final_digest
                        or saved["prompt_sha256"] != hashlib.sha256(_grounding_prompt(source_plan, inputs).encode()).hexdigest()):
                    return False
                actual_review = CapabilityReview.model_validate(saved["review"])
            else:
                operation = runtime["operations"][f"variant_{variant}:{stage}"]
                if operation.get("state") != "completed" or operation.get("usage", {}).get("settled") is not True:
                    return False
                actual_review = CapabilityReview.model_validate(_json(operation["response"]["content"]))
            validate_grounding_review(actual_review, plan, inputs)
            normalized_review = actual_review.model_dump(mode="json")
            if (normalized_review != review["review"] or digest(normalized_review) != review.get("review_sha256")):
                return False
        return True
    except (KeyError, IndexError, TypeError, ValueError, OSError, RuntimeError, zipfile.BadZipFile, zlib.error):
        return False


def _state(row, doc):
    runtime = doc["_runtime"]
    state = runtime["state"]
    saved_error = str(row.last_error or "").partition(":")[0]
    if row.status in {"failed_dead", "failed", "poison", "cancelled"} and state != "ready":
        state = "failed"
    code = runtime.get("error")
    if state == "ready" and row.status in {"failed_dead", "failed", "poison"} and saved_error == "pagebuilder_candidate_invalid":
        state, code = "failed", saved_error
    if state == "ready" and not _candidate_current(doc):
        state, code = "failed", "pagebuilder_candidate_policy_obsolete"
    if not code and state == "failed":
        code = saved_error if re.fullmatch(r"pagebuilder_[a-z_]{1,80}", saved_error) else "pagebuilder_job_failed"
    return state, code


def _view(row):
    doc = json.loads(row.payload_json)
    runtime = doc["_runtime"]
    operations = runtime["operations"]
    state, code = _state(row, doc)
    receipt = {"credits_micro": sum(v.get("usage", {}).get("credits_micro", 0) - v.get("usage", {}).get("refunded_micro", 0) for v in operations.values()),
        "calls": runtime["calls_used"], "ceiling_micro": runtime["quote"]["ceiling_micro"],
        "payer": runtime["quote"]["payer"]}
    receipt["spent_credits"] = receipt["credits_micro"] / 10000
    receipt["credits"] = receipt["spent_credits"]
    receipt["cost_usd"] = sum(v.get("usage", {}).get("provider_actual_micro", v.get("usage", {}).get("actual_micro", 0))
        for v in operations.values()) / 1000000
    unknown = [v for v in operations.values() if v.get("state") in {"calling", "uncertain"}
        and "actual_micro" not in v.get("usage", {})]
    receipt["uncertain_outcomes"] = len(unknown)
    receipt["unresolved_ceiling_usd"] = sum(v.get("usage", {}).get("estimate_micro", 0) for v in unknown) / 1000000
    if is_local():
        from app.pagebuilder.local import failure_reason
        from app.pagebuilder.tool import progress as tool_progress
        error_reason = (runtime.get("error_reason") or failure_reason(code)) if code else None
        if code == "pagebuilder_interrupted" and unknown:
            error_reason = "Your landing page build was interrupted when your app stopped. Your saved pages are kept. On Website, choose Refresh status to check the last model request before building again."
        return {"job_id": row.id, "state": state, "stage": runtime["stage"], "progress": runtime["progress"],
            "variant_count": doc["variant_count"], "receipt": {"cost_usd": receipt["cost_usd"],
                "uncertain_outcomes": len(unknown)},
            **({"preview_url": f"/api/ventures/{row.venture_id}/pagebuilder/jobs/{row.id}/preview",
                "image_note": doc["result"]["bundles"][0]["manifest"].get("image_note")} if state == "ready" else {}),
            **({"error": code, "error_reason": error_reason} if code else {}),
            **({"retry": {"method": "POST", "url": f"/api/ventures/{row.venture_id}/pagebuilder/build",
                "body": {"new_version_of": row.id}}} if state == "failed" and not unknown else {}),
            **tool_progress(row, doc)}
    reservation_ids = [v.get("usage", {}).get("reservation_id") for v in operations.values()]
    with db()() as session:
        receipt["reserved_micro"] = sum(session.scalars(select(CreditReservation.amount_micro).where(
            CreditReservation.id.in_([rid for rid in reservation_ids if rid]),
            CreditReservation.status.in_(["open", "active", "reconciling"]))))
    return {"job_id": row.id, "state": state, "stage": runtime["stage"], "progress": runtime["progress"],
        "variant_count": doc["variant_count"], "receipt": receipt,
        **({"improvement_available": True, "improvement_reason": "Your page is ready with a simpler version. You can improve it again."}
            if state == "ready" and any(bundle.get("manifest", {}).get("repair_fallback")
                for bundle in doc.get("result", {}).get("bundles", [])) else {}),
        **({"preview_url": f"/api/ventures/{row.venture_id}/pagebuilder/jobs/{row.id}/preview"} if state == "ready" else {}),
        **({"error": code, "error_reason": _error_reason(code)} if code else {})}


def _error_reason(code):
    if is_local():
        from app.pagebuilder.local import failure_reason
        return failure_reason(code)
    from app.tasks.contracts import blocker
    problem = blocker(code)
    return problem["reason"] + " " + problem["unblock_action"]


def _authorized_job(venture_id, caller, job_id):
    wid, _ = authority(venture_id, caller)
    with db()() as session:
        row = session.get(Job, job_id)
        if not row or (row.kind, row.venture_id, row.workspace_id) != (KIND, venture_id, wid):
            raise HTTPException(404, detail={"code": "not_found"})
        session.expunge(row)
        return row


def status(venture_id: str, caller, job_id: str) -> dict:
    return _view(_authorized_job(venture_id, caller, job_id))


def result(venture_id: str, caller, job_id: str) -> dict:
    row = _authorized_job(venture_id, caller, job_id)
    view = _view(row)
    if view["state"] != "ready":
        raise HTTPException(409, detail={"code": view.get("error", "pagebuilder_not_ready")})
    doc = json.loads(row.payload_json)
    output = doc["result"]
    prefix = f"/api/ventures/{venture_id}/pagebuilder/jobs/{job_id}"
    return {**view, **{k: v for k, v in output.items() if k != "bundles"},
        "variants": [{"direction": b["direction"], "manifest": b["manifest"], "qa": b["qa"],
            "preview_url": prefix + f"/preview?variant={i}", "source_url": prefix + f"/source?variant={i}",
            "screenshots": {name: {**metadata, "url": prefix + f"/screenshots/{name}?variant={i}"}
                for name, metadata in b["screenshot_manifest"].items()}}
            for i, b in enumerate(output["bundles"])],
        "manifest": output["bundles"][0]["manifest"], "source_url": prefix + "/source", "publish_ready": False}


def build_spec_for_coding_tool(venture_id: str, caller) -> str:
    wid, _ = authority(venture_id, caller)
    with db()() as session:
        revision_id, _ = snapshot(session, venture_id, wid)
        rows = session.scalars(select(Job).where(Job.kind == KIND, Job.workspace_id == wid, Job.venture_id == venture_id)
            .order_by(Job.created_at.desc()).limit(20))
        for row in rows:
            doc = json.loads(row.payload_json)
            if doc.get("revision_id") == revision_id:
                view = _view(row)
                if view.get("error") == "pagebuilder_candidate_policy_obsolete":
                    raise HTTPException(409, detail={"code": view["error"]})
                if view["state"] == "ready":
                    return doc["result"]["bundles"][0]["spec_markdown"]
    raise HTTPException(409, detail={"code": "pagebuilder_not_ready"})


def _requeue_free_failure(session, row, *, actor, revision_id, inputs, resource_digest):
    """Retry a proven renderer failure under the same inputs, approval and custody."""
    doc = json.loads(row.payload_json)
    runtime = doc["_runtime"]
    saved_quote = runtime.get("quote")
    if (row.status not in FAILED_QUEUE - {"cancelled"} or runtime.get("state") != "failed"
            or runtime.get("error") != "pagebuilder_renderer_unavailable"
            or doc.get("_actor_subject") != actor or doc.get("revision_id") != revision_id
            or runtime.get("input") != inputs or not isinstance(saved_quote, dict)
            or saved_quote.get("resource_digest") != resource_digest
            or type(runtime.get("calls_used")) is not int or runtime["calls_used"] != 0
            or not isinstance(runtime.get("operations"), dict) or runtime["operations"]
            or not row.effect_key or row.effect_key != row.dedupe_key):
        return False
    if session.scalar(select(Job.id).where(Job.effect_key == row.effect_key, Job.id != row.id).limit(1)):
        return False
    for model in (CreditReservation, CreditLedgerEntry):
        if session.scalar(select(model.id).where(or_(model.request_id == row.id,
                model.request_id.startswith(row.id + ":", autoescape=True))).limit(1)):
            return False
    from core.clock import utcnow
    from core.enums import JobStatus
    row.status, row.attempts = JobStatus.SCHEDULED.value, 0
    row.locked_by = row.locked_at = row.started_at = row.finished_at = row.run_after = row.last_error = None
    row.scheduled_for = utcnow()
    runtime.update(state="queued", stage="queued", progress=0)
    runtime.pop("error", None)
    row.payload_json = dump(doc)
    session.flush()
    return True
