"""Run the generated app in a Docker container and return http://127.0.0.1:<port>.

Docker CLI is invoked as a list of args (no ``shell=True``). A caller can inject
``runner`` in tests so we never talk to a real daemon.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

from preview.base import PreviewResult
from preview.detect import exposed_port

log = logging.getLogger("preview.local")

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LocalDockerPreview:
    name = "local_docker"

    def __init__(self, *, runner: Runner | None = None) -> None:
        self._runner = runner or _default_runner

    def publish(
        self,
        workdir: str,
        *,
        slug: str,
        port: int | None = None,
    ) -> PreviewResult:
        root = Path(workdir).resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"workdir does not exist: {root}")
        dockerfile = root / "Dockerfile"
        if dockerfile.is_symlink() or not dockerfile.is_file():
            raise RuntimeError(
                "Ask your connected coding tool to add a regular Dockerfile in your project folder, "
                "then retry your preview; connect a tool in Settings > Keys and connections > Coding tools if needed."
            )
        host_port = port or _free_port()
        container_port = exposed_port(root)
        name = f"cos-preview-{slug[:32]}-{uuid.uuid4().hex[:8]}"
        image = f"cos-preview:{name}"
        build = self._runner(
            ["docker", "build", "-t", image, str(root)]
        )
        if build.returncode != 0:
            raise RuntimeError(f"docker build failed: {build.stderr.strip() or build.stdout}")

        run = self._runner(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "-p",
                f"127.0.0.1:{host_port}:{container_port}",
                image,
            ]
        )
        if run.returncode != 0:
            raise RuntimeError(f"docker run failed: {run.stderr.strip() or run.stdout}")
        container_id = (run.stdout or "").strip() or name
        return PreviewResult(
            url=f"http://127.0.0.1:{host_port}",
            backend=self.name,
            container_id=container_id,
            detail=name,
        )

    def teardown(self, handle: PreviewResult) -> None:
        target = handle.container_id or handle.detail
        if not target:
            return
        self._runner(["docker", "rm", "-f", target])
