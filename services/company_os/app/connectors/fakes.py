"""Fake adapter machinery for local development and tests (39 §7).

Every provider ships a fake selected when ``CONNECTORS_MODE=local`` (the
default). A fake implements the identical protocol, is driven through the same
chokepoint and therefore records identical ``connection_events``, and can
simulate pagination, rate limits, expiry and webhook delivery. No test may need
a live account, or CI becomes a coin flip.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.connectors.protocol import (
    Action,
    ActionResult,
    AuthExpired,
    Capability,
    ConnectionView,
    ConnectorError,
    Page,
    RateLimited,
    ReadResult,
    ResourceRef,
    Resource,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[4] / "data" / "fixtures" / "connectors"
PAGE_SIZE = 2


@dataclass
class FakeState:
    """Per-provider simulation switches and a log tests can assert against."""

    provider: str
    fetch_log: list[str] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)
    calls: int = 0
    rate_limit_once: bool = False
    rate_limit_retry_after_s: float = 0.01
    expired: bool = False
    page_size: int = PAGE_SIZE

    def reset(self) -> None:
        self.fetch_log.clear()
        self.created.clear()
        self.revoked.clear()
        self.calls = 0
        self.rate_limit_once = False
        self.expired = False
        self.page_size = PAGE_SIZE


_STATES: dict[str, FakeState] = {}


def fake_state(provider: str) -> FakeState:
    st = _STATES.get(provider)
    if st is None:
        st = FakeState(provider=provider)
        _STATES[provider] = st
    return st


def reset_fakes() -> None:
    for st in _STATES.values():
        st.reset()


def load_fixture(provider: str, name: str) -> Any:
    path = FIXTURE_ROOT / provider / f"{name}.json"
    if not path.exists():
        raise ConnectorError(f"missing fixture {provider}/{name}")
    with path.open() as f:
        return json.load(f)


class FakeMixin:
    """Shared simulation behaviour. Mixed in front of the provider's base."""

    provider: str

    @property
    def state(self) -> FakeState:
        return fake_state(self.provider)

    def _precheck(self) -> None:
        st = self.state
        st.calls += 1
        if st.expired:
            raise AuthExpired(self.provider)
        if st.rate_limit_once:
            st.rate_limit_once = False
            raise RateLimited(st.rate_limit_retry_after_s, provider=self.provider)

    def _resources(self, kind: str) -> list[dict[str, Any]]:
        data = load_fixture(self.provider, "resources")
        items = data.get(kind)
        if items is None:
            raise ConnectorError(f"{self.provider} fixture has no '{kind}'")
        return list(items)

    def _page(self, kind: str, cursor: str | None) -> Page:
        rows = self._resources(kind)
        size = max(1, self.state.page_size)
        index = _cursor_index(cursor)
        self.state.fetch_log.append(f"{kind}:{cursor or 'p1'}")
        window = rows[index * size : (index + 1) * size]
        exhausted = (index + 1) * size >= len(rows)
        items = tuple(
            Resource(
                kind=kind,
                ref=str(row.get("ref", "")),
                display=str(row.get("display", row.get("ref", ""))),
                detail={k: v for k, v in row.items() if k not in {"ref", "display"}},
            )
            for row in window
        )
        return Page(
            items=items,
            cursor=None if exhausted else f"p{index + 2}",
            exhausted=exhausted,
            bytes_in=len(json.dumps(window)),
        )

    async def _perform_list(
        self, con: ConnectionView, kind: str, cursor: str | None
    ) -> Page:
        self._precheck()
        return self._page(kind, cursor)

    async def _perform_read(
        self, con: ConnectionView, ref: ResourceRef
    ) -> ReadResult:
        self._precheck()
        blobs = load_fixture(self.provider, "resources").get("content") or {}
        content = blobs.get(ref.ref)
        if content is None:
            raise ConnectorError(f"nothing at {ref.ref}")
        return ReadResult(
            ref=ref.ref,
            kind=ref.kind,
            content=str(content),
            bytes_in=len(str(content)),
        )

    async def _perform_write(
        self, con: ConnectionView, action: Action, cap: Capability
    ) -> ActionResult:
        self._precheck()
        st = self.state
        seq = len([c for c in st.created if c["operation"] == cap.operation]) + 1
        external_id = f"fake_{cap.operation}_{seq}"
        st.created.append(
            {
                "operation": cap.operation,
                "target": action.target,
                "external_id": external_id,
                "connection_id": con.id,
                "engagement_id": action.engagement_id,
            }
        )
        return ActionResult(
            external_id=external_id,
            display=f"{action.target}#{seq}",
            url=None,
            bytes_out=len(json.dumps(action.payload, sort_keys=True)),
            simulated=True,
        )


def _cursor_index(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(str(cursor).lstrip("p")) - 1)
    except ValueError:
        return 0
