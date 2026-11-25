"""Fixed device-work-order protocol for the native-owned background worker.

The bridge supplies its authenticated request function and pins account/device
identity around every network call. Neither a page nor a model can supply a
remote origin, API path, bearer credential, claim digest or price.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from datetime import datetime
from collections.abc import Callable

from coding_agents.jev_decision import Candidate, Observation, reviewable_input_edit
from desktop_runtime.hosted import _is_local as is_local


def build_capability_ready() -> bool:
    try:
        from app.core.local_capabilities import enabled
        return enabled("build_with_my_tool")
    except (ImportError, KeyError) as error:
        if isinstance(error, KeyError) and error.args != ("build_with_my_tool",):
            raise
        from desktop_runtime.agent_status import statuses
        return any(item.get("available") is True for item in statuses())


class ClaimRejected(PermissionError):
    def __init__(self, code: str = ""):
        super().__init__("native_claim_rejected")
        self.code = code


class UpstreamRejected(PermissionError):
    """Core answered with an error. Same text as before for existing handlers."""

    def __init__(self, status: int, code: str = ""):
        super().__init__("native_worker_upstream_rejected")
        self.status, self.code = status, code


class WorkOrderGateway:
    def __init__(self, *, venture_id: str, order_id: str, device_id: str, request: Callable,
                 upload: Callable | None = None, cancellable_request: Callable | None = None,
                 account_id: str | None = None, download: Callable | None = None,
                 local_review_actor: str | None = None):
        if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
               for value in (venture_id, order_id, device_id)) or not callable(request):
            raise ValueError("native_order_identity_invalid")
        if local_review_actor is not None:
            # Only the verified native review bridge supplies this mapping.
            # Core resolves the local Gateway's founder subject to this actor.
            if local_review_actor != "per_local000000" or account_id != "founder":
                raise ValueError("native_order_identity_invalid")
            account_id = local_review_actor
        elif account_id is not None and (not isinstance(account_id, str) or not re.fullmatch(
                r"(?:usr_|per_)[A-Za-z0-9_-]{1,128}" if is_local() else r"usr_[A-Za-z0-9_-]{1,128}", account_id)):
            raise ValueError("native_order_identity_invalid")
        self.account_id = account_id
        self.local_review_actor = local_review_actor
        self.venture_id, self.order_id, self.device_id = venture_id, order_id, device_id
        self.path = f"/api/ventures/{venture_id}/device-work-orders/{order_id}"
        self.request = request
        self.cancellable_request = cancellable_request
        self.upload = upload
        self.download = download
        self.review_digest = None
        self.claim_attempted = False
        self.claim_rejection = None  # Core's conflict code when it definitely refused the claim
        self.claimed = False
        self.execution_id = None
        self.typed_navigation_proposal_v1 = False
        self.browser_cuai_profile_digest = None
        self.browser_qualification_binding = None
        self.browser_qualification_grant_digest = None

    def fail_before_start(self, failure_code: str):
        if self.claim_attempted or self.claimed or self.execution_id:
            raise PermissionError("native_execution_requires_reconciliation")
        saved = self.read()
        result = self.request("POST", self.path + "/fail-before-start", {
            "review_digest": saved["review_digest"], "failure_code": failure_code})
        if (not isinstance(result, dict) or result.get("id") != self.order_id
                or result.get("state") != "failed" or result.get("failure_code") != failure_code):
            raise PermissionError("native_start_failure_unconfirmed")
        return result

    def report_needs_you(self, kind: str, prompt: str):
        if kind not in {"captcha", "code", "other", "permission", "login", "sign_in", "guardrail"}:
            kind = "other"
        return self.request("POST", f"/api/ventures/{self.venture_id}/needs-you/device",
                            {"order_id": self.order_id, "pause_kind": kind, "prompt": prompt[:300]})

    def read(self, *, request=None):
        value = (request or self.request)("GET", self.path, None)
        rotated_cancelled = (isinstance(value, dict)
            and value.get("state") in {"cancelled", "stopped"}
            and isinstance(value.get("claimed_at"), str)
            and isinstance(value.get("execution_id"), str))
        if (not isinstance(value, dict) or value.get("id") != self.order_id
                or value.get("venture_id") != self.venture_id
                or (value.get("device_id") != self.device_id and not rotated_cancelled)
                or not isinstance(value.get("review_digest"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", value["review_digest"])
                or (self.review_digest is not None and value["review_digest"] != self.review_digest)
                or is_local() and self.account_id is not None and value.get("actor_id") != self.account_id
                or self.local_review_actor is not None and value.get("actor_id") != self.local_review_actor):
            raise PermissionError("native_order_identity_changed")
        self._qualification(value)
        return value

    def _qualification(self, saved):
        """Parse controller metadata only; current Core authority remains mandatory."""
        wire = saved.get("browser_qualification_grant")
        binding_wire = saved.get("browser_qualification_trial_binding")
        if wire is None and binding_wire is None:
            if self.browser_qualification_binding is not None:
                raise PermissionError("browser_qualification_binding_changed")
            return None
        from core.browser_qualification import OwnedFixtureGrant, QualificationTrialBinding
        from desktop_runtime.cu_owned_browser_adapter import validate_profile
        try:
            grant = OwnedFixtureGrant.from_dict(wire) if wire is not None else None
            binding = QualificationTrialBinding.from_dict(binding_wire) if binding_wire is not None else grant.trial_binding()
            profile = ((saved.get("manifest") or {}).get("general_browser_verification") or {}).get("execution_profile")
            expected = dict(actor_id=self.account_id, device_id=self.device_id,
                workspace_id=saved.get("workspace_id"), venture_id=self.venture_id,
                order_id=self.order_id, review_digest=saved.get("review_digest"),
                profile_digest=validate_profile(profile))
            if saved.get("state") in {"cancelled", "stopped"}:
                if any(getattr(binding, key) != value for key, value in expected.items()):
                    raise ValueError("binding_changed")
            else:
                binding.assert_current(**expected, now=int(time.time()))
            if grant is not None and grant.trial_binding() != binding:
                raise ValueError("grant_binding_mismatch")
            if (saved.get("actor_id") != self.account_id or self.account_id is None
                    or self.browser_qualification_binding is not None
                    and self.browser_qualification_binding != binding):
                raise ValueError("binding_changed")
            if (self.browser_qualification_binding is not None
                    and self.browser_qualification_grant_digest != (grant.grant_digest if grant else None)):
                raise ValueError("grant_changed")
        except (TypeError, ValueError, AttributeError, PermissionError):
            raise PermissionError("browser_qualification_binding_changed") from None
        return grant, binding

    def qualification_current(self):
        saved = self.current()
        qualified = self._qualification(saved)
        if qualified is None:
            raise PermissionError("browser_qualification_authority_required")
        return qualified[0]

    def qualification_order_status(self):
        grant = self.qualification_current()
        if grant is None:
            raise PermissionError("browser_qualification_fixture_required")
        value = self.request("GET", f"/api/ventures/{self.venture_id}/browser-qualification/"
            f"{grant.trial_id}/orders/{self.order_id}", None)
        expected = {key: getattr(grant, key) for key in (
            "actor_id", "device_id", "workspace_id", "venture_id", "trial_id", "order_id",
            "catalog_digest", "task_id", "seed_digest", "review_digest", "profile_digest", "expires_at")}
        expected["grant_digest"] = grant.grant_digest
        if (not isinstance(value, dict) or any(value.get(key) != item for key, item in expected.items())
                or type(value.get("expires_at")) is not int
                or not isinstance(value.get("completed_rules"), list)
                or any(not isinstance(key, str) for key in value["completed_rules"])
                or len(set(value["completed_rules"])) != len(value["completed_rules"])
                or not set(value["completed_rules"]) <= {rule.rule_id for rule in grant.rules}
                or not isinstance(value.get("read_prerequisites", value["completed_rules"]), list)
                or any(not isinstance(key, str) for key in value.get("read_prerequisites", value["completed_rules"]))
                or len(set(value.get("read_prerequisites", value["completed_rules"])))
                    != len(value.get("read_prerequisites", value["completed_rules"]))
                or not set(value["completed_rules"]) <= set(value.get("read_prerequisites", value["completed_rules"]))
                or not set(value.get("read_prerequisites", value["completed_rules"])) <= {rule.rule_id for rule in grant.rules}
                or not isinstance(value.get("rules"), dict)
                or set(value["rules"]) != {rule.rule_id for rule in grant.rules}):
            raise PermissionError("browser_qualification_status_unconfirmed")
        for rule in grant.rules:
            count = value["rules"][rule.rule_id]
            if (not isinstance(count, dict) or set(count) != {"request_count", "committed_count", "protected"}
                    or type(count["request_count"]) is not int or not 0 <= count["request_count"] <= rule.max_requests
                    or type(count["committed_count"]) is not int or not 0 <= count["committed_count"] <= rule.max_commits
                    or count["committed_count"] > count["request_count"] or type(count["protected"]) is not bool):
                raise PermissionError("browser_qualification_status_unconfirmed")
        self.qualification_current()
        return value

    def claim(self):
        if self.claim_attempted:
            raise PermissionError("native_claim_already_attempted")
        value = self.read()
        resuming = value.get("state") == "waiting_for_device"
        if value.get("state") not in {"approved", "waiting_for_device"}:
            raise PermissionError("native_order_not_approved")
        manifest = value.get("manifest") or {}
        local_product = is_local() and manifest.get("coding_loop_verification") is not None
        if local_product:
            if resuming and value.get("execution_id"):
                raise PermissionError("native_execution_requires_reconciliation")
            capabilities = {"available": build_capability_ready(), "coding_loop_v1": True}
        else:
            capabilities = self.request("GET", self.path + "/capabilities", None)
        if capabilities.get("available") is not True:
            raise PermissionError("native_execution_unavailable")
        if ((manifest.get("coding_loop_verification") is not None
                and capabilities.get("coding_loop_v1") is not True)
                or (manifest.get("deploy_verification") is not None
                    and capabilities.get("owned_deploy_v1") is not True)):
            raise PermissionError("native_product_execution_unavailable")
        qualification = self._qualification(value)
        if qualification is not None and capabilities.get("browser_qualification_v1") is not True:
            raise PermissionError("browser_qualification_capability_unavailable")
        candidate = ((value.get("manifest") or {}).get("coding_verification") or {}).get("pagebuilder_candidate")
        if candidate is not None:
            from desktop_runtime.pagebuilder_reference import validate_binding
            validate_binding(candidate, venture_id=self.venture_id, workspace_id=value.get("workspace_id"))
            if capabilities.get("pagebuilder_reference_v1") is not True or not callable(self.download):
                raise PermissionError("native_pagebuilder_reference_unavailable")
        if ((value.get('manifest') or {}).get('general_browser_verification') is not None
                and capabilities.get('general_browser_execution_v1') is not True):
            # Old Core's broad browser flag cannot attest this beta boundary.
            raise PermissionError('native_execution_unavailable')
        profile = ((value.get('manifest') or {}).get('general_browser_verification') or {}).get('execution_profile')
        profile_digest = None
        if profile is not None:
            from desktop_runtime.cu_owned_browser_adapter import validate_profile
            profile_digest = validate_profile(profile)
            if (capabilities.get(profile['version']) is not True
                    or capabilities.get('execution_profile_digest') != profile_digest):
                raise PermissionError('browser_cuai_capability_unavailable')
        self.review_digest = value["review_digest"]
        self.claim_attempted = True  # Unknown response must never cause a retry.
        try:
            result = self.request("POST", self.path + ("/heartbeat" if resuming else "/claim"),
                {"execution_id": value["execution_id"], "resume": True} if resuming
                else {"review_digest": self.review_digest})
        except UpstreamRejected as error:
            if 400 <= error.status < 500 and error.status not in {401, 408, 429}:
                # A definite refusal: Core did not claim, so nothing ran and nothing is uncertain.
                self.claim_attempted = False
                self.claim_rejection = error.code or "rejected"
                raise ClaimRejected(error.code) from None
            raise
        if (result.get("state") != "claimed" or result.get("review_digest") != self.review_digest
                or result.get("id") != self.order_id or result.get("device_id") != self.device_id
                or resuming and result.get("execution_id") != value.get("execution_id")
                or not isinstance(result.get("execution_id"), str)
                or not re.fullmatch(r"dex_[A-Za-z0-9]+", result["execution_id"])):
            raise PermissionError("native_claim_unconfirmed")
        self.claimed = True
        self.execution_id = result["execution_id"]
        self.typed_navigation_proposal_v1 = capabilities.get("typed_navigation_proposal_v1") is True
        self.browser_cuai_profile_digest = profile_digest
        if qualification is not None:
            self.browser_qualification_binding = qualification[1]
            self.browser_qualification_grant_digest = qualification[0].grant_digest if qualification[0] else None
            self._qualification(result)
        return result

    def prepare_product_run(self, plan: dict) -> dict:
        source = self.read()
        descriptor = (source.get("manifest") or {}).get("coding_loop_verification") or {}
        if (not isinstance(plan.get("plan_digest"), str) or not re.fullmatch(r"[a-f0-9]{64}", plan["plan_digest"])
                or not is_local() or not build_capability_ready() or source.get("state") != "stopped"
                or descriptor.get("phase") != "plan" or not source.get("closed_at")
                or not source.get("requested_build")
                or (source.get("approved_plan") or {}).get("plan_digest") != plan.get("plan_digest")
                or (source.get("native_stop") or {}).get("processes_stopped") is not True
                or (source.get("native_stop") or {}).get("task_verified") is not True):
            raise PermissionError("coding_plan_changed")
        result = self.request("POST", self.path + "/prepare-run", {})
        run = (result.get("manifest") or {}).get("coding_loop_verification") or {}
        if (result.get("state") != "approved" or result.get("venture_id") != self.venture_id
                or result.get("device_id") != self.device_id or result.get("actor_id") != self.account_id
                or not isinstance(result.get("id"), str) or result["id"] == self.order_id
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", result["id"])
                or run.get("phase") != "implement" or run.get("plan_order_id") != self.order_id
                or run.get("provider") != descriptor.get("provider")
                or run.get("workspace_slug") != descriptor.get("workspace_slug")
                or run.get("plan_digest") != plan.get("plan_digest")
                or result.get("requested_build") != source.get("requested_build")):
            raise PermissionError("coding_plan_changed")
        return result

    def heartbeat(self, *, step=0, percent=0, last_action="starting"):
        if not self.claimed:
            raise PermissionError("native_claim_required")
        value = self.request("POST", self.path + "/heartbeat", {"execution_id": self.execution_id,
            "step": step, "percent": percent, "last_action": last_action})
        if (value.get("id") != self.order_id or value.get("device_id") != self.device_id
                or value.get("execution_id") != self.execution_id or value.get("state") != "claimed"
                or value.get("review_digest") != self.review_digest):
            raise PermissionError("native_heartbeat_unconfirmed")
        return value

    def current(self, *, request=None):
        if not self.claimed:
            raise PermissionError("native_claim_required")
        request = request or self.request
        value = self.read(request=request)
        if is_local() and (value.get("manifest") or {}).get("coding_loop_verification") and not build_capability_ready():
            raise PermissionError("coding_capability_disabled")
        expired_lease = (isinstance(value.get("lease_expires_at"), str)
                         and time.time() >= datetime.fromisoformat(value["lease_expires_at"]).timestamp())
        if (value.get("state") == "waiting_for_device" or value.get("state") == "claimed" and expired_lease) and value.get("execution_id") == self.execution_id:
            self.heartbeat()
            value = self.read(request=request)
        if value.get("state") != "claimed" or value.get("execution_id") != self.execution_id:
            raise PermissionError("native_order_not_executing")
        # Saved state alone cannot establish current authority: expiry, plan
        # supersession, disabled execution and reconciliation also revoke it.
        if request("GET", self.path + "/authority", None).get("current") is not True:
            raise PermissionError("native_order_authority_unconfirmed")
        return value

    def decide(self, invocation_id: str, observation: Observation) -> Candidate:
        self.current()
        value = self.request("POST", self.path + "/browser-decision",
                             {"invocation_id": invocation_id, "observation": observation.model_dump()})
        if value.get("authority") != "proposal_only" or not isinstance(value.get("candidate"), dict):
            raise PermissionError("native_decision_response_invalid")
        candidate = Candidate(**value["candidate"])
        if candidate.observation_digest != observation.digest():
            raise PermissionError("native_decision_observation_changed")
        self.current()
        return candidate

    def pagebuilder_reference(self, binding: dict, *, stop, deadline) -> bytes:
        from desktop_runtime.pagebuilder_reference import validate_binding
        if not self.claimed:
            raise PermissionError("native_claim_required")
        if not callable(self.cancellable_request):
            raise RuntimeError("native_model_transport_unavailable")
        def request(method, path, body):
            return self.cancellable_request(method, path, body, stop=stop, deadline=deadline)
        def check_current():
            saved = self.current(request=request)
            pinned = validate_binding(binding, venture_id=self.venture_id, workspace_id=saved.get("workspace_id"))
            if ((saved.get("manifest") or {}).get("coding_verification") or {}).get("pagebuilder_candidate") != pinned:
                raise PermissionError("native_pagebuilder_binding_changed")
            if stop.is_set() or time.monotonic() >= deadline:
                raise PermissionError("native_reference_authority_unconfirmed")
            return pinned
        pinned = check_current()
        if not callable(self.download):
            raise PermissionError("native_pagebuilder_reference_unavailable")
        blob = self.download(self.path + "/pagebuilder-reference", stop=stop, deadline=deadline,
                             max_bytes=pinned["reference_size_bytes"])
        check_current()
        if (not isinstance(blob, bytes) or len(blob) != pinned["reference_size_bytes"]
                or hashlib.sha256(blob).hexdigest() != pinned["reference_sha256"]):
            raise ValueError("pagebuilder_reference_mismatch")
        return blob

    def browser_ai_call(self, invocation_id, phase, observation, *, profile_digest,
                        checkpoint_digest, current_load, context, cancelled=None, deadline=None,
                        prepare_request=None):
        self.browser_decision_failure_code = None
        try:
            return self._browser_ai_call(invocation_id, phase, observation, profile_digest=profile_digest,
                checkpoint_digest=checkpoint_digest, current_load=current_load, context=context,
                cancelled=cancelled, deadline=deadline, prepare_request=prepare_request)
        except Exception as error:
            self.browser_decision_failed(error)
            raise

    def browser_decision_failed(self, error):
        codes = {
            'browser_cuai_request_over_bound': 'cu_browser_request_over_bound',
            'browser_cuai_profile_changed': 'cu_browser_profile_changed',
            'browser_cuai_capability_unavailable': 'cu_browser_capability_unavailable',
            'browser_cuai_evidence_stale': 'cu_browser_evidence_stale',
            'browser_cuai_evidence_unavailable': 'cu_browser_evidence_unavailable',
            'browser_cuai_observation_invalid': 'cu_browser_observation_invalid',
            'browser_cuai_sensitive_observation': 'cu_browser_sensitive_observation',
            'browser_cuai_response_invalid': 'cu_browser_response_invalid',
            'browser_cuai_reply_unusable': 'cu_browser_reply_unusable',
            'browser_cuai_plan_invalid': 'cu_browser_plan_invalid',
            'browser_cuai_verdict_invalid': 'cu_browser_verdict_invalid',
            'browser_cuai_nonactor_effect_refused': 'cu_browser_nonactor_effect_refused',
            'browser_cuai_cancelled_or_expired': 'cu_browser_decision_expired',
            'browser_cuai_answer_invalid': 'cu_browser_answer_invalid',
            'browser_cuai_action_unsupported': 'cu_browser_action_unsupported',
            'browser_cuai_terminal_action_refused': 'cu_browser_terminal_action_refused',
            'browser_cuai_effect_proposal_invalid': 'cu_browser_effect_proposal_invalid',
            'browser_cuai_dispatch_requires_reconciliation': 'cu_browser_dispatch_unconfirmed',
            'browser_cuai_call_limit_reached': 'cu_browser_call_limit_reached',
            'native_model_transport_unavailable': 'cu_browser_transport_unavailable',
            'native_model_transport_busy': 'cu_browser_transport_busy',
            'native_model_request_wrong_thread': 'cu_browser_transport_wrong_thread',
            'native_worker_request_unconfirmed': 'cu_browser_transport_unconfirmed',
            'native_worker_account_changed': 'cu_auth_lost',
            'native_model_request_scope_invalid': 'cu_browser_decision_scope_invalid',
            'native_model_request_deadline': 'cu_browser_decision_timeout',
            'native_model_request_stopped': 'cu_stopped',
            'provider_outcome_unknown': 'cu_browser_provider_outcome_unknown',
            'device_browser_ai_request_bounds': 'cu_browser_provider_request_over_bound',
            'device_browser_ai_request_invalid': 'cu_browser_server_request_invalid',
            'device_browser_ai_reply_invalid': 'cu_browser_reply_invalid',
            'device_browser_ai_reply_truncated': 'cu_browser_reply_unusable',
            'device_browser_ai_provider_or_usage_unconfirmed': 'cu_browser_provider_outcome_unknown',
            'device_browser_ai_configuration_unavailable': 'cu_browser_provider_unavailable',
        }
        if isinstance(error, UpstreamRejected):
            fallback = ('cu_auth_lost' if error.status in {401, 403} else
                'cu_browser_server_unavailable' if error.status >= 500 else 'cu_browser_upstream_rejected')
            self.browser_decision_failure_code = codes.get(error.code, fallback)
        else:
            from coding_agents.cu_ai_loop import ActionError
            fallback = ('cu_browser_reply_invalid' if isinstance(error, (ActionError, json.JSONDecodeError)) else
                'cu_browser_decision_timeout' if isinstance(error, TimeoutError) else
                'cu_browser_transport_unavailable' if isinstance(error, ConnectionError) else
                'cu_browser_decision_runtime_error')
            self.browser_decision_failure_code = codes.get(str(error), fallback)

    def _browser_ai_call(self, invocation_id, phase, observation, *, profile_digest,
                        checkpoint_digest, current_load, context, cancelled=None, deadline=None,
                        prepare_request=None):
        """Explicit saved browser-only phase; no native AI-step or Jev fallback."""
        if phase not in {'plan','actor','verify'} or self.browser_cuai_profile_digest != profile_digest:
            raise PermissionError('browser_cuai_capability_unavailable')
        request=self.request
        if cancelled is not None:
            if not callable(self.cancellable_request):
                raise PermissionError('native_model_transport_unavailable')
            deadline=min(deadline if deadline is not None else time.monotonic()+360,time.monotonic()+360)
            def request(method,path,body):
                return self.cancellable_request(method,path,body,stop=cancelled,deadline=deadline)
        saved=self.current(request=request)
        profile=((saved.get('manifest') or {}).get('general_browser_verification') or {}).get('execution_profile')
        from desktop_runtime.cu_owned_browser_adapter import validate_profile
        if validate_profile(profile)!=profile_digest:
            raise PermissionError('browser_cuai_profile_changed')
        keys=('qualified','session_id','page_id','sequence','location','document_id',
              'ready_state','visible_text','visibility_scope','observation_digest')
        if not isinstance(current_load,dict):
            raise PermissionError('browser_cuai_evidence_unavailable')
        load={**{key:current_load.get(key) for key in keys},'response':dict(current_load.get('response') or {})}
        if current_load.get('kind') == 'qualification_error':
            load['kind'] = 'qualification_error'
        body={'invocation_id':invocation_id,'phase':phase,'execution_profile_digest':profile_digest,
              'checkpoint_digest':checkpoint_digest,'observation':observation.model_dump(),
              'current_load':load,'context':context}
        size = len(json.dumps(body).encode())
        while size > profile['max_request_bytes']:
            if not callable(prepare_request):
                raise PermissionError('browser_cuai_request_over_bound')
            page_bytes = len(json.dumps(observation.context).encode()) + len(json.dumps(
                [control.model_dump() for control in observation.controls]).encode())
            context, current_load = prepare_request(max(0, page_bytes - (size - profile['max_request_bytes']) - 256))
            body.update(observation=observation.model_dump(), context=context)
            body['current_load']['observation_digest'] = current_load.get('observation_digest')
            trimmed_size = len(json.dumps(body).encode())
            if trimmed_size >= size:
                raise PermissionError('browser_cuai_request_over_bound')
            size = trimmed_size
        value=request('POST',self.path+'/browser-ai-call',body)
        role=profile['roles'][phase]
        if (not isinstance(value,dict) or value.get('authority')!='proposal_only'
                or value.get('source_kind')!='browser_cuai_text_v1'
                or value.get('invocation_id')!=invocation_id or value.get('phase')!=phase
                or value.get('profile_digest')!=profile_digest
                or value.get('observation_digest')!=observation.digest()
                or value.get('model')!=role['model'] or value.get('service_tier')!='default'
                or type(value.get('input_tokens')) is not int or not 0<=value['input_tokens']<=200000
                or type(value.get('output_tokens')) is not int or not 0<=value['output_tokens']<=role['max_output_tokens']
                or not isinstance(value.get('reply'),str) or len(value['reply'].encode())>65536
                or not isinstance(value.get('effect_proposals'),list)):
            raise PermissionError('browser_cuai_response_invalid')
        if phase!='actor' and value['effect_proposals']:
            raise PermissionError('browser_cuai_nonactor_effect_refused')
        qualified = self._qualification(saved)
        grant = qualified[0] if qualified is not None else None
        for proposal in value['effect_proposals']:
            keys = {'action_index','invocation_id','candidate'}
            if (not isinstance(proposal,dict) or not keys <= set(proposal)
                    or not set(proposal) <= keys | ({'qualification_rule_ids'} if grant is not None else set())
                    or type(proposal['action_index']) is not int or proposal['action_index']!=0
                    or proposal['invocation_id']!=invocation_id+'_a0' or not isinstance(proposal['candidate'],dict)):
                raise PermissionError('browser_cuai_effect_proposal_invalid')
            candidate=Candidate(**proposal['candidate'])
            operations = {'click','type'}
            if grant is not None:
                # Select/check use the existing type/click dispatch shapes.
                operations = {'click','type','navigate','scroll','extract','hover','new_tab','switch_tab'} & set(grant.allowed_operations)
                if 'select' in grant.allowed_operations:
                    operations.add('type')
                if 'check' in grant.allowed_operations:
                    operations.add('click')
            elif profile['version'] == 'browser_cuai_text_v2' or context.get('bounded_reads') is True:
                operations.update({'navigate','scroll','extract'})
            if candidate.operation not in operations or candidate.observation_digest!=observation.digest():
                raise PermissionError('browser_cuai_effect_proposal_invalid')
            rule_ids = proposal.get('qualification_rule_ids', [])
            if (not isinstance(rule_ids, list) or len(rule_ids) > 128
                    or any(not isinstance(key, str) for key in rule_ids)
                    or len(set(rule_ids)) != len(rule_ids)
                    or rule_ids and (candidate.operation != 'click' or grant is None
                        or not set(rule_ids) <= {rule.rule_id for rule in grant.rules if rule.method == 'POST'})):
                raise PermissionError('browser_qualification_proposal_invalid')
        if len(value['effect_proposals'])>1 or context.get('terminal_only') is True and value['effect_proposals']:
            raise PermissionError('browser_cuai_effect_proposal_invalid')
        self.current(request=request)
        return value

    def navigation_proposal(self, invocation_id: str, observation: Observation,
                            offer: dict, qualified_loads: list, *, expected_location: str) -> Candidate:
        """Only advertised Core can create a durable reviewed navigation proposal."""
        if self.typed_navigation_proposal_v1 is not True:
            raise PermissionError("typed_navigation_capability_unavailable")
        self.current()
        keys = ('qualified', 'session_id', 'page_id', 'sequence', 'location', 'document_id',
                'ready_state', 'visible_text', 'visibility_scope', 'observation_digest')
        loads = [{**{key: proof.get(key) for key in keys},
                  'response': dict(proof.get('response') or {})} for proof in qualified_loads]
        value = self.request("POST", self.path + "/browser-navigation-proposal", {
            'invocation_id': invocation_id, 'observation': observation.model_dump(),
            'current_load': offer['current_load'], 'qualified_loads': loads,
            'control_snapshots': offer['control_snapshots']})
        if (not isinstance(value, dict) or value.get('authority') != 'proposal_only'
                or value.get('source_kind') != 'typed_reviewed_url_v1'
                or value.get('invocation_id') != invocation_id
                or value.get('expected_location') != expected_location
                or not isinstance(value.get('candidate'), dict)):
            raise PermissionError('typed_navigation_proposal_invalid')
        candidate = Candidate(**value['candidate'])
        if (candidate.operation != 'click' or candidate.target != offer['target']
                or candidate.observation_digest != observation.digest()
                or candidate.value_index is not None):
            raise PermissionError('typed_navigation_proposal_changed')
        self.current()
        return candidate

    def review_desktop_image(self, invocation_id: str, snapshot_id: str,
                             screenshot_png_base64: str, observation: Observation | None = None) -> dict:
        """Request observation only; a vision reply is never an effect grant."""
        self.current()
        value = self.request("POST", self.path + "/desktop-vision", {
            "invocation_id": invocation_id, "snapshot_id": snapshot_id,
            "screenshot_png_base64": screenshot_png_base64,
            "observation": observation.model_dump() if observation else None})
        report = value.get("report") if isinstance(value, dict) else None
        allowed_ids = ({item.id for item in observation.controls if "click" in item.operations}
                       if observation else set())
        input_edit_ids = ({item.id for item in observation.controls
            if "click" in item.operations and reviewable_input_edit(observation.goal, item.label)}
            if observation else set())
        if (not isinstance(value, dict) or value.get("authority") != "observation_only"
                or value.get("snapshot_id") != snapshot_id
                or value.get("image_sha256") != hashlib.sha256(
                    base64.b64decode(screenshot_png_base64, validate=True)).hexdigest()
                or not isinstance(report, dict)
                or report.get("verdict") not in {"done", "continue", "blocked"}
                or not isinstance(report.get("visible_facts"), list)
                or not isinstance(report.get("next_step"), str)
                or report.get("click_control_id") not in {None, "none", *allowed_ids}):
            raise PermissionError("native_vision_response_invalid")
        input_edit_id = report.get("input_edit_control_id", "none")
        if (input_edit_id not in {"none", *input_edit_ids}
                or input_edit_id != "none" and input_edit_id != report.get("click_control_id")
                or report.get("click_control_id") in input_edit_ids
                   and input_edit_id != report.get("click_control_id")):
            raise PermissionError("native_vision_response_invalid")
        if report.get("click_control_id") not in {None, "none"} and (
                value.get("proposal_invocation_id") != invocation_id
                or observation is None or value.get("observation_digest") != observation.digest()):
            raise PermissionError("native_vision_proposal_unavailable")
        self.current()
        return value

    def ai_step(self, invocation_id: str, kind: str, context: dict, png_base64: str | None = None,
                *, cancelled=None, deadline=None) -> dict:
        """AI-primary loop: one metered model call (step/plan/verify). Proposal only; never an effect grant."""
        if kind not in {"step", "plan", "verify"}:
            raise ValueError("native_ai_step_kind_invalid")
        request = self.request
        if cancelled is not None:
            if not callable(self.cancellable_request):
                raise RuntimeError("native_model_transport_unavailable")
            deadline = min(deadline if deadline is not None else time.monotonic() + 40, time.monotonic() + 40)
            def request(method, path, body):
                return self.cancellable_request(method, path, body, stop=cancelled, deadline=deadline)
        self.current(request=request)
        body = {"invocation_id": invocation_id, "kind": kind, "context": {**context, "action_contract_version": 3}}
        if png_base64:
            body["image_png_base64"] = png_base64
        value = request("POST", self.path + "/ai-step", body)
        if not isinstance(value, dict) or value.get("authority") != "proposal_only":
            raise PermissionError("native_ai_step_response_invalid")
        self.current(request=request)
        return value

    def _qualification_request(self, qualification):
        from core.browser_qualification import request_binding_digest
        grant = self.qualification_current()
        if (grant is None or not isinstance(qualification, dict) or set(qualification) != {
                "grant_digest", "rule_id", "request_id", "request_digest"}
                or qualification.get("grant_digest") != grant.grant_digest):
            raise PermissionError("browser_qualification_request_invalid")
        rules = [r for r in grant.rules if r.rule_id == qualification.get("rule_id")]
        try:
            if (len(rules) != 1 or request_binding_digest(grant, rules[0], qualification["request_id"])
                    != qualification["request_digest"]):
                raise ValueError("request_changed")
        except (KeyError, TypeError, ValueError):
            raise PermissionError("browser_qualification_request_invalid") from None
        return grant, rules[0]

    def qualification_receipt(self, qualification, *, invocation_id, effect_digest):
        """Signed Core readback, never authority inferred from a page response."""
        grant, rule = self._qualification_request(qualification)
        value = self.request("GET", f"/api/ventures/{self.venture_id}/browser-qualification/"
            f"{grant.trial_id}/requests/{qualification['request_id']}", None)
        expected = {**qualification, "trial_id": grant.trial_id, "order_id": self.order_id,
            "execution_id": self.execution_id, "invocation_id": invocation_id,
            "effect_digest": effect_digest, "workspace_id": grant.workspace_id,
            "venture_id": self.venture_id, "actor_id": self.account_id, "device_id": self.device_id,
            "method": rule.method, "path": rule.path, "query": rule.query}
        if (not isinstance(value, dict) or any(value.get(key) != item for key, item in expected.items())
                or value.get("state") not in {"pending", "committed", "rejected_before_mutation", "unknown"}
                or type(value.get("mutated")) is not bool
                or type(value.get("server_response_sent")) is not bool
                or not isinstance(value.get("body_digest"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", value["body_digest"])
                or value.get("status_code") is not None and (type(value["status_code"]) is not int
                    or not 100 <= value["status_code"] <= 599)
                or value.get("state") == "committed" and value.get("mutated") is not (rule.method == "POST")
                or value.get("state") == "rejected_before_mutation" and (
                    rule.method != "POST" or rule.rejected_retry_limit != 1
                    or value.get("mutated") is not False or value.get("status_code") not in {200, 400})
                or value.get("state") in {"committed", "rejected_before_mutation"} and (
                    not isinstance(value.get("response_digest"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", value["response_digest"])
                    or type(value.get("response_size_bytes")) is not int
                    or not 0 <= value["response_size_bytes"] <= 65536
                    or not isinstance(value.get("response_mime_type"), str)
                    or not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", value["response_mime_type"]))):
            raise PermissionError("browser_qualification_receipt_unconfirmed")
        self.qualification_current()
        return value

    def bind_qualification_effect(self, qualification, *, invocation_id, effect_digest):
        self._qualification_request(qualification)
        value = self.request("POST", self.path + "/effects/bind-qualification", {
            "invocation_id": invocation_id, "effect_digest": effect_digest, "qualification": qualification})
        # False or a lost acknowledgement never permits a second HTTP dispatch.
        return isinstance(value, dict) and value.get("bound_now") is True

    def qualification_read_receipt(self, response, *, allow_pending=False):
        """Rejoin an actual source GET response/native artifact to its delivered Core receipt."""
        from core.browser_qualification import request_binding_digest
        grant = self.qualification_current()
        if (grant is None or not isinstance(response, dict)
                or response.get("complete") is not True
                or not isinstance(response.get("request_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", response["request_id"])
                or type(response.get("status")) is not int
                or not (200 <= response["status"] < 300 or 400 <= response["status"] < 600)
                or type(response.get("size_bytes")) is not int or not 0 <= response["size_bytes"] <= 1048576
                or not isinstance(response.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", response["sha256"])
                or not isinstance(response.get("mime_type"), str)
                or not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", response["mime_type"])):
            raise PermissionError("browser_qualification_receipt_unconfirmed")
        status = self.qualification_order_status()
        try:
            rule = grant.request_rule(url=response.get("url"), method="GET",
                completed_rules=status.get("read_prerequisites", status["completed_rules"]))
        except (ValueError, TypeError):
            raise PermissionError("browser_qualification_receipt_unconfirmed") from None
        value = self.request("GET", f"/api/ventures/{self.venture_id}/browser-qualification/"
            f"{grant.trial_id}/requests/{response['request_id']}", None)
        expected = {"grant_digest": grant.grant_digest, "trial_id": grant.trial_id,
            "request_id": response["request_id"], "rule_id": rule.rule_id,
            "request_digest": request_binding_digest(grant, rule, response["request_id"]),
            "order_id": self.order_id, "execution_id": self.execution_id,
            "workspace_id": grant.workspace_id, "venture_id": self.venture_id,
            "actor_id": self.account_id, "device_id": self.device_id, "method": "GET",
            "path": rule.path, "query": rule.query, "status_code": response["status"],
            "response_digest": response["sha256"], "response_size_bytes": response["size_bytes"],
            "response_mime_type": response["mime_type"], "mutated": False,
            "state": "failed_read" if response["status"] >= 400 else "committed"}
        if (not isinstance(value, dict) or any(value.get(key) != item for key, item in expected.items())
                or type(value.get("status_code")) is not int
                or type(value.get("response_size_bytes")) is not int
                or not 0 <= value["response_size_bytes"] <= 1048576
                or value.get("mutated") is not False
                or type(value.get("server_response_sent")) is not bool
                or value["server_response_sent"] is not True and allow_pending is not True):
            raise PermissionError("browser_qualification_receipt_unconfirmed")
        self.qualification_current()
        return value

    def admit_effect(self, invocation_id: str, observation_digest: str, effect_digest: str,
                     *, qualification=None) -> bool:
        self.current()
        body = {
            "invocation_id": invocation_id, "observation_digest": observation_digest,
            "effect_digest": effect_digest}
        if qualification is not None:
            self._qualification_request(qualification)
            body["qualification"] = qualification
        result = self.request("POST", self.path + "/effects/admit", body)
        return result.get("admitted_now") is True

    def effect_receipt(self, invocation_id: str, effect_digest: str, outcome: str,
                       pre_dispatch_code: str | None = None):
        # Late native outcomes remain reportable after cancellation. This method
        # cannot reopen authority or retry an effect.
        payload = {"invocation_id": invocation_id, "effect_digest": effect_digest, "outcome": outcome}
        if pre_dispatch_code is not None:
            payload["pre_dispatch_code"] = pre_dispatch_code
        return self.request("POST", self.path + "/effects/receipt", payload)

    def reconcile_desktop_pre_dispatch(self, execution_id: str, evidence_digest: str):
        """Settle a cancelled order whose only effect was rejected before I/O."""
        return self.request("POST", self.path + "/stop-receipt", {
            "execution_id": execution_id, "evidence_digest": evidence_digest,
            "processes_stopped": True})

    def retry_stopped_known_usage(self, execution_id: str, evidence_digest: str):
        """Retry the exact saved stop receipt; never infer or replay an effect."""
        value = self.read()
        if (value.get("state") != "cancelled" or value.get("execution_id") != execution_id
                or value.get("native_stop") != {"execution_id": execution_id,
                    "evidence_digest": evidence_digest, "processes_stopped": True}
                or not isinstance(value.get("invocations"), dict)
                or any(call.get("state") != "completed" or
                       (call.get("effect") and call["effect"].get("state") not in {"returned", "not_dispatched"})
                       for call in value["invocations"].values())):
            raise PermissionError("native_known_usage_reconciliation_unavailable")
        result = self.request("POST", self.path + "/stop-receipt", {
            "execution_id": execution_id, "evidence_digest": evidence_digest,
            "processes_stopped": True})
        if result.get("state") != "stopped":
            raise PermissionError("native_known_usage_reconciliation_unavailable")
        return result

    def settle_stopped_unknown_usage(self, execution_id: str, evidence_digest: str):
        """Charge only admitted call ceilings when actual usage cannot be proven."""
        value = self.read()
        if (value.get("state") != "cancelled" or value.get("execution_id") != execution_id
                or value.get("native_stop") != {"execution_id": execution_id,
                    "evidence_digest": evidence_digest, "processes_stopped": True}):
            raise PermissionError("native_unknown_usage_reconciliation_unavailable")
        result = self.request("POST", self.path + "/unknown-usage-reconciliation", {
            "execution_id": execution_id, "evidence_digest": evidence_digest})
        if (result.get("state") != "stopped"
                or result.get("reconciliation") != "admitted_quote_upper_bound"):
            raise PermissionError("native_unknown_usage_reconciliation_unavailable")
        return result

    def reconcile_stopped_desktop_text(self, execution_id: str, evidence_digest: str):
        """Settle known usage while preserving one uncertain reviewed type effect."""
        value = self.read()
        if (value.get("execution_id") != execution_id or value.get("state") != "cancelled"
                or value.get("native_stop") != {"execution_id": execution_id,
                    "evidence_digest": evidence_digest, "processes_stopped": True}):
            raise PermissionError("native_desktop_text_reconciliation_unavailable")
        result = self.request("POST", self.path + "/stop-receipt", {
            "execution_id": execution_id, "evidence_digest": evidence_digest,
            "processes_stopped": True})
        if result.get("state") != "stopped":
            raise PermissionError("native_desktop_text_reconciliation_unavailable")
        return result

    def reconcile_acknowledged_desktop_click(self, execution_id: str, evidence_digest: str):
        """Settle usage after a founder-reviewed uncertain click; never replay it."""
        value = self.read()
        marker = value.get("uncertain_desktop_click_ack") or {}
        if (value.get("execution_id") != execution_id or value.get("state") != "cancelled"
                or value.get("native_stop") != {"execution_id": execution_id,
                    "evidence_digest": evidence_digest, "processes_stopped": True}
                or marker.get("execution_id") != execution_id
                or marker.get("evidence_digest") != evidence_digest):
            raise PermissionError("native_desktop_click_reconciliation_unavailable")
        result = self.request("POST", self.path + "/stop-receipt", {
            "execution_id": execution_id, "evidence_digest": evidence_digest,
            "processes_stopped": True})
        if result.get("state") != "stopped":
            raise PermissionError("native_desktop_click_reconciliation_unavailable")
        return result

    def iteration_receipt(self, receipt: dict):
        if not self.claimed or not self.execution_id:
            raise PermissionError("native_claim_required")
        return self.request("POST", self.path + "/iterations",
            {**receipt, "execution_id": self.execution_id})

    def coding_event(self, event: dict):
        if not self.claimed or not self.execution_id:
            raise PermissionError("native_claim_required")
        return self.request("POST", self.path + "/events", {**event, "execution_id": self.execution_id})

    def post_plan(self, receipt: dict):
        if not self.claimed or not self.execution_id:
            raise PermissionError("native_claim_required")
        return self.request("POST", self.path + "/plan",
            {**receipt, "execution_id": self.execution_id})

    def landing_result(self, job_id: str, blob: bytes):
        if not is_local() or not self.claimed or not self.execution_id:
            raise PermissionError("native_claim_required")
        return self.request("POST", f"/api/ventures/{self.venture_id}/pagebuilder/jobs/{job_id}/tool-result",
            {"order_id": self.order_id, "execution_id": self.execution_id,
             "source_zip": base64.b64encode(blob).decode()})

    def deploy_receipt(self, receipt: dict):
        if not self.claimed or not self.execution_id:
            raise PermissionError("native_claim_required")
        # The v1 Core schema rejects local HTTP journey fields; native custody keeps them.
        packet = {key: value for key, value in receipt.items()
            if key not in {"availability_status", "functional_status", "http_journey"}}
        return self.request("POST", self.path + "/deploy-receipt",
            {**packet, "execution_id": self.execution_id})

    def stop_receipt(self, evidence_digest: str, processes_stopped: bool, *, task_verified: bool = False,
                     reason_code: str | None = None):
        if not self.claimed or not self.execution_id:
            raise PermissionError("native_claim_required")
        payload = {
            "execution_id": self.execution_id, "evidence_digest": evidence_digest,
            "processes_stopped": processes_stopped}
        if task_verified:
            payload["task_verified"] = True
        if reason_code == 'cu_browser_decision_unavailable':
            reason_code = getattr(self, 'browser_decision_failure_code', None) or reason_code
        if reason_code:
            payload["reason_code"] = reason_code
        code = getattr(self, "outcome_code", None)
        if code in {"ok", "blocked", "timeout", "auth_lost", "model_unavailable", "needs_founder"}:
            payload["outcome_code"] = code if code != "ok" or task_verified else "needs_founder"
        return self.request("POST", self.path + "/stop-receipt", payload)

    def emergency_stop(self, source: str = "app"):
        """Idempotent founder-Stop receipt; Core cancels the order and holds unknown effects."""
        return self.request("POST", self.path + "/emergency-stop", {"source": source})

    def store_coding_source(self, receipt: dict, archive: bytes) -> dict:
        """Upload only an already settled, locally frozen coding order."""
        if self.upload is None or not isinstance(receipt, dict) or not isinstance(archive, bytes):
            raise PermissionError("native_coding_source_upload_unavailable")
        saved = self.read()
        if (saved.get("state") != "stopped" or saved.get("device_id") != self.device_id
                or receipt.get("order_id") != self.order_id
                or receipt.get("venture_id") != self.venture_id
                or receipt.get("device_id") != self.device_id
                or receipt.get("execution_id") != saved.get("execution_id")
                or receipt.get("workspace_id") != saved.get("workspace_id")
                or receipt.get("plan_task_id") != (saved.get("task_snapshot") or {}).get("task_id")
                or receipt.get("provider") not in {"codex", "claude"}
                or receipt.get("provider") != ((saved.get("manifest") or {}).get("coding_verification") or {}).get("provider")
                or receipt.get("qa_verified") is not False):
            raise PermissionError("native_coding_source_scope_changed")
        intent = {k: receipt[k] for k in ("execution_id", "sha256", "manifest_sha256", "size_bytes")}
        prepared = self.request("POST", self.path + "/source-intent", intent)
        if (prepared.get("order_id") != self.order_id or prepared.get("sha256") != receipt["sha256"]
                or prepared.get("state") not in {"awaiting_upload", "stored"}):
            raise PermissionError("native_coding_source_intent_unconfirmed")
        if prepared["state"] == "stored":
            return prepared
        stored = self.upload(self.path + "/source-archive", archive)
        if (stored.get("state") != "stored" or stored.get("order_id") != self.order_id
                or stored.get("sha256") != receipt["sha256"]
                or stored.get("manifest_sha256") != receipt["manifest_sha256"]):
            raise PermissionError("native_coding_source_upload_unconfirmed")
        return stored

    def reconcile_stopped_navigation(self, execution_id: str, evidence_digest: str):
        value = self.read()
        if (value.get("execution_id") != execution_id or value.get("state") not in {"claimed", "cancelled"}
                or value.get("native_stop") != {"execution_id": execution_id,
                    "evidence_digest": evidence_digest, "processes_stopped": True}):
            raise PermissionError("native_navigation_reconciliation_unavailable")
        result = self.request("POST", self.path + "/stop-receipt", {
            "execution_id": execution_id, "evidence_digest": evidence_digest,
            "processes_stopped": True})
        if result.get("state") != "stopped":
            raise PermissionError("native_navigation_reconciliation_unavailable")
        return result

    def reconcile_process_cleanup(self, execution_id: str, evidence_digest: str):
        value = self.read()
        prior = value.get("native_stop")
        if (value.get("execution_id") != execution_id or value.get("state") not in {"claimed", "cancelled"}
                or not isinstance(prior, dict) or prior.get("execution_id") != execution_id
                or prior.get("processes_stopped") is not False
                or value.get("stop_reconciliation_required") is not True):
            raise PermissionError("native_process_cleanup_reconciliation_unavailable")
        result = self.request("POST", self.path + "/stop-receipt", {
            "execution_id": execution_id, "evidence_digest": evidence_digest,
            "processes_stopped": True})
        if result.get("state") != "stopped":
            raise PermissionError("native_process_cleanup_reconciliation_unavailable")
        return result

    def reconcile_startup_failure(self, execution_id: str, evidence_digest: str):
        return self.request("POST", self.path + "/startup-reconciliation", {
            "execution_id": execution_id, "evidence_digest": evidence_digest})
