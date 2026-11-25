"""Explicit CUAI proposals over one claimed OwnedBrowser; no provider transport.

Core owns model selection, paid-call admission and durable actor proposals.
Only the first source-issued action can enter the existing effect path; fresh
observation follows every input. A semantic verdict never proves business
completion. Restart/resume is deliberately refused by the worker's claim gate.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from uuid import uuid4

from coding_agents.cu_ai_loop import parse_reply
from coding_agents.cu_ai_provider import azure_endpoint
from coding_agents.cu_exact_button_budget import exact_button_budgets
from coding_agents.jev_decision import Candidate
from core.redact import redact_string


def profile_digest(profile: dict) -> str:
    return hashlib.sha256(
        json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_profile(profile: dict) -> str:
    if (
        not isinstance(profile, dict)
        or set(profile)
        != {
            "version",
            "backend",
            "policy_version",
            "roles",
            "max_provider_calls",
            "max_request_bytes",
            "completion_policy",
            "pricing_digest",
        }
        or profile.get("version") not in {"browser_cuai_text_v1", "browser_cuai_text_v2"}
        or profile.get("backend") != "cuai"
        or type(profile.get("policy_version")) is not int
        or profile["policy_version"] != (2 if profile["version"] == "browser_cuai_text_v2" else 1)
        or profile.get("completion_policy") != "browser_semantic_review_v1"
        or type(profile.get("max_provider_calls")) is not int
        or not 1 <= profile["max_provider_calls"] <= 128
        or type(profile.get("max_request_bytes")) is not int
        or not 1024 <= profile["max_request_bytes"] <= 65536
        or not isinstance(profile.get("pricing_digest"), str)
        or len(profile["pricing_digest"]) != 64
        or any(c not in "0123456789abcdef" for c in profile["pricing_digest"])
        or not isinstance(profile.get("roles"), dict)
        or set(profile["roles"]) != {"plan", "actor", "verify"}
    ):
        raise PermissionError("browser_cuai_profile_invalid")
    for phase, role in profile["roles"].items():
        if (
            not isinstance(role, dict)
            or set(role)
            != {"provider", "model", "service_tier", "reasoning", "max_output_tokens"} | (
                {"endpoint", "deployment"} if role.get("provider") == "azure" else set())
            or role.get("provider") not in {"openai", "azure"}
            or role.get("service_tier") != "default"
            or not ((role.get("reasoning") == "none" and role.get("model") != "gpt-6.1-sol")
                    or (phase == "actor" and role.get("model") == "gpt-6.1-sol" and role.get("reasoning") == "low"))
            or not isinstance(role.get("model"), str)
            or not 1 <= len(role["model"]) <= 100
            or not role["model"].isascii()
            or any(not (c.isalnum() or c in "._-") for c in role["model"])
            or type(role.get("max_output_tokens")) is not int
            or not 1 <= role["max_output_tokens"] <= 2500
        ):
            raise PermissionError("browser_cuai_profile_invalid")
        if role["provider"] == "azure":
            try:
                azure_endpoint(role["endpoint"])
            except ValueError:
                raise PermissionError("browser_cuai_profile_invalid") from None
            deployment = role["deployment"]
            if (not isinstance(deployment, str) or not 1 <= len(deployment) <= 100 or not deployment.isascii()
                    or any(not (char.isalnum() or char in "._-") for char in deployment)):
                raise PermissionError("browser_cuai_profile_invalid")
    return profile_digest(profile)


@dataclass(frozen=True)
class BrowserProposal:
    candidate: Candidate
    invocation_id: str
    action: object | None = None
    summary: str = ""
    semantic_satisfied: bool = False
    qualification_rule_ids: tuple[str, ...] = ()
    terminal_operation: str = ""


class CuOwnedBrowserAdapter:
    def __init__(
        self,
        *,
        browser,
        gateway,
        profile,
        goal,
        stop,
        deadline,
        record=lambda **row: None,
    ):
        self.browser, self.gateway, self.profile, self.goal = (
            browser,
            gateway,
            profile,
            goal,
        )
        self.profile_digest = validate_profile(profile)
        self.read_only = getattr(browser, "read_only", False)
        self.bounded_reads = getattr(browser, "bounded_reads", False)
        if type(self.read_only) is not bool or type(self.bounded_reads) is not bool:
            raise PermissionError("browser_cuai_scope_invalid")
        if gateway.browser_cuai_profile_digest != self.profile_digest:
            raise PermissionError("browser_cuai_capability_unavailable")
        self.stop, self.deadline, self.record = stop, deadline, record
        self.subgoals, self.subgoal, self.memory, self.history = [], 0, "", []
        self.calls, self.steps, self.replans = 0, 0, 0
        self.terminal_only = False
        self.pending_dispatch = False
        self.answer = ""
        self.semantic_satisfied = False
        self._budgets = exact_button_budgets(goal)
        self._reservations = []
        self._recent_states = []
        self._subgoal_check_pending = False

    def _check(self):
        if self.stop.is_set() or time.monotonic() >= self.deadline:
            raise PermissionError("browser_cuai_cancelled_or_expired")
        if self.pending_dispatch:
            raise PermissionError("browser_cuai_dispatch_requires_reconciliation")
        self.gateway.current()

    def _recoverable_load(self, observation):
        recovery = getattr(self.browser, "qualification_recoverable_load", None)
        return (getattr(self.browser, "qualification_grant", None) is not None
                and callable(recovery) and recovery(observation) is True)

    def _terminal(self, action, observation, invocation):
        summary = redact_string(action.summary).strip()[:500]
        if not summary:
            raise PermissionError("browser_cuai_answer_invalid")
        self.terminal_only = True
        self.answer = summary
        return BrowserProposal(Candidate("blocked", None, observation.digest(), True), invocation,
            action=replace(action, summary=summary), summary=summary, terminal_operation=action.op)

    def _context(self, observation, *, reason="", claim="", compact=False):
        result = {
            "subgoals": self.subgoals,
            "subgoal": self.subgoal,
            "memory": self.memory,
            "history": self.history[-12:],
            "notes": self.browser.cuai_readback(observation),
            "hint": "Only currently observed click and exact reviewed fill values are supported. Historical inputs are not acceptance proof.",
            "reason": reason,
            "claim": claim,
            "terminal_only": self.terminal_only,
        }
        qualifier = getattr(self.browser, "qualification_grant", None)
        if self.bounded_reads:
            result["bounded_reads"] = True
        if getattr(self.browser, "read_urls", ()):
            result["read_urls"] = list(self.browser.read_urls)
        if (qualifier is None and (self.profile["version"] == "browser_cuai_text_v2" or self.bounded_reads)
                or qualifier is not None and "navigate" in qualifier.allowed_operations):
            result["links"] = self.browser.cuai_navigation_targets(observation)
            result["hint"] = "Use only offered same-origin links, bounded document scroll/read and exact reviewed fills. Historical inputs are not acceptance proof."
        if qualifier is not None:
            result["qualification"] = self.browser.qualification_context(observation)
            result["hint"] = "Use only the current controller fixture controls, explicit input/select literals and offered links/tabs. Saved request rules and durable Core effect admission bound every mutation. Credential, payment and unreviewed routes remain closed."
        if self.read_only:
            result["hint"] = "Read-only authority: no clicks, typing, submissions, downloads or new tabs; only observe/wait/answer and offered same-origin navigation or bounded document reads/scroll."
        if compact:
            result["hint"] += " COMPACT RETRY: your previous reply was incomplete or invalid. Return minimal schema-valid JSON, one action, empty memory, no explanation."
        return result

    def _call(self, phase, observation, *, reason="", claim=""):
        from desktop_runtime.device_gateway import UpstreamRejected

        for attempt in range(2):
            value = None
            try:
                value = self._call_once(phase, observation, reason=reason, claim=claim, compact=bool(attempt))
                json.loads(value["reply"])
                return value
            except (UpstreamRejected, json.JSONDecodeError) as error:
                if value is not None and value.get("effect_proposals"):
                    raise
                if isinstance(error, UpstreamRejected) and error.code not in {
                    "device_browser_ai_reply_truncated", "device_browser_ai_reply_invalid",
                    "device_browser_ai_plan_invalid", "device_browser_ai_verdict_invalid",
                }:
                    raise
                if attempt:
                    raise PermissionError("browser_cuai_reply_unusable") from None

    def _call_once(self, phase, observation, *, reason="", claim="", compact=False):
        self._check()
        if self.calls >= self.profile["max_provider_calls"]:
            raise PermissionError("browser_cuai_call_limit_reached")
        if compact:
            self.browser.bound_cuai_observation(observation, 1600)
        context = self._context(observation, reason=reason, claim=claim, compact=compact)
        checkpoint = {
            "version": 1,
            "profile_digest": self.profile_digest,
            "goal_sha256": hashlib.sha256(self.goal.encode()).hexdigest(),
            "order_id": self.gateway.order_id,
            "execution_id": self.gateway.execution_id,
            "session_id": self.browser.session_id,
            "calls": self.calls,
            "steps": self.steps,
            "subgoals": self.subgoals,
            "subgoal": self.subgoal,
            "memory": self.memory,
            "history": self.history[-12:],
            "terminal_only": self.terminal_only,
            "pending_dispatch": self.pending_dispatch,
            "budgets": [
                {"label": b.label, "limit": b.limit, "used": b.used}
                for b in self._budgets
            ],
        }
        checkpoint_digest = profile_digest(checkpoint)
        invocation = "bai_" + uuid4().hex
        # Every attempted phase consumes a call, including unknown failures.
        self.calls += 1
        value = self.gateway.browser_ai_call(
            invocation,
            phase,
            observation,
            profile_digest=self.profile_digest,
            checkpoint_digest=checkpoint_digest,
            current_load=self.browser.last_load_evidence,
            context=context,
            cancelled=self.stop,
            deadline=self.deadline,
            prepare_request=lambda page_bytes: self._prepare_request(observation, page_bytes, reason=reason, claim=claim, compact=compact),
        )
        self._check()
        if not (self.browser.verified_load(observation) or self._recoverable_load(observation)):
            raise PermissionError("browser_cuai_evidence_stale")
        self.record(
            kind="browser_cuai_call",
            phase=phase,
            invocation_id=invocation,
            observation_digest=observation.digest(),
            profile_digest=self.profile_digest,
            input_tokens=value["input_tokens"],
            output_tokens=value["output_tokens"],
        )
        return value

    def _plan(self, observation, reason):
        value = self._call("plan", observation, reason=reason)
        reply = json.loads(value["reply"])
        goals = (
            reply.get("subgoals")
            if isinstance(reply, dict) and set(reply) == {"subgoals"}
            else None
        )
        if (
            not isinstance(goals, list)
            or not 1 <= len(goals) <= 12
            or any(not isinstance(g, str) or not 0 < len(g) <= 400 for g in goals)
        ):
            raise PermissionError("browser_cuai_plan_invalid")
        self.subgoals, self.subgoal = goals, 0

    def _verify(self, observation, summary, *, subgoal=False):
        value = self._call("verify", observation, claim=summary, reason="subgoal_progress" if subgoal else "")
        reply = json.loads(value["reply"])
        if (
            not isinstance(reply, dict)
            or set(reply) != {"passed", "reason"}
            or type(reply.get("passed")) is not bool
            or not isinstance(reply.get("reason"), str)
            or len(reply["reason"]) > 200
        ):
            raise PermissionError("browser_cuai_verdict_invalid")
        self.record(
            kind="browser_cuai_verdict",
            semantic_satisfied=reply["passed"] if not subgoal else False,
            subgoal_satisfied=reply["passed"] if subgoal else None,
            task_verified=False,
            invocation_id=value["invocation_id"],
        )
        return reply["passed"]

    def _check_subgoal(self, observation):
        self.steps += 1
        if self._verify(observation, self.subgoals[self.subgoal], subgoal=True):
            self.subgoal = min(self.subgoal + 1, len(self.subgoals) - 1)
            self._recent_states.clear()
            self.record(kind="browser_cuai_subgoal", completed_subgoals=self.subgoal, task_verified=False)
        else:
            self.history.append("Sub-goal completion was rejected against current source evidence. Continue missing work, read field validation and never repeat accepted inputs.")

    def _prepare_request(self, observation, page_bytes, *, reason, claim, compact=False):
        self.browser.bound_cuai_observation(observation, page_bytes)
        return self._context(observation, reason=reason, claim=claim, compact=compact), self.browser.last_load_evidence

    def decide(self, observation):
        try:
            return self._decide(observation)
        except Exception as error:
            failed = getattr(self.gateway, 'browser_decision_failed', None)
            if callable(failed):
                failed(error)
            raise

    def _decide(self, observation):
        self._check()
        if (
            observation.goal != self.goal
            or observation.confirmation_pending
            or observation.prior_effect_uncertain
        ):
            raise PermissionError("browser_cuai_observation_invalid")
        if not self.subgoals:
            self._plan(observation, "initial")
        if self._subgoal_check_pending and not self.terminal_only:
            self._subgoal_check_pending = False
            self._check_subgoal(observation)
        # Random observation revisions are not evidence of progress.
        state = hashlib.sha256(
            (observation.location + "\n" + observation.context).encode()
        ).hexdigest()
        self._recent_states.append(state)
        if (
            len(self._recent_states) >= 3
            and len(set(self._recent_states[-3:])) == 1
            and self.replans < 2
            and not self.terminal_only
        ):
            self.replans += 1
            self._plan(
                observation,
                "No observed progress after prior returned inputs; choose a different authorized route.",
            )
        value = self._call("actor", observation)
        raw = json.loads(value["reply"])
        reply = parse_reply(raw, action_contract_version=3)
        action = reply.actions[0]
        self.memory = reply.memory
        if self.terminal_only and action.op not in {"done", "fail", "needs_you"}:
            raise PermissionError("browser_cuai_terminal_action_refused")
        if self._recoverable_load(observation) and action.op not in {"navigate", "wait", "fail", "needs_you"}:
            raise PermissionError("browser_cuai_action_unsupported")
        observed_subgoal_done = reply.subgoal_done and len(reply.actions) == 1 and action.op == "wait"
        if (not self.terminal_only and self.subgoal < len(self.subgoals) - 1
                and observed_subgoal_done):
            self._check_subgoal(self.browser.observe())
            return BrowserProposal(Candidate("wait", None, observation.digest(), True), value["invocation_id"])
        # Retain the original goal; subplans and model progress cannot rewrite it.
        if observed_subgoal_done:
            action = replace(action, op="done", summary="Verify the entire original task against current source evidence")
        if action.op == "done":
            items = raw.get("actions") if isinstance(raw, dict) else None
            summary = (
                action.summary if observed_subgoal_done else items[0].get("summary")
                if isinstance(items, list) and items and isinstance(items[0], dict)
                else None
            )
            if (
                not isinstance(summary, str)
                or not summary.strip()
                or len(summary) > 500
            ):
                raise PermissionError("browser_cuai_answer_invalid")
            if self.subgoal >= len(self.subgoals) - 1:
                self.terminal_only = True
            fresh = self.browser.observe()
            if self._verify(fresh, summary) and not observed_subgoal_done:
                self.terminal_only = True
                self.answer = summary
                self.semantic_satisfied = True
                return BrowserProposal(
                    Candidate("done", None, fresh.digest(), True),
                    value["invocation_id"],
                    summary=summary,
                    semantic_satisfied=True,
                )
            if not self.terminal_only and self.subgoal < len(self.subgoals) - 1:
                self._check_subgoal(fresh)
                return BrowserProposal(Candidate("wait", None, fresh.digest(), True), value["invocation_id"])
            # Close every mutation route before semantic completion/answer repair.
            self.terminal_only = True
            self.history.append(
                "Return the requested facts/results in one done summary, not a completion-check instruction. Input routes are closed; answer from observed source or request review."
                if observed_subgoal_done else
                "The verifier rejected the exact returned answer against the whole original goal. Input routes are closed; answer from observed source or request review."
            )
            repaired = self._call("actor", fresh)
            repaired_raw = json.loads(repaired["reply"])
            repaired_action = parse_reply(
                repaired_raw, action_contract_version=3
            ).actions[0]
            if repaired_action.op != "done":
                if repaired_action.op in {"fail", "needs_you"}:
                    return self._terminal(repaired_action, fresh, repaired["invocation_id"])
                return BrowserProposal(
                    Candidate("blocked", None, fresh.digest(), True),
                    repaired["invocation_id"],
                )
            items = repaired_raw.get("actions")
            if (
                not isinstance(items, list)
                or len(items) != 1
                or not isinstance(items[0].get("summary"), str)
                or not 0 < len(items[0]["summary"]) <= 500
            ):
                raise PermissionError("browser_cuai_answer_invalid")
            summary = items[0]["summary"]
            fresh = self.browser.observe()
            if self._verify(fresh, summary):
                self.answer = summary
                self.semantic_satisfied = True
                return BrowserProposal(
                    Candidate("done", None, fresh.digest(), True),
                    repaired["invocation_id"],
                    summary=summary,
                    semantic_satisfied=True,
                )
            return BrowserProposal(
                Candidate("blocked", None, fresh.digest(), True),
                repaired["invocation_id"],
            )
        if action.op in {"fail", "needs_you"}:
            return self._terminal(action, observation, value["invocation_id"])
        if action.op == "wait":
            return BrowserProposal(
                Candidate("wait", None, observation.digest(), True),
                value["invocation_id"],
            )
        v2 = self.profile["version"] == "browser_cuai_text_v2"
        if self.read_only and action.op not in {"navigate", "scroll", "extract"}:
            raise PermissionError("browser_cuai_action_unsupported")
        qualifier = getattr(self.browser, "qualification_grant", None)
        supported = ({"click", "type", "hover", "navigate", "new_tab", "switch_tab", "scroll", "extract"}
            if qualifier is not None else {"click", "type", "navigate", "scroll", "extract"}
                if v2 or self.bounded_reads else {"click", "type"})
        if action.op not in supported:
            raise PermissionError("browser_cuai_action_unsupported")
        proposals = value.get("effect_proposals")
        selected = (
            [p for p in proposals if p.get("action_index") == 0]
            if isinstance(proposals, list)
            else []
        )
        if len(selected) != 1:
            raise PermissionError("browser_cuai_effect_proposal_unavailable")
        selected = selected[0]
        candidate = Candidate(**selected["candidate"])
        if (
            candidate.operation != action.op
            or (action.op in {"click", "type", "hover", "switch_tab"} and candidate.target != action.element_id)
            or candidate.observation_digest != observation.digest()
        ):
            raise PermissionError("browser_cuai_effect_proposal_changed")
        if action.op in {"scroll", "extract"}:
            from coding_agents.owned_browser import cuai_document_target
            if candidate.target != cuai_document_target(self.browser.last_load_evidence, action):
                raise PermissionError("browser_cuai_effect_proposal_changed")
        if action.op in {"navigate", "new_tab"}:
            if isinstance(candidate.target, str) and candidate.target.startswith("document_"):
                from coding_agents.owned_browser import cuai_document_target
                reviewed_read = (qualifier is None and self.read_only and self.bounded_reads and action.op == "navigate"
                    and action.text in getattr(self.browser, "read_urls", ()) and self.browser.verified_load(observation))
                fixture_read = (qualifier is not None and action.op in qualifier.allowed_operations
                    and (action.op != "navigate" or self._recoverable_load(observation))
                    and any(rule.method == "GET" and action.text == qualifier.origin + rule.path
                        + ("?" + rule.query if rule.query else "") for rule in qualifier.rules))
                if (not (reviewed_read or fixture_read)
                        or candidate.target != cuai_document_target(self.browser.last_load_evidence, action)):
                    raise PermissionError("browser_cuai_effect_proposal_changed")
            else:
                matches = [link for link in self.browser.cuai_navigation_targets(observation)
                           if link["href"] == action.text and link["id"] == candidate.target]
                if len(matches) != 1:
                    raise PermissionError("browser_cuai_effect_proposal_changed")
        if action.op == "type":
            text = self.browser.resolve_fill(candidate.target)
            if (
                not text
                or action.text != text
                or type(candidate.value_index) is not int
                or not 0 <= candidate.value_index < len(observation.reviewed_text)
                or observation.reviewed_text[candidate.value_index] != text
            ):
                raise PermissionError("browser_cuai_reviewed_fill_mismatch")
        rule_ids = selected.get("qualification_rule_ids", [])
        if (not isinstance(rule_ids, list) or any(not isinstance(key, str) for key in rule_ids)
                or len(set(rule_ids)) != len(rule_ids) or len(rule_ids) > 128
                or qualifier is None and rule_ids
                or qualifier is not None and not set(rule_ids) <= {rule.rule_id for rule in qualifier.rules if rule.method == "POST"}):
            raise PermissionError("browser_qualification_proposal_invalid")
        self._subgoal_check_pending = reply.subgoal_done
        return BrowserProposal(candidate, selected["invocation_id"], action, qualification_rule_ids=tuple(rule_ids))

    def before_dispatch(self, proposal, observation):
        self._check()
        recoverable = self._recoverable_load(observation)
        if (
            self.terminal_only
            or proposal.action is None
            or self.read_only and proposal.action.op not in {"navigate", "scroll", "extract"}
            or not (self.browser.verified_load(observation) or recoverable)
            or recoverable and (proposal.action.op != "navigate"
                or not str(proposal.candidate.target).startswith("document_"))
        ):
            raise PermissionError("browser_cuai_dispatch_refused")
        if getattr(self.browser, "qualification_grant", None) is not None:
            self.browser.qualification_proposal(proposal.invocation_id, proposal.candidate, proposal.qualification_rule_ids)
        elements = []
        for control in observation.controls:
            meta = self.browser._control_meta.get(control.id) or {}
            elements.append(
                {
                    "id": control.id,
                    "label": control.label,
                    "ax_role": str(meta.get("tag") or "").lower(),
                    "type": meta.get("input_type"),
                    "submits_form": meta.get("submit") is True,
                    "opaque_activation": meta.get("opaque_activation") is True,
                }
            )
        reservations = []
        for budget in self._budgets:
            cost, refusal = budget.admission(proposal.action, elements)
            if refusal:
                raise PermissionError(refusal)
            reservations.append((budget, cost))
        self._reservations = reservations
        for budget, cost in reservations:
            budget.used += cost
        self.pending_dispatch = True

    def not_dispatched(self):
        """Only source-proven pre-input refusal restores an input quota."""
        for budget, cost in self._reservations:
            budget.used -= cost
        self._reservations = []
        self.pending_dispatch = False

    def delivered(self, proposal):
        if not self.pending_dispatch:
            raise PermissionError("browser_cuai_dispatch_not_pending")
        self.pending_dispatch = False
        self._reservations = []
        self.steps += 1
        self.history.append(
            f"Returned source input {self.steps}: {proposal.candidate.operation}; business acceptance is unknown."
        )
