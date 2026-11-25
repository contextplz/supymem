"""Live preview backends for generated apps (doc 104 B4).

Local compose uses Docker on the same host. Staging/production uses Coolify
when configured. Selection is by ``APP_ENV``, not by guessing.
"""

from __future__ import annotations

from preview.base import PreviewBackend, PreviewNotConfigured, PreviewResult, get_preview
from preview.coolify import CoolifyPreview
from preview.local import LocalDockerPreview

__all__ = [
    "CoolifyPreview",
    "LocalDockerPreview",
    "PreviewBackend",
    "PreviewNotConfigured",
    "PreviewResult",
    "get_preview",
]
