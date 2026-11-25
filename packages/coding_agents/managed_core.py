"""Trusted in-process adapter to Core's sdk_authority helper contracts.

This module is not imported by the sandbox driver. The Core service supplies its
already imported authority module to avoid the services' shared `app` namespace.
There are deliberately no controller tokens, URL defaults or network routes.
"""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CoreAuthorityAdapter:
    authority: Any
    run_id: str
    generation: int
    controller_id: str
    attempt: int

    @property
    def fence(self):
        return dict(generation=self.generation, controller_id=self.controller_id, attempt=self.attempt)

    def register(self, **kwargs):
        return self.authority.register(self.run_id, **kwargs, **self.fence)

    def read_receipt(self, invocation_id):
        return self.authority.read_receipt(self.run_id, invocation_id, **self.fence)

    def mirror(self, event):
        return self.authority.mirror(self.run_id, event, **self.fence)["seq"]

    def consume(self, invocation_id, *, mirrored_event_seq):
        return self.authority.consume(self.run_id, invocation_id,
                                      mirrored_event_seq=mirrored_event_seq, **self.fence)

    def advance_cursor(self, *, event_id):
        try:
            self.authority.advance_cursor(self.run_id, event_id, **self.fence)
        except Exception as exc:
            # Core uses this one explicit state for a pending response. A retry
            # may replay that slot, but can never register a new one yet.
            if isinstance(exc, self.authority.BuildModelError) and str(exc) == "build_prior_invocation_not_consumed":
                return False
            raise
        return True
