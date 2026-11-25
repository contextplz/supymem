from __future__ import annotations

from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_every_adapter_declares_cost_quota_prereqs_version():
    data = yaml.safe_load((ROOT / "config/channel_manifests.yaml").read_text())
    channels = data["channels"]
    assert channels
    for name, m in channels.items():
        assert "version" in m, name
        assert "cost_per_action_usd" in m, name
        assert "quota_per_day" in m, name
        assert "prerequisites" in m, name
