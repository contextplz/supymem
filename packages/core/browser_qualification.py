"""Immutable controller-issued fixture scope; parsing alone grants no authority.

The saved Core order, fresh browser document/control, durable effect admission,
public-address DNS check and fixture handler must independently enforce this
same scope. No origin-wide permission or caller supplied ownership assertion.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields
from urllib.parse import parse_qsl, urlencode, urlsplit

from .canonical import digest

FIXTURE_ORIGIN = "https://api.supymem.com"
FIXTURE_PREFIX = "/public/browser-qualification/"
TRIAL_CEILING_MICRO_USD = 10_000_000
SHORT_TASKS = (
    "read_fact", "search_price", "multitab", "form_valid", "form_recover", "download",
    "paginate", "todos", "counter", "delayed", "iframe", "new_tab_link", "scroll_footer",
    "cookie_banner", "select_prefs", "cheapest_item", "load_more", "table_max", "wizard",
    "faq_details", "library_all", "directory_email", "login_creds", "login_wall", "captcha_wall", "pay_wall",
)
LONG_TASKS = (
    "l01_research_synthesis", "l02_wizard_back_forward_validation", "l03_mini_shop_stop_at_payment",
    "l04_ten_page_collection", "l05_multitab_compare_and_fill", "l06_recover_from_error_page",
    "qualify_compare_create", "refresh_replan", "bundle_exact_progress", "explore_current_reference",
)
REFUSAL_TASKS = frozenset({"login_creds", "login_wall", "captcha_wall", "pay_wall"})
OPERATIONS = frozenset({"click", "type", "wait", "navigate", "scroll", "extract", "select", "check",
                        "new_tab", "switch_tab", "close_tab", "download", "hover"})
INVALID = "browser_qualification_scope_invalid"


def _require(value: bool) -> None:
    if not value:
        raise ValueError(INVALID)


def _identifier(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None


def _hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _integer(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _path(value: object) -> bool:
    return (isinstance(value, str) and len(value) <= 512 and value.startswith(FIXTURE_PREFIX)
            and re.fullmatch(r"/[A-Za-z0-9_./-]+", value) is not None
            and not any(part in {".", "..", ""} for part in value.split("/")[1:]))


def _query(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 512:
        return False
    pairs = parse_qsl(value, keep_blank_values=True)
    return len({key for key, _ in pairs}) == len(pairs) and urlencode(pairs) == value


def _shape(cls, value: object, *, optional=()) -> dict:
    _require(isinstance(value, dict))
    names = {field.name for field in fields(cls)}
    _require(set(value) <= names and names - set(optional) <= set(value))
    return dict(value)


@dataclass(frozen=True)
class FixtureField:
    name: str
    value: str | int | bool | tuple[str, ...]
    control_id: str
    kind: str
    input_value: str | None = None
    input_type: str | None = None

    def __post_init__(self):
        _require(_identifier(self.name) and _identifier(self.control_id))
        _require(isinstance(self.kind, str) and self.kind in {
            "text", "email", "synthetic_code", "synthetic_billing_contact", "select", "radio", "checkbox", "json_literal"})
        # Exceptions are exact synthetic fixture literals, never real credentials or payment fields.
        _require(not re.search(r"password|passwd|otp|captcha|card|cvc|cvv|terms|token|secret|account", self.name, re.I))
        if self.input_value is None:
            _require(self.input_type is None)
        else:
            _require(isinstance(self.input_value, str) and 1 <= len(self.input_value) <= 600
                     and not any(ord(c) < 32 or ord(c) == 127 for c in self.input_value)
                     and isinstance(self.input_type, str) and self.input_type in {"text", "email", "tel", "textarea"})
            if self.kind in {"email", "synthetic_billing_contact"}:
                _require(self.input_value == self.value and self.input_type in {"text", "email"})
            if self.kind == "synthetic_code":
                _require(self.input_value == self.value and self.input_type == "text")
        if self.kind == "json_literal":
            values = self.value if type(self.value) is tuple else (self.value,)
            _require(len(values) <= 16 and (type(self.value) is not tuple or all(isinstance(value, str) for value in values))
                and all(type(value) is bool or _integer(value, -1_000_000, 1_000_000)
                or isinstance(value, str) and 0 <= len(value) <= 600
                and not any(ord(c) < 32 or ord(c) == 127 for c in value) for value in values))
            return
        _require(isinstance(self.value, str) and 1 <= len(self.value) <= 600
                 and not any(ord(c) < 32 or ord(c) == 127 for c in self.value))
        if self.kind in {"email", "synthetic_billing_contact"}:
            _require(re.fullmatch(r"[A-Za-z0-9._+-]+@example\.invalid", self.value) is not None)
        if self.kind == "synthetic_code":
            _require(self.name in {"code", "prev"} and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.value) is not None)
        if self.kind == "checkbox":
            _require(self.value in {"yes", "no"})

    @classmethod
    def from_dict(cls, value):
        doc = _shape(cls, value, optional={"input_value", "input_type"})
        if isinstance(doc["value"], list):
            _require(doc["kind"] == "json_literal")
            doc["value"] = tuple(doc["value"])
        return cls(**doc)


@dataclass(frozen=True)
class FixtureRule:
    rule_id: str
    method: str
    path: str
    query: str
    document_path: str
    document_id: str
    form_id: str
    control_id: str
    fields: tuple[FixtureField, ...]
    max_requests: int
    max_commits: int
    prerequisites: tuple[str, ...]
    rejected_retry_limit: int = 0
    body_kind: str = "form"

    def __post_init__(self):
        _require(_identifier(self.rule_id) and isinstance(self.method, str) and self.method in {"GET", "HEAD", "POST"})
        _require(_path(self.path) and _path(self.document_path) and _query(self.query))
        _require(_identifier(self.document_id))
        _require(isinstance(self.body_kind, str) and self.body_kind in {"form", "json"})
        _require(type(self.fields) is tuple and len(self.fields) <= 16
                 and all(isinstance(field, FixtureField) for field in self.fields)
                 and len({field.name for field in self.fields}) == len(self.fields)
                 and len({field.control_id for field in self.fields}) == len(self.fields))
        _require(_integer(self.max_requests, 1, 128) and _integer(self.max_commits, 0, 16)
                 and self.max_commits <= self.max_requests and _integer(self.rejected_retry_limit, 0, 1))
        _require(type(self.prerequisites) is tuple and len(self.prerequisites) <= 36
                 and all(_identifier(key) and key != self.rule_id for key in self.prerequisites)
                 and len(set(self.prerequisites)) == len(self.prerequisites))
        if self.method == "POST":
            _require(_identifier(self.form_id) and _identifier(self.control_id)
                     and (self.max_commits > 0 or self.rejected_retry_limit == 1)
                     and self.body_kind in {"form", "json"}
                     and (self.body_kind == "json" or all(isinstance(field.value, str) for field in self.fields)))
        else:
            _require(self.max_commits == self.rejected_retry_limit == 0)
            if self.fields:
                _require(_identifier(self.form_id) and _identifier(self.control_id)
                         and all(isinstance(field.value, str) and (field.name, field.value) in parse_qsl(self.query)
                                 for field in self.fields))
            else:
                _require(self.form_id == self.control_id == "")

    @classmethod
    def from_dict(cls, value):
        doc = _shape(cls, value, optional={"rejected_retry_limit", "body_kind"})
        _require(isinstance(doc["fields"], list) and isinstance(doc["prerequisites"], list))
        doc["fields"] = tuple(FixtureField.from_dict(field) for field in doc["fields"])
        doc["prerequisites"] = tuple(doc["prerequisites"])
        return cls(**doc)


@dataclass(frozen=True)
class FixtureFrame:
    document_path: str
    parent_document_path: str
    frame_id: str

    def __post_init__(self):
        _require(_path(self.document_path) and _path(self.parent_document_path) and _identifier(self.frame_id))
        _require(self.document_path != self.parent_document_path)

    @classmethod
    def from_dict(cls, value):
        return cls(**_shape(cls, value))


@dataclass(frozen=True)
class FixtureDownload:
    rule_id: str
    filename: str
    media_type: str
    max_bytes: int
    sha256: str

    def __post_init__(self):
        _require(_identifier(self.rule_id) and isinstance(self.filename, str)
                 and re.fullmatch(r"[A-Za-z0-9_-]{1,80}\.csv", self.filename) is not None)
        _require(self.media_type == "text/csv" and _integer(self.max_bytes, 1, 1_048_576) and _hash(self.sha256))

    @classmethod
    def from_dict(cls, value):
        return cls(**_shape(cls, value))


@dataclass(frozen=True)
class OwnedFixtureGrant:
    schema_version: int
    actor_id: str
    device_id: str
    workspace_id: str
    venture_id: str
    trial_id: str
    catalog_digest: str
    task_id: str
    seed_digest: str
    order_id: str
    review_digest: str
    profile_digest: str
    expires_at: int
    nonce: str
    origin: str
    rules: tuple[FixtureRule, ...]
    allowed_operations: tuple[str, ...] = ()
    max_tabs: int = 1
    frames: tuple[FixtureFrame, ...] = ()
    downloads: tuple[FixtureDownload, ...] = ()

    def __post_init__(self):
        _require(type(self.schema_version) is int and self.schema_version == 1)
        _require(all(_identifier(getattr(self, key)) for key in (
            "actor_id", "device_id", "workspace_id", "venture_id", "trial_id", "order_id", "nonce")))
        _require(len(self.nonce) >= 32 and all(_hash(getattr(self, key)) for key in (
            "catalog_digest", "seed_digest", "review_digest", "profile_digest")))
        _require(self.task_id in SHORT_TASKS + LONG_TASKS and self.origin == FIXTURE_ORIGIN
                 and _integer(self.expires_at, 1, 32_503_680_000))
        _require(type(self.rules) is tuple and 1 <= len(self.rules) <= 128
                 and all(isinstance(rule, FixtureRule) for rule in self.rules))
        ids = [rule.rule_id for rule in self.rules]
        _require(len(set(ids)) == len(ids) and len({(r.method, r.path, r.query, digest(asdict(r)["fields"]))
                                                 for r in self.rules}) == len(ids))
        prefix = FIXTURE_PREFIX + self.nonce + "/"
        for index, rule in enumerate(self.rules):
            _require(rule.path.startswith(prefix) and rule.document_path.startswith(prefix)
                     and set(rule.prerequisites) <= set(ids[:index]))
        _require(type(self.allowed_operations) is tuple and all(isinstance(op, str) for op in self.allowed_operations)
                 and set(self.allowed_operations) <= OPERATIONS
                 and len(set(self.allowed_operations)) == len(self.allowed_operations)
                 and _integer(self.max_tabs, 1, 4))
        _require(type(self.frames) is tuple and len(self.frames) <= 2
                 and all(isinstance(frame, FixtureFrame) and frame.document_path.startswith(prefix)
                         and frame.parent_document_path.startswith(prefix) for frame in self.frames))
        _require(type(self.downloads) is tuple and len(self.downloads) <= 1
                 and all(isinstance(download, FixtureDownload) and any(
                     rule.rule_id == download.rule_id and rule.method == "GET" for rule in self.rules)
                         for download in self.downloads))
        _require((self.max_tabs == 1 or "new_tab" in self.allowed_operations)
                 and (not self.downloads or "download" in self.allowed_operations))
        if self.task_id in REFUSAL_TASKS:
            _require(all(rule.method in {"GET", "HEAD"} for rule in self.rules)
                     and not set(self.allowed_operations) & {"click", "type", "select", "check", "download"}
                     and not self.downloads)

    @property
    def grant_digest(self) -> str:
        return digest(asdict(self))

    def to_dict(self) -> dict:
        import json
        # JSON roundtrip makes tuples lists without retaining mutable caller-owned values.
        return json.loads(json.dumps(asdict(self))) | {"grant_digest": self.grant_digest}

    @classmethod
    def from_dict(cls, value):
        _require(isinstance(value, dict) and _hash(value.get("grant_digest")))
        doc = _shape(cls, {key: item for key, item in value.items() if key != "grant_digest"},
                     optional={"allowed_operations", "max_tabs", "frames", "downloads"})
        for key, item_type in (("rules", FixtureRule), ("frames", FixtureFrame), ("downloads", FixtureDownload)):
            if key in doc:
                _require(isinstance(doc[key], list))
                doc[key] = tuple(item_type.from_dict(item) for item in doc[key])
        if "allowed_operations" in doc:
            _require(isinstance(doc["allowed_operations"], list))
            doc["allowed_operations"] = tuple(doc["allowed_operations"])
        grant = cls(**doc)
        _require(grant.grant_digest == value["grant_digest"])
        return grant

    def assert_current(self, *, actor_id, device_id, workspace_id, venture_id,
                       order_id, review_digest, profile_digest, now: int) -> None:
        expected = (actor_id, device_id, workspace_id, venture_id, order_id, review_digest, profile_digest)
        actual = (self.actor_id, self.device_id, self.workspace_id, self.venture_id,
                  self.order_id, self.review_digest, self.profile_digest)
        _require(actual == expected and type(now) is int and now < self.expires_at)

    def trial_binding(self) -> QualificationTrialBinding:
        return QualificationTrialBinding(**{field.name: getattr(self, field.name) for field in fields(QualificationTrialBinding)})

    def request_rule(self, *, url: str, method: str, submitted_fields=(), completed_rules=()) -> FixtureRule:
        """Exact request match only; durable counts/effect custody are checked separately."""
        parsed = urlsplit(url)
        _require(parsed.scheme + "://" + parsed.netloc == self.origin and not parsed.fragment)
        _require(isinstance(submitted_fields, (tuple, list)))
        submitted = []
        for pair in submitted_fields:
            _require(isinstance(pair, (tuple, list)) and len(pair) == 2 and isinstance(pair[0], str))
            name, value = pair
            _require(type(value) in {str, int, bool, tuple, list})
            if isinstance(value, (tuple, list)):
                _require(all(isinstance(item, str) for item in value))
                value = tuple(value)
            submitted.append((name, value))
        _require(len({name for name, _ in submitted}) == len(submitted))
        for rule in self.rules:
            expected = tuple((field.name, field.value) for field in rule.fields)
            request_fields = submitted
            if method in {"GET", "HEAD"} and not submitted and rule.fields:
                request_fields = [(field.name, dict(parse_qsl(parsed.query)).get(field.name)) for field in rule.fields]
            if ((rule.method, rule.path, rule.query) == (method, parsed.path, parsed.query)
                    and digest(sorted(request_fields)) == digest(sorted(expected))
                    and set(rule.prerequisites) <= set(completed_rules)):
                return rule
        raise ValueError(INVALID)


def request_binding_digest(grant: OwnedFixtureGrant, rule: FixtureRule, request_id: str) -> str:
    """Core effect scope and handler join the same exact one-use request identity."""
    _require(isinstance(rule, FixtureRule) and any(digest(asdict(rule)) == digest(asdict(item)) for item in grant.rules)
             and _identifier(request_id))
    return digest({"grant_digest": grant.grant_digest, "rule_id": rule.rule_id, "request_id": request_id,
        "method": rule.method, "path": rule.path, "query": rule.query, "body_kind": rule.body_kind,
        "fields": sorted((field.name, field.value) for field in rule.fields)})


@dataclass(frozen=True)
class QualificationTrialBinding:
    """Budget membership only: grants no fixture, input, network or effect exception."""
    schema_version: int
    actor_id: str
    device_id: str
    workspace_id: str
    venture_id: str
    trial_id: str
    catalog_digest: str
    task_id: str
    seed_digest: str
    order_id: str
    review_digest: str
    profile_digest: str
    expires_at: int

    def __post_init__(self):
        _require(type(self.schema_version) is int and self.schema_version == 1)
        _require(all(_identifier(getattr(self, key)) for key in (
            "actor_id", "device_id", "workspace_id", "venture_id", "trial_id", "order_id")))
        _require(all(_hash(getattr(self, key)) for key in ("catalog_digest", "seed_digest", "review_digest", "profile_digest")))
        _require(self.task_id in SHORT_TASKS + LONG_TASKS and _integer(self.expires_at, 1, 32_503_680_000))

    @property
    def binding_digest(self):
        return digest(asdict(self))

    def to_dict(self):
        return asdict(self) | {"binding_digest": self.binding_digest}

    @classmethod
    def from_dict(cls, value):
        _require(isinstance(value, dict) and _hash(value.get("binding_digest")))
        binding = cls(**_shape(cls, {key: item for key, item in value.items() if key != "binding_digest"}))
        _require(binding.binding_digest == value["binding_digest"])
        return binding

    def assert_current(self, *, actor_id, device_id, workspace_id, venture_id,
                       order_id, review_digest, profile_digest, now):
        actual = (self.actor_id, self.device_id, self.workspace_id, self.venture_id, self.order_id, self.review_digest, self.profile_digest)
        _require(actual == (actor_id, device_id, workspace_id, venture_id, order_id, review_digest, profile_digest)
                 and type(now) is int and now < self.expires_at)
