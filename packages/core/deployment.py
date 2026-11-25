"""Resolve private deployment identifiers without publishing their values."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ECS_REGISTER_FIELDS = {
    "family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions",
    "volumes", "placementConstraints", "requiresCompatibilities", "cpu", "memory",
    "pidMode", "ipcMode", "proxyConfiguration", "inferenceAccelerators",
    "ephemeralStorage", "runtimePlatform", "enableFaultInjection",
}


def infrastructure(template: str) -> str:
    path = Path(os.environ.get("COMPANY_OS_DEPLOYMENT_CONFIG",
                               str(Path(__file__).resolve().parents[2] / "config/deployment.private.json")))
    try:
        values = None

        def replace(match: re.Match[str]) -> str:
            nonlocal values
            name = match[1]
            value = os.environ.get(name)
            if value is None:
                if values is None:
                    values = json.loads(path.read_text())
                    if not isinstance(values, dict):
                        raise TypeError
                value = values[name]
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_/!:-]+", value):
                raise ValueError
            if name == "AWS_ACCOUNT_ID" and not re.fullmatch(r"\d{12}", value):
                raise ValueError
            return value

        return re.sub(r"\$\{([A-Z][A-Z0-9_]+)\}", replace, template)
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("Please correct your deployment identifier environment values or configure COMPANY_OS_DEPLOYMENT_CONFIG and retry.") from None
