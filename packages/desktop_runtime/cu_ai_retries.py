"""Bound recipient/action-family retries using observed effects, never model claims.

AX and pixel click aliases share a budget for the same owned recipient. Public
feedback uses fixed codes; identifiers and content are only local hashed state.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field

EDITABLE_ROLES = frozenset({'AXTextArea', 'AXTextField', 'AXSearchField', 'AXCheckBox', 'AXRadioButton',
    'AXSlider', 'AXPopUpButton', 'AXComboBox', 'textbox', 'input', 'textarea', 'checkbox', 'radio', 'select', 'combobox'})
CLICK_OPS = frozenset({'click', 'double_click', 'right_click'})
FAMILIES = CLICK_OPS | frozenset({'drag', 'type', 'key', 'menu', 'scroll', 'open_path', 'open_app',
    'switch_app', 'navigate', 'new_tab', 'switch_tab', 'hover', 'upload', 'extract', 'template', 'script', 'fs_mkdir', 'fs_move', 'fs_copy', 'fs_rename',
    'fs_write_text', 'fs_read_text', 'fs_list', 'done'})


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def route(action, obs, mac) -> tuple[str, str]:
    family = 'click' if action.op in CLICK_OPS else action.op
    if family == 'fs':
        family += '_' + action.fs_verb
        recipient = ('fs', action.fs_path)
    elif family == 'template':
        recipient = ('template', action.chord)
    else:
        recipient = (obs.frontmost, obs.pid, getattr(mac, 'shot_window', None), getattr(mac, 'ax_window', None),
                     _tab_state(obs)[1])
        if obs.frontmost == 'Chrome (browser)':
            if family == 'type':
                element = next((item for item in obs.elements if item.get('id') == action.element_id), {})
                recipient += (element.get('document_scope'), element.get('form_scope'),
                              element.get('name') or element.get('id'), action.text)
            elif action.op == 'click':
                key_of = getattr(mac, 'submit_key', None)
                try:
                    submit = key_of(action, obs) if callable(key_of) else None
                except Exception:
                    submit = None
                if submit is not None:
                    recipient += (submit,)
    return _hash((recipient, family)), family


def _tab_state(obs):
    value = getattr(obs, 'browser_tab_state', None)
    if (type(value) is tuple and len(value) == 2 and type(value[0]) is int and value[0] > 0
            and type(value[1]) is str and len(value[1]) == 32):
        return value
    return (None, None)


def semantic_state(obs) -> str:
    """Ignore PNG/IDs/positions/clock labels; use recipient and meaningful AX/DOM state.

    This is route progress, not goal completion. A new role structure or editable
    value/selection state is observable progress; task success still needs its
    independent verifier. Raw contents never enter public feedback/checkpoints.
    """
    roles = Counter()
    values = []
    for position, element in enumerate(obs.elements):
        role = str(element.get('ax_role') or element.get('role') or '')
        if not role:
            continue
        roles[role] += 1
        if role in EDITABLE_ROLES or element.get('checked') is not None or element.get('selected') is not None or element.get('focused') is not None:
            identity = (role, element.get('name'), element.get('type'), position)
            values.append((identity, element.get('value') if role in EDITABLE_ROLES else None,
                           element.get('checked'), element.get('selected'), element.get('focused'), element.get('is_enabled')))
    page_url = obs.notes.split('\n', 1)[0] if obs.notes.startswith('PAGE URL: ') else ''
    # Web page text is progress too (a counter or list changing without any control changing); only browser notes carry it.
    page_text = obs.notes.split('PAGE TEXT: ', 1)[1] if page_url and 'PAGE TEXT: ' in obs.notes else ''
    return _hash((obs.frontmost, obs.pid, page_url, _tab_state(obs), sorted(roles.items()), sorted(values, key=repr), page_text))


def failure_code(text: str) -> str | None:
    public = text.split('§', 1)[0]
    # Match authoritative prefixes, not labels/content containing failure words.
    if public.startswith(('refused:', 'error:', 'script error', 'fs error')):
        return 'action_refused_or_failed'
    if public.startswith(('cancelled', 'fs cancelled', 'script cancelled', 'script timed out', 'launch_focus_uncertain')) or '; launch_focus_uncertain;' in public:
        return 'action_effect_uncertain'
    return None


@dataclass
class RetryGuard:
    counts: dict[str, int] = field(default_factory=dict)
    blocked: set[str] = field(default_factory=set)
    recovery_used: bool = False
    seen_states: dict[str, set[str]] = field(default_factory=dict)
    pending: list[tuple[str, str, str]] = field(default_factory=list)

    def rejected(self, key: str) -> bool:
        return key in self.blocked

    def failed(self, key: str, family: str, code: str) -> str | None:
        count = self.counts.get(key, 0) + 1
        self.counts[key] = min(count, 3)
        if count >= 3 or len(self.counts) > 128:
            self.blocked.add(key)
            return family
        return None

    def observe(self, obs) -> str | None:
        current = semantic_state(obs)
        exhausted = None
        for key, family, before in self.pending:
            seen = self.seen_states.setdefault(key, {before})
            if current == before or current in seen:
                exhausted = self.failed(key, family, 'action_no_observed_effect') or exhausted
            else:
                self.counts.pop(key, None)
                seen.add(current)
        self.pending.clear()
        return exhausted

    def snapshot(self) -> dict:
        return {'counts': dict(self.counts), 'blocked': sorted(self.blocked), 'recovery_used': self.recovery_used,
                'pending': [list(p) for p in self.pending], 'seen_states': {k: sorted(v) for k, v in self.seen_states.items()}}

    def restore(self, value) -> bool:
        if value is None:
            return True  # older same-task checkpoints carry original step/time caps
        if not isinstance(value, dict):
            return False
        counts, blocked, pending = value.get('counts'), value.get('blocked'), value.get('pending', [])
        def key_ok(key):
            return isinstance(key, str) and len(key) == 64 and all(c in '0123456789abcdef' for c in key)
        if (not isinstance(counts, dict) or len(counts) > 129 or any(not key_ok(k) or type(v) is not int or not 1 <= v <= 3 for k, v in counts.items())
                or not isinstance(blocked, list) or len(blocked) > 129 or any(not key_ok(k) for k in blocked)
                or type(value.get('recovery_used')) is not bool or not isinstance(pending, list) or len(pending) > 4
                or any(not isinstance(p, list) or len(p) != 3 or not key_ok(p[0]) or not isinstance(p[1], str)
                       or p[1] not in FAMILIES or not key_ok(p[2]) for p in pending)):
            return False
        seen = value.get('seen_states', {})
        if (not isinstance(seen, dict) or len(seen) > 129 or any(not key_ok(k) or not isinstance(v, list)
                or len(v) > 4096 or any(not key_ok(item) for item in v) for k, v in seen.items())):
            return False
        self.seen_states = {k: set(v) for k, v in seen.items()}
        self.counts, self.blocked = dict(counts), set(blocked)
        self.recovery_used, self.pending = value['recovery_used'], [tuple(p) for p in pending]
        return True



def acknowledged_effect(action, result: str) -> bool:
    """Closed dispatch acknowledgement, not semantic/whole-task success.

    Status is generated by the owned adapter after its checked operation; user
    labels or model summaries do not decide success. Ambiguous generic 'ok' is
    excluded. The independent goal verifier and semantic retry proof remain.
    """
    public = result.split('§', 1)[0]
    if failure_code(public):
        return False
    op = action.op
    if op == 'fs':
        return action.fs_verb in {'mkdir', 'move', 'copy', 'rename', 'write_text'} and public == f'fs {action.fs_verb} ok'
    if op == 'template':
        return public.startswith('script ok (')
    native = {'click': ('click ', 'clicked '), 'double_click': ('double_click ',),
              'right_click': ('right_click ',), 'type': ('typed ', 'set '),
              'key': ('pressed ',), 'drag': ('dragged ',), 'menu': ('menu ',),
              'scroll': ('scrolled',), 'open_path': ('opened ',)}
    if any(public.startswith(prefix) for prefix in native.get(op, ())):
        return True
    browser = {'click': 'click', 'double_click': 'click', 'right_click': 'click', 'type': 'type',
               'navigate': 'navigate', 'new_tab': 'new_tab', 'switch_tab': 'switch_tab', 'key': 'press', 'scroll': 'scroll',
               'hover': 'hover', 'upload': 'upload', 'extract': 'extract'}
    return op in browser and public.startswith(browser[op] + ' ok (')


def completion_context(mac) -> str:
    """Only a closed source-owned receipt schema may reach the text verifier."""
    reader = getattr(mac, "completion_evidence", None)
    if not callable(reader):
        return ""
    try:
        value = reader()
        from desktop_runtime.cu_completion_evidence import validated_evidence
        value = validated_evidence(value)
        if value is None:
            return ""
        return "\nSOURCE-OWNED TRANSPORT/FIELD READBACK (input match is not goal proof; HTTP success is not business acceptance):\n" + json.dumps(value, sort_keys=True)
    except Exception:
        return ""


def completion_key(obs, file_evidence: str, owned_evidence: str) -> str:
    # Claims, agent notes, screenshots, IDs and clocks never refresh this budget.
    # Sequence numbers are bookkeeping, not progress. Repeated receipts dedupe.
    try:
        envelope = json.loads(owned_evidence.split("\n")[-1]) if owned_evidence else {}
        receipts = sorted({_hash({k: v for k, v in item.items() if k not in {"seq", "action_seq"}})
                           for group in ("actions", "responses") for item in envelope.get(group, [])})
    except (ValueError, TypeError, AttributeError):
        receipts = []
    return _hash(("completion", semantic_state(obs), file_evidence, receipts))
