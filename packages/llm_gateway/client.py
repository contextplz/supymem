"""Chat facade; live transport and credit admission are owned by sole.transmit."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from llm_gateway.budget import apply_purpose_budget
from llm_gateway.cassette import Cassette, fixture_for
from llm_gateway.ledger import TokenLedger
from llm_gateway.messages import (
    assistant_message_preserving_reasoning,
    ensure_reasoning_roundtrip,
)
from llm_gateway.provider_foundry import (
    ProviderConfig,
    load_provider_config_from_env,
    redacted_status,
    sample_mode_enabled,
)
from llm_gateway.reliability import Reliability
from llm_gateway.routing import (
    UnroutedPurposeError,
    resolve_purpose_config,
    resolve_tier,
)


@dataclass
class ChatResult:
    content: str | None
    raw: Any = None
    tier: str = "flash"
    purpose: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: list[Any] = field(default_factory=list)
    assistant_message: dict[str, Any] = field(default_factory=dict)


class LLMClient:
    def __init__(
        self,
        config: ProviderConfig | None = None,
        *,
        cassette: Cassette | None = None,
        ledger: TokenLedger | None = None,
        reliability: Reliability | None = None,
        offline: bool | None = None,
        daily_cap_usd: float | None = None,
    ) -> None:
        self.config = config or load_provider_config_from_env()
        self.cassette = cassette
        self.ledger = ledger or TokenLedger()
        # Compatibility only: the sole gateway owns live concurrency and retries.
        self.reliability = reliability
        key = self.config.api_key.get_secret_value()
        self.offline = (
            offline
            if offline is not None
            else (not key or os.getenv("LLM_OFFLINE", "").lower() in {"1", "true"})
        )
        from llm_gateway.provider_foundry import local_mode_enabled
        self._offline_override = offline
        if local_mode_enabled() and offline is None:
            from app.core.local_mode import is_offline
            self.offline = is_offline()

    def status(self) -> dict[str, Any]:
        return redacted_status(self.config)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        purpose: str,
        tier: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict | None = None,
        ledger_ref: str | None = None,
        venture_id: str | None = None,
        agent_id: str | None = None,
        run_id: str | None = None,
    ) -> ChatResult:
        from llm_gateway.provider_foundry import local_mode_enabled
        if local_mode_enabled() and self._offline_override is None:
            from app.core.local_mode import is_offline
            self.offline = is_offline()
            if not self.offline:
                self.config = load_provider_config_from_env()

        declared = resolve_tier(purpose)
        resolved = tier or self.config.captured_tier or declared

        msgs = ensure_reasoning_roundtrip(messages)
        msgs, _clog = apply_purpose_budget(purpose, msgs)

        if sample_mode_enabled():
            fix = {**(fixture_for(purpose) or {"content": "{}"}), "sample": True,
                "tokens_in": 0, "tokens_out": 0}
            return self._from_fixture(fix, purpose, resolved, venture_id, agent_id, run_id)

        cached = None
        if self.cassette and self.cassette.mode == "replay":
            cached = self.cassette.get(self.cassette.key(msgs, purpose, resolved))
        config = None
        workspace_id = ledger_ref if (ledger_ref or "").startswith("wsp_") else None
        if not cached and not self.offline:
            from llm_gateway.sole import resolve_override

            config = resolve_purpose_config(purpose, resolve_override(workspace_id, config=self.config)
                or self.config, tier=resolved)

        # Cassette / offline deterministic path
        if cached:
            return self._from_fixture(cached, purpose, resolved, venture_id, agent_id, run_id)

        if self.offline:
            fix = fixture_for(purpose) or {
                "content": "{}",
                "tokens_in": 10,
                "tokens_out": 10,
            }
            return self._from_fixture(fix, purpose, resolved, venture_id, agent_id, run_id)

        # One admitted route and one transport call; no independent paid retry.
        from llm_gateway.errors import ProviderCallError
        from llm_gateway.sole import transmit

        try:
            resp = await transmit(msgs, purpose=purpose, tier=config.captured_tier,
                config=config, tools=tools, response_schema=response_schema,
                max_tokens=config.max_output_tokens, workspace_id=workspace_id,
                single_attempt=True, use_workspace_override=False)
        except ProviderCallError:
            raise
        except Exception as exc:
            from llm_gateway.errors import classify_provider_error

            raise classify_provider_error(exc) from exc
        usage = getattr(resp, "usage", None)
        from llm_gateway.usage import extract_confirmed_token_usage

        counts = extract_confirmed_token_usage(usage)
        if counts is None:
            raise ProviderCallError("usage_unconfirmed", "The provider did not confirm this call's usage.", retryable=False)
        tin = counts["prompt_tokens"]
        tout = counts["completion_tokens"]
        rtok = counts["reasoning_tokens"]
        assistant = assistant_message_preserving_reasoning(resp)
        content = assistant.get("content")
        entry = self.ledger.record(
            purpose=purpose,
            tier=resolved,
            tokens_in=tin,
            tokens_out=tout,
            reasoning_tokens=rtok,
            catalog_model=config.catalog_model,
            cached_tokens=counts["cached_tokens"],
            venture_id=venture_id,
            agent_id=agent_id,
            run_id=run_id or ledger_ref,
        )
        if self.cassette and self.cassette.mode == "record":
            self.cassette.put(
                self.cassette.key(msgs, purpose, resolved),
                {
                    "content": content,
                    "tokens_in": tin,
                    "tokens_out": tout,
                    "reasoning_tokens": rtok,
                    "catalog_model": config.catalog_model,
                    "cached_tokens": counts["cached_tokens"],
                    "assistant_message": assistant,
                },
            )
        return ChatResult(
            content=content,
            raw=resp,
            tier=resolved,
            purpose=purpose,
            tokens_in=tin,
            tokens_out=tout,
            reasoning_tokens=rtok,
            cost_usd=entry.cost_usd,
            tool_calls=assistant.get("tool_calls") or [],
            assistant_message=assistant,
        )

    def _from_fixture(
        self,
        fix: dict[str, Any],
        purpose: str,
        tier: str,
        venture_id: str | None,
        agent_id: str | None,
        run_id: str | None,
    ) -> ChatResult:
        tin = int(fix.get("tokens_in", 0))
        tout = int(fix.get("tokens_out", 0))
        rtok = int(fix.get("reasoning_tokens", 0))
        entry = self.ledger.record(
            purpose=purpose,
            tier=tier,
            tokens_in=tin,
            tokens_out=tout,
            reasoning_tokens=rtok,
            catalog_model=fix.get("catalog_model"),
            cached_tokens=int(fix.get("cached_tokens", 0)),
            venture_id=venture_id,
            agent_id=agent_id,
            run_id=run_id,
        )
        assistant = fix.get("assistant_message") or {
            "role": "assistant",
            "content": fix.get("content"),
        }
        return ChatResult(
            content=fix.get("content"),
            raw={"sample": True} if fix.get("sample") else None,
            tier=tier,
            purpose=purpose,
            tokens_in=tin,
            tokens_out=tout,
            reasoning_tokens=rtok,
            cost_usd=entry.cost_usd,
            tool_calls=assistant.get("tool_calls") or [],
            assistant_message=assistant,
        )


# Re-export for convenience
__all__ = ["ChatResult", "LLMClient", "ProviderConfig", "UnroutedPurposeError"]
