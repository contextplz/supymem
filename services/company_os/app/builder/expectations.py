"""Planner-proposed product expectations; founder review never requires selectors."""
from __future__ import annotations

import re
from typing import Annotated
from pydantic import Field, model_serializer, model_validator
from core.static_qa_contracts import MAX_REQUIRED_TEXT, Action, Contract, Name, RequiredText, Strict, Visual


_UNSUPPORTED_STATIC_BEHAVIOR = re.compile(
    r"\b(?:form|email\s+input|submit(?:\s+button)?|backend\s+request|"
    r"local(?:-only|\s+only)?\s+(?:message|success)|authentication|payment\s+processing)\b",
    re.IGNORECASE,
)

_EXPLICIT_EXCLUSION = re.compile(
    r"\b(?:no|without|exclude|excludes|excluding|avoid|avoids|avoiding)\b|"
    r"\b(?:do|does|must|should|will)\s+not\s+"
    r"(?:include|contain|use|render|show|provide|add|require)\b",
    re.IGNORECASE,
)


def _requests_unsupported_behavior(text: str) -> bool:
    for match in _UNSUPPORTED_STATIC_BEHAVIOR.finditer(text):
        prefix = text[max(0, match.start() - 120):match.start()]
        clause = re.split(
            r"[\n.;:]|\b(?:but|however|although|except)\b", prefix, flags=re.IGNORECASE
        )[-1]
        if not _EXPLICIT_EXCLUSION.search(clause):
            return True
    return False


class Criterion(Strict):
    criterion: Annotated[int, Field(ge=0, le=5)]
    summary: Annotated[str, Field(min_length=1, max_length=300)]
    text_indices: list[Annotated[int, Field(ge=0, le=MAX_REQUIRED_TEXT - 1)]] = Field(default_factory=list, max_length=10)
    visual_ids: list[Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9_]{0,39}$")]] = Field(default_factory=list, max_length=6)
    action_ids: list[Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9_]{0,39}$")]] = Field(default_factory=list, max_length=30)


    @model_serializer(mode="wrap")
    def legacy_wire(self, handler):
        data = handler(self)
        if not self.visual_ids:
            data.pop("visual_ids", None)
        return data


class Expectations(Strict):
    contract: Contract
    criteria: list[Criterion] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def coverage(self):
        if self.contract.managed_request_form is not None:
            raise ValueError("landing_expectations_controller_form_forbidden")
        actions = {action.id for action in self.contract.actions}
        visuals = {visual.id for visual in self.contract.visuals}
        seen = set()
        for item in self.criteria:
            if (item.criterion in seen or not item.text_indices and not item.action_ids and not item.visual_ids
                    or len(set(item.text_indices)) != len(item.text_indices) or len(set(item.action_ids)) != len(item.action_ids)
                    or len(set(item.visual_ids)) != len(item.visual_ids) or not set(item.visual_ids) <= visuals
                    or any(i >= len(self.contract.required_text) for i in item.text_indices) or not set(item.action_ids) <= actions):
                raise ValueError("landing_expectation_coverage_invalid")
            seen.add(item.criterion)
        return self


def validate(task):
    requested = "\n".join([
        str(task.get("description") or ""),
        *(str(item) for item in task.get("acceptance") or []),
    ])
    if _requests_unsupported_behavior(requested):
        raise ValueError("landing_expectations_unsupported_behavior")
    proposal = Expectations.model_validate(task.get("landing_expectations"))
    if {item.criterion for item in proposal.criteria} != set(range(len(task["acceptance"]))):
        raise ValueError("landing_expectations_incomplete")
    return proposal.model_dump()


def founder_view(task):
    proposal = validate(task)
    return {"profile": "static_landing_v1", "product_name": proposal["contract"]["product_name"],
            "required_text": proposal["contract"]["required_text"],
            "visuals": proposal["contract"].get("visuals", []),
            "interactions": [{"label": action["name"], "behavior": {"anchor": "Scroll to the named section", "local_navigation": "Open the named page", "toggle": "Show or hide the named content", "external_link": "Provide the approved destination link"}[action["kind"]]} for action in proposal["contract"]["actions"]],
            "criteria": [{"expectation": task["acceptance"][item["criterion"]], "verification": item["summary"]} for item in proposal["criteria"]],
            "limitations": ["Static pages only; no backend, signup, payments, submitted forms or remote service verification.",
                            "Every discovered button, form and link must have a supported observable result; unsupported controls fail QA."]}


class InlineEvidence(Strict):
    summary: Annotated[str, Field(min_length=1, max_length=300)]
    required_text: list[RequiredText] = Field(max_length=10)
    actions: list[Action] = Field(max_length=30)
    visuals: list[Visual] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.required_text and not self.actions and not self.visuals:
            raise ValueError("landing_evidence_empty")
        return self


class InlineExpectations(Strict):
    product_name: Name
    evidence: list[InlineEvidence] = Field(min_length=1, max_length=6)


def compile_inline(value, acceptance):
    """Derive references from authored evidence, never repair missing evidence.

    The position binds each evidence item to the corresponding acceptance item.
    Shared exact text/actions are reused; conflicting definitions fail closed.
    Existing Contract/Expectations validators still enforce all execution rules.
    """
    inline = InlineExpectations.model_validate(value)
    if not isinstance(acceptance, list) or len(inline.evidence) != len(acceptance):
        raise ValueError("landing_expectations_incomplete")
    texts, actions, visuals, criteria = [], {}, {}, []
    for index, item in enumerate(inline.evidence):
        indices, ids, visual_ids = [], [], []
        for text in item.required_text:
            if text not in texts:
                texts.append(text)
            position = texts.index(text)
            if position not in indices:
                indices.append(position)
        for action in item.actions:
            data = action.model_dump()
            if action.id in actions and actions[action.id] != data:
                raise ValueError("landing_action_definition_conflict")
            actions[action.id] = data
            if action.id not in ids:
                ids.append(action.id)
        for visual in item.visuals:
            data = visual.model_dump()
            if visual.id in visuals and visuals[visual.id] != data:
                raise ValueError("landing_visual_definition_conflict")
            visuals[visual.id] = data
            if visual.id not in visual_ids:
                visual_ids.append(visual.id)
        criteria.append({"criterion": index, "summary": item.summary,
                         "text_indices": indices, "action_ids": ids, "visual_ids": visual_ids})
    return Expectations.model_validate({"contract": {"profile": "static_landing_v1",
        "product_name": inline.product_name, "required_text": texts, "actions": list(actions.values()), "visuals": list(visuals.values())},
        "criteria": criteria}).model_dump()
