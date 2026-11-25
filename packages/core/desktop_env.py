"""Stdlib-only detection for source launchers and packaged desktop code."""

import os
import sys
from collections.abc import Mapping


def is_packaged_desktop(environment: Mapping[str, str] | None = None) -> bool:
    source = os.environ if environment is None else environment
    flag = source.get("COMPANY_OS_PACKAGED", "").strip().lower()
    return flag in {"1", "true", "yes"} or bool(getattr(sys, "frozen", False))
