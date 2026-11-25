"""The controller supplies expectations; generated files are never test authority."""
from __future__ import annotations

from html.parser import HTMLParser
import os
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

from core.artifact_policy import safe_name
from core.artifact_snapshot import canonical, manifest_digest, validate_manifest
from core.local_security import _local_hostname

SHA = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Name = Annotated[str, Field(min_length=1, max_length=160)]
RequiredText = Annotated[str, Field(min_length=1, max_length=500)]
# Six reviewed landing criteria can each carry ten distinct text observations.
# Keep the aggregate contract large enough to preserve every authored item.
MAX_REQUIRED_TEXT = 60
MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
MAX_REPORT_BYTES = 1024 * 1024
MAX_SECONDS = 180
# Bump whenever the controller's required report cases change. The QA image
# advertises this before source upload so a stale pinned runtime cannot run a
# browser job whose otherwise valid report the controller would reject.
CASE_CONTRACT_VERSION = 4
SUPPORTED_CASE_CONTRACT_VERSIONS = (3, 4)
VIEWPORTS = {"desktop": {"width": 1440, "height": 900}, "mobile": {"width": 390, "height": 844}}
STATIC_MEDIA = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript",
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
                ".svg": "image/svg+xml", ".ico": "image/x-icon", ".woff": "font/woff", ".woff2": "font/woff2", ".txt": "text/plain"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# Provider-facing guidance mirrors supported(); these descriptions confer no
# execution authority and do not replace strict runtime validation.
ACTION_PAGE_RULE = (
    "A relative artifact path ending in lowercase .html (for example index.html or docs/about.html). "
    "Use NFC-normalized text, no backslash, colon or Unicode category-C characters, no empty, '.' or '..' "
    "path components, at most 20 components, at most 255 UTF-8 bytes per component and 1024 total. "
    "No component may case-insensitively equal .git, .ssh, .aws, .azure, .npmrc, .pypirc, .netrc, "
    ".openhands, credentials, credentials.json, id_rsa or id_ed25519; start with .env; "
    "or end with .pem, .p12, .pfx or .key. Field character limits also apply."
)
ACTION_TARGET_RULES = {
    "anchor": "Bare destination element ID, without a leading # or CSS selector. Removing '_' and '-' "
              "must leave a nonempty Unicode alphanumeric string; no other characters are allowed. Example: details.",
    "toggle": "Bare destination element ID, without a leading # or CSS selector. Removing '_' and '-' "
              "must leave a nonempty Unicode alphanumeric string; no other characters are allowed. Example: details_panel.",
    "local_navigation": ACTION_PAGE_RULE,
    "external_link": "URL parsed with scheme https or mailto and a nonempty path or network location. "
                     "Local mode also allows HTTP loopback waitlist links, including path prefixes. "
                     "No parsed username/password and no character with code point below 32. Role must be link. "
                     "Examples: https://example.test/about or mailto:hello@example.test. "
                     "A declared URL does not prove destination access or behavior.",
}
ACTION_DEPENDENCY_RULE = (
    "IDs must be unique across actions. Every depends_on ID must refer to an earlier action on the same page; "
    "no unknown, forward, self or cross-page dependency. Use [] when independent."
)


class Action(Strict):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,39}$", min_length=1, max_length=40)]
    kind: Literal["anchor", "local_navigation", "toggle", "external_link"]
    role: Literal["link", "button"]
    name: Name
    page: Annotated[str, Field(min_length=1, max_length=160)] = "index.html"
    target: Name
    depends_on: list[Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9_]{0,39}$")]] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def supported(self):
        safe_name(self.page)
        if not self.page.endswith(".html"):
            raise ValueError("action_page_invalid")
        if self.kind == "local_navigation":
            safe_name(self.target)
            if not self.target.endswith(".html"):
                raise ValueError("action_target_invalid")
        elif self.kind == "external_link":
            target = urlsplit(self.target)
            local_preview = os.environ.get("SUPYMEM_MODE", "").strip().lower() == "local"
            try:
                local_waitlist = (local_preview
                    and target.scheme == "http" and _local_hostname(target.hostname)
                    and (target.port is None or 1 <= target.port <= 65535)
                    and re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*/public/waitlists/[A-Za-z0-9_-]{1,128}(?:/pricing/exp_[a-f0-9]{24}/[a-z][a-z0-9_]{0,19})?", target.path)
                    and not target.fragment)
            except ValueError:
                raise ValueError("external_target_invalid") from None
            if self.role != "link" or (target.scheme not in {"https", "mailto"} and not local_waitlist) or not target.path and not target.netloc:
                raise ValueError("external_target_invalid")
            if target.username or target.password or any(ord(c) < 32 for c in self.target):
                raise ValueError("external_target_invalid")
        elif not self.target.replace("_", "").replace("-", "").isalnum():
            raise ValueError("target_id_invalid")
        return self


