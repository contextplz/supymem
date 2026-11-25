"""Reviewed browser text phases: metered proposals, never completion/effect authority.

One parsed Responses result per durable admission; zero-output Azure timeout
fallback retains the Azure ceiling plus OpenAI usage, without SDK retry.
The broker attests DOM provenance; it must recheck actual controls before effects.
"""

from __future__ import annotations

from dataclasses import asdict
import copy
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import os
import re
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator
from coding_agents import cu_ai_loop as loop, cu_ai_provider as providers
from coding_agents.jev_decision import Candidate, Observation

from app.build import device_invocations as calls, device_orders as orders
from app.build.device_browser_navigation import LoadProof
from app.credits.units import MIN_RESERVE_MICRO, usd_to_micro
from app.llm.pricing import load_pricing, pricing_table

SOURCE_KIND = "browser_cuai_text_v1"
# Paid phase lineage stays fixed; the exact reviewed profile gates action policy.
UNAVAILABLE = "device_browser_ai_configuration_unavailable"
INVALID = "device_browser_ai_request_invalid"
ENDPOINT = providers.OPENAI_ENDPOINT
SUPPORTED_MODELS = frozenset({"gpt-6-sol", "gpt-6.1-sol", "gpt-6-luna", "gpt-5.6-luna"})
# The generated clock is at most 60 characters, each up to 12 bytes when JSON-escaped.
CLOCK_QUOTE_TEXT = " " * (60 * 12)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def strict_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("device_browser_ai_reply_invalid")
            value[key] = item
        return value

    return json.loads(
        raw,
        object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError(INVALID)),
    )


def reply_json(raw):
    """Model replies only (never provider bodies/config): strict_json rules on the first complete object, trailing text ignored."""
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("device_browser_ai_reply_invalid")
            value[key] = item
        return value

    try:
        return loop.first_json_object(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError(INVALID)),
        )
    except ValueError:
        raise ValueError("device_browser_ai_reply_invalid") from None


def profile_digest(profile):
    return digest(
        profile.model_dump()
        if isinstance(profile, orders.BrowserAIProfile)
        else profile
    )


def pricing_digest(profile):
    models = sorted(
        {
            key
            for role in (profile.roles.plan, profile.roles.actor, profile.roles.verify)
            for key in (role.model, pricing_key(role.model, role.provider))
        }
    )
    table = pricing_table()
    rows = {}
    for model in models:
        row = table.get(
            model
        )  # Exact key only; neither aliases nor default prices qualify.
        if not isinstance(row, dict):
            raise ValueError(UNAVAILABLE)
        rates = {}
        for key in ("input_per_mtok", "output_per_mtok", "cache_write_per_mtok"):
            if key == "cache_write_per_mtok" and key not in row:
                continue
            raw = row.get(key)
            if isinstance(raw, bool) or raw is None:
                raise ValueError(UNAVAILABLE)
            try:
                value = Decimal(str(raw))
                if not value.is_finite() or value <= 0:
                    raise ValueError(UNAVAILABLE)
            except (ValueError, ArithmeticError):
                raise ValueError(UNAVAILABLE) from None
            rates[key] = str(value)
        rows[model] = rates
    return digest({"as_of": load_pricing().get("as_of"), "models": rows})


def pricing_key(model, provider):
    return f"azure/{model}" if provider == "azure" else model


def configured_profile():
    profile = orders.BrowserAIProfile.model_validate(
        strict_json(os.environ.get("DEVICE_BROWSER_CUAI_PROFILE_JSON", "")))
    if profile.pricing_digest != pricing_digest(profile):
        raise ValueError(UNAVAILABLE)
    provider = os.environ.get("DEVICE_BROWSER_CUAI_PROVIDER")
    if provider is None:
        return profile
    if provider not in {"openai", "azure"}:
        raise ValueError(UNAVAILABLE)
    value = profile.model_dump()
    for role in value["roles"].values():
        role.update(provider=provider)
        if provider == "azure":
            role.update(endpoint=os.environ.get("AZURE_OPENAI_CUAI_ENDPOINT"),
                        deployment=os.environ.get("AZURE_OPENAI_CUAI_DEPLOYMENT") or role["model"])
        else:
            role.pop("endpoint", None)
            role.pop("deployment", None)
    configured = orders.BrowserAIProfile.model_validate(value)
    return configured.model_copy(update={"pricing_digest": pricing_digest(configured)})


def cost_micro(model, input_tokens, output_tokens, *, reserve=False, rates=None):
    row = rates if rates is not None else pricing_table()[model]
    input_rate = max(
        Decimal(str(row["input_per_mtok"])),
        Decimal(str(row.get("cache_write_per_mtok", row["input_per_mtok"]))),
    )
    output_rate = Decimal(str(row["output_per_mtok"]))
    if model in {"gpt-6-sol", "gpt-6.1-sol"} and input_tokens > 272000:
        input_rate *= 2
        output_rate *= Decimal("1.5")
    usd = (
        (Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate)
        / Decimal(1000000)
    ).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
    amount = usd_to_micro(usd)
    return max(amount, MIN_RESERVE_MICRO) if reserve else amount


def assert_configuration(profile):
    profile = orders.BrowserAIProfile.model_validate(profile)
    try:
        configured = configured_profile()
    except ValueError:
        raise ValueError(UNAVAILABLE) from None
    allowed = set(os.environ.get("OPENAI_BROWSER_CUAI_MODELS", "").split(","))
    if (
        os.environ.get("DEVICE_BROWSER_CUAI_EXECUTION_ENABLED") != "1"
        or not os.environ.get("OPENAI_DEVICE_VISION_API_KEY")
        or (any(role.provider == "azure" for role in (profile.roles.plan, profile.roles.actor, profile.roles.verify))
            and not (os.environ.get("AZURE_OPENAI_CUAI_API_KEY") or os.environ.get("AZURE_OPENAI_API_KEY")))
        or any(
            role.model not in allowed or role.model not in SUPPORTED_MODELS
            for role in (profile.roles.plan, profile.roles.actor, profile.roles.verify)
        )
        or profile.pricing_digest != pricing_digest(profile)
        or configured != profile
    ):
        raise ValueError(UNAVAILABLE)
    return profile


def available_profile(venture_id, caller):
    wid, actor, _ = orders._scope(venture_id, caller)
    with orders.db()() as session:
        orders.authorize(session, workspace_id=wid, venture_id=venture_id, actor=actor)
    try:
        orders.assert_general_browser_available({"general_browser_verification": {}}, actor)
        if os.environ.get("DEVICE_BROWSER_EXECUTION_ENABLED") != "1":
            raise ValueError(UNAVAILABLE)
        profile = assert_configuration(configured_profile())
    except ValueError:
        return {"available": False, "code": UNAVAILABLE}
    return {
        "available": True,
        "execution_profile": profile.model_dump(),
        "execution_profile_digest": profile_digest(profile),
    }


class NavigationTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^c[0-9]{1,3}$")
    href: str = Field(min_length=8, max_length=500)
    tag: Literal["A"]
    form: Literal[False]

    @field_validator("form", mode="before")
    @classmethod
    def source_non_form(cls, value):
        if value is not False:
            raise ValueError(INVALID)
        return value


