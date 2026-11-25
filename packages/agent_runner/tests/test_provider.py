from agent_runner.provider import normalize_base_url, redacted_status, ProviderConfig


def test_normalize_foundry_url():
    assert normalize_base_url("azure_foundry", "https://x.openai.azure.com").endswith("/openai/v1")
    assert normalize_base_url("azure_foundry", "https://x.openai.azure.com/openai/v1") == "https://x.openai.azure.com/openai/v1"


def test_redacted_status_no_key_leak():
    cfg = ProviderConfig(
        provider="azure_foundry",
        base_url="https://example.openai.azure.com/openai/v1",
        api_key="SECRET_KEY_VALUE",
        deployment_name="deepseek-v4-pro-primary",
    )
    status = redacted_status(cfg)
    blob = str(status)
    assert "SECRET_KEY_VALUE" not in blob
    assert status["has_api_key"] is True
    assert status["endpoint_host"] == "example.openai.azure.com"
