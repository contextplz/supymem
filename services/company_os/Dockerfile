# syntax=docker/dockerfile:1
ARG PYTHON_BASE=public.ecr.aws/docker/library/python:3.12.11-slim-bookworm
FROM ${PYTHON_BASE} AS external-deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# The hashed runtime graph is generated from the non-optional dependencies in
# the service and imported first-party package manifests.  Keep this COPY
# ahead of every source COPY: it is the large layer that source-only releases
# must be able to reuse in the registry.
COPY services/company_os/requirements.runtime.lock /tmp/requirements.runtime.lock
RUN python -m venv /venv \
    && /venv/bin/pip install --require-hashes -r /tmp/requirements.runtime.lock \
    && /venv/bin/python -c "import jinja2"

# Build first-party packages in a separate target.  They are installed into a
# directory outside /venv so the runtime's external dependency layer remains
# byte-for-byte reusable when application source changes.
FROM external-deps AS firstparty
COPY services/company_os/requirements.build.lock /tmp/requirements.build.lock
RUN /venv/bin/pip install --require-hashes -r /tmp/requirements.build.lock
# Only the packages this service actually imports (see services/company_os/app).
COPY packages/core packages/core
COPY packages/coding_agents packages/coding_agents
COPY packages/desktop_runtime packages/desktop_runtime
COPY packages/huginn_watch packages/huginn_watch
COPY packages/llm_gateway packages/llm_gateway
COPY packages/guardrails packages/guardrails
COPY packages/connectors packages/connectors
COPY services/company_os/pyproject.toml services/company_os/pyproject.toml
COPY services/company_os/app services/company_os/app
# The service itself runs from its direct workspace source below so its
# path-derived config behavior is unchanged; retain only its dist-info here.
RUN --mount=type=bind,source=packages,target=/context-packages \
    set -e; \
    set --; \
    if [ -f /context-packages/harness/pyproject.toml ]; then \
        cp -R /context-packages/harness /src/packages/harness; \
        set -- /src/packages/harness; \
    fi; \
    /venv/bin/pip install --no-deps --no-build-isolation --target /firstparty \
        /src/packages/core \
        /src/packages/coding_agents \
        /src/packages/desktop_runtime \
        /src/packages/huginn_watch \
        /src/packages/llm_gateway \
        /src/packages/guardrails \
        /src/packages/connectors \
        "$@" \
        /src/services/company_os \
    && rm -rf /firstparty/app \
    && PYTHONPATH=/firstparty /venv/bin/python -c "import importlib.metadata as m, tomllib; from pathlib import Path; from packaging.version import Version; expected = tomllib.loads(Path('services/company_os/pyproject.toml').read_text())['project']['version']; assert Version(m.version('company-os')) == Version(expected); assert m.version('company-os-core') == '0.1.0'; assert m.version('llm-gateway') == '0.1.0'" \
    && if [ "$#" -gt 0 ]; then PYTHONPATH=/firstparty /venv/bin/python -c "import importlib.metadata as m; import harness; assert m.version('companyos-harness') == '0.1.0'"; fi

# ---------------------------------------------------------------------------
FROM ${PYTHON_BASE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/venv/bin:$PATH \
    PYTHONPATH=/workspace/services/company_os:/workspace/firstparty \
    PORT=3300 \
    COMPANY_OS_DATA_DIR=/workspace/data \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# git: first-look + local repo adapters shell out to it.
# Research CLIs (gh, yt-dlp, twitter-cli, …) are intentionally omitted.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends ca-certificates git gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 appuser \
    && useradd --system --uid 10001 --gid appuser --home /workspace --shell /usr/sbin/nologin appuser \
    && mkdir -p /workspace/data /workspace/scripts

# This is the stable, external-only dependency layer.  Do not copy it from
# firstparty: doing so would cause source-only changes to repush it.
COPY --from=external-deps /venv /venv
# The API and worker share one published artifact. Cache its version-matched
# Debian browser/package layer ahead of all first-party application sources.
RUN /venv/bin/playwright install --with-deps firefox \
    && rm -rf /var/lib/apt/lists/* \
    && /venv/bin/python -c "from pathlib import Path; from playwright.sync_api import sync_playwright; p = sync_playwright().start(); assert Path(p.firefox.executable_path).is_file(); p.stop()"
COPY --from=firstparty --chown=appuser:appuser /firstparty /workspace/firstparty
# The service stays at this path because several runtime adapters derive the
# workspace/config location from app.__file__.
COPY --chown=appuser:appuser services/company_os/app /workspace/services/company_os/app
COPY --chown=appuser:appuser services/company_os/alembic.ini /workspace/services/company_os/alembic.ini
COPY --chown=appuser:appuser config /workspace/config
# Vendored GStack skill text (MIT); app/gstack/methodology.py loads it at import.
COPY --chown=appuser:appuser third_party /workspace/third_party
COPY LICENSE NOTICE /workspace/
COPY --chown=appuser:appuser scripts/seed_local.py /workspace/scripts/seed_local.py
COPY docker/entrypoint.sh /docker-entrypoint.sh
COPY docker/python-healthcheck.py /docker/python-healthcheck.py
RUN chmod 755 /docker-entrypoint.sh

WORKDIR /workspace/services/company_os
EXPOSE 3300
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-3300} --timeout-graceful-shutdown 30"]
HEALTHCHECK --interval=5s --timeout=3s --start-period=25s --retries=12 \
    CMD ["python", "/docker/python-healthcheck.py"]

FROM runtime AS local
RUN chown appuser:appuser /workspace

# Test extra: pytest is not in the runtime image. Mount tests/ at run time.
FROM runtime AS test
USER root
RUN /venv/bin/pip install --no-cache-dir pytest pytest-asyncio httpx
# Keep the same entrypoint (drops to appuser). Override command in compose/Make.
CMD ["python", "-m", "pytest", "/workspace/tests", "-q"]

# Worker and API use the same validated/published runtime and browser layer.
FROM runtime AS worker
ENV APP_ROLE=worker
CMD ["python", "-m", "app.worker.main"]

# An unqualified build publishes the API image. Workers override command/role
# in their existing reviewed task definition, retaining the same immutable image.
FROM runtime AS production
