"""Strict source-intake contracts. Values authorize neither host paths nor execution."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from core.artifact_policy import DEFAULT_LIMITS, ArtifactRejected, safe_name
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

POLICY_VERSION = "import-intake-v1"
MAX_ARCHIVE_BYTES = DEFAULT_LIMITS.max_archive_bytes
MAX_REPORT_BYTES = 256 * 1024
MAX_CONFIG_BYTES = 1024 * 1024
MAX_PARSE_BYTES = 4 * 1024 * 1024
OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
REPO = r"[A-Za-z0-9_.-]{1,100}"
SHA = r"[a-f0-9]{40}(?:[a-f0-9]{24})?"


class ImportFailure(ValueError):
    """Safe stable error; never carry source bytes or remote error bodies."""
    def __init__(self, code: str, *, status: int = 422, retry_after: int | None = None):
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,99}", code):
            raise ValueError("invalid_import_error")
        self.code, self.status, self.retry_after = code, status, retry_after
        super().__init__(code)


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def repository(value: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment
                or any(ord(c) < 33 or ord(c) == 127 for c in value) or "%" in value or "\\" in value):
            raise ValueError()
        parts = parsed.path.removesuffix("/").split("/")
        if len(parts) != 3 or parts[0]:
            raise ValueError()
        owner, repo = parts[1], parts[2].removesuffix(".git")
        if not re.fullmatch(OWNER, owner) or not re.fullmatch(REPO, repo) or repo in {".", ".."}:
            raise ValueError()
        return owner, repo
    except (ValueError, TypeError, AttributeError):
        raise ValueError("import_repository_url_invalid") from None


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GitHubSource(Strict):
    source: Literal["github_public"]
    repo_url: str = Field(min_length=1, max_length=300)
    ref: str | None = Field(default=None, min_length=1, max_length=200)
    subdirectory: str | None = Field(default=None, min_length=1, max_length=1024)

    @field_validator("repo_url")
    @classmethod
    def url(cls, value):
        owner, repo = repository(value)
        return f"https://github.com/{owner}/{repo}"

    @field_validator("ref")
    @classmethod
    def valid_ref(cls, value):
        if value is not None and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", value)
                                  or ".." in value or "//" in value or value.endswith(("/", ".", ".lock"))):
            raise ValueError("import_ref_invalid")
        return value

    @field_validator("subdirectory")
    @classmethod
    def directory(cls, value):
        if value is not None:
            try:
                safe_name(value)
            except ArtifactRejected:
                raise ValueError("import_subdirectory_invalid") from None
        return value


class ZipSource(Strict):
    source: Literal["upload_zip"]
    filename: str = Field(min_length=5, max_length=180)
    size_bytes: int = Field(gt=0, le=MAX_ARCHIVE_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    # A caller-selected wrapper is explicit; source directories are not guessed.
    root: str | None = Field(default=None, min_length=1, max_length=1024)

    @field_validator("filename")
    @classmethod
    def name(cls, value):
        if (not value.lower().endswith(".zip") or "/" in value or "\\" in value
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError("import_filename_invalid")
        return value

    @field_validator("root")
    @classmethod
    def directory(cls, value):
        return GitHubSource.directory(value)


Source = Annotated[GitHubSource | ZipSource, Field(discriminator="source")]


class ResolvedGitHub(Strict):
    repository: str
    commit_sha: str = Field(pattern="^" + SHA + "$")
    requested_ref: str
    public: Literal[True]
    policy_version: Literal["import-intake-v1"] = POLICY_VERSION

    @model_validator(mode="after")
    def identity(self):
        owner, repo = repository("https://github.com/" + self.repository)
        if f"{owner}/{repo}" != self.repository:
            raise ValueError("import_repository_identity_invalid")
        GitHubSource.valid_ref(self.requested_ref)
        return self
