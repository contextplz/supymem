from llm_gateway.messages import normalize_chat_messages, sanitize_chat_tools
from llm_gateway.provider_foundry import ProviderConfig, resolve_live_model


def test_supymem_flash_maps_to_foundry_flash_deployment() -> None:
    cfg = ProviderConfig(deployment_flash="DeepSeek-V4-Pro", deployment_pro="DeepSeek-V4-Pro")
    assert resolve_live_model(cfg, tier="flash", passthrough_model="supymem-flash") == "DeepSeek-V4-Pro"
    assert resolve_live_model(cfg, tier="passthrough", passthrough_model="supymem-flash") == "DeepSeek-V4-Pro"


def test_real_provider_model_is_left_alone() -> None:
    cfg = ProviderConfig()
    assert resolve_live_model(cfg, tier="flash", passthrough_model="gpt-4o-mini") == "gpt-4o-mini"


def test_hermes_content_parts_flatten_to_text() -> None:
    msgs = normalize_chat_messages(
        [{"role": "user", "content": [{"type": "text", "text": "List the files and stop."}]}]
    )
    assert msgs[0]["content"] == "List the files and stop."


def test_tools_keep_openai_shape() -> None:
    tools = sanitize_chat_tools(
        [{"type": "function", "function": {"name": "read_file", "description": "read"}}]
    )
    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "read",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
