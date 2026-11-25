"""Company OS LLM gateway — sole ``openai`` importer."""

from llm_gateway.boundary import BoundaryViolation, Decision, register_filter
from llm_gateway.client import ChatResult, LLMClient
from llm_gateway.hosted import (
    PUBLIC_MODEL_ID,
    HostedAuthRequired,
    HostedCreditsRequired,
    HostedPlanExpired,
    HostedUpdateRequired,
    hosted_chat_completion,
    hosted_session,
)
from llm_gateway.messages import (
    SchemaFailure,
    apply_schema_or_escalate,
    assistant_message_preserving_reasoning,
    json_schema_response_format,
)
from llm_gateway.provider_foundry import (
    ProviderConfig,
    normalize_base_url,
    redacted_status,
)
from llm_gateway.routing import UnroutedPurposeError, resolve_tier
from llm_gateway.sole import (
    boundary_decision,
    embed,
    env_chat_completion,
    open_env_client,
    require_env_api_key,
    transmit,
    transmit_stream,
)
from llm_gateway.toolargs import validate_tool_call

__all__ = [
    "PUBLIC_MODEL_ID",
    "BoundaryViolation",
    "ChatResult",
    "Decision",
    "HostedAuthRequired",
    "HostedCreditsRequired",
    "HostedPlanExpired",
    "HostedUpdateRequired",
    "LLMClient",
    "ProviderConfig",
    "SchemaFailure",
    "UnroutedPurposeError",
    "apply_schema_or_escalate",
    "assistant_message_preserving_reasoning",
    "boundary_decision",
    "embed",
    "env_chat_completion",
    "hosted_chat_completion",
    "hosted_session",
    "json_schema_response_format",
    "normalize_base_url",
    "open_env_client",
    "redacted_status",
    "register_filter",
    "require_env_api_key",
    "resolve_tier",
    "transmit",
    "transmit_stream",
    "validate_tool_call",
]
