"""How to run a generated workdir as a web app. Honest: None if we cannot tell."""

from __future__ import annotations

from pathlib import Path


def exposed_port(workdir: str | Path) -> int:
    """Port the *container* should listen on. Host mapping is chosen by the backend."""
    root = Path(workdir)
    dockerfile = root / "Dockerfile"
    if dockerfile.is_file():
        text = dockerfile.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            stripped = line.strip().upper()
            if stripped.startswith("EXPOSE "):
                token = stripped.split()[1]
                try:
                    return int(token.split("/")[0])
                except ValueError:
                    continue
    pkg = root / "package.json"
    if pkg.is_file():
        return 3000
    return 80


def suggested_dockerfile(workdir: str | Path) -> str | None:
    """Inline Dockerfile for Coolify when the repo has none. None = we will not invent a stack."""
    root = Path(workdir)
    if (root / "Dockerfile").is_file():
        return (root / "Dockerfile").read_text(encoding="utf-8")
    if (root / "index.html").is_file():
        return (
            "FROM nginx:alpine\n"
            "COPY . /usr/share/nginx/html\n"
            "EXPOSE 80\n"
        )
    pkg = root / "package.json"
    if pkg.is_file():
        return (
            "FROM node:22-alpine\n"
            "WORKDIR /app\n"
            "COPY package.json package-lock.json* ./ \n"
            "RUN npm install --omit=dev || npm install\n"
            "COPY . .\n"
            "EXPOSE 3000\n"
            'CMD ["npm", "start"]\n'
        )
    return None
