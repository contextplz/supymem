"""Dedicated import storage; never fall back to build bucket retention policies."""
from __future__ import annotations

import os

from app.builder.artifact_store import ArtifactStoreConfig, S3ArtifactStore
from app.builder.model_proxy import BuildModelError
from .contracts import digest
from .cleanup_store import POLICY

ENV_FIELDS = ("IMPORT_STORAGE_S3_BUCKET", "IMPORT_STORAGE_S3_PREFIX", "IMPORT_STORAGE_KMS_KEY_ARN",
              "IMPORT_STORAGE_AWS_REGION", "IMPORT_STORAGE_S3_EXPECTED_OWNER")


def configuration():
    try:
        return ArtifactStoreConfig(*(os.environ[key] for key in ENV_FIELDS))
    except (KeyError, BuildModelError):
        raise BuildModelError("import_store_configuration_unavailable") from None


def fingerprint(config=None):
    return digest({"storage": (config or configuration()).fingerprint, "cleanup_policy": POLICY,
                   "fences_expire": False, "raw_cleanup_after_hours": 6})


def cleanup_ready():
    config = configuration()
    if os.getenv("IMPORT_CLEANUP_ENABLED") != "1" or os.getenv("IMPORT_CLEANUP_ACCEPTANCE") != fingerprint(config):
        raise BuildModelError("import_cleanup_not_accepted")
    return config


def configured():
    import boto3
    from botocore.config import Config
    config = configuration()
    client = boto3.client("s3", region_name=config.region,
        config=Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1},
                      ignore_configured_endpoint_urls=True, proxies={}))
    return S3ArtifactStore(config, client)
