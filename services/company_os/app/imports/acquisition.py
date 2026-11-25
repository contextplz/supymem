"""Bounded anonymous GitHub archive reads; never clone or borrow host credentials.

The caller persists resolve() before archive() and supervises the whole process
with a wall deadline, including libc DNS resolution. No internal retry loop.
"""
from __future__ import annotations

import ipaddress
import json
import time
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from core.research_web.guard import get_checked

from app.connectors.github.adapter import API_VERSION
from .contracts import GitHubSource, ImportFailure, MAX_ARCHIVE_BYTES, ResolvedGitHub, repository

MAX_METADATA = 1024 * 1024
MAX_REQUESTS = 5


class PublicGitHub:
    def __init__(self, *, client=None, deadline=None):
        self.deadline = deadline if deadline is not None else time.monotonic() + 60
        self.calls = 0
        self.bytes_received = 0
        self.client = client or httpx.Client(follow_redirects=False, trust_env=False, timeout=5,
            headers={"User-Agent": "SupymemImport/1.0", "Accept-Encoding": "identity",
                     "X-GitHub-Api-Version": API_VERSION})
        self._owns_client = client is None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self._owns_client:
            self.client.close()

    def _read(self, logical, *, kind, allowed_paths):
        boundary = self
        limit = MAX_ARCHIVE_BYTES if kind == "zip" else MAX_METADATA

        class Stream:
            def get(self, url, *, headers=None, extensions=None):
                headers, extensions = headers or {}, extensions or {}
                try:
                    parsed = urlsplit(url)
                    address = ipaddress.ip_address(parsed.hostname or "")
                    host = headers.get("Host")
                    if (not address.is_global or parsed.scheme != "https" or parsed.port is not None
                            or parsed.username or parsed.password or parsed.query or parsed.fragment
                            or host not in allowed_paths or extensions.get("sni_hostname") != host
                            or unquote(parsed.path) not in allowed_paths[host]):
                        raise ValueError()
                except (ValueError, KeyError, TypeError):
                    raise ImportFailure("import_remote_target_denied") from None
                boundary.calls += 1
                remaining = boundary.deadline - time.monotonic()
                if boundary.calls > MAX_REQUESTS or remaining <= 0:
                    raise ImportFailure("import_acquisition_limit")
                # Headers contain only public API metadata and vetted Host. No
                # Authorization, cookies, ambient proxy or redirected identity.
                with boundary.client.stream("GET", url, headers={**headers,
                        "Accept": "application/zip" if kind == "zip" else "application/vnd.github+json"},
                        extensions=extensions, timeout=min(5, remaining)) as response:
                    if time.monotonic() > boundary.deadline:
                        raise ImportFailure("import_acquisition_limit")
                    status = response.status_code
                    if status in {301, 302, 303, 307, 308}:
                        if kind != "zip":
                            raise ImportFailure("import_repository_identity_changed", status=409)
                        target = urlsplit(urljoin(f"https://{host}{parsed.path}", response.headers.get("location", "")))
                        if (target.scheme != "https" or target.netloc not in allowed_paths
                                or target.query or target.fragment or unquote(target.path) not in allowed_paths[target.netloc]):
                            raise ImportFailure("import_remote_target_denied")
                        return httpx.Response(status, headers=response.headers, request=response.request)
                    if status == 429 or status == 403 and (response.headers.get("x-ratelimit-remaining") == "0"
                                                         or response.headers.get("retry-after")):
                        # External rate hints cannot schedule an unbounded delay.
                        raw = response.headers.get("retry-after", "60")
                        delay = min(900, max(60, int(raw))) if raw.isdecimal() and len(raw) <= 6 else 60
                        raise ImportFailure("import_github_rate_limited", status=429, retry_after=delay)
                    if status in {401, 403, 404}:
                        raise ImportFailure("import_repository_unavailable", status=404)
                    if status != 200:
                        raise ImportFailure("import_github_unavailable", status=503)
                    media = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    allowed_media = {"application/zip", "application/x-zip-compressed", "application/octet-stream"} if kind == "zip" else {"application/json"}
                    length = response.headers.get("content-length")
                    if (response.headers.get("content-encoding", "identity").lower() != "identity"
                            or media not in allowed_media or length is not None and (not length.isdecimal() or int(length) > limit)):
                        raise ImportFailure("import_remote_response_invalid")
                    data = bytearray()
                    for chunk in response.iter_raw():
                        if len(data) + len(chunk) > limit or time.monotonic() > boundary.deadline:
                            raise ImportFailure("import_remote_response_limit")
                        data.extend(chunk)
                    if (time.monotonic() > boundary.deadline or not data
                            or length is not None and len(data) != int(length)):
                        raise ImportFailure("import_remote_response_invalid")
                    boundary.bytes_received += len(data)
                    return httpx.Response(200, content=bytes(data), headers=response.headers, request=response.request)

        try:
            return get_checked(Stream(), logical, max_redirects=2 if kind == "zip" else 0).content
        except ImportFailure:
            raise
        except PermissionError:
            raise ImportFailure("import_remote_target_denied") from None
        except (httpx.HTTPError, OSError, RuntimeError, ValueError):
            raise ImportFailure("import_acquisition_failed", status=503) from None

    def _json(self, path):
        raw = self._read("https://api.github.com" + path, kind="json",
                         allowed_paths={"api.github.com": {unquote(path)}})
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, RecursionError):
            raise ImportFailure("import_github_metadata_invalid") from None

    def resolve(self, source: GitHubSource) -> ResolvedGitHub:
        owner, repo = repository(source.repo_url)
        identity = f"{owner}/{repo}"
        metadata = self._json(f"/repos/{identity}")
        if (metadata.get("private") is not False or metadata.get("visibility", "public") != "public"
                or metadata.get("full_name") != identity or metadata.get("html_url") != source.repo_url):
            raise ImportFailure("import_repository_identity_changed", status=409)
        ref = source.ref or metadata.get("default_branch")
        try:
            if not isinstance(ref, str):
                raise ValueError()
            GitHubSource.valid_ref(ref)
            commit = self._json(f"/repos/{identity}/commits/{quote(ref, safe='')}")
            return ResolvedGitHub(repository=identity, requested_ref=ref, commit_sha=commit.get("sha"), public=True)
        except ImportFailure:
            raise
        except (ValueError, TypeError):
            raise ImportFailure("import_github_commit_invalid") from None

    def archive(self, resolved: ResolvedGitHub) -> bytes:
        # Re-validate persisted data even when the caller supplies a model.
        resolved = ResolvedGitHub.model_validate(resolved.model_dump())
        repo, sha = resolved.repository, resolved.commit_sha
        api_path = f"/repos/{repo}/zipball/{sha}"
        return self._read("https://api.github.com" + api_path, kind="zip", allowed_paths={
            "api.github.com": {api_path},
            "codeload.github.com": {f"/{repo}/legacy.zip/{sha}", f"/{repo}/zip/{sha}"}})
