"""Write the rendered page to disk and return a local URL (45 §5)."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

from app.core.local_mode import is_local

from coding_agents.env import child_env

_LINK_STYLESHEET_RE = re.compile(
    r'<link\b(?=[^>]*\brel=["\']stylesheet["\'])[^>]*>',
    re.I,
)
_HREF_RE = re.compile(r'\bhref=["\']([^"\']+)["\']', re.I)


def _stylesheet_name(href: str) -> str:
    path = href.split("#", 1)[0].split("?", 1)[0]
    return path.rsplit("/", 1)[-1]


def inline_stylesheets(html: str, extras: dict[str, str] | None) -> str:
    """Embed CSS files into the HTML so a missing styles.css cannot unstyle the page."""
    if is_local() or not html or not extras:
        return html

    def repl(match: re.Match[str]) -> str:
        tag = match.group(0)
        href_m = _HREF_RE.search(tag)
        if not href_m:
            return tag
        name = _stylesheet_name(href_m.group(1))
        css = extras.get(name)
        if not isinstance(css, str) or not css.strip():
            return tag
        return f'<style data-supymem-inline="{name}">\n{css}\n</style>'

    return _LINK_STYLESHEET_RE.sub(repl, html)


def version_stylesheet_hrefs(html: str, version: int) -> str:
    """Point leftover stylesheet links at the same site version as the HTML."""
    if is_local() or not html:
        return html

    def repl(match: re.Match[str]) -> str:
        tag = match.group(0)
        href_m = _HREF_RE.search(tag)
        if not href_m:
            return tag
        href = href_m.group(1)
        if href.startswith(("http://", "https://", "//", "data:")):
            return tag
        name = _stylesheet_name(href)
        if not name:
            return tag
        new = f"{name}?v={int(version)}"
        return tag[: href_m.start(1)] + new + tag[href_m.end(1) :]

    return _LINK_STYLESHEET_RE.sub(repl, html)


log = logging.getLogger(__name__)

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _data_root() -> Path:
    override = os.environ.get("FIRSTLOOK_DATA_DIR") or os.environ.get("COMPANY_OS_DATA_DIR")
    return Path(override) if override else _repo_root() / "data"


def site_dir(
    *,
    workspace_id: str | None = None,
    venture_id: str | None = None,
    version: int = 1,
) -> Path:
    root = _data_root()
    if workspace_id:
        return root / "workspaces" / workspace_id / "site" / f"v{version}"
    if venture_id:
        return root / "ventures" / venture_id / "site" / f"v{version}"
    raise ValueError("workspace_id or venture_id required")


def write_site(
    html: str,
    *,
    workspace_id: str | None = None,
    venture_id: str | None = None,
    version: int = 1,
    prefer_http: bool = False,
    port: int | None = None,
    extras: dict[str, str] | None = None,
) -> tuple[Path, str]:
    """Persist index.html and return (path, url).

    URL is ``file://…`` by default so the page opens offline. When
    ``prefer_http`` is set, return ``http://127.0.0.1:<port>/w/<id>/site/``.
    """
    directory = site_dir(
        workspace_id=workspace_id, venture_id=venture_id, version=version
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "index.html"
    if is_local():
        path.write_bytes(html.encode("utf-8"))
    else:
        path.write_text(html, encoding="utf-8")
    for name, body in (extras or {}).items():
        if not name or "/" in name or name.startswith(".") or name == "index.html":
            continue
        if is_local():
            (directory / name).write_bytes(body.encode("utf-8"))
        else:
            (directory / name).write_text(body, encoding="utf-8")
    _commit_site(directory, version=version)

    if prefer_http and workspace_id:
        listen = port or int(os.environ.get("CORE_PORT") or 3300)
        url = f"http://127.0.0.1:{listen}/w/{workspace_id}/site/"
    else:
        url = path.resolve().as_uri()
    return path, url


def _landing_repo(directory: Path) -> Path:
    directory = directory.resolve()
    return directory.parent if directory.name.startswith("v") else directory


def _isolated_git_env(repo: Path) -> dict[str, str]:
    env = child_env()
    git_dir = repo / ".git"
    env.update(
        {
            "GIT_DIR": str(git_dir),
            "GIT_WORK_TREE": str(repo),
            "GIT_CEILING_DIRECTORIES": str(repo.parent.resolve()),
            "GIT_AUTHOR_NAME": "Company OS",
            "GIT_AUTHOR_EMAIL": "local@company-os",
            "GIT_COMMITTER_NAME": "Company OS",
            "GIT_COMMITTER_EMAIL": "local@company-os",
        }
    )
    return env


def _run_git(args: list[str], *, repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if "nothing to commit" not in err.lower():
            log.warning("firstlook git %s failed (exit %s): %s", args, proc.returncode, err)
    return proc


def _resolved_toplevel(repo: Path, env: dict[str, str]) -> Path | None:
    proc = _run_git(["rev-parse", "--show-toplevel"], repo=repo, env=env)
    if proc.returncode != 0:
        return None
    text = (proc.stdout or "").strip()
    if not text:
        return None
    return Path(text).resolve()


def _commit_site(directory: Path, *, version: int) -> None:
    """Keep the landing page in a local git repo so each look is a commit.

    Isolation is structural: every git invocation sets GIT_DIR and
    GIT_WORK_TREE to this landing-page directory. Discovery walk-up to the
    monorepo is also blocked by GIT_CEILING_DIRECTORIES and a toplevel guard.
    """
    repo = _landing_repo(directory)
    repo.mkdir(parents=True, exist_ok=True)
    monorepo = _repo_root().resolve()
    intended = repo.resolve()
    if intended == monorepo:
        log.warning("firstlook git aborted: landing repo path is the monorepo root")
        return

    env = _isolated_git_env(repo)
    try:
        init = _run_git(["init"], repo=repo, env=env)
        if init.returncode != 0:
            return
        readme = repo / "README.md"
        if not readme.exists():
            readme.write_text(
                "Local landing-page repo. Each first look is a commit. Nothing is public.\n",
                encoding="utf-8",
            )
        toplevel = _resolved_toplevel(repo, env)
        if toplevel is None:
            log.warning("firstlook git aborted: could not resolve toplevel for %s", repo)
            return
        if toplevel == monorepo:
            log.warning(
                "firstlook git aborted: resolved toplevel %s is the monorepo root",
                toplevel,
            )
            return
        if toplevel != intended:
            log.warning(
                "firstlook git aborted: resolved toplevel %s is not landing repo %s",
                toplevel,
                intended,
            )
            return
        add = _run_git(["add", "-A", "--", "."], repo=repo, env=env)
        if add.returncode != 0:
            return
        _run_git(["commit", "-m", f"first look v{version}"], repo=repo, env=env)
    except OSError as exc:
        log.warning("firstlook git unavailable: %s", exc)
        return


__all__ = [
    "inline_stylesheets",
    "site_dir",
    "version_stylesheet_hrefs",
    "write_site",
]