class Visual(Strict):
    """A named image observation, distinct from visible marketing copy."""
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,39}$", min_length=1, max_length=40)]
    name: Annotated[str, Field(min_length=12, max_length=160)]


class ManagedRequestForm(Strict):
    html: Annotated[str, Field(min_length=1, max_length=16000)]
    target: Annotated[str, Field(min_length=1, max_length=2048)]

    @property
    def controls(self):
        controls = []
        parser = HTMLParser()
        parser.handle_starttag = lambda tag, attrs: controls.append(dict(attrs)) if tag == "textarea" or (
            tag == "input" and dict(attrs).get("type") not in {"hidden", "submit", "button"}) else None
        parser.feed(self.html)
        parser.close()
        return controls

    @model_validator(mode="after")
    def safe_form(self):
        target = urlsplit(self.target)
        local_waitlist = (os.environ.get("SUPYMEM_MODE", "").strip().lower() == "local"
            and target.scheme == "http" and target.hostname in {"127.0.0.1", "localhost", "::1"})
        if ((target.scheme != "https" and not local_waitlist) or not target.hostname or target.username or target.password
                or target.query or target.fragment or any(ord(character) < 32 for character in self.target)
                or not re.fullmatch(r"/public/waitlists/[A-Za-z0-9_-]{1,128}", target.path)):
            raise ValueError("managed_request_target_invalid")
        elements = []
        parser = HTMLParser()
        parser.handle_starttag = lambda tag, attrs: elements.append((tag, attrs))
        parser.handle_endtag = lambda tag: elements.append(("/" + tag, []))
        parser.feed(self.html)
        parser.close()
        stack, forms, consent = [], 0, False
        allowed = {"id", "style", "name", "type", "value", "required", "maxlength", "autocomplete", "for"}
        for tag, attributes in elements:
            if tag.startswith("/"):
                if not stack or stack.pop() != tag[1:]:
                    raise ValueError("managed_request_form_invalid")
                continue
            attrs = dict(attributes)
            if (tag not in {"form", "input", "textarea", "label", "button"}
                    or len(attrs) != len(attributes) or set(attrs) - (allowed | {"method", "action"} if tag == "form" else allowed)
                    or not stack and tag != "form"
                    or "style" in attrs and not re.fullmatch(r"[a-z-]+:[a-z0-9%.-]+(?:;[a-z-]+:[a-z0-9%.-]+)*;?", attrs["style"] or "")):
                raise ValueError("managed_request_form_invalid")
            if tag == "form":
                forms += 1
                if forms != 1 or attrs.get("id") != "request_form" or attrs.get("method") != "post" or attrs.get("action") != self.target:
                    raise ValueError("managed_request_form_invalid")
            if tag == "input":
                if attrs.get("type") not in {"hidden", "text", "email", "checkbox"}:
                    raise ValueError("managed_request_form_invalid")
                consent |= attrs.get("type") == "checkbox" and attrs.get("name") == "consent" and attrs.get("value") == "yes" and "required" in attrs
            else:
                stack.append(tag)
        if stack or forms != 1 or not consent:
            raise ValueError("managed_request_form_invalid")
        return self


