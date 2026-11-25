"""Preview backend protocol. Implementations live in ``local.py`` / ``coolify.py``."""

from __future__ import annotations

import os
from typing import Protocol

from pydantic import BaseModel

from core.env_contract import parse_app_env


class PreviewNotConfigured(RuntimeError):
    """Hosted preview was requested without Coolify configuration."""


class PreviewResult(BaseModel):
    url: str
    backend: str
    container_id: str | None = None
    application_uuid: str | None = None
    detail: str | None = None


class PreviewBackend(Protocol):
    name: str

    def publish(self, workdir: str, *, slug: str, port: int | None = None) -> PreviewResult: ...

    def teardown(self, handle: PreviewResult) -> None: ...


def get_preview(*, app_env: str | None = None) -> PreviewBackend:
    """Pick the preview backend for this environment.

    Local always uses Docker on this machine. Outside local, Coolify is required
    — a hosted API must not bind a developer loopback port and call it a preview.
    """
    from preview.coolify import CoolifyPreview
    from preview.local import LocalDockerPreview

    env = app_env if app_env is not None else parse_app_env()
    if env == "local":
        return LocalDockerPreview()
    if not (os.environ.get("COOLIFY_API_URL") or "").strip():
        raise PreviewNotConfigured(
            "Coolify is required outside local (set COOLIFY_API_URL)"
        )
    return CoolifyPreview()
