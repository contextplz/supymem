"""Exact-repeat cache of model replies for read-only web steps (Stagehand-style, but replay-only and conservative).

A reply is reused only when the whole situation is identical: same goal, sub-goal and recent actions (intent), same
origin+path, same control signature and the same page text/field values (DOM hash). Any difference is a miss and the
model decides as usual. Only read-only/navigation actions are ever stored (navigate, new_tab, scroll, wait, click on a
link); typing, buttons, keys, submits and done/fail/needs_you are never cached. A reply is committed only after the next
observation showed the page changed, so a reply that did nothing is not remembered. Cached replies go through the same
parser, safety blocks and admission checks as a model reply.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

SAFE_OPS = {"navigate", "new_tab", "scroll", "wait"}
LINK_ROLES = {"a", "link"}
_TIMING = re.compile(r"\(\d+(\.\d+)?s\)")


def _h(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _page(obs):
    notes = getattr(obs, "notes", "") or ""
    if not notes.startswith("PAGE URL: "):
        return None
    url = notes.split("\n", 1)[0][len("PAGE URL: "):]
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    return parsed.scheme + "://" + parsed.netloc + parsed.path, notes.split("PAGE TEXT: ", 1)[-1]


def situation_key(payload) -> str | None:
    obs = payload.get("observation")
    page = _page(obs) if obs is not None else None
    if page is None:
        return None
    where, text = page
    elements = getattr(obs, "elements", [])
    signature = [(e.get("ax_role"), e.get("label"), e.get("name"), e.get("type")) for e in elements]
    state = [(e.get("value"), e.get("checked"), e.get("selected")) for e in elements]
    history = [_TIMING.sub("", line) for line in payload.get("history", [])[-3:]]
    intent = _h([payload.get("goal"), payload.get("subgoal"), history])
    return _h([intent, where, signature, _h([text, state])])


def cacheable(reply, obs) -> bool:
    actions = reply.get("actions") if isinstance(reply, dict) else None
    if not isinstance(actions, list) or not actions or len(actions) > 2:
        return False
    byid = {e.get("id"): e for e in getattr(obs, "elements", [])}
    for a in actions:
        op = a.get("op")
        if op in SAFE_OPS:
            continue
        if op == "click" and byid.get(a.get("element_id"), {}).get("ax_role") in LINK_ROLES:
            continue
        return False
    return True


class ActionCache:
    def __init__(self, path=None, ttl: float = 86400, limit: int = 500):
        self.path, self.ttl, self.limit = Path(path) if path else None, ttl, limit
        self.items: dict[str, dict] = {}
        self.pending = None   # (key, reply) awaiting proof that the page changed
        self.hits = self.misses = 0
        if self.path and self.path.is_file():
            try:
                self.items = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self.items = {}

    def _save(self):
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(dict(list(self.items.items())[-self.limit:])))
                self.path.chmod(0o600)
            except OSError:
                pass

    def wrap(self, decide):
        def cached(payload):
            key = situation_key(payload)
            if self.pending and key is not None and key != self.pending[0]:
                self.items[self.pending[0]] = {"reply": self.pending[1], "at": time.time()}
                self._save()
            self.pending = None
            hit = self.items.get(key) if key else None
            if hit and time.time() - hit["at"] < self.ttl:
                self.hits += 1
                return hit["reply"], {"in": 0, "out": 0, "cached": True}
            self.misses += 1
            reply, usage = decide(payload)
            if key and cacheable(reply, payload["observation"]):
                self.pending = (key, reply)
            return reply, usage
        return cached
