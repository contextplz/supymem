from __future__ import annotations

import pytest
import yaml
from pathlib import Path
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.feedback.fitness import assert_promotion_metrics, resolve_metric

ROOT = Path(__file__).resolve().parents[2]


def test_catalog_metrics_resolve():
    catalog = yaml.safe_load((ROOT / "config/agent_catalog.yaml").read_text())
    for role in catalog["roles"]:
        resolve_metric(role["fitness_metric"])


def test_soft_only_promotion_refused():
    with pytest.raises(PermissionError):
        assert_promotion_metrics(["landing_conversion_rate"])  # soft
    assert_promotion_metrics(["stage_velocity_days"])  # hard
