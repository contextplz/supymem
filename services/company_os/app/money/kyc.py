"""KYC state and PRE_KYC collection caps."""
from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml


class KYCStatus(StrEnum):
    PRE_KYC = "pre_kyc"
    KYC_COMPLETE = "kyc_complete"


_CONFIG = Path(__file__).resolve().parents[4] / "config" / "money.yaml"


def money_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or _CONFIG
    with config_path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def default_currency() -> str:
    return str(money_config().get("default_currency", "INR")).upper()


def pre_kyc_balance_cap_minor() -> int:
    value = money_config().get("pre_kyc_balance_cap_minor", 100000)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("pre_kyc_balance_cap_minor must be a non-negative integer")
    return value


def can_collect(status: KYCStatus | str, held_minor: int, amount_minor: int) -> bool:
    if status == KYCStatus.KYC_COMPLETE:
        return True
    return held_minor + amount_minor <= pre_kyc_balance_cap_minor()
