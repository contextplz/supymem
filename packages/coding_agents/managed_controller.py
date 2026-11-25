"""Trusted mirror/intent bridge. Deploy OUTSIDE the code execution sandbox.

The reader supplies durably mirrored SDK JSON, independently read by the
controller after write/readback. JSON ancestry only deduplicates model receipts;
it never proves that a generated program, test, command or deployment succeeded.
Artifact QA remains a separate trusted operation.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
import json

from .managed_protocol import CoreAuthority, ManagedInvocationError, SyncResult, digest


CHAIN_TYPES = frozenset({"SystemPromptEvent", "MessageEvent", "ActionEvent",
                         "ObservationEvent", "AgentErrorEvent", "ConversationErrorEvent"})
MAX_EVENTS = 500


def metadata_chain(raw_events: Sequence[str]) -> list[dict]:
    """Validate bounded linear metadata without importing executable SDK tools."""
    if len(raw_events) > MAX_EVENTS:
        raise ManagedInvocationError("managed_event_limit")
    chain, seen, leaf = [], set(), None
    for raw in raw_events:
        if not isinstance(raw, str) or len(raw.encode()) > 1_048_576:
            raise ManagedInvocationError("managed_event_size_limit")
        try:
            event = json.loads(raw)
        except ValueError as exc:
            raise ManagedInvocationError("managed_event_json_invalid") from exc
        if not isinstance(event, dict):
            raise ManagedInvocationError("managed_event_json_invalid")
        kind, event_id = event.get("kind"), event.get("id")
        if not isinstance(event_id, str) or not 8 <= len(event_id) <= 128 or event_id in seen:
            raise ManagedInvocationError("managed_event_identity_invalid")
        seen.add(event_id)
        if kind == "ConversationStateUpdateEvent":
            continue  # SDK deliberately excludes these from its active leaf.
        if kind not in CHAIN_TYPES:
            raise ManagedInvocationError("managed_event_profile_unsupported")
        if event.get("parent_id") != leaf:
            raise ManagedInvocationError("managed_event_ancestry_invalid")
        try:
            event_digest = digest(event)
        except ValueError as exc:
            raise ManagedInvocationError("managed_event_json_invalid") from exc
        metadata = {"event_id": event_id, "parent_id": leaf, "event_type": kind,
                    "source": event.get("source"), "event_digest": event_digest}
        if kind == "MessageEvent":
            message = event.get("llm_message", {})
            if not isinstance(message, dict):
                raise ManagedInvocationError("managed_message_role_invalid")
            metadata["message_role"] = message.get("role")
            if metadata["source"] not in {"user", "agent", "environment"}:
                raise ManagedInvocationError("managed_message_source_invalid")
            if metadata["message_role"] != ("assistant" if metadata["source"] == "agent" else "user"):
                raise ManagedInvocationError("managed_message_role_invalid")
        if kind == "ActionEvent" or (kind == "MessageEvent" and metadata["source"] == "agent"):
            response_id = event.get("llm_response_id")
            if not isinstance(response_id, str) or not response_id.startswith("bmc_"):
                raise ManagedInvocationError("managed_response_identity_missing")
            metadata.update(llm_response_id=response_id)
        if kind in {"ActionEvent", "ObservationEvent", "AgentErrorEvent"}:
            call_id = event.get("tool_call_id")
            if not isinstance(call_id, str) or not 1 <= len(call_id) <= 128:
                raise ManagedInvocationError("managed_tool_identity_missing")
            metadata["tool_call_id"] = call_id
        if kind == "ActionEvent" and (not isinstance(event.get("tool_call"), dict) or event["tool_call"].get("id") != metadata["tool_call_id"]):
            raise ManagedInvocationError("managed_tool_identity_mismatch")
        if kind == "ObservationEvent":
            metadata["action_id"] = event.get("action_id")
        chain.append(metadata)
        leaf = event_id
    return chain


class ManagedController:
    """One-build capability behind a future private intent service adapter.

    No public endpoint is invented here. The Core adapter owns its fencing and
    credentials; the sandbox can call only prepare/synchronize through its
    scoped link. Do not place this controller object in a sandbox process.
    """

    def __init__(self, authority: CoreAuthority, durable_reader: Callable[[], Sequence[str]]):
        self.authority = authority
        self.durable_reader = durable_reader

    def synchronize(self) -> SyncResult:
        chain = metadata_chain(self.durable_reader())
        consumed: set[str] = set()
        response_events: set[str] = set()
        actions: dict[str, dict] = {}
        for metadata in chain:
            invocation_id = metadata.get("llm_response_id")
            if invocation_id:
                if invocation_id in response_events:
                    raise ManagedInvocationError("managed_duplicate_response_event")
                response_events.add(invocation_id)
                receipt = self.authority.read_receipt(invocation_id)
                if (receipt.get("state") != "settled" or receipt.get("error_code")
                        or receipt.get("invocation_id") != invocation_id
                        or (receipt.get("expected") or {}).get("event_type") != metadata["event_type"]
                        or (receipt.get("expected") or {}).get("tool_call_id") != metadata.get("tool_call_id")
                        or not receipt.get("response_digest")):
                    raise ManagedInvocationError("managed_receipt_event_mismatch")
                # The digest comes from Core's encrypted receipt, never from
                # fields provided by the sandbox's SDK event or local journal.
                # Core.mirror independently retrieves and adds this digest.
                sequence = self.authority.mirror(metadata)
                self.authority.consume(invocation_id, mirrored_event_seq=sequence)
                consumed.add(invocation_id)
                if metadata["event_type"] == "ActionEvent":
                    actions[metadata["event_id"]] = metadata
            else:
                if metadata["event_type"] == "ObservationEvent":
                    action = actions.get(metadata.get("action_id"))
                    if not action or action["tool_call_id"] != metadata["tool_call_id"]:
                        raise ManagedInvocationError("managed_observation_unmatched")
                self.authority.mirror(metadata)
        leaf = chain[-1]["event_id"] if chain else None
        self.authority.advance_cursor(event_id=leaf)
        return SyncResult(leaf, frozenset(consumed))

    def prepare(self, *, invocation_key: str, input_event_id: str | None, request: dict) -> dict:
        self.synchronize()
        # Core alone decides whether this is a replay of a pending old leaf or
        # a new slot at the latest verified descendant.
        return self.authority.register(invocation_key=invocation_key,
                                       input_event_id=input_event_id, request=request)
