"""Pure contracts for tenant-scoped static publication; no provider effects."""
from __future__ import annotations

import json
from pathlib import PurePosixPath
import re

from core.artifact_snapshot import canonical, manifest_digest, validate_manifest
from core.static_qa_contracts import STATIC_MEDIA

PUBLIC_DOMAINS = frozenset({"supymem.com", "staging.supymem.com"})
RESERVED_SLUGS = frozenset({
    "account", "accounts", "admin", "ai", "api", "app", "assets", "auth",
    "billing", "cdn", "console", "credits", "docs", "download", "downloads", "events",
    "files", "ftp", "gateway", "help", "img", "internal", "login", "mail",
    "media", "mx", "ns", "pagebuilder", "portal", "root", "smtp", "ssh", "staging", "status",
    "support", "supymem", "updates", "www",
})
RESERVED_PREFIXES = ("xn--", "aws-", "supymem-", "internal-")
KVS_VALUE_BYTES = 1024
KVS_CAPACITY_BYTES = 5 * 1024 * 1024
KVS_HEADROOM_BYTES = 1024 * 1024
MAX_PUBLIC_PATH = 512
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{1,46}[a-z0-9]")
_ID = re.compile(r"(?:app|dep)_[a-z0-9]{8,40}")
_FILE = re.compile(r"[A-Za-z0-9_~-][A-Za-z0-9._~-]*")


class HostingRejected(ValueError):
    """Stable safe failure code, never source content or cloud credentials."""


def valid_slug(slug: str, *, additional_reserved: frozenset[str] = frozenset()) -> str:
    if (not isinstance(slug, str) or not _SLUG.fullmatch(slug)
            or slug in RESERVED_SLUGS or slug in additional_reserved
            or slug.startswith(RESERVED_PREFIXES) or slug.isdecimal()
            or re.fullmatch(r"(?:\d{1,3}-){3}\d{1,3}", slug)
            or re.fullmatch(r"(?:em|url)\d+", slug)):
        raise HostingRejected("hosting_slug_unavailable")
    return slug


def public_hostname(slug: str, domain: str) -> str:
    if domain not in PUBLIC_DOMAINS:
        raise HostingRejected("hosting_domain_invalid")
    return valid_slug(slug) + "." + domain


def public_path(path: str) -> str:
    """Conservative public filename profile, checked before source publication."""
    if not isinstance(path, str) or not 1 <= len(path) <= MAX_PUBLIC_PATH:
        raise HostingRejected("hosting_public_path_invalid")
    if any(not _FILE.fullmatch(part) for part in path.split("/")):
        raise HostingRejected("hosting_public_path_invalid")
    if PurePosixPath(path).suffix.lower() not in STATIC_MEDIA:
        raise HostingRejected("hosting_public_file_unsupported")
    return path


def public_manifest(manifest: list[dict]) -> dict:
    """Validate an already attested QA source, without guessing a build folder."""
    validate_manifest(manifest)
    pages = []
    files = []
    for item in manifest:
        path = public_path(item["path"])
        if path.endswith(".html"):
            pages.append(path)
        files.append({**item, "content_type": STATIC_MEDIA[PurePosixPath(path).suffix.lower()]})
    if "index.html" not in pages or len(pages) > 10:
        raise HostingRejected("hosting_static_entry_invalid")
    return {"v": 1, "source_manifest_digest": manifest_digest(manifest), "files": files,
            "total_bytes": sum(item["size_bytes"] for item in manifest)}


def host_mapping(*, app_id: str, deployment_id: str, generation: int, enabled: bool) -> dict:
    if (not isinstance(app_id, str) or not _ID.fullmatch(app_id) or not app_id.startswith("app_")
            or not isinstance(deployment_id, str) or not _ID.fullmatch(deployment_id) or not deployment_id.startswith("dep_")
            or type(generation) is not int or not 1 <= generation <= 2**31 - 1 or type(enabled) is not bool):
        raise HostingRejected("hosting_mapping_invalid")
    return {"v": 1, "app": app_id, "deployment": deployment_id, "generation": generation,
            "prefix": f"published/{app_id}/{deployment_id}", "enabled": enabled}


def validate_mapping(raw: str) -> dict:
    if not isinstance(raw, str) or len(raw.encode()) > KVS_VALUE_BYTES:
        raise HostingRejected("hosting_mapping_invalid")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"v", "app", "deployment", "generation", "prefix", "enabled"}:
            raise HostingRejected("hosting_mapping_invalid")
        expected = host_mapping(app_id=value["app"], deployment_id=value["deployment"],
                                generation=value["generation"], enabled=value["enabled"])
        if type(value["v"]) is not int or value != expected:
            raise HostingRejected("hosting_mapping_invalid")
        return value
    except (TypeError, ValueError, KeyError) as exc:
        raise HostingRejected("hosting_mapping_invalid") from exc


def check_mapping_capacity(hostname: str, value: dict, *, total_bytes: int, replaced_bytes: int = 0) -> bytes:
    """Require measured KVS usage before a publish operation can copy or switch."""
    if (not isinstance(hostname, str) or not any(
            hostname.endswith("." + domain) and public_hostname(hostname[:-(len(domain) + 1)], domain) == hostname
            for domain in sorted(PUBLIC_DOMAINS, key=len, reverse=True))):
        raise HostingRejected("hosting_hostname_invalid")
    raw = canonical(value)
    validate_mapping(raw.decode())
    if (type(total_bytes) is not int or not 0 <= total_bytes <= KVS_CAPACITY_BYTES
            or type(replaced_bytes) is not int or not 0 <= replaced_bytes <= total_bytes):
        raise HostingRejected("hosting_capacity_unknown")
    if total_bytes - replaced_bytes + len(hostname.encode()) + len(raw) > KVS_CAPACITY_BYTES - KVS_HEADROOM_BYTES:
        raise HostingRejected("hosting_capacity_exhausted")
    return raw