class QualificationControl(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^c[0-9]{1,3}$")
    document_path: str = Field(min_length=1, max_length=512)
    document_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    container_id: str = Field(default="", max_length=128)
    control_id: str = Field(pattern=r"^(?:[A-Za-z0-9_-]{1,128})?$")
    tag: Literal["A", "BUTTON", "INPUT", "TEXTAREA", "SELECT", "SUMMARY", "DETAILS", "DIV", "SPAN", "TD", "TR", "LABEL"]
    input_type: Literal["text", "email", "tel", "textarea", "select-one", "checkbox", "radio", "button", "submit", "hidden"] | None = None
    input_value: str | None = Field(default=None, max_length=600)
    kind: Literal["text", "email", "synthetic_code", "synthetic_billing_contact", "select", "radio", "checkbox", "json_literal"] | None = None
    select_value: str | None = Field(default=None, max_length=600)
    hover_only: bool = False


class QualificationTab(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^t[a-f0-9]{32}$")
    url: str = Field(min_length=8, max_length=500)


class QualificationFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    document_path: str = Field(min_length=1, max_length=512)
    parent_document_path: str = Field(min_length=1, max_length=512)
    frame_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


class QualificationLink(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^c[0-9]{1,3}$")
    href: str = Field(min_length=8, max_length=500)


class QualificationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    _eligible_rule_ids: tuple[str, ...] | None = PrivateAttr(default=None)
    _recovery_only: bool = PrivateAttr(default=False)
    _source_artifacts_digest: str | None = PrivateAttr(default=None)
    grant_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    controls: list[QualificationControl] = Field(default_factory=list, max_length=100)
    tabs: list[QualificationTab] = Field(default_factory=list, max_length=4)
    frames: list[QualificationFrame] = Field(default_factory=list, max_length=2)
    links: list[QualificationLink] = Field(default_factory=list, max_length=16)
    completed_rules: list[str] = Field(default_factory=list, max_length=128)


class BrowserContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    subgoals: list[str] = Field(default_factory=list, max_length=12)
    subgoal: int = Field(default=0, ge=0, le=12)
    memory: str = Field(default="", max_length=800)
    history: list[str] = Field(default_factory=list, max_length=12)
    notes: str = Field(default="", max_length=5000)
    hint: str = Field(default="", max_length=500)
    reason: str = Field(default="start", max_length=200)
    claim: str = Field(default="", max_length=500)
    terminal_only: bool = False
    links: list[NavigationTarget] = Field(default_factory=list, max_length=16)
    read_urls: list[str] = Field(default_factory=list, max_length=6)
    qualification: QualificationContext | None = None
    bounded_reads: Literal[True] | None = None

    @field_validator("bounded_reads", mode="before")
    @classmethod
    def source_read_capability(cls, value):
        if value is not None and value is not True:
            raise ValueError(INVALID)
        return value


class QualificationErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(min_length=8, max_length=500)
    status: int = Field(ge=400, lt=600)
    complete: Literal[True]
    sequence: int = Field(ge=1, le=1000000000)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0, le=65536)
    mime_type: str = Field(pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$", max_length=120)

    @field_validator("complete", mode="before")
    @classmethod
    def literal_complete(cls, value):
        if value is not True:
            raise ValueError(INVALID)
        return value


class QualificationErrorLoadProof(BaseModel):
    """Known failed fixture document; never a successful ordinary load or completion proof."""
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["qualification_error"]
    qualified: Literal[False]
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    page_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    document_id: str = Field(pattern=r"^[a-f0-9-]{32,36}$")
    sequence: int = Field(ge=1, le=1000000000)
    response: QualificationErrorResponse
    location: str = Field(min_length=8, max_length=500)
    ready_state: Literal["complete"]
    visible_text: Literal[True]
    visibility_scope: Literal["bounded_dom_text_geometry"]
    observation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("qualified", "visible_text", mode="before")
    @classmethod
    def literal_flags(cls, value, info):
        if value is not (False if info.field_name == "qualified" else True):
            raise ValueError(INVALID)
        return value

    @model_validator(mode="after")
    def bound_document_response(self):
        if self.response.sequence != self.sequence or self.response.url != self.location:
            raise ValueError(INVALID)
        return self


class BrowserAIBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    invocation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")
    phase: Literal["plan", "actor", "verify"]
    execution_profile_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    checkpoint_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    observation: Observation
    current_load: LoadProof | QualificationErrorLoadProof
    context: BrowserContext


class SourceOmissions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    facts: int = Field(ge=0, le=1000000000)
    fields: int = Field(ge=0, le=1000000000)
    values: int = Field(ge=0, le=1000000000)


class SourceField(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    label: str = Field(max_length=160)
    type: str = Field(min_length=1, max_length=32)
    populated: bool
    reviewed_value_index: int | None = Field(ge=0, lt=8)


class SourceContent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    facts: list[str] = Field(max_length=32)
    fields: list[SourceField] = Field(max_length=32)
    omitted: SourceOmissions


class SourceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    page_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    document_id: str = Field(
        pattern=r"^(?:[a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$"
    )
    sequence: int = Field(ge=1, le=1000000000)
    location: str = Field(min_length=8, max_length=500)
    observation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: SourceContent


class SourceInput(SourceEntry):
    invocation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    operation: Literal["click", "type"]
    delivery: Literal["input_delivered", "delivery_unconfirmed"]
    acceptance: Literal["unknown"]


class SourceNotesOmissions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pages: int = Field(ge=0, le=1000000000)
    inputs: int = Field(ge=0, le=1000000000)


class SourceNotes(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    scope: Literal["source_main_document_inputs_not_acceptance"]
    current: SourceEntry
    prior_pages: list[SourceEntry] = Field(max_length=8)
    input_history: list[SourceInput] = Field(max_length=8)
    omitted: SourceNotesOmissions


class SourceRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=160)
    passages: list[str] = Field(max_length=6)
    omitted: int = Field(ge=0, le=1000000000)
    invocation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    observation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    historical: Literal[True]

    @field_validator("historical", mode="before")
    @classmethod
    def historical_read(cls, value):
        if value is not True:
            raise ValueError(INVALID)
        return value


class SourceContentV2(SourceContent):
    reads: list[SourceRead] = Field(max_length=4)
    reads_omitted: int = Field(ge=0, le=1000000000)


class SourceEntryV2(SourceEntry):
    source: SourceContentV2


class SourceInputV2(SourceEntryV2):
    invocation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    operation: Literal["click", "type", "navigate", "scroll", "extract"]
    delivery: Literal["input_delivered", "delivery_unconfirmed"]
    acceptance: Literal["unknown"]


class SourceNotesV2(SourceNotes):
    schema_version: Literal[2]
    current: SourceEntryV2
    prior_pages: list[SourceEntryV2] = Field(max_length=8)
    input_history: list[SourceInputV2] = Field(max_length=8)


class SourceInputQualification(SourceEntryV2):
    invocation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    operation: Literal["click", "type", "navigate", "scroll", "extract", "hover", "new_tab", "switch_tab"]
    delivery: Literal["input_delivered", "delivery_unconfirmed"]
    acceptance: Literal["unknown"]


class SourceQualificationDownload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rule_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    url: str = Field(min_length=8, max_length=500)
    filename: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}\.csv$")
    media_type: Literal["text/csv"]
    size_bytes: int = Field(ge=1, le=1_048_576)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    rows_excluding_header: int = Field(ge=0, le=1_048_576)
    invocation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    effect_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    observation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    delivery: Literal["input_delivered"]
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


class SourceQualificationFailedDownload(QualificationErrorResponse):
    mime_type: Literal["text/html"]


class SourceNotesQualification(SourceNotesV2):
    input_history: list[SourceInputQualification] = Field(max_length=8)
    download: SourceQualificationDownload | None = None
    failed_download: SourceQualificationFailedDownload | None = None


READS = frozenset({"navigate", "scroll", "extract"})
QUALIFIED_ACTIONS = frozenset({"hover", "new_tab", "switch_tab"})


def _fixture(saved):
    """Controller-sealed owned-fixture grant on this saved order, else None."""
    from core.browser_qualification import OwnedFixtureGrant
    grant = orders._qualification_binding(saved)
    return grant if isinstance(grant, OwnedFixtureGrant) else None


def _reads(general, fixture, *, bounded_reads=False):
    """Read/navigation ops: exactly the grant's for a fixture order, else the v2 profile's."""
    if fixture is not None:
        return READS & set(fixture.allowed_operations)
    return READS if bounded_reads is True or (general.get("execution_profile") or {}).get("version") == "browser_cuai_text_v2" else frozenset()


def _qualified_read(fixture, qualification, url):
    if fixture is None or qualification is None or qualification.grant_digest != fixture.grant_digest:
        raise ValueError(INVALID)
    try:
        rule = fixture.request_rule(url=url, method="GET", completed_rules=qualification.completed_rules)
    except ValueError:
        raise ValueError("device_browser_ai_target_invalid") from None
    if qualification._eligible_rule_ids is not None and rule.rule_id not in qualification._eligible_rule_ids:
        raise ValueError("device_browser_ai_target_invalid")
    return rule


def _qualification_rules(fixture, qualification, target):
    """Saved rule identities matching this validated source control, never labels."""
    if fixture is None or qualification is None or qualification.grant_digest != fixture.grant_digest:
        raise ValueError(INVALID)
    controls = [control for control in qualification.controls if control.id == target]
    if len(controls) != 1:
        raise ValueError("device_browser_ai_target_invalid")
    control = controls[0]
    rules = [rule for rule in fixture.rules
             if (rule.document_path, rule.document_id, rule.form_id)
             == (control.document_path, control.document_id, control.container_id)
             and set(rule.prerequisites) <= set(qualification.completed_rules)
             and (qualification._eligible_rule_ids is None or rule.rule_id in qualification._eligible_rule_ids)]
    return control, rules


def _qualification_fill(fixture, qualification, target):
    control, rules = _qualification_rules(fixture, qualification, target)
    for rule in rules:
        for field in rule.fields:
            if field.control_id != control.control_id or field.kind != control.kind:
                continue
            if ("select" in fixture.allowed_operations and field.kind == "select" and control.tag == "SELECT"
                    and control.input_type == "select-one" and control.select_value == field.value):
                return field.value
            if ("type" in fixture.allowed_operations and field.input_value is not None
                    and control.tag in {"INPUT", "TEXTAREA"} and control.input_type == field.input_type
                    and control.input_value == field.input_value and field.kind not in {"select", "radio", "checkbox"}):
                return field.input_value
    raise ValueError("device_browser_ai_type_unreviewed")


def _qualification_click_rules(fixture, qualification, target):
    control, rules = _qualification_rules(fixture, qualification, target)
    if control.hover_only:
        raise ValueError("device_browser_ai_target_invalid")
    submits = tuple(rule.rule_id for rule in rules
                    if rule.method == "POST" and rule.control_id == control.control_id)
    if submits and "click" in fixture.allowed_operations:
        return submits
    if "check" in fixture.allowed_operations and control.tag == "INPUT":
        if any(field.control_id == control.control_id and field.kind == control.kind == control.input_type
               and field.kind in {"checkbox", "radio"} and control.select_value == field.value
               for rule in rules for field in rule.fields):
            return ()
    if ("click" in fixture.allowed_operations and control.kind is None
            and control.tag not in {"INPUT", "TEXTAREA", "SELECT"}
            and not (control.tag == "BUTTON" and control.container_id)):
        return ()
    raise ValueError("device_browser_ai_target_invalid")


def _fixture_values(fixture, qualification, observation):
    return list(dict.fromkeys(_qualification_fill(fixture, qualification, control.id)
                              for control in observation.controls if "type" in control.operations))


def _card_location(location, fixture):
    """Only the current sealed fixture's exact GET nonce is custody metadata."""
    if fixture is None or not isinstance(location, str):
        return location
    prefix = fixture.origin + "/public/browser-qualification/" + fixture.nonce + "/"
    if (not location.startswith(prefix) or not any(rule.method == "GET" and location == fixture.origin + rule.path
            + ("?" + rule.query if rule.query else "") for rule in fixture.rules)):
        return location
    return fixture.origin + "/public/browser-qualification/owned/" + location[len(prefix):]


def source_notes_card_surfaces(notes, request, saved, reviewed_values):
    """Recognize closed custody metadata; page content keeps the card filter.

    Metadata qualification changes no model text or task/effect authority.
    Unrecognized or opaque notes retain the original whole-string scan, and
    SECRETISH is always checked against the complete original notes.
    """
    fixture = _fixture(saved)
    # The owned browser emits v2 readback (with reads) whenever a fixture grant is present.
    v2 = (saved["manifest"]["general_browser_verification"]["execution_profile"]["version"] == "browser_cuai_text_v2"
          or fixture is not None or request.context.bounded_reads is True)
    try:
        value = strict_json(notes)
    except (ValueError, TypeError):
        if v2:
            raise ValueError(INVALID) from None
        return [notes]
    if (
        not isinstance(value, dict)
        or value.get("scope") != "source_main_document_inputs_not_acceptance"
    ):
        if v2:
            raise ValueError(INVALID)
        return [notes]
    if type(value.get("schema_version")) is not int or value["schema_version"] != (2 if v2 else 1):
        raise ValueError(INVALID)
    try:
        source = (SourceNotesQualification if fixture is not None else SourceNotesV2 if v2 else SourceNotes).model_validate(value)
    except ValueError:
        raise ValueError(INVALID) from None
    if isinstance(source,SourceNotesQualification) and (source.download is not None or source.failed_download is not None):
        qualification=request.context.qualification
        if (qualification is None or qualification._source_artifacts_digest != hashlib.sha256(notes.encode()).hexdigest()):
            raise ValueError(INVALID)
    load, current = request.current_load, source.current
    if any(
        getattr(current, key) != getattr(load, key)
        for key in (
            "session_id",
            "page_id",
            "document_id",
            "sequence",
            "location",
            "observation_digest",
        )
    ):
        raise ValueError(INVALID)
    from app.build.device_jev import _origin

    surfaces = []
    for entry in [current, *source.prior_pages, *source.input_history]:
        if (
            entry.session_id != load.session_id
            or _origin(entry.location) not in saved["manifest"]["origins"]
            or any(len(fact) > 700 for fact in entry.source.facts)
            or any(
                field.reviewed_value_index is not None
                and field.reviewed_value_index >= len(reviewed_values)
                for field in entry.source.fields
            )
        ):
            raise ValueError(INVALID)
        surfaces.extend([_card_location(entry.location, fixture), *entry.source.facts])
        if v2:
            from coding_agents.owned_browser import cuai_document_target
            for read in entry.source.reads:
                if any(len(text) > 700 for text in read.passages) or len(json.dumps(read.model_dump(), ensure_ascii=False, separators=(",", ":")).encode()) > 1600:
                    raise ValueError(INVALID)
                surfaces.extend([read.query, *read.passages])
                child = saved.get("invocations", {}).get(read.invocation_id, {})
                read_load = entry.model_dump(exclude={"source"})
                read_load["observation_digest"] = read.observation_digest
                if (child.get("source_kind") != "browser_cuai_actor_effect_v1"
                        or child.get("proposal", {}).get("operation") != "extract"
                        or child.get("proposal", {}).get("observation_digest") != read.observation_digest
                        or child.get("proposal", {}).get("target") != cuai_document_target(read_load, loop.Action(op="extract", text=read.query))
                        or child.get("effect", {}).get("state") != "returned"):
                    raise ValueError(INVALID)
        for field in entry.source.fields:
            surfaces.extend([field.label, field.type])
        if isinstance(entry, (SourceInput, SourceInputV2, SourceInputQualification)):
            # Only an exact durable actor child can qualify the runtime's
            # namespaced opaque ID. Arbitrary invocation strings stay scanned.
            child = saved.get("invocations", {}).get(entry.invocation_id, {})
            if not (
                re.fullmatch(r"bai_[a-f0-9]{32}_a0", entry.invocation_id)
                and child.get("source_kind") == "browser_cuai_actor_effect_v1"
                and child.get("no_provider_io") is True
                and child.get("proposal", {}).get("observation_digest")
                == entry.observation_digest
            ):
                surfaces.append(entry.invocation_id)
    if isinstance(source,SourceNotesQualification):
        if source.download is not None:
            surfaces.extend([_card_location(source.download.url,fixture),source.download.filename,source.download.media_type])
        if source.failed_download is not None:
            surfaces.extend([_card_location(source.failed_download.url,fixture),source.failed_download.mime_type])
    return surfaces


def scoped(saved, request):
    general = saved["manifest"].get("general_browser_verification")
    if not general or not general.get("execution_profile"):
        raise ValueError(INVALID)
    profile = assert_configuration(general["execution_profile"])
    from app.build.device_jev import _scoped_observation

    observation = _scoped_observation(saved, request.observation)
    load = request.current_load
    if (
        request.execution_profile_digest != profile_digest(profile)
        or observation.digest() != request.observation.digest()
        or load.observation_digest != observation.digest()
        or load.location != observation.location
        or not observation.permitted
        or observation.confirmation_pending
        or observation.prior_effect_uncertain
        or any(len(s) > 400 for s in request.context.subgoals)
        or any(len(s) > 300 for s in request.context.history)
        or len(request.context.notes.encode()) > 5000
    ):
        raise ValueError(INVALID)
    fixture = _fixture(saved)
    if isinstance(load, QualificationErrorLoadProof) and (
            fixture is None or request.context.qualification is None or not request.context.qualification._recovery_only):
        raise ValueError(INVALID)
    read_urls = general.get("read_urls") or []
    if (request.context.read_urls != read_urls or read_urls and (
            fixture is not None or not orders.general_browser_read_only(saved["manifest"])
            or request.context.bounded_reads is not True)):
        raise ValueError(INVALID)
    values = (_fixture_values(fixture, request.context.qualification, observation) if fixture is not None
              else list(dict.fromkeys(general.get("fill_values", {}).values())))
    if fixture is not None and request.context.qualification is not None:
        if [(link.id, link.href) for link in request.context.links] != [
                (link.id, link.href) for link in request.context.qualification.links]:
            raise ValueError(INVALID)
    if request.context.links:
        from app.build.device_jev import _origin
        if "navigate" not in _reads(general, _fixture(saved), bounded_reads=request.context.bounded_reads is True) or len({link.id for link in request.context.links}) != len(request.context.links):
            raise ValueError(INVALID)
        controls = {control.id: control for control in observation.controls}
        for link in request.context.links:
            control = controls.get(link.id)
            if (not control or not control.visible or not control.enabled or "click" not in control.operations
                    or _origin(link.href) != _origin(observation.location)):
                raise ValueError(INVALID)
    if (fixture is not None or observation.reviewed_text) and observation.reviewed_text != values:
        raise ValueError(INVALID)
    surfaces = [
        observation.context,
        observation.location,
        request.context.memory,
        *values,
        *(control.label for control in observation.controls),
        *(link.href for link in request.context.links),
    ]
    if any(loop.SECRETISH.search(text) for text in [*surfaces, request.context.notes]):
        raise ValueError(INVALID)
    card_surfaces = source_notes_card_surfaces(
        request.context.notes, request, saved, values
    )
    if any(loop.SECRETISH.search(text) for text in card_surfaces):
        raise ValueError(INVALID)
    card_surfaces = [observation.context, _card_location(observation.location, fixture), request.context.memory,
        *values, *(control.label for control in observation.controls),
        *(_card_location(link.href, fixture) for link in request.context.links), *card_surfaces]
    if any(loop.CARD.search(text) for text in card_surfaces):
        raise ValueError(INVALID)
    return profile, observation


def request_body(saved, request, profile, observation, *, clock_part=None):
    from coding_agents.cu_page_context import bounded_page_observation

    if clock_part is None and request.phase != "plan":
        clock_part = {"type": "input_text", "text": loop.now_line()}
    page_bytes = len(json.dumps(observation.context).encode()) + len(json.dumps(
        [control.model_dump() for control in observation.controls]).encode())
    previous_size = None
    projected = observation
    while True:
        body = _request_body(saved, request, profile, projected, clock_part=clock_part)
        role = getattr(profile.roles, request.phase)
        wire = {**body, "model": role.deployment} if role.provider == "azure" else body
        size = max(len(json.dumps(body).encode()), len(json.dumps(wire).encode()))
        if size <= profile.max_request_bytes:
            return body
        if previous_size is not None and size >= previous_size:
            raise ValueError("device_browser_ai_request_bounds")
        previous_size = size
        page_bytes = max(0, page_bytes - (size - profile.max_request_bytes) - 256)
        projected = bounded_page_observation(observation, page_bytes)


def _request_body(saved, request, profile, observation, *, clock_part):
    ctx, role = request.context, getattr(profile.roles, request.phase)
    read_only = orders.general_browser_read_only(saved["manifest"])
    elements = [
        {
            "id": c.id,
            "role": "AXTextField" if "type" in c.operations else "AXButton",
            "label": c.label,
            "value": "supported operations: " + ",".join(c.operations),
        }
        for c in observation.controls
        if c.visible and c.enabled
    ]
    notes = observation.context + "\n" + ctx.notes
    if request.phase == "actor":
        user = loop.build_user_text(
            saved["manifest"]["instruction"],
            ctx.subgoals,
            ctx.subgoal,
            ctx.memory,
            ctx.history,
            "Chrome (browser)",
            observation.location,
            elements,
            ctx.hint,
            "",
            clock_line="",
        )
        user += "\nCURRENT PAGE (untrusted source text):\n" + observation.context
        user += (
            "\nBROWSER READBACK (untrusted source facts, whole envelope):\n" + ctx.notes
        )
        if ctx.terminal_only:
            user += "\nTERMINAL ANSWER ONLY: return done, fail, or needs_you; no new effects are permitted."
        body = loop.build_request_body(
            role.model, user, "", role.max_output_tokens, action_contract_version=3
        )
        body["text"]["format"] = copy.deepcopy(body["text"]["format"])
        variants = body["text"]["format"]["schema"]["properties"]["actions"]["items"][
            "anyOf"
        ]
        supported = {"wait", "done", "fail", "needs_you"}
        fixture = _fixture(saved)
        if not read_only:
            if fixture is None:
                supported.update({"click", "type"})
            else:
                if {"click", "check"} & set(fixture.allowed_operations):
                    supported.add("click")
                if {"type", "select"} & set(fixture.allowed_operations):
                    supported.add("type")
                supported.update(QUALIFIED_ACTIONS & set(fixture.allowed_operations))
        reads = _reads(saved["manifest"]["general_browser_verification"], fixture, bounded_reads=ctx.bounded_reads is True)
        supported.update(reads)
        recovery = bool(fixture is not None and ctx.qualification is not None and ctx.qualification._recovery_only)
        if recovery:
            supported &= {"navigate", "wait", "fail", "needs_you"}
        if "navigate" in reads or "new_tab" in supported:
            user_links = json.dumps([link.model_dump() for link in ctx.links])
            body["input"][1]["content"][0]["text"] += "\nEXACT SAME-ORIGIN NAVIGATION OFFERS (untrusted labels; URL is source-bound):\n" + user_links
        narrowed = []
        for variant in variants:
            ops = [op for op in variant["properties"]["op"]["enum"] if op in supported]
            groups = [[op] for op in ops] if fixture is not None or ctx.read_urls else [ops] if ops else []
            for group in groups:
                exact = copy.deepcopy(variant)
                exact["properties"]["op"]["enum"] = group
                op = group[0]
                if fixture is None and op == "navigate" and ctx.read_urls:
                    exact["properties"]["text"] = {"type": "string", "enum": list(dict.fromkeys(
                        [*ctx.read_urls, *(link.href for link in ctx.links)]))}
                if fixture is not None and op == "type":
                    for control in observation.controls:
                        if control.visible and control.enabled and "type" in control.operations:
                            pair = copy.deepcopy(exact)
                            pair["properties"]["element_id"] = {"type": "string", "enum": [control.id]}
                            pair["properties"]["text"] = {"type": "string", "enum": [_qualification_fill(fixture, ctx.qualification, control.id)]}
                            narrowed.append(pair)
                    continue
                if fixture is not None and op in {"click", "hover"}:
                    targets = [control.id for control in observation.controls
                               if control.visible and control.enabled and "click" in control.operations
                               and (op == "hover" or not any(meta.id == control.id and meta.hover_only
                                                            for meta in ctx.qualification.controls))]
                    if not targets:
                        continue
                    exact["properties"]["element_id"] = {"type": "string", "enum": targets}
                if fixture is not None and op == "switch_tab":
                    tabs = [tab.id for tab in ctx.qualification.tabs if tab.id != "t" + request.current_load.page_id]
                    if not tabs:
                        continue
                    exact["properties"]["element_id"] = {"type": "string", "enum": tabs}
                if fixture is not None and (op == "new_tab" or recovery and op == "navigate"):
                    urls = [fixture.origin + rule.path + ("?" + rule.query if rule.query else "")
                            for rule in fixture.rules if rule.method == "GET"
                            and set(rule.prerequisites) <= set(ctx.qualification.completed_rules)
                            and (ctx.qualification._eligible_rule_ids is None or rule.rule_id in ctx.qualification._eligible_rule_ids)
                            and (not any(download.rule_id == rule.rule_id for download in fixture.downloads)
                                or recovery and op=='navigate' and not read_only and 'download' in fixture.allowed_operations)]
                    if not urls or op == "new_tab" and len(ctx.qualification.tabs) >= fixture.max_tabs:
                        continue
                    exact["properties"]["text"] = {"type": "string", "enum": urls}
                narrowed.append(exact)
        body["text"]["format"]["schema"]["properties"]["actions"]["items"]["anyOf"] = (
            narrowed
        )
        if ctx.read_urls:
            body["input"][1]["content"][0]["text"] += (
                "\nREVIEWED EXACT READ URLS (GET navigation only, no forms or new tabs):\n" + json.dumps(ctx.read_urls))
        if fixture is not None:
            fields = [{"id": control.id, "text": _qualification_fill(fixture, ctx.qualification, control.id)}
                      for control in observation.controls if control.visible and control.enabled and "type" in control.operations]
            submits = []
            for control in observation.controls:
                if ("click" in supported and control.visible and control.enabled and "click" in control.operations
                        and not any(meta.id == control.id and meta.hover_only for meta in ctx.qualification.controls)):
                    rule_ids = _qualification_click_rules(fixture, ctx.qualification, control.id)
                    if rule_ids:
                        submits.append({"id": control.id, "rule_ids": list(rule_ids)})
            body["input"][1]["content"][0]["text"] += (
                "\nCONTROLLER-REVIEWED FIXTURE INPUTS (exact control/value pairs):\n" + json.dumps(fields)
                + "\nCONTROLLER-REVIEWED FIXTURE SUBMITS (only these POST rules):\n" + json.dumps(submits))
            body["input"][0]["content"][0]["text"] += (
                "\nFixture fields use only the exact control/value pairs above. Select uses op=type and the exact option value; "
                "checkbox/radio uses op=click only when the observed control is offered. Each turn delivers only the first action. "
                "A listed fixture submit is permitted by its reviewed rules; reread after every input and submit. "
                "Never invent values or submit a real account, payment, credential or CAPTCHA form.")
            if QUALIFIED_ACTIONS & supported:
                body["input"][1]["content"][0]["text"] += (
                    "\nOWNED TABS (opaque IDs; current tab is t" + request.current_load.page_id + "):\n"
                    + json.dumps([tab.model_dump() for tab in ctx.qualification.tabs]))
                body["input"][0]["content"][0]["text"] += (
                    "\nGrant-only hover uses a current observed element ID. new_tab uses exactly one offered URL "
                    "within the reviewed tab limit; switch_tab uses an offered owned tab ID. Never invent a tab or frame.")
            if "new_tab" in supported or recovery:
                urls = [fixture.origin + rule.path + ("?" + rule.query if rule.query else "")
                        for rule in fixture.rules if rule.method == "GET"
                        and set(rule.prerequisites) <= set(ctx.qualification.completed_rules)
                        and (ctx.qualification._eligible_rule_ids is None or rule.rule_id in ctx.qualification._eligible_rule_ids)
                        and (not any(download.rule_id == rule.rule_id for download in fixture.downloads)
                            or recovery and not read_only and 'download' in fixture.allowed_operations)]
                body["input"][1]["content"][0]["text"] += (
                    "\nEXACT CONTROLLER-REVIEWED DOCUMENT URLS (grant authority, not observed DOM links):\n" + json.dumps(urls))
            if recovery:
                body["input"][0]["content"][0]["text"] += (
                    "\nKNOWN FAILED FIXTURE DOCUMENT: this is not task success. Only navigate to an exact reviewed "
                    "document URL above, wait, fail, or ask a specific needs_you question. Never submit, mutate or return done here.")
        body["input"][0]["content"][0]["text"] += (
            "\nIf the goal lacks a required value, target, permission, or success criterion, return needs_you "
            "with one specific clarification question before changing anything. Do not invent the founder's intent. "
            "A safe observation may resolve uncertainty; unrelated actions and guessed submissions may not.")
        body["input"][0]["content"][0]["text"] += (
            "\nV2 also permits navigate to exactly one offered link URL, document scroll (up/down/left/right, amount 1..5), "
            "and extract with literal words (1..160 characters) or 'tables'. No script, selectors, form submission or invented URLs."
            if profile.version == "browser_cuai_text_v2" and fixture is None else
            "\nThis source permits only offered same-origin navigate, bounded document scroll, and literal extract. "
            "No scripts, selectors, form submission or invented URLs."
            if ctx.bounded_reads is True and fixture is None else
            "\nThis owned fixture also permits only: " + ", ".join(sorted(reads))
            + " (navigate to exactly one offered link URL; document scroll up/down/left/right, amount 1..5; "
            "extract with literal words of 1..160 characters or 'tables'). No script, selectors or invented URLs."
            if reads else ""
        )
        body["input"][0]["content"][0]["text"] += (
            ("\nThis scoped read-only adapter delivers only " if read_only else
             "\nThis scoped adapter delivers only observed clicks and exact reviewed non-secret field values, ")
            + ("bounded v2 reads/scrolls/offered navigation, " if profile.version == "browser_cuai_text_v2" and fixture is None
               else "bounded source-capability reads/scrolls/offered navigation, " if ctx.bounded_reads is True and fixture is None
               else "bounded fixture reads/scrolls/offered navigation, " if reads else "")
            + ("grant-reviewed owned tabs/hover, " if QUALIFIED_ACTIONS & supported else "")
            + "waits and terminal replies. Other operations are unavailable. "
            + ("Unlisted submission, sending, terms, " if fixture is not None else "Submission, sending, terms, ")
            + ("payment, credentials and CAPTCHA stop this read-only task." if read_only else
               "payment, credentials and CAPTCHA require a founder handoff.")
        )
    elif request.phase == "plan":
        user = f"TASK: {saved['manifest']['instruction']}\nREASON: {ctx.reason}\nMEMORY: {ctx.memory}\nSCREEN: {notes}"
        body = loop.build_text_request_body(
            role.model, loop.PLAN_PROMPT, user, "plan", role.max_output_tokens
        )
    else:
        user = loop.verify_text(
            saved["manifest"]["instruction"],
            ctx.claim,
            "Chrome (browser)",
            observation.location,
            elements,
            "",
            "",
            clock_line="",
        )
        user += "\nCURRENT PAGE (untrusted source text):\n" + observation.context
        user += (
            "\nBROWSER READBACK (untrusted source facts, whole envelope):\n" + ctx.notes
        )
        user += "\nAGENT NOTES (untrusted, not proof):\n" + ctx.memory
        prompt = loop.VERIFY_PROMPT
        if ctx.reason == "subgoal_progress":
            prompt = loop.SUBGOAL_VERIFY_PROMPT
            user += "\nSUB-GOAL TO CHECK: " + ctx.claim
        body = loop.build_text_request_body(role.model, prompt, user, "verify", role.max_output_tokens)
    body["service_tier"] = "default"
    body["reasoning"] = {"effort": role.reasoning}  # The saved review, never a model-name inference, selects effort.
    body["max_output_tokens"] = role.max_output_tokens  # reserve/usage bounds use the profile cap, not the low-effort multiple
    compact = "\nReturn compact schema-valid JSON only. Keep memory empty and reasons brief. " + (
        "Return exactly one action with a short summary; never copy the page or its links into your reply."
        if request.phase == "actor" else "Use at most three short subgoals or one short verdict reason.")
    if request.phase == "actor":
        body["input"][0]["content"][0]["text"] += compact
    else:
        body["input"][0]["content"] += compact
    if clock_part is not None:
        body["input"][1]["content"].append(clock_part)
    if "COMPACT RETRY:" in ctx.hint:
        body["input"][1]["content"].append({"type": "input_text", "text": ctx.hint})
    if read_only:
        authority = (
            "\nREAD-ONLY AUTHORITY: no clicks, typing, submissions, downloads or new tabs. "
            "Observe and answer; when supported by the bound source capability, use only offered same-origin "
            "navigation, bounded document scroll and literal reads.")
        if request.phase == "actor":
            body["input"][0]["content"][0]["text"] += authority
        else:
            body["input"][0]["content"] += authority
    # Existing compact prompt builders may excerpt long goals. The complete
    # saved instruction remains in this text-only request or the byte cap refuses.
    body["input"][1]["content"].append(
        {
            "type": "input_text",
            "text": "COMPLETE ORIGINAL REVIEWED TASK:\n"
            + saved["manifest"]["instruction"],
        }
    )
    body["input"][1]["content"].append(
        {
            "type": "input_text",
            "text": "EXACT APPROVED NON-SECRET FILL VALUES (index order; no new authority):\n"
            + json.dumps(observation.reviewed_text, ensure_ascii=False),
        }
    )
    return body


def _send_provider(url, headers, body):
    with httpx.Client(
        timeout=httpx.Timeout(120, connect=5), follow_redirects=False, trust_env=False
    ) as client:
        status, raw = providers.read_response(client, url, headers, body)
    return status, strict_json(raw) if raw else None


def _provider_call(body):
    return providers.responses_call(_send_provider, body)[0]


def _known_usage(value, role, input_bound, provider="openai"):
    if (
        not isinstance(value, dict)
        or value.get("model") != role.model
        or not (value.get("status") == "completed" or value.get("status") == "incomplete"
            and value.get("incomplete_details") == {"reason": "max_output_tokens"})
        or value.get("service_tier", "default" if provider == "azure" else None) != "default"
    ):
        raise ValueError("device_browser_ai_usage_unconfirmed")
    usage = value.get("usage")
    if (
        not isinstance(usage, dict)
        or any(
            type(usage.get(k)) is not int or not 0 <= usage[k] <= 1000000
            for k in ("input_tokens", "output_tokens")
        )
        or type(usage.get("total_tokens")) is not int
        or usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]
    ):
        raise ValueError("device_browser_ai_usage_unconfirmed")
    # Known over-envelope usage is retained at its larger cost and stops admission.
    return usage, usage["input_tokens"] > input_bound or usage[
        "output_tokens"
    ] > role.max_output_tokens


def _candidate(reply, observation, general, terminal_only, *, current_load=None, links=(), read_only=False, fixture=None, qualification=None, bounded_reads=False):
    raw = reply_json(reply)
    if (
        not isinstance(raw, dict)
        or set(raw) != {"actions", "memory", "subgoal_done"}
        or not isinstance(raw["memory"], str)
        or len(raw["memory"]) > 700
        or type(raw["subgoal_done"]) is not bool
        or not isinstance(raw["actions"], list)
        or not 1 <= len(raw["actions"]) <= 4
    ):
        raise ValueError("device_browser_ai_reply_invalid")
    shapes = {
        "click": {"op", "element_id"},
        "type": {"op", "element_id", "text"},
        "wait": {"op", "ms"},
        **{op: {"op", "summary"} for op in ("done", "fail", "needs_you")},
    }
    reads = _reads(general, fixture, bounded_reads=bounded_reads)
    shapes.update({op: shape for op, shape in (("navigate", {"op", "text"}), ("extract", {"op", "text"}),
                                               ("scroll", {"op", "direction", "amount"})) if op in reads})
    if fixture is not None and not read_only:
        shapes.update({op: shape for op, shape in (("hover", {"op", "element_id"}),
            ("new_tab", {"op", "text"}), ("switch_tab", {"op", "element_id"}))
            if op in fixture.allowed_operations})
    recovery = bool(fixture is not None and qualification is not None and qualification._recovery_only)
    if recovery:
        shapes = {op: shape for op, shape in shapes.items() if op in {"navigate", "wait", "fail", "needs_you"}}
    if read_only:
        shapes.pop("click", None)
        shapes.pop("type", None)
    elif fixture is not None:
        if not {"click", "check"} & set(fixture.allowed_operations):
            shapes.pop("click", None)
        if not {"type", "select"} & set(fixture.allowed_operations):
            shapes.pop("type", None)
    for action in raw["actions"]:
        if (
            not isinstance(action, dict)
            or action.get("op") not in shapes
            or set(action) != shapes[action["op"]]
        ):
            raise ValueError("device_browser_ai_reply_invalid")
        if action["op"] == "wait":
            if type(action["ms"]) is not int or not 1 <= action["ms"] <= 5000:
                raise ValueError("device_browser_ai_reply_invalid")
        elif action["op"] == "scroll":
            if type(action["amount"]) is not int or not 1 <= action["amount"] <= 5 or action["direction"] not in loop.SCROLL_DIRS:
                raise ValueError("device_browser_ai_reply_invalid")
        elif any(not isinstance(value, str) for value in action.values()):
            raise ValueError("device_browser_ai_reply_invalid")
        if "summary" in action and len(action["summary"]) > 500:
            raise ValueError("device_browser_ai_reply_invalid")
    try:
        parsed = loop.parse_reply(raw, action_contract_version=3)
    except loop.ActionError:
        raise ValueError("device_browser_ai_reply_invalid") from None
    first = parsed.actions[0]
    if terminal_only and first.op not in {"done", "fail", "needs_you"}:
        raise ValueError("device_browser_ai_terminal_effect_refused")
    if first.op == "switch_tab":
        if (qualification is None or fixture is None or qualification.grant_digest != fixture.grant_digest
                or sum(tab.id == first.element_id for tab in qualification.tabs) != 1
                or current_load is None or first.element_id == "t" + current_load["page_id"]):
            raise ValueError("device_browser_ai_target_invalid")
        return Candidate("switch_tab", first.element_id, observation.digest())
    if first.op in {"extract", "scroll"} & reads:
        from coding_agents.owned_browser import cuai_document_target
        if first.op == "extract" and (not 0 < len(first.text.strip()) <= 160 or loop.SECRETISH.search(first.text) or loop.CARD.search(first.text)):
            raise ValueError("device_browser_ai_reply_invalid")
        if current_load is None:
            raise ValueError(INVALID)
        return Candidate(first.op, cuai_document_target(current_load, first), observation.digest())
    if first.op in {"navigate", "new_tab"}:
        from app.build.device_jev import _origin
        if (first.op == "navigate" and fixture is None and read_only and bounded_reads is True
                and first.text in (general.get("read_urls") or [])):
            from coding_agents.owned_browser import cuai_document_target
            if current_load is None or current_load.get("qualified") is not True:
                raise ValueError("device_browser_ai_target_invalid")
            return Candidate("navigate", cuai_document_target(current_load, first), observation.digest())
        matching = [link for link in links if link.href == first.text]
        if recovery or first.op == "new_tab" and fixture is not None and not matching:
            from coding_agents.owned_browser import cuai_document_target
            rule = _qualified_read(fixture, qualification, first.text)
            if (current_load is None or any(download.rule_id == rule.rule_id for download in fixture.downloads)
                    and (first.op=='new_tab' or read_only or 'download' not in fixture.allowed_operations)
                    or first.op == "new_tab" and len(qualification.tabs) >= fixture.max_tabs):
                raise ValueError("device_browser_ai_target_invalid")
            return Candidate(first.op, cuai_document_target(current_load, first), observation.digest())
        if len(matching) != 1 or _origin(first.text) != _origin(observation.location):
            raise ValueError("device_browser_ai_target_invalid")
        if fixture is not None:
            # Exact reviewed GET route only. ponytail: prerequisites and per-rule request
            # counts are enforced against durable Core status by the owned route guard.
            try:
                fixture.request_rule(url=first.text, method="GET", completed_rules=(qualification.completed_rules
                    if qualification is not None else [rule.rule_id for rule in fixture.rules]))
            except ValueError:
                raise ValueError("device_browser_ai_target_invalid") from None
            if first.op == "new_tab" and (qualification is None or len(qualification.tabs) >= fixture.max_tabs):
                raise ValueError("device_browser_ai_target_invalid")
        first = loop.Action(op=first.op, element_id=matching[0].id, text=first.text)
    elif first.op not in {"click", "type", "hover"}:
        if first.op not in {"done", "fail", "needs_you", "wait"}:
            raise ValueError("device_browser_ai_action_unsupported")
        return None
    control = next((c for c in observation.controls if c.id == first.element_id), None)
    if (
        not control
        or not control.visible
        or not control.enabled
        or ("click" if first.op in {"navigate", "new_tab", "hover"} else first.op) not in control.operations
    ):
        raise ValueError("device_browser_ai_target_invalid")
    index = None
    if first.op == "type":
        if fixture is not None:
            reviewed = _qualification_fill(fixture, qualification, first.element_id)
            allowed = first.text == reviewed
        else:
            allowed = general.get("allow_type") and first.text in general.get("fill_values", {}).values()
        if not allowed or first.text not in observation.reviewed_text:
            raise ValueError("device_browser_ai_type_unreviewed")
        index = observation.reviewed_text.index(first.text)
    elif fixture is not None and first.op == "click":
        _qualification_click_rules(fixture, qualification, first.element_id)
    return Candidate(
        first.op, first.element_id, observation.digest(), value_index=index
    )


def run(venture_id, caller, order_id, *, request: BrowserAIBody):
    saved = orders.retrieve(venture_id, caller, order_id)
    if isinstance(request.current_load, QualificationErrorLoadProof) and request.phase == "verify":
        raise ValueError(INVALID)
    if orders._qualification_binding(saved) is not None or request.context.qualification is not None:
        from app.build import browser_qualification as qualification
        # Validate caller metadata against the current durable sealed grant before
        # building a provider request, reserving spend, or issuing a child proposal.
        with orders.db()() as session:
            _, saved, account = calls._locked(session, venture_id, caller, order_id)
            qualification.validate_runtime_context(session, account, saved, request.context.qualification,
                observation=request.observation, current_url=request.current_load.location, current_load=request.current_load,
                source_notes=request.context.notes)
    profile, observation = scoped(saved, request)
    # Keep wall time out of the byte budget and fingerprint of an exact retry.
    live_clock = loop.now_line() if request.phase != "plan" else None
    clock_part = {"type": "input_text", "text": CLOCK_QUOTE_TEXT} if live_clock is not None else None
    body = request_body(saved, request, profile, observation, clock_part=clock_part)
    role = getattr(profile.roles, request.phase)
    table = pricing_table()
    rates = {provider: dict(table[pricing_key(role.model, provider)])
             for provider in ({"azure", "openai"} if role.provider == "azure" else {"openai"})}
    input_bound = max(len(json.dumps(body).encode()),
                      len(json.dumps({**body, "model": role.deployment}).encode()) if role.provider == "azure" else 0) + 1024
    quotes = {provider: cost_micro(role.model, input_bound, role.max_output_tokens, reserve=True, rates=row)
              for provider, row in rates.items()}
    quote = sum(quotes.values())
    request_digest = digest(
        {
            "body": body,
            "profile": request.execution_profile_digest,
            "checkpoint": request.checkpoint_digest,
            "review": saved["review_digest"],
            "observation": observation.digest(),
            "load": request.current_load.model_dump(),
        }
    )
    admitted = calls.admit(
        venture_id,
        caller,
        order_id,
        invocation_id=request.invocation_id,
        request_digest=request_digest,
        quote_micro=quote,
        browser_phase={
            "phase": request.phase,
            "profile_digest": request.execution_profile_digest,
            "checkpoint_digest": request.checkpoint_digest,
            "terminal_only": request.context.terminal_only,
            "load": request.current_load.model_dump(),
            **({"bounded_reads": True} if request.context.bounded_reads is True else {}),
        },
    )
    if not admitted["admitted_now"]:
        raise ValueError("device_browser_ai_already_attempted")
    if clock_part is not None:
        clock_part["text"] = live_clock
    fallback = {}
    try:
        if role.provider == "azure":
            value, provider = providers.responses_call(_send_provider, body, provider="azure",
                endpoint=role.endpoint, deployment=role.deployment, fallback_metadata=fallback)
        else:
            value, provider = _provider_call(body), "openai"
        usage, breach = _known_usage(value, role, input_bound, provider)
    except BaseException:
        calls.record_result(
            venture_id,
            caller,
            order_id,
            invocation_id=request.invocation_id,
            actual_micro=None,
            **({"provider": "openai", "fallback_reason": fallback["fallback_reason"],
                "unknown_provider_micro": quotes["azure"], "provider_attempts": [
                    {"provider": "azure", "state": "usage_unconfirmed", "held_micro": quotes["azure"]},
                    *([{"provider": "openai", "state": "usage_unconfirmed"}] if fallback.get("fallback_attempted") else [])]
               } if fallback.get("azure_usage_unconfirmed") else {}),
        )
        raise ValueError("device_browser_ai_provider_or_usage_unconfirmed") from None
    calls.record_result(
        venture_id,
        caller,
        order_id,
        invocation_id=request.invocation_id,
        actual_micro=cost_micro(
            role.model, usage["input_tokens"], usage["output_tokens"], rates=rates[provider]
        ),
        usage={
            "model": role.model,
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
        },
        force_hold=breach,
        provider=provider,
        fallback_reason=fallback.get("fallback_reason"),
        unknown_provider_micro=quotes["azure"] if fallback.get("azure_usage_unconfirmed") else 0,
        provider_attempts=([
            {"provider": "azure", "state": "usage_unconfirmed", "held_micro": quotes["azure"]},
            {"provider": "openai", "state": "known"},
        ] if fallback.get("azure_usage_unconfirmed") else None),
    )
    if breach:
        raise ValueError("device_browser_ai_usage_bounds")
    calls.check_current(venture_id, caller, order_id)
    if value.get("status") == "incomplete":
        raise ValueError("device_browser_ai_reply_truncated")
    try:
        reply = loop.response_text(value)
    except loop.ActionError as error:
        if str(error) in {"openai_response_invalid", "openai_response_text_missing"}:
            raise ValueError("device_browser_ai_reply_invalid") from None
        raise
    proposals = []
    if request.phase == "actor":
        candidate = _candidate(
            reply,
            observation,
            saved["manifest"]["general_browser_verification"],
            request.context.terminal_only,
            current_load=request.current_load.model_dump(), links=request.context.links,
            read_only=orders.general_browser_read_only(saved["manifest"]),
            fixture=_fixture(saved), qualification=request.context.qualification,
            bounded_reads=request.context.bounded_reads is True,
        )
        if candidate:
            rule_ids = None
            fixture = _fixture(saved)
            if fixture is not None and candidate.operation == "click":
                rule_ids = _qualification_click_rules(fixture, request.context.qualification, candidate.target) or None
            extras = {"qualification_rule_ids": rule_ids} if rule_ids is not None else {}
            if fixture is not None and candidate.operation in {'click','navigate'}:
                action=loop.parse_reply(reply_json(reply),action_contract_version=3).actions[0]
                uri=action.text if candidate.operation=='navigate' else next((link.href
                    for link in request.context.qualification.links if link.id==candidate.target),None)
                if uri is not None:
                    _qualified_read(fixture,request.context.qualification,uri)
                    extras['qualification_read_url']=uri
            child = calls.save_browser_actor_proposal(
                venture_id,
                caller,
                order_id,
                invocation_id=request.invocation_id,
                candidate=candidate,
                **extras,
            )
            proposals.append(
                {
                    "action_index": 0,
                    "invocation_id": child,
                    "candidate": asdict(candidate),
                    **({"qualification_rule_ids": list(rule_ids)} if rule_ids is not None else {}),
                }
            )
    elif request.phase == "plan":
        parsed = reply_json(reply)
        if (
            not isinstance(parsed, dict)
            or set(parsed) != {"subgoals"}
            or not isinstance(parsed["subgoals"], list)
            or not 1 <= len(parsed["subgoals"]) <= 12
            or any(
                not isinstance(x, str) or not 1 <= len(x) <= 400
                for x in parsed["subgoals"]
            )
        ):
            raise ValueError("device_browser_ai_plan_invalid")
    else:
        parsed = reply_json(reply)
        if (
            not isinstance(parsed, dict)
            or set(parsed) != {"passed", "reason"}
            or type(parsed["passed"]) is not bool
            or not isinstance(parsed["reason"], str)
            or len(parsed["reason"]) > 200
        ):
            raise ValueError("device_browser_ai_verdict_invalid")
    return {
        "authority": "proposal_only",
        "source_kind": SOURCE_KIND,
        "invocation_id": request.invocation_id,
        "phase": request.phase,
        "profile_digest": request.execution_profile_digest,
        "observation_digest": observation.digest(),
        "model": role.model,
        "provider": provider,
        **fallback,
        "service_tier": "default",
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "reply": reply,
        "effect_proposals": proposals,
        "task_verified": False,
    }
