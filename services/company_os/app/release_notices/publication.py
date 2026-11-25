"""Verify the existing public updater contract without downloading into RAM.

The publisher authenticates artifact bytes with Minisign; latest.json and its
policy are authoritative through the fixed HTTPS channel, not a JSON signature.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MANIFEST_URL = "https://supymem.com/downloads/latest.json"
DOWNLOAD_URL = "https://supymem.com/downloads/Supymem.dmg"
# The production Tauri updater key. A source test fences rotation against its config.
UPDATER_PUBLIC_KEY = "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IEFGOTEyQjQxNTRBNDA2QQpSV1JxUUVvVnRCTDVDcUVEQmhzZUw4WS9GR1dsNk9hejArREo2Uk9wSDByR0FxWG1XTEYxTUt2Two="
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
FETCH_SECONDS = 60
VERSION = re.compile(r"(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})")
MARKER = re.compile(r"<!--cos-update\s*(\{.*?\})\s*-->", re.DOTALL)


class ReleaseUnavailable(ValueError):
    """Uncertain/unverifiable publication: defer delivery, never authorize it."""


def version_tuple(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ReleaseUnavailable("release_version_invalid")
    return tuple(map(int, value.split(".")))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseUnavailable("release_duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("non_json_number")


def _publication_date(value):
    if value is None:
        return
    if not isinstance(value, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})", value):
        raise ValueError("release_date_invalid")
    datetime.fromisoformat(value.upper().replace("Z", "+00:00"))


@dataclass(frozen=True, repr=False)
class Publication:
    version: str
    mandatory: bool
    min_version: str | None
    manifest_sha256: str
    artifact_url: str
    artifact_sha256: str
    signature: str

    @property
    def witness_id(self):
        fields = [self.version, self.artifact_url, self.artifact_sha256, UPDATER_PUBLIC_KEY]
        return "ane_release_" + hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()

    def required_for(self, current):
        try:
            installed, remote = version_tuple(current), version_tuple(self.version)
            return installed < remote and (self.mandatory or self.min_version is not None
                                           and installed < version_tuple(self.min_version))
        except ReleaseUnavailable:
            return False  # An unknown/legacy device version is not current version evidence.


def parse_manifest(raw):
    try:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_MANIFEST_BYTES:
            raise ValueError
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_invalid_constant)
        if not isinstance(body, dict) or body.get("test_fixture") not in (None, False):
            raise ValueError
        version_tuple(body.get("version"))
        _publication_date(body.get("pub_date"))
        notes = body.get("notes")
        if not isinstance(notes, str) or len(notes.encode()) > MAX_MANIFEST_BYTES:
            raise ValueError
        markers = MARKER.findall(notes)
        if len(markers) != 1 or notes.count("<!--cos-update") != 1:
            raise ValueError
        policy = json.loads(markers[0], object_pairs_hook=_unique, parse_constant=_invalid_constant)
        if not isinstance(policy, dict) or set(policy) - {"mandatory", "min_version"} or type(policy.get("mandatory")) is not bool:
            raise ValueError
        minimum = policy.get("min_version")
        if minimum is not None and version_tuple(minimum) > version_tuple(body["version"]):
            raise ValueError
        platforms = body.get("platforms")
        if not isinstance(platforms, dict) or set(platforms) != {"darwin-aarch64"}:
            raise ValueError
        spec = platforms["darwin-aarch64"]
        if not isinstance(spec, dict) or set(spec) != {"url", "signature"}:
            raise ValueError
        url, signature = spec["url"], spec["signature"]
        if (not isinstance(url, str) or any(ord(c) <= 32 or c == "\\" for c in url)
                or not isinstance(signature, str) or not 0 < len(signature) <= 8192):
            raise ValueError
        parts = urlsplit(url)
        path = parts.path.split("/")
        if (parts.scheme != "https" or parts.netloc != "supymem.com" or parts.query or parts.fragment
                or len(path) != 5 or path[:3] != ["", "downloads", body["version"]]
                or not re.fullmatch(r"[a-f0-9]{64}", path[3])
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.app\.tar\.gz", path[4])):
            raise ValueError
        _signature(signature)  # Reject invalid key/global comment before artifact I/O.
        return Publication(body["version"], policy["mandatory"], minimum, hashlib.sha256(raw).hexdigest(), url, path[3], signature)
    except ReleaseUnavailable:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ReleaseUnavailable("release_manifest_invalid") from None


def _signature(signature):
    try:
        public_lines = base64.b64decode(UPDATER_PUBLIC_KEY, validate=True).decode().splitlines()
        signature_lines = base64.b64decode(signature, validate=True).decode().splitlines()
        if len(public_lines) != 2 or len(signature_lines) != 4:
            raise ValueError
        public = base64.b64decode(public_lines[1], validate=True)
        signed = base64.b64decode(signature_lines[1], validate=True)
        global_signature = base64.b64decode(signature_lines[3], validate=True)
        if (len(public) != 42 or public[:2] not in {b"Ed", b"ED"} or len(signed) != 74
                or signed[:2] != b"ED" or public[2:10] != signed[2:10] or len(global_signature) != 64
                or not signature_lines[2].startswith("trusted comment: ")):
            raise ValueError
        key = Ed25519PublicKey.from_public_bytes(public[10:])
        key.verify(global_signature, signed[10:] + signature_lines[2][17:].encode())
        return key, signed[10:]
    except Exception:
        raise ReleaseUnavailable("release_signature_invalid") from None


def client():
    return httpx.Client(timeout=httpx.Timeout(5, connect=3), follow_redirects=False, trust_env=False,
                        headers={"Accept-Encoding": "identity"})


def _chunks(client, url, maximum, deadline):
    try:
        if time.monotonic() >= deadline:
            raise ReleaseUnavailable("release_fetch_expired")
        with client.stream("GET", url) as response:
            if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                raise ReleaseUnavailable("release_publication_unavailable")
            declared = response.headers.get("content-length")
            if declared is not None and (not re.fullmatch(r"[0-9]{1,10}", declared) or not 0 < int(declared) <= maximum):
                raise ReleaseUnavailable("release_size_invalid")
            size = 0
            # Check each transport chunk; coalescing to 64KiB would hide a slow
            # dribble below the per-read timeout from the total deadline.
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > maximum or time.monotonic() >= deadline:
                    raise ReleaseUnavailable("release_fetch_bound_exceeded")
                yield chunk
            if not size or declared is not None and size != int(declared):
                raise ReleaseUnavailable("release_size_invalid")
    except httpx.HTTPError:
        raise ReleaseUnavailable("release_publication_unavailable") from None


def fetch_manifest(client, deadline):
    return parse_manifest(b"".join(_chunks(client, MANIFEST_URL, MAX_MANIFEST_BYTES, deadline)))


def verify_artifact(client, publication, deadline):
    sha, prehash, size = hashlib.sha256(), hashlib.blake2b(digest_size=64), 0
    for chunk in _chunks(client, publication.artifact_url, MAX_ARTIFACT_BYTES, deadline):
        size += len(chunk)
        sha.update(chunk)
        prehash.update(chunk)
    if sha.hexdigest() != publication.artifact_sha256:
        raise ReleaseUnavailable("release_artifact_hash_changed")
    witness = {"schema_version": 1, "version": publication.version, "artifact_url": publication.artifact_url,
               "artifact_sha256": sha.hexdigest(), "artifact_blake2b": prehash.hexdigest(), "artifact_bytes": size,
               "key_sha256": hashlib.sha256(UPDATER_PUBLIC_KEY.encode()).hexdigest()}
    verify_witness(publication, witness)
    return witness


def verify_witness(publication, witness):
    try:
        if (not isinstance(witness, dict) or set(witness) != {"schema_version", "version", "artifact_url", "artifact_sha256",
                "artifact_blake2b", "artifact_bytes", "key_sha256"} or type(witness["schema_version"]) is not int
                or witness["schema_version"] != 1 or witness["version"] != publication.version
                or witness["artifact_url"] != publication.artifact_url or witness["artifact_sha256"] != publication.artifact_sha256
                or witness["key_sha256"] != hashlib.sha256(UPDATER_PUBLIC_KEY.encode()).hexdigest()
                or type(witness["artifact_bytes"]) is not int or not 0 < witness["artifact_bytes"] <= MAX_ARTIFACT_BYTES
                or not isinstance(witness["artifact_blake2b"], str) or not re.fullmatch(r"[a-f0-9]{128}", witness["artifact_blake2b"])):
            raise ValueError
        key, signature = _signature(publication.signature)
        key.verify(signature, bytes.fromhex(witness["artifact_blake2b"]))
    except ReleaseUnavailable:
        raise
    except Exception:
        raise ReleaseUnavailable("release_witness_invalid") from None
