"""Source-owned proposals for the next exact reviewed browser URL.

These authenticated native-broker receipts attest bounded DOM observations,
not independent server pixel visibility. A saved typed descriptor, current
order authority and a fresh driver recheck remain necessary. No provider is
called and no task completion or effect permission is returned here.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal

from coding_agents.jev_decision import Candidate, Observation, verify_current
from coding_agents.owned_browser import strip_check_marker
from app.build import device_invocations as calls, device_orders as orders

SOURCE_KIND = "typed_reviewed_url_v1"
UNAVAILABLE = "device_typed_navigation_unavailable"


class ResponseProof(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(min_length=8, max_length=500)
    status: int = Field(ge=200, lt=300)
    complete: bool
    sequence: int = Field(ge=1, le=1000000000)


class LoadProof(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    qualified: bool
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    page_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    sequence: int = Field(ge=1, le=1000000000)
    response: ResponseProof
    location: str = Field(min_length=8, max_length=500)
    document_id: str = Field(pattern=r"^[a-f0-9-]{32,36}$")
    ready_state: Literal["complete"]
    visible_text: bool
    visibility_scope: Literal["bounded_dom_text_geometry"]
    observation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def closed_qualification(self):
        if (
            self.qualified is not True
            or self.visible_text is not True
            or self.response.complete is not True
            or self.response.status in {204, 205}
            or self.response.sequence != self.sequence
            or self.response.url != self.location
        ):
            raise ValueError(UNAVAILABLE)
        return self


class ControlSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^c[0-9]{1,3}$")
    tag: str = Field(pattern=r"^[A-Z][A-Z0-9]{0,31}$")
    href: str | None = Field(default=None, max_length=500)
    form: bool
    visible: bool
    enabled: bool


class NavigationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    invocation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    observation: Observation
    current_load: LoadProof
    qualified_loads: list[LoadProof] = Field(min_length=1, max_length=32)
    control_snapshots: list[ControlSnapshot] = Field(max_length=100)


def supported(manifest):
    return bool(
        isinstance(manifest, dict)
        and manifest.get("browser_verification")
        and manifest.get("general_browser_verification") is None
        and not orders.native_desktop_manifest(manifest)
        and manifest.get("applications") == ["com.google.Chrome"]
        and set(manifest.get("actions") or []) == {"browser.read", "browser.interact"}
    )


def strip_marker(value):
    return strip_check_marker(value)


def select(saved, request: NavigationBody):
    from app.build.device_jev import _origin, _scoped_observation

    manifest = saved["manifest"]
    if not supported(manifest):
        raise ValueError(UNAVAILABLE)
    observation = _scoped_observation(saved, request.observation)
    current = request.current_load
    if (
        observation.digest() != request.observation.digest()
        or current != request.qualified_loads[-1]
        or current.observation_digest != observation.digest()
        or current.location != observation.location
    ):
        raise ValueError(UNAVAILABLE)
    expected = manifest["browser_verification"]["expected_locations"]
    position, documents, pages = 0, {}, {}
    for proof in request.qualified_loads:
        if (
            proof.session_id != current.session_id
            or _origin(proof.location) not in manifest["origins"]
        ):
            raise ValueError(UNAVAILABLE)
        identity = (
            proof.page_id,
            proof.sequence,
            proof.location,
            proof.response.model_dump(),
        )
        if proof.document_id in documents and documents[proof.document_id] != identity:
            raise ValueError(UNAVAILABLE)
        previous = pages.get(proof.page_id)
        if previous is not None:
            previous_sequence, previous_document = previous
            if (
                proof.sequence < previous_sequence
                or proof.sequence == previous_sequence
                and proof.document_id != previous_document
                or proof.sequence > previous_sequence
                and proof.document_id == previous_document
            ):
                raise ValueError(UNAVAILABLE)
        location = strip_marker(proof.location)
        if position < len(expected) and location == expected[position]:
            position += 1
        elif not position or location != expected[position - 1]:
            raise ValueError(UNAVAILABLE)
        documents[proof.document_id] = identity
        pages[proof.page_id] = (proof.sequence, proof.document_id)
    if (
        not 0 < position < len(expected)
        or strip_marker(current.location) != expected[position - 1]
    ):
        raise ValueError(UNAVAILABLE)
    controls = {c.id: c for c in observation.controls}
    snapshots = {c.id: c for c in request.control_snapshots}
    if len(snapshots) != len(request.control_snapshots) or set(snapshots) != set(
        controls
    ):
        raise ValueError(UNAVAILABLE)
    target_url = expected[position]
    matches = []
    for key, snapshot in snapshots.items():
        control = controls[key]
        if snapshot.visible != control.visible or snapshot.enabled != control.enabled:
            raise ValueError(UNAVAILABLE)
        if (
            snapshot.tag == "A"
            and snapshot.form is False
            and snapshot.visible
            and snapshot.enabled
            and "click" in control.operations
            and snapshot.href is not None
            and strip_marker(snapshot.href) == target_url
        ):
            if _origin(snapshot.href) not in manifest["origins"]:
                raise ValueError(UNAVAILABLE)
            matches.append(key)
    if len(matches) != 1:
        raise ValueError(UNAVAILABLE)
    candidate = Candidate("click", matches[0], observation.digest())
    verify_current(candidate, observation)
    summary = {
        "session_id": current.session_id,
        "page_id": current.page_id,
        "sequence": current.sequence,
        "document_id": current.document_id,
        "location": current.location,
        "progress_count": position,
        "expected_location": target_url,
    }
    return candidate, summary


def propose(venture_id, caller, order_id, *, request: NavigationBody):
    saved = orders.retrieve(venture_id, caller, order_id)
    candidate, summary = select(saved, request)
    digest = hashlib.sha256(
        json.dumps(
            {
                "source_kind": SOURCE_KIND,
                "request": request.model_dump(),
                "review_digest": saved["review_digest"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    calls.admit_zero_provider_proposal(
        venture_id,
        caller,
        order_id,
        invocation_id=request.invocation_id,
        request_digest=digest,
        review_digest=saved["review_digest"],
        candidate=candidate,
        source_summary=summary,
    )
    return {
        "authority": "proposal_only",
        "source_kind": SOURCE_KIND,
        "invocation_id": request.invocation_id,
        "candidate": {
            "operation": candidate.operation,
            "target": candidate.target,
            "observation_digest": candidate.observation_digest,
            "requires_outcome_verification": False,
        },
        "expected_location": summary["expected_location"],
    }
