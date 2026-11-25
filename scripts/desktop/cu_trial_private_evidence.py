"""Opt-in owned-fixture QA custody; never a runtime, console or provider log."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit
import uuid

from cu_tasks.fixture_server import FixtureServer
from coding_agents.cu_verification_context import (
    MAX_CONTEXT_BYTES, MAX_REQUEST_BYTES, WIDE_CONTEXT_BYTES, WIDE_REQUEST_BYTES, is_wide,
)


class PrivateEvidenceError(RuntimeError):
    """A closed error code only; never include the failed input or OS message."""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


class PrivateTrialEvidence:
    MAX_CALLS = 64
    MAX_FILE_BYTES = 48_000
    MAX_TOTAL_BYTES = 2_000_000
    ERROR_CODES = {"qa_private_evidence_scope_invalid", "qa_private_evidence_directory_invalid",
                   "qa_private_evidence_closed", "qa_private_evidence_schema_invalid",
                   "qa_private_evidence_bound_exceeded", "qa_private_evidence_write_failed",
                   "qa_private_evidence_call_mismatch", "qa_private_evidence_call_unfinished"}

    def __init__(self, fixture, goal, *, run_id, task, repetition, source_digest,
                 harness_digest, directory=None, fixture_scope="heldout"):
        prefix = getattr(fixture, "prefix", None)
        if fixture_scope == "controlled_baseline":
            prefix = ""
        goal_urls = re.findall(r"https?://[^\s<>\"'`]+", goal, flags=re.IGNORECASE) if isinstance(goal, str) else []
        origin = fixture.base if isinstance(fixture, FixtureServer) and fixture.httpd is not None else None
        owned_goal = (isinstance(origin, str) and isinstance(goal, str) and isinstance(prefix, str)
                      and origin + prefix in goal)
        if fixture_scope == "controlled_baseline":
            try:
                owned_goal = (getattr(fixture, "prefix", None) is None and owned_goal
                              and origin + "/" in goal and bool(goal_urls)
                              and all((urlsplit(url).scheme, urlsplit(url).netloc) ==
                                      (urlsplit(origin).scheme, urlsplit(origin).netloc)
                                      for url in goal_urls))
            except ValueError:
                owned_goal = False
        if (not isinstance(fixture, FixtureServer) or fixture.httpd is None
                or fixture.thread is None or not fixture.thread.is_alive()
                or fixture.httpd.server_address[0] != "127.0.0.1"
                or not isinstance(goal, str) or len(goal.encode()) > 8000
                or not isinstance(fixture.uid, str) or not re.fullmatch(r"[A-Za-z0-9]{1,32}", fixture.uid)
                or not isinstance(fixture_scope, str) or fixture_scope not in {"heldout", "controlled_baseline"}
                or (fixture_scope == "heldout" and prefix != "/qa-" + fixture.uid)
                or not owned_goal
                or not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id)
                or not isinstance(task, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", task)
                or type(repetition) is not int or not 1 <= repetition <= 1000
                or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                       for value in (source_digest, harness_digest))):
            raise PrivateEvidenceError("qa_private_evidence_scope_invalid")
        self.trial_id = uuid.uuid4().hex
        self.origin, self.prefix, self.fixture_scope = fixture.base, prefix, fixture_scope
        self._fixture, self._mac, self._fresh_factory = fixture, None, None
        self.goal_sha256 = hashlib.sha256(goal.encode()).hexdigest()
        self._binding = {"schema_version": 1, "run_id": run_id, "trial_id": self.trial_id,
                         "task": task, "repetition": repetition, "fixture_nonce": fixture.uid,
                         "fixture_origin": self.origin, "fixture_prefix": self.prefix,
                         "fixture_scope": self.fixture_scope,
                         "goal_sha256": self.goal_sha256, "source_digest": source_digest,
                         "harness_source_digest": harness_digest}
        self.binding_sha256 = digest(self._binding)
        self.error_code = ""
        self._files, self._total, self._calls, self._engine_verdicts = [], 0, [], 0
        self._pending = None
        self._manifest_sha256 = None
        self._result_sha256 = None
        self._unlinked_verdict = None
        self._fd = None
        try:
            parent = Path(tempfile.gettempdir()).resolve(strict=True) if directory is None else Path(directory)
            # Canonical OS tmp is trusted; caller-supplied symlink components are not.
            if directory is not None and parent.absolute() != parent.resolve(strict=True):
                raise PrivateEvidenceError("qa_private_evidence_directory_invalid")
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                if not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
                    raise PrivateEvidenceError("qa_private_evidence_directory_invalid")
                directory_name = "supymem-owned-trial-" + self.trial_id + "-" + uuid.uuid4().hex
                os.mkdir(directory_name, 0o700, dir_fd=parent_fd)
                self.path = parent / directory_name
                self._fd = os.open(directory_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
            info = os.fstat(self._fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise PrivateEvidenceError("qa_private_evidence_directory_invalid")
            self._write("binding.json", {"binding": self._binding, "original_goal": goal}, "binding")
        except PrivateEvidenceError:
            self.close()
            raise
        except (OSError, ValueError, TypeError):
            self.close()
            raise PrivateEvidenceError("qa_private_evidence_directory_invalid") from None

    def _fail(self, code):
        self.error_code = self.error_code or code
        raise PrivateEvidenceError(self.error_code)

    def admit(self, goal, mac):
        from desktop_runtime.cu_ai_browser import BrowserMac, ChromeFactory
        if (not isinstance(mac, BrowserMac) or not isinstance(goal, str)
                or any(os.environ.get(k) for k in ("CU_TRIAL_DEBUG", "CU_TRIAL_PRIORITY", "CU_BROWSER_CACHE"))
                or hashlib.sha256(goal.encode()).hexdigest() != self.goal_sha256):
            self._fail("qa_private_evidence_scope_invalid")
        self._mac = mac
        factory = mac.factory
        # A blank document may belong to a completed download, but its URL
        # cannot establish custody. Bind the clean factory before it launches.
        self._fresh_factory = (factory if isinstance(ChromeFactory, type) and isinstance(factory, ChromeFactory)
                               and getattr(factory, "profile_email", None) == "" and mac.b is None
                               and getattr(factory, "session", None) is None
                               and getattr(factory, "_owned_profile", None) is None else None)

    def _owned_blank_download(self, current_url, snapshot_id, tab_state, user_text, readback_context):
        """Inspect existing local custody only; never observe, navigate or read files."""
        from desktop_runtime.cu_browser import BrowserBackend, ChromeSession
        mac, factory, fixture = self._mac, self._fresh_factory, self._fixture
        try:
            if (self.fixture_scope != "controlled_baseline" or current_url != "about:blank"
                    or mac is None or factory is None or mac.factory is not factory
                    or fixture.base != self.origin or fixture.httpd is None
                    or fixture.thread is None or not fixture.thread.is_alive()):
                return None
            backend, session, obs = mac.b, factory.session, mac.last
            if not isinstance(backend, BrowserBackend) or not isinstance(session, ChromeSession) or obs is None:
                return None
            page, process = backend.page, session.proc
            scope = backend._page_scopes.get(id(page))
            pages = backend.context.pages
            if (not backend._owned_tabs or page.is_closed() or len(pages) != 1 or pages[0] is not page
                    or mac.current_url != current_url or page.url != current_url
                    or backend._indexed_page is not page or backend._indexed_url != current_url
                    or backend._indexed_generation != backend._document_generations.get(id(page), 0)
                    or not isinstance(snapshot_id, str) or not re.fullmatch(r"[a-f0-9]{32}", snapshot_id)
                    or snapshot_id != backend.snapshot_id or snapshot_id != obs.browser_snapshot_id
                    or not isinstance(scope, str) or not re.fullmatch(r"[a-f0-9]{32}", scope)
                    or tab_state != (1, scope) or obs.browser_tab_state != tab_state
                    or obs.elements or mac.seen.get(current_url) != "" or mac.history_omitted
                    or factory.profile_email != "" or factory._owned_profile is None
                    or session.user_data_dir != factory._owned_profile or session.closed
                    or session.headless is not True or process is None or process.poll() is not None
                    or process is not session._owned_process
                    or session._owned_group != session._group or session._group != process.pid
                    or not backend.downloads
                    or any(Path(path).parent != factory.data_dir / "downloads" for path in backend.downloads)):
                return None
            receipts = backend._action_receipts
            if (not receipts or receipts[-1].get("action") != "navigate"
                    or receipts[-1].get("status") != "ok" or receipts[-1].get("page_scope") != scope):
                return None
            # Unknown older pages or any foreign URL in already-bounded input
            # cannot be authorized by this owned blank tab.
            urls = list(mac.seen) + re.findall(r"https?://[^\s<>\"'`]+", user_text + "\n" + readback_context + "\n" + "\n".join(mac.did), flags=re.IGNORECASE)
            origin = urlsplit(self.origin)
            for value in urls:
                if value == "about:blank":
                    continue
                url = urlsplit(value)
                if ((url.scheme, url.netloc) != (origin.scheme, origin.netloc)
                        or not (url.path == self.prefix or url.path.startswith(self.prefix + "/"))):
                    return None
            return {"kind": "owned_blank_after_stored_download", "page_scope": scope,
                    "observed_snapshot_id": snapshot_id, "owned_tabs": 1}
        except (AttributeError, TypeError, ValueError, OSError):
            return None

    def _write(self, filename, value, kind):
        if self.error_code or self._fd is None or self._manifest_sha256 is not None:
            self._fail("qa_private_evidence_closed")
        try:
            raw = encoded(value)
        except (ValueError, TypeError):
            self._fail("qa_private_evidence_schema_invalid")
        if len(raw) > self.MAX_FILE_BYTES or self._total + len(raw) > self.MAX_TOTAL_BYTES:
            self._fail("qa_private_evidence_bound_exceeded")
        try:
            descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=self._fd)
            with os.fdopen(descriptor, "wb") as stream:
                if stream.write(raw) != len(raw):
                    self._fail("qa_private_evidence_write_failed")
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o400)
        except OSError:
            self._fail("qa_private_evidence_write_failed")
        checksum = hashlib.sha256(raw).hexdigest()
        self._total += len(raw)
        self._files.append({"name": filename, "kind": kind, "sha256": checksum, "size_bytes": len(raw)})
        return checksum

    def request(self, user_text, readback_context, *, model, phase, current_url,
                snapshot_id=None, tab_state=None, system_prompt=""):
        wide = is_wide(user_text, readback_context)
        if (not isinstance(user_text, str) or len(user_text.encode()) > (WIDE_REQUEST_BYTES if wide else MAX_REQUEST_BYTES)
                or not isinstance(readback_context, str) or len(readback_context.encode()) > (WIDE_CONTEXT_BYTES if wide else MAX_CONTEXT_BYTES)
                or not isinstance(model, str) or len(model) > 100
                or not isinstance(phase, str) or phase not in {"whole_task_verification", "terminal_answer_verification"}
                or not isinstance(current_url, str) or not isinstance(system_prompt, str) or len(system_prompt.encode()) > 10_240):
            self._fail("qa_private_evidence_schema_invalid")
        try:
            url, origin = urlsplit(current_url), urlsplit(self.origin)
        except ValueError:
            self._fail("qa_private_evidence_schema_invalid")
        custody = None
        if (url.scheme, url.netloc) != (origin.scheme, origin.netloc) or not (
                url.path == self.prefix or url.path.startswith(self.prefix + "/")):
            custody = self._owned_blank_download(current_url, snapshot_id, tab_state, user_text, readback_context)
            if custody is None:
                self._fail("qa_private_evidence_scope_invalid")
        if self._pending is not None or len(self._calls) >= self.MAX_CALLS:
            self._fail("qa_private_evidence_bound_exceeded")
        ordinal = len(self._calls) + 1
        call_id = self.trial_id + ":" + str(ordinal)
        value = {"binding_sha256": self.binding_sha256, "call_id": call_id, "ordinal": ordinal,
                 "model": model, "phase": phase, "user_text": user_text,
                 "system_prompt": system_prompt,
                 "readback_context": readback_context, "current_url": current_url,
                 "observed_snapshot_id": snapshot_id, "observed_tab_state": tab_state}
        if custody is not None:
            value["current_page_custody"] = custody
        checksum = self._write(f"verify-{ordinal:04d}-request.json", value, "verifier_request")
        metadata = {"call_id": call_id, "request_sha256": checksum, "phase": phase,
                    "user_bytes": len(user_text.encode()), "readback_bytes": len(readback_context.encode()),
                    "wide_context": wide,
                    "truncation_marked": "omitted content is not verified" in user_text,
                    "current_url_sha256": hashlib.sha256(current_url.encode()).hexdigest()}
        self._calls.append(metadata)
        self._pending = call_id
        return call_id

    def verdict(self, call_id, verdict=None, *, error_code=None):
        if call_id != self._pending:
            self._fail("qa_private_evidence_call_mismatch")
        if verdict is not None:
            if (not isinstance(verdict, tuple) or len(verdict) != 2 or type(verdict[0]) is not bool
                    or not isinstance(verdict[1], str) or len(verdict[1].encode()) > 4096 or error_code is not None):
                self._fail("qa_private_evidence_schema_invalid")
            result = {"passed": verdict[0], "reason": verdict[1]}
        elif not isinstance(error_code, str) or (error_code not in {"verifier_transport_timeout", "verifier_transport_error",
                                "verifier_response_invalid", "verifier_error", "verifier_interrupted"} and not (
                re.fullmatch(r"provider_[1-5][0-9]{2}", error_code))):
            self._fail("qa_private_evidence_schema_invalid")
        else:
            result = {"error_code": error_code}
        checksum = self._write(f"verify-{len(self._calls):04d}-verdict.json",
                               {"binding_sha256": self.binding_sha256, "call_id": call_id,
                                "request_sha256": self._calls[-1]["request_sha256"], "result": result}, "verifier_verdict")
        self._calls[-1].update(verdict_sha256=checksum, passed=verdict[0] if verdict is not None else None,
                              error_code=error_code)
        self._unlinked_verdict = (call_id, verdict)
        self._pending = None

    def engine_verdict(self, value):
        if (not isinstance(value, dict) or type(value.get("step")) is not int
                or type(value.get("passed")) is not bool or not isinstance(value.get("reason"), str)
                or len(value["reason"].encode()) > 4096 or self._engine_verdicts >= self.MAX_CALLS):
            self._fail("qa_private_evidence_schema_invalid")
        self._engine_verdicts += 1
        linked = self._unlinked_verdict
        # A runtime admission refusal may produce a judgment without any model
        # verifier call. Never assign that judgment to a prior invocation.
        call_id = linked[0] if linked and linked[1] == (value["passed"], value["reason"]) else None
        self._unlinked_verdict = None
        self._write(f"engine-verdict-{self._engine_verdicts:04d}.json",
                    {"binding_sha256": self.binding_sha256,
                     "verifier_call_id": call_id,
                     "verdict": {k: value.get(k) for k in ("step", "passed", "reason", "code")}}, "engine_verdict")

    def finalize(self, result, oracle_metadata):
        if self._pending is not None:
            self._fail("qa_private_evidence_call_unfinished")
        checksum = self._write("result.json", {"binding_sha256": self.binding_sha256,
                                "result": result, "oracle": oracle_metadata}, "final_result")
        manifest = {"schema_version": 1, "binding": self._binding,
                    "binding_sha256": self.binding_sha256, "files": list(self._files),
                    "calls": self._calls, "result_sha256": checksum}
        # Link publication is atomic and exclusive: readers cannot observe a
        # partial final manifest, and a pre-existing file is never replaced.
        manifest_checksum = self._write(".manifest.pending", manifest, "manifest")
        try:
            os.link(".manifest.pending", "manifest.json", src_dir_fd=self._fd,
                    dst_dir_fd=self._fd, follow_symlinks=False)
            os.unlink(".manifest.pending", dir_fd=self._fd)
            os.fsync(self._fd)
            os.fchmod(self._fd, 0o500)
        except OSError:
            self._fail("qa_private_evidence_write_failed")
        self._result_sha256 = checksum
        self._manifest_sha256 = manifest_checksum
        self.close()

    def public_metadata(self):
        return {"diagnostic_requested": True, "diagnostic_trial_id": self.trial_id,
                "diagnostic_scope": self.fixture_scope,
                "diagnostic_binding_sha256": self.binding_sha256,
                "diagnostic_manifest_sha256": self._manifest_sha256,
                "diagnostic_result_sha256": self._result_sha256,
                "diagnostic_binding": {k: self._binding[k] for k in
                                       ("run_id", "task", "repetition", "source_digest", "harness_source_digest")},
                "diagnostic_custody_verified": bool(self._manifest_sha256 and not self.error_code),
                "diagnostic_error_code": self.error_code or None,
                "diagnostic_calls": [dict(row) for row in self._calls]}

    @classmethod
    def metadata_valid(cls, row):
        """Validate exported identity/bounds; public hashes do not prove content semantics."""
        binding = {k: row.get(k) for k in ("run_id", "task", "repetition", "source_digest", "harness_source_digest")}
        trial_id, calls = row.get("diagnostic_trial_id"), row.get("diagnostic_calls")
        if (row.get("diagnostic_requested") is not True or row.get("diagnostic_custody_verified") is not True
                or row.get("diagnostic_error_code") is not None or row.get("diagnostic_binding") != binding
                or any(not isinstance(row.get(k), str) or not re.fullmatch(r"[a-f0-9]{64}", row[k]) for k in
                       ("diagnostic_binding_sha256", "diagnostic_manifest_sha256", "diagnostic_result_sha256"))
                or not isinstance(trial_id, str) or not re.fullmatch(r"[a-f0-9]{32}", trial_id)
                or not isinstance(calls, list) or len(calls) > cls.MAX_CALLS):
            return False
        for ordinal, call in enumerate(calls, 1):
            wide = call.get("wide_context", False) if isinstance(call, dict) else False
            if (not isinstance(call, dict) or call.get("call_id") != trial_id + ":" + str(ordinal)
                    or type(wide) is not bool
                    or not isinstance(call.get("phase"), str)
                    or call.get("phase") not in {"whole_task_verification", "terminal_answer_verification"}
                    or any(not isinstance(call.get(k), str) or not re.fullmatch(r"[a-f0-9]{64}", call[k])
                           for k in ("request_sha256", "verdict_sha256", "current_url_sha256"))
                    or any(type(call.get(k)) is not int or not 0 <= call[k] <= bound
                           for k, bound in (("user_bytes", WIDE_REQUEST_BYTES if wide else MAX_REQUEST_BYTES),
                                            ("readback_bytes", WIDE_CONTEXT_BYTES if wide else MAX_CONTEXT_BYTES)))
                    or type(call.get("truncation_marked")) is not bool
                    or (type(call.get("passed")) is not bool and call.get("passed") is not None)):
                return False
            error = call.get("error_code")
            allowed = isinstance(error, str) and (error in {"verifier_transport_timeout", "verifier_transport_error", "verifier_response_invalid",
                                "verifier_error", "verifier_interrupted"} or re.fullmatch(r"provider_[1-5][0-9]{2}", error))
            if (call.get("passed") is None and not allowed) or (type(call.get("passed")) is bool and error is not None):
                return False
        return True

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
