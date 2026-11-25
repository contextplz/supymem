"""Local operator/default-secret services must not publish to the LAN."""
from pathlib import Path

import yaml


def test_developer_published_ports_are_loopback_only():
    root = Path(__file__).resolve().parents[2]
    config = yaml.load((root / "docker-compose.companyos.yml").read_text(), Loader=yaml.BaseLoader)
    ports = [port for service in config["services"].values() for port in service.get("ports", [])]
    assert len(ports) == 8
    assert all(port.startswith("127.0.0.1:") for port in ports)
    assert all("${" in port for port in ports)  # Existing caller-selected host port variables are preserved.