class Contract(Strict):
    profile: Annotated[Literal["static_landing_v1", "founder_product_v1"], Field(min_length=1, max_length=32)]
    product_name: Name
    required_text: list[RequiredText] = Field(min_length=1, max_length=MAX_REQUIRED_TEXT)
    actions: list[Action] = Field(default_factory=list, max_length=30)
    visuals: list[Visual] = Field(default_factory=list, max_length=6)
    managed_request_form: ManagedRequestForm | None = None

    @property
    def case_contract_version(self):
        return 4 if self.managed_request_form is not None else 3

    @model_serializer(mode="wrap")
    def legacy_wire(self, handler):
        data = handler(self)
        if not self.visuals:
            data.pop("visuals", None)
        if self.managed_request_form is None:
            data.pop("managed_request_form", None)
        return data

    @model_validator(mode="after")
    def graph(self):
        seen = {}
        for action in self.actions:
            if action.id in seen or any(key not in seen or seen[key].page != action.page for key in action.depends_on):
                raise ValueError("action_dependency_invalid")
            seen[action.id] = action
        ids = [visual.id for visual in self.visuals]
        names = [" ".join(visual.name.casefold().split()) for visual in self.visuals]
        if len(set(ids)) != len(ids) or len(set(names)) != len(names) or set(ids) & set(seen):
            raise ValueError("visual_identity_invalid")
        return self

    @property
    def digest(self):
        import hashlib
        return hashlib.sha256(canonical(self.model_dump(mode="json"))).hexdigest()


class Input(Strict):
    command_id: str
    source_artifact_id: Name
    source_sha256: SHA
    size_bytes: Annotated[int, Field(gt=0, le=32 * 1024 * 1024)]
    manifest: list[dict] = Field(min_length=1, max_length=2000)
    contract: Contract

    @field_validator("command_id")
    @classmethod
    def command_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("command_id_invalid")
        return value

    @field_validator("manifest")
    @classmethod
    def manifest_valid(cls, value):
        return validate_manifest(value)

    @property
    def digest(self):
        import hashlib
        return hashlib.sha256(canonical(self.model_dump(mode="json"))).hexdigest()

    @property
    def source_manifest_digest(self):
        return manifest_digest(self.manifest)


class Run(Strict):
    command_id: str
    input_digest: SHA


class Case(Strict):
    id: str = Field(min_length=1, max_length=240)
    suite: Literal["artifact", "http", "render", "actions", "forms", "network", "coverage", "runtime"]
    target: str = Field(max_length=200)
    status: Literal["passed", "failed", "unsupported"]
    observed: str = Field(max_length=600)
    expected: str = Field(max_length=600)
    evidence_ids: list[str] = Field(default_factory=list, max_length=6)


class Evidence(Strict):
    id: Annotated[str, Field(pattern=r"^[a-z0-9_-]+\.png$")]
    media_type: Literal["image/png"]
    sha256: SHA
    size_bytes: Annotated[int, Field(gt=0, le=MAX_EVIDENCE_BYTES)]


class Report(Strict):
    schema_version: Literal[1] = 1
    qa_run_id: str
    build_run_id: str
    generation: int = Field(ge=1)
    source_artifact_id: str
    source_sha256: SHA
    source_manifest_digest: SHA
    contract_digest: SHA
    input_digest: SHA
    harness_version: Literal["static_landing_v1", "founder_product_v1"] = "static_landing_v1"
    browser_version: str | None
    browser_sandbox_required: Literal[True] = True
    browser_sandbox_verified: bool
    runtime_uid: int = Field(ge=1)
    started_at: str
    finished_at: str
    outcome: Literal["passed", "failed"]
    cases: list[Case] = Field(min_length=1, max_length=1800)
    evidence: list[Evidence] = Field(max_length=6)
    coverage: dict[str, int | bool]
    exclusions: list[str]

    @model_validator(mode="after")
    def honest_outcome(self):
        ids = [case.id for case in self.cases]
        evidence_ids = [item.id for item in self.evidence]
        if len(ids) != len(set(ids)) or len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("duplicate_report_id")
        if any(key not in evidence_ids for case in self.cases for key in case.evidence_ids):
            raise ValueError("unknown_evidence")
        if sum(item.size_bytes for item in self.evidence) > MAX_EVIDENCE_BYTES:
            raise ValueError("evidence_limit")
        if self.outcome == "passed" and (not self.browser_version or not self.browser_sandbox_verified or not self.coverage.get("complete")
                                        or any(case.status != "passed" for case in self.cases)):
            raise ValueError("report_false_success")
        return self
