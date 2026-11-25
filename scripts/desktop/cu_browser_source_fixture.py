"""Bounded owned reading fixture and independent source/output checks.

Synthetic source identity is fresh per trial and never supplied in the task.
These oracles prove scoped source/output facts, not every intermediate UI step.
"""
from __future__ import annotations

import html
import uuid

from cu_artifact_checks import exact_move, exact_text
from cu_browser_form_fixture import BrowserFormFixture


class BrowserSourceFixture(BrowserFormFixture):
    def __init__(self):
        super().__init__()
        self.headline = 'Synthetic research ' + uuid.uuid4().hex
        self.features = tuple(label + ' ' + uuid.uuid4().hex for label in (
            'Track synthetic check-ins', 'Review synthetic retention', 'Schedule synthetic follow-ups'))
        self.source_reads = 0

    @property
    def source_url(self):
        return self.origin + self.form_path

    @property
    def research_text(self):
        return self.headline + '\n' + '\n'.join('- ' + feature for feature in self.features)

    def form_html(self):
        with self.lock:
            self.source_reads += 1
        return ('<!doctype html><html><head><title>Owned synthetic research</title></head><body>'
                '<h1>' + html.escape(self.headline) + '</h1><ul>'
                + ''.join('<li>' + html.escape(feature) + '</li>' for feature in self.features)
                + '</ul></body></html>')

    def source_delivered(self):
        with self.lock:
            return self.source_reads > 0 and not self.stop.is_set() and not self.closed

    def verify_browser(self, browser):
        if not self.source_delivered():
            return False, 'qa_source_not_delivered'
        backend = getattr(browser, 'b', None)
        if backend is None or not callable(getattr(backend, 'observe', None)):
            return None, 'qa_source_owned_browser_readback_unavailable'
        try:
            observed = backend.observe()  # no lazy browser creation or model summary
            text = observed.text
            lines = {line.strip() for line in text.splitlines()} if isinstance(text, str) and len(text) <= 200_000 else set()
            ok = observed.url == self.source_url and {self.headline, *self.features}.issubset(lines)
        except Exception:
            return None, 'qa_source_owned_browser_readback_unavailable'
        self.readback_ok = ok is True and self.source_delivered()
        return self.readback_ok, 'qa_source_independent_page'

    def verify_file(self, destination, *, source=None):
        expected = self.headline if source is None else self.research_text
        ok = self.source_delivered() and (exact_text(destination, expected) if source is None
                                         else exact_move(source, destination, expected))
        self.readback_ok = ok is True
        return self.readback_ok, 'qa_source_exact_output'

    def final_verdict(self):
        with self.lock:
            return (self.readback_ok and self.source_reads > 0) if self.closed and self.cleanup_verified is True else None

    def public_receipt(self):
        with self.lock:
            return {'fixture_id': self.run_id, 'synthetic': True, 'counts_as_real_user': False,
                    'source_reads': self.source_reads, 'owned_source_output_verified': self.readback_ok,
                    'cleanup_verified': self.cleanup_verified}
