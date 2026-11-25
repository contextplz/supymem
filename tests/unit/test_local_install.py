from pathlib import Path

from coding_agents.local_install import (
    already_installed,
    consent_copy,
    hermes_bin,
    hermes_config_yaml,
    hermes_env_file,
    hermes_venv_dir,
    pip_packages,
)


def test_hermes_lives_in_supymem_folder(tmp_path: Path):
    assert hermes_venv_dir(tmp_path) == tmp_path / "hermes-venv"
    assert hermes_bin(tmp_path) == tmp_path / "hermes-venv" / "bin" / "hermes"


def test_already_installed_none_on_empty_path(tmp_path: Path):
    assert already_installed(path_env=str(tmp_path), data_root=tmp_path) is None


def test_pip_package_is_hermes_only():
    assert pip_packages() == ["hermes-agent[acp]"]


def test_consent_copy_is_hermes_only():
    copy = consent_copy()
    assert "Supymem Computer Use" in copy["installs"]
    assert "Does not install Cursor" in copy["does_not"]
    assert "OpenCode" in copy["does_not"]
    assert "Nothing is installed until you click" in copy["body"]


def test_hermes_config_points_at_platform_proxy():
    yaml = hermes_config_yaml(
        base_url="http://127.0.0.1:3200/openai/v1",
        model="supymem-flash",
        api_key="sk_app_test.secret",
    )
    assert "provider: custom" in yaml
    assert "supymem-flash" in yaml
    assert "http://127.0.0.1:3200/openai/v1" in yaml
    assert "sk_app_test.secret" in yaml
    assert "gateway:" in yaml
    assert "telegram:" in yaml
    assert "anthropic" not in yaml.lower() or "ignored" in yaml
    env = hermes_env_file(base_url="http://127.0.0.1:3200/openai/v1", api_key="sk_app_test.secret")
    assert "OPENAI_API_KEY=sk_app_test.secret" in env
    assert "SUPYMEM_AI_API_KEY=sk_app_test.secret" in env
