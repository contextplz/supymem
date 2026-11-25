"""Coolify REST client for hosted preview URLs (doc 104 B4).

Call shapes taken from Coolify's public API:

- ``POST /api/v1/applications/dockerfile`` — create from an inline Dockerfile
  (https://coolify.io/docs/api-reference/api/applications/create-dockerfile-application)
- ``GET|POST /api/v1/applications/{uuid}/start`` — deploy
- ``GET /api/v1/applications/{uuid}`` — read ``fqdn``

Auth is ``Authorization: Bearer <COOLIFY_API_TOKEN>``. The token never appears
in logs or exception text.
"""

from __future__ import annotations

import logging
import os
import httpx

from preview.base import PreviewNotConfigured, PreviewResult
from preview.detect import suggested_dockerfile

log = logging.getLogger("preview.coolify")


class CoolifyPreview:
    name = "coolify"

    def __init__(
        self,
        *,
        api_url: str | None = None,
        token: str | None = None,
        project_uuid: str | None = None,
        server_uuid: str | None = None,
        environment_name: str | None = None,
        domain: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_url = (api_url or os.environ.get("COOLIFY_API_URL") or "").rstrip("/")
        self.token = token if token is not None else os.environ.get("COOLIFY_API_TOKEN", "")
        self.project_uuid = project_uuid or os.environ.get("COOLIFY_PROJECT_UUID", "")
        self.server_uuid = server_uuid or os.environ.get("COOLIFY_SERVER_UUID", "")
        self.environment_name = environment_name or os.environ.get(
            "COOLIFY_ENVIRONMENT", "production"
        )
        self.domain = (domain or os.environ.get("PREVIEW_APPS_DOMAIN") or "").lstrip(".")
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _client_or(self) -> httpx.Client:
        return self._client or httpx.Client(timeout=60.0)

    def _require_config(self) -> None:
        missing = [
            name
            for name, value in (
                ("COOLIFY_API_URL", self.api_url),
                ("COOLIFY_API_TOKEN", self.token),
                ("COOLIFY_PROJECT_UUID", self.project_uuid),
                ("COOLIFY_SERVER_UUID", self.server_uuid),
            )
            if not (value or "").strip()
        ]
        if missing:
            raise PreviewNotConfigured(
                "Coolify is not configured (" + ", ".join(missing) + " missing)"
            )

    def publish(
        self,
        workdir: str,
        *,
        slug: str,
        port: int | None = None,
    ) -> PreviewResult:
        self._require_config()
        dockerfile = suggested_dockerfile(workdir)
        if dockerfile is None:
            raise RuntimeError(
                "no Dockerfile, index.html, or package.json — will not invent a stack"
            )
        fqdn = f"{slug}.{self.domain}" if self.domain else None
        payload: dict[str, object] = {
            "project_uuid": self.project_uuid,
            "server_uuid": self.server_uuid,
            "environment_name": self.environment_name,
            "dockerfile": dockerfile,
            "name": slug,
            "instant_deploy": True,
            "ports_exposes": str(port or 80),
        }
        if fqdn:
            payload["domains"] = fqdn
        owns_client = self._client is None
        client = self._client_or()
        try:
            created = client.post(
                f"{self.api_url}/api/v1/applications/dockerfile",
                headers=self._headers(),
                json=payload,
            )
            created.raise_for_status()
            body = created.json() if created.content else {}
            app_uuid = str(
                body.get("uuid")
                or body.get("application_uuid")
                or (body.get("application") or {}).get("uuid")
                or ""
            )
            if not app_uuid:
                raise RuntimeError("Coolify create did not return an application uuid")
            url = (
                (body.get("fqdn") or body.get("domains") or "")
                or (f"https://{fqdn}" if fqdn else "")
            )
            if isinstance(url, list):
                url = url[0] if url else ""
            url = str(url)
            if url and not url.startswith("http"):
                url = f"https://{url}"
            if not url:
                got = client.get(
                    f"{self.api_url}/api/v1/applications/{app_uuid}",
                    headers=self._headers(),
                )
                if got.is_success:
                    info = got.json()
                    url = str(info.get("fqdn") or "")
                    if url and not url.startswith("http"):
                        url = f"https://{url}"
            if not url:
                raise RuntimeError("Coolify application has no fqdn yet")
            return PreviewResult(
                url=url.rstrip("/"),
                backend=self.name,
                application_uuid=app_uuid,
                detail=slug,
            )
        finally:
            if owns_client:
                client.close()

    def teardown(self, handle: PreviewResult) -> None:
        if not handle.application_uuid:
            return
        self._require_config()
        owns_client = self._client is None
        client = self._client_or()
        try:
            client.delete(
                f"{self.api_url}/api/v1/applications/{handle.application_uuid}",
                headers=self._headers(),
            )
        finally:
            if owns_client:
                client.close()
