"""Caller-owned PRIVATE bounded verifier diagnostics; never public/provider logs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import uuid


class PrivateVerifierArtifact:
    MAX_ROWS = 64
    MAX_REASON = 200

    def __init__(self, directory: Path | None = None):
        self.directory = directory
        self.path: Path | None = None
        self.count = 0
        self._stream = None
        self._closed = False
        self._lock = threading.Lock()

    def record(self, value: dict) -> None:
        # No proposal/provider response/context object may be serialized here.
        if not isinstance(value, dict) or type(value.get('passed')) is not bool:
            return
        step = value.get('step')
        if type(step) is not int or not 0 <= step <= 1_000_000:
            return
        reason = value.get('reason')
        if not isinstance(reason, str):
            return
        code = value.get('code')
        if code not in {None, 'independent_verifier_required', 'required_durable_output_missing_or_unreadable', 'independent_verifier_rejected'}:
            code = 'independent_verifier_rejected'
        row = {'step': step, 'passed': value['passed'], 'reason': reason[:self.MAX_REASON], 'code': code}
        with self._lock:
            if self._closed or self.count >= self.MAX_ROWS:
                return
            if self._stream is None:
                # mkdtemp owns a fresh mode0700 directory; never reuse a task/name
                # or follow an attacker-chosen filename. Keep it for private QA.
                parent = Path(tempfile.mkdtemp(prefix='supymem-verifier-', dir=self.directory))
                path = parent / (uuid.uuid4().hex + '.jsonl')
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                self._stream = os.fdopen(descriptor, 'w', encoding='utf-8')
                self.path = path
            self._stream.write(json.dumps(row, ensure_ascii=True) + '\n')
            self._stream.flush()
            self.count += 1

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._stream is not None:
                self._stream.close()
                self._stream = None

    def metadata(self) -> dict:
        # The only fields allowed in returned/public trial journals/stdout.
        return {'verifier_diagnostic_file': str(self.path) if self.path is not None else None,
                'verifier_diagnostic_count': self.count}
