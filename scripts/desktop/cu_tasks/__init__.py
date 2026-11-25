"""Computer-use task catalog: data + independent oracles, selectable by --category / --only.

Tasks are plain data (`Task`). Oracles (see oracles.py) read outcomes from disk, clipboard,
AppleScript read-back of owned records, or the fixture HTTP server's receipts; never the engine's claims.
Importing this package performs no desktop or network action.
"""
from __future__ import annotations

import re
import json
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REFUSALS = ("refused", "blocked", "denied", "declined", "stopped", "needs_user", "signin_needed", "returned")


def fmt(text: str, vars: dict) -> str:
    """Replace {name} only when name is a known var (AppleScript/JSON braces stay intact)."""
    return re.sub(r"\{(\w+)\}", lambda m: str(vars[m[1]]) if m[1] in vars else m[0], text)


def _run(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15, **kw)
    except (subprocess.TimeoutExpired, OSError):
        return None


def default_osa(script: str):
    r = _run(["osascript", "-e", script])
    return r.stdout.strip() if r and r.returncode == 0 else None


def default_clip():
    r = _run(["pbpaste"])
    return r.stdout if r and r.returncode == 0 else None


@dataclass
class Ctx:
    dir: Path
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    server: object = None
    vars: dict = field(default_factory=dict)
    osa: Callable = default_osa
    clip: Callable = default_clip
    cleanups: list = field(default_factory=list)
    cleanup_verified: bool = True
    hashes: dict = field(default_factory=dict)
    download_custody: object = None

    def __post_init__(self):
        self.vars.update(dir=str(self.dir), uid=self.uid, base=self.server.base if self.server else "")

    def path(self, rel) -> Path:
        return self.dir / fmt(rel, self.vars)

    def seed(self, rel, content, mode=None):
        """Create an owned fixture file and remember its hash so oracles detect tampering."""
        import hashlib
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else fmt(content, self.vars).encode())
        self.hashes[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
        return p


@dataclass(frozen=True)
class Task:
    id: str
    category: str
    steps: int          # expected distinct UI steps (short: 1-8, long: 30+)
    task: str           # founder phrasing; {vars} resolved from Ctx.vars after setup
    oracle: str         # human description of the independent check
    risk: str           # low | medium | high (blast radius if the engine misbehaves)
    verify: Callable    # (Ctx) -> (True|False|None, detail)
    setup: Callable = None
    long: bool = False
    expect: tuple = ("done",)   # acceptable engine outcomes (refusal tasks accept REFUSALS)
    needs: tuple = ()           # "server" | "numbers" | "mail" | "calendar" ...
    bind_backend: Callable = None   # QA-only custody preparation, before actor dispatch


REGISTRY: dict[str, Task] = {}


def add(id, category, steps, task, oracle, risk, verify, **kw):
    if id in REGISTRY:
        raise ValueError("duplicate task id " + id)
    REGISTRY[id] = Task(id, category, steps, task, oracle, risk, verify, **kw)


def load():
    from . import short_tasks, long_tasks  # noqa: F401  (registration side effects)
    return REGISTRY


def select(category: str = "", only: str = "", *, long: bool | None = None) -> list[Task]:
    tasks = list(load().values())
    cats = {c for c in category.split(",") if c}
    ids = {i for i in only.split(",") if i}
    unknown = (cats - {t.category for t in tasks}) | (ids - {t.id for t in tasks})
    if unknown:
        raise ValueError("unknown selection: " + ",".join(sorted(unknown)))
    return [t for t in tasks if (not cats or t.category in cats) and (not ids or t.id in ids) and (long is None or t.long == long)]


def outcome_ok(name: str, outcome) -> bool:
    t = REGISTRY.get(name)
    return t is not None and outcome in (t.expect + (REFUSALS if "refused" in t.expect else ()))


def normalize(name: str, outcome):
    """Map an acceptable engine outcome for this task (e.g. a truthful refusal) to 'done' for the pass gate."""
    if name not in REGISTRY:
        return outcome
    return "done" if outcome_ok(name, outcome) else "unexpected_" + str(outcome)


def install(trials: dict, run_dir: Callable[[], Path]) -> None:
    """Register every catalog task into the suite's TRIALS dict (name -> spec)."""
    state = {}

    def ctx_for(t):
        if t.id not in state:
            uid = uuid.uuid4().hex[:8]
            server = None
            if "server" in t.needs:
                from .fixture_server import FixtureServer
                server = FixtureServer(uid)
            state[t.id] = Ctx(run_dir() / t.id, uid=uid, server=server)
        return state[t.id]

    for t in load().values():
        def setup(t=t):
            c = ctx_for(t)
            c.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Immutable recovery metadata is committed before any server/app
            # effect. Interrupted QA can recover exact catalog-owned subjects
            # from task_id + uid without retaining task/body/recipient content.
            manifest = c.dir / "owned-fixture.json"
            try:
                fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                raise RuntimeError("qa_owned_scope_not_fresh") from None
            with os.fdopen(fd, "w") as output:
                json.dump({"schema_version": 1, "task_id": t.id, "uid": c.uid}, output)
                output.flush()
                os.fsync(output.fileno())
            parent_fd = os.open(c.dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            if c.server and not c.server.httpd:
                c.server.start()
                c.vars["base"] = c.server.base
            if t.setup:
                t.setup(c)

        def verify(t=t):
            c = ctx_for(t)
            return t.verify(c)

        def cleanup(t=t):
            c = ctx_for(t)
            for fn in reversed(c.cleanups):
                try:
                    if fn() is False:
                        c.cleanup_verified = False
                except Exception:
                    c.cleanup_verified = False
            c.cleanups.clear()
            if c.server:
                try:
                    c.server.close()
                    if c.server.thread and c.server.thread.is_alive():
                        c.cleanup_verified = False
                except Exception:
                    c.cleanup_verified = False
            return c.cleanup_verified

        def bind_backend(mac, t=t):
            if t.bind_backend:
                t.bind_backend(ctx_for(t), mac)

        trials[t.id] = dict(task=lambda t=t: fmt(t.task, ctx_for(t).vars), setup=setup, verify=verify,
                            cleanup=cleanup, category=t.category, expect=t.expect, bind_backend=bind_backend)
