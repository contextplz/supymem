"""CUAI Responses routing; zero-output timeout fallback retains unknown spend."""
from __future__ import annotations

import os
import time
from urllib.parse import urlsplit

import httpx

OPENAI_ENDPOINT = "https://api.openai.com/v1/responses"
FILTER_CODES = {"content_filter", "ResponsibleAIPolicyViolation"}
AZURE_RESPONSE_TIMEOUT = httpx.Timeout(30, connect=10)
FALLBACK_REASONS = {"azure_connect_error", "azure_http_error", "azure_content_filter", "azure_pre_output_timeout"}


class NoOutputReadTimeout(httpx.ReadTimeout):
    """Transport observed zero response-body bytes; provider spend remains unknown."""


def read_response(client, url, headers, body):
    raw, response = bytearray(), None
    deadline = time.monotonic() + 180
    options = {"timeout": AZURE_RESPONSE_TIMEOUT} if "api-key" in headers else {}
    try:
        with client.stream("POST", url, headers=headers, json=body, **options) as response:
            status = response.status_code
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 200000 or time.monotonic() > deadline:
                    raise ValueError("device_browser_ai_response_bounds")
    except httpx.ReadTimeout:
        if not raw and (response is None or response.num_bytes_downloaded == 0):
            raise NoOutputReadTimeout("provider_pre_output_timeout", request=httpx.Request("POST", url)) from None
        raise
    return status, bytes(raw)


def azure_error_metadata(value):
    error = value.get("error") if isinstance(value, dict) else None
    error = error if isinstance(error, dict) else {}
    inner = error.get("innererror")
    inner = inner if isinstance(inner, dict) else {}
    known_codes = FILTER_CODES | {"invalid_request_error", "invalid_request", "DeploymentNotFound", "rate_limit_exceeded"}
    result = {key: node["code"] if isinstance(node.get("code"), str) and node["code"] in known_codes else "other"
              for key, node in (("error_code", error), ("innererror_code", inner)) if "code" in node}
    filters = inner.get("content_filter_result", error.get("content_filter_result"))
    if isinstance(filters, dict):
        result["content_filter_result"] = {
            category: {key: flags[key] for key in ("filtered", "detected") if type(flags.get(key)) is bool}
            for category, flags in filters.items() if category in {
                "hate", "self_harm", "sexual", "violence", "jailbreak", "indirect_attack",
                "protected_material_code", "protected_material_text", "profanity", "custom_blocklists", "pii"}
            and isinstance(flags, dict)}
    return result


def azure_endpoint(base):
    if not isinstance(base, str) or not base.isascii() or any(char.isspace() for char in base):
        raise ValueError("device_browser_ai_configuration_unavailable")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname
            or not parsed.hostname.endswith(".openai.azure.com")
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.path.rstrip("/") != "/openai/v1" or parsed.query or parsed.fragment):
        raise ValueError("device_browser_ai_configuration_unavailable")
    return base.rstrip("/") + "/responses"


def responses_call(send, body, *, provider="openai", endpoint=None, deployment=None, fallback_metadata=None,
                   on_timeout_fallback=None):
    if provider not in {"openai", "azure"}:
        raise ValueError("device_browser_ai_configuration_unavailable")
    if provider == "azure" and (not isinstance(deployment, str) or not 1 <= len(deployment) <= 100
            or not deployment.isascii() or any(not (char.isalnum() or char in "._-") for char in deployment)):
        raise ValueError("device_browser_ai_configuration_unavailable")
    providers = ("azure", "openai") if provider == "azure" else ("openai",)
    timed_out = False
    for served in providers:
        wire = dict(body)
        if served == "azure":
            url = azure_endpoint(endpoint)
            wire["model"] = deployment
            wire.pop("service_tier", None)
            key = os.environ.get("AZURE_OPENAI_CUAI_API_KEY") or os.environ.get("AZURE_OPENAI_API_KEY")
            headers = {"api-key": key}
        else:
            url = OPENAI_ENDPOINT
            key = os.environ.get("OPENAI_DEVICE_VISION_API_KEY") or os.environ.get("OPENAI_API_KEY")
            headers = {"Authorization": "Bearer " + (key or "")}
        if not key:
            raise ValueError("device_browser_ai_configuration_unavailable")
        if served == "openai" and timed_out and on_timeout_fallback is not None:
            on_timeout_fallback()
        if served == "openai" and timed_out and fallback_metadata is not None:
            fallback_metadata["fallback_attempted"] = True
        try:
            status, value = send(url, headers, wire)
        except NoOutputReadTimeout:
            if served != "azure":
                raise
            if fallback_metadata is not None:
                fallback_metadata.update(fallback_reason="azure_pre_output_timeout", azure_usage_unconfirmed=True)
            timed_out = True
            continue
        except httpx.ConnectError:
            if served == "azure":
                if fallback_metadata is not None:
                    fallback_metadata["fallback_reason"] = "azure_connect_error"
                continue
            raise
        if status == 200:
            return value, served
        no_output = value is None or (isinstance(value, dict)
            and not value.get("output") and not value.get("usage")
            and not value.get("id") and not value.get("status"))
        filtered = status == 400 and any(code in FILTER_CODES for key, code in azure_error_metadata(value).items()
                                       if key in {"error_code", "innererror_code"})
        if served == "azure" and no_output and (filtered or status in {401, 403, 404, 429} or 500 <= status <= 599):
            if fallback_metadata is not None:
                fallback_metadata["fallback_reason"] = "azure_content_filter" if filtered else "azure_http_error"
            continue
        raise ValueError("device_browser_ai_provider_failed")
    raise ValueError("device_browser_ai_provider_failed")
