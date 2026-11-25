from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.hermes.messaging import messaging_env_lines, messaging_gateway_yaml, messaging_status


def test_messaging_ignores_anthropic_and_stays_off_without_tokens(monkeypatch):
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "DISCORD_BOT_TOKEN",
        "SLACK_BOT_TOKEN",
        "WHATSAPP_CLOUD_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    status = messaging_status()
    assert status["ignored"] == ["anthropic"]
    assert "telegram" in status["pending"]
    assert status["armed"] == []
    yaml = messaging_gateway_yaml()
    assert "telegram:" in yaml
    assert "enabled: false" in yaml
    assert messaging_env_lines() == []


def test_telegram_token_arms_channel(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    status = messaging_status()
    assert "telegram" in status["armed"]
    assert "TELEGRAM_BOT_TOKEN=123:abc" in messaging_env_lines()
    assert "telegram:\n      enabled: true" in messaging_gateway_yaml()
