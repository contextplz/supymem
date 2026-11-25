#!/usr/bin/env python3
"""Ordered supervision: warden → company_os → gateway → console with health gating."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".venv" / "bin" / "python"


def _wait_health(url: str, timeout_s: float = 20.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                if 200 <= r.status < 300:
                    return True
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.3)
    return False


def _load_dotenv_into(env: dict[str, str]) -> None:
    """Merge repo `.env` / `.env.local` into env without overwriting exports."""
    for name in (".env", ".env.local"):
        dotenv = ROOT / name
        if not dotenv.is_file():
            continue
        for raw in dotenv.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if not key or key in env:
                continue
            env[key] = val.strip().strip("'").strip('"')


def _mark_owned_auth(env: dict[str, str]) -> None:
    """Tag Company-OS-owned API keys so BYO coding-agent children scrub them."""
    owned = {
        "LLM_API_KEY",
        "AZURE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CURSOR_API_KEY",
        "CODEX_API_KEY",
        "DEEPSEEK_API_KEY",
    }
    present = sorted(k for k in owned if env.get(k))
    if not present:
        return
    existing = {x for x in env.get("_COS_OWNED_AUTH", "").split(",") if x}
    existing |= set(present)
    env["_COS_OWNED_AUTH"] = ",".join(sorted(existing))


def main() -> int:
    env = os.environ.copy()
    _load_dotenv_into(env)
    env.update(SUPYMEM_MODE="local", APP_ENV="local")
    env.setdefault("SESSION_SECRET", "dev-session-secret-change-me-32b!!")
    env.setdefault("WARDEN_URL", "http://127.0.0.1:3400")
    env.setdefault("COMPANY_OS_URL", "http://127.0.0.1:3300")
    env.setdefault("GATEWAY_URL", "http://127.0.0.1:3200")
    env.setdefault("NEXT_PUBLIC_GATEWAY_URL", "http://127.0.0.1:3200")
    # Ops console must hit company_os directly; gateway /api/v1 is session-gated.
    env.setdefault("NEXT_PUBLIC_OPS_API_URL", "http://127.0.0.1:3300")
    env.setdefault("COMPANY_OS_URL", "http://127.0.0.1:3300")
    env.setdefault("PROVISIONING_MODE", "local")
    # Product UX: do not block Start building / quiz submit on the full pipeline.
    # Acceptance tests set ONBOARDING_SYNC=1 themselves.
    env.setdefault("ONBOARDING_SYNC", "0")
    # Stub keeps onboarding fast in tests; native dogfood uses the real builder.
    # Override with CODING_AGENT=inhouse_stub or byo/cursor/claude/codex.
    env.setdefault("CODING_AGENT", "openhands_builder")
    env.setdefault("CODING_AGENT_LIVE", "1")
    # Safe by default: live sends/posts require an explicit operator opt-in.
    # Configured Gmail or social accounts must never arm actuation implicitly.
    env.setdefault("CHANNELS_DRY_RUN", "true")
    env.setdefault("SANDBOX_MODE", "false")
    env.setdefault("BUILDER_SERVICE_URL", "http://127.0.0.1:8090")
    env.setdefault("BUILDER_SERVICE_TOKEN", "dev-builder-token-change-me")
    env.setdefault("BUILDER_SANDBOX", "auto")
    env.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
    env.setdefault("APP_ENV", "local")
    _mark_owned_auth(env)
    # Prefer live Foundry copy for first-look pages when LLM_* is configured.
    if env.get("LLM_OFFLINE", "false").lower() in {"0", "false", "no"}:
        env.setdefault("FIRSTLOOK_COPY_MODE", "live")
    else:
        env.setdefault("FIRSTLOOK_COPY_MODE", "fallback")

    procs: list[subprocess.Popen] = []

    def stop_all(*_args: object) -> None:
        for p in reversed(procs):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    p.terminate()
        time.sleep(0.5)
        for p in reversed(procs):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop_all)
    signal.signal(signal.SIGTERM, stop_all)

    py = str(VENV_PY if VENV_PY.exists() else sys.executable)

    specs = [
        (
            "warden",
            [py, "-m", "uvicorn", "app.main:app", "--app-dir", "services/warden", "--host", "127.0.0.1", "--port", "3400"],
            "http://127.0.0.1:3400/health",
            False,
        ),
        (
            "builder",
            [py, "-m", "uvicorn", "app.main:app", "--app-dir", "services/builder", "--host", "127.0.0.1", "--port", "8090"],
            "http://127.0.0.1:8090/health",
            True,
        ),
        (
            "company_os",
            [py, "-m", "uvicorn", "app.main:app", "--app-dir", "services/company_os", "--host", "127.0.0.1", "--port", "3300"],
            "http://127.0.0.1:3300/health",
            False,
        ),
        (
            "worker",
            [py, "-m", "app.worker.main"],
            "http://127.0.0.1:3301/health",
            False,
        ),
        (
            "gateway",
            [py, "-m", "uvicorn", "app.main:app", "--app-dir", "services/gateway", "--host", "127.0.0.1", "--port", "3200"],
            "http://127.0.0.1:3200/health",
            False,
        ),
    ]

    for name, cmd, health, optional in specs:
        print(f"[dev] starting {name}: {' '.join(cmd)}", flush=True)
        p = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env={**env, "PYTHONPATH": f"{ROOT / 'packages'}:{ROOT / 'services/company_os'}",
                 "APP_ROLE": "worker", "WORKER_HEALTH_PORT": "3301"} if name == "worker" else env,
            start_new_session=True,
        )
        procs.append(p)
        if not _wait_health(health, 40 if name == "builder" else 25):
            if optional:
                print(f"[dev] {name} not healthy at {health} — continuing (optional)", flush=True)
                continue
            print(f"[dev] {name} failed health check at {health}", flush=True)
            stop_all()
            return 1
        print(f"[dev] {name} healthy", flush=True)

    # Console (optional if pnpm/node missing). Failure here must not tear down backends.
    console_dir = ROOT / "apps" / "console"
    console_pid: int | None = None
    if (console_dir / "package.json").exists():
        pnpm = "pnpm"
        cmd = [pnpm, "--dir", str(console_dir), "dev"]
        print("[dev] starting console", flush=True)
        try:
            p = subprocess.Popen(cmd, cwd=str(ROOT), env=env, start_new_session=True)
            procs.append(p)
            console_pid = p.pid
        except FileNotFoundError:
            print("[dev] pnpm not found — skipping console", flush=True)

    print("[dev] stack up. Ctrl-C to stop.", flush=True)
    while True:
        for p in procs:
            if p.poll() is not None:
                if console_pid is not None and p.pid == console_pid:
                    print(
                        f"[dev] console exited code={p.returncode} — backends still running "
                        "(run: cd apps/console && pnpm install && pnpm dev)",
                        flush=True,
                    )
                    procs = [x for x in procs if x is not p]
                    console_pid = None
                    continue
                print(f"[dev] process exited code={p.returncode}", flush=True)
                stop_all()
                return p.returncode or 1
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
