"""Explicit original-task repetition limits, never page/model-derived authority."""
from __future__ import annotations

import re
from dataclasses import dataclass


_PATTERN = re.compile(
    r'\b(?:press|click|tap)\s+(?:the\s+)?'
    r'(?P<label>"[^"\n]{1,120}"|\'[^\'\n]{1,120}\'|“[^”\n]{1,120}”|‘[^’\n]{1,120}’|[^,;\n.!?:]{1,120}?)'
    r'\s+(?:button\s+)?exactly\s+(?P<count>\d{1,6})\s+times\b', re.I)


def normalized_label(value):
    return ' '.join(str(value or '').split()).casefold()


def is_button(element):
    return (str(element.get('ax_role') or element.get('role') or '').casefold() in {'button', 'axbutton'}
            or element.get('type') in {'button', 'submit', 'reset'})


def button_label(element):
    return normalized_label(element.get('label') or element.get('title') or element.get('text'))


@dataclass
class ExactSubmissionBudget:
    """A maximum number of observed form deliveries, not business-effect proof."""
    label: str = '__original_task_form_submission__'
    limit: int = 1
    used: int = 0

    def admission(self, action, elements):
        target = next((e for e in elements if e.get('id') == action.element_id), None)
        candidates = [e for e in elements if e.get('submits_form') is True and e.get('form_scope')
                      and e.get('is_enabled') is not False]
        if action.op in {'script', 'template'}:
            return 0, 'exact_submission_opaque_route_refused'
        if action.op in {'click', 'double_click', 'right_click', 'type'} and target is not None:
            if target.get('opaque_activation') is True or str(target.get('ax_role') or '').casefold() not in {
                    'a', 'link', 'button', 'input', 'select', 'textarea', 'summary', 'option'}:
                return 0, 'exact_submission_opaque_activation_refused'
        if action.op in {'click', 'double_click', 'right_click'}:
            if target is None:
                return 0, 'exact_submission_requires_observed_control_id'
            if target.get('submits_form') is not True:
                return (0, 'exact_submission_recipient_unknown') if is_button(target) else (0, None)
            if action.op == 'right_click':
                return 0, 'exact_submission_requires_primary_input'
            cost = 2 if action.op == 'double_click' else 1
            scope = target.get('form_scope')
        elif action.op == 'key':
            chords = action.chord.casefold().split()
            enter = sum(c.split('+')[-1] in {'enter', 'return'} for c in chords)
            space = sum(c.split('+')[-1] in {'space', 'spacebar'} for c in chords)
            focused = [e for e in elements if e.get('focused') is True]
            if any(e.get('opaque_activation') is True or str(e.get('ax_role') or '').casefold() not in {
                    'a', 'link', 'button', 'input', 'select', 'textarea', 'summary', 'option'} for e in focused):
                return 0, 'exact_submission_opaque_activation_refused'
            if not enter and not space:
                return 0, None
            if len(focused) != 1:
                return 0, 'exact_submission_keyboard_recipient_unknown'
            target = focused[0]
            if target.get('opaque_activation') is True or str(target.get('ax_role') or '').casefold() not in {
                    'a', 'link', 'button', 'input', 'select', 'textarea', 'summary', 'option'}:
                return 0, 'exact_submission_opaque_activation_refused'
            if target.get('ax_role') == 'textarea':
                return ((0, 'exact_submission_keyboard_recipient_unknown') if any('+' in c for c in chords)
                        else (0, None))  # Plain textarea Enter inserts a newline.
            if not enter and target.get('submits_form') is not True:
                return ((0, 'exact_submission_recipient_unknown') if is_button(target) else (0, None))
            scope = target.get('form_scope')
            if not scope:
                return 0, 'exact_submission_keyboard_recipient_unknown'
            cost = enter + (space if target.get('submits_form') is True else 0)
        else:
            return 0, None
        if len(candidates) != 1 or candidates[0].get('form_scope') != scope:
            return 0, 'exact_submission_recipient_ambiguous'
        if self.used + cost > self.limit:
            return 0, 'exact_submission_limit_reached'
        return cost, None


@dataclass
class ExactButtonBudget:
    label: str
    limit: int
    used: int = 0

    def admission(self, action, elements):
        """(input cost, refusal); a matching recipient must be exact and unique."""
        candidates = [e for e in elements if is_button(e) and button_label(e) == self.label]
        target = next((e for e in elements if e.get('id') == action.element_id), None)
        if action.op in {'click', 'double_click', 'right_click'}:
            if not action.element_id:
                return 0, 'exact_repetition_requires_observed_control_id'
            if target is None or not is_button(target) or button_label(target) != self.label:
                return 0, None
            if action.op == 'right_click':
                return 0, 'exact_repetition_requires_primary_button_input'
            cost = 2 if action.op == 'double_click' else 1
        elif action.op == 'key' and any(chord.split('+')[-1] in {'enter', 'return', 'space'} for chord in action.chord.casefold().split()):
            focused = [e for e in elements if e.get('focused') is True]
            if len(focused) != 1:
                return 0, 'exact_repetition_keyboard_recipient_unknown'
            if not is_button(focused[0]):
                return 0, 'exact_repetition_keyboard_recipient_unknown' if candidates else None
            if button_label(focused[0]) != self.label:
                return 0, None
            cost = sum(chord.split('+')[-1] in {'enter', 'return', 'space'} for chord in action.chord.casefold().split())
        else:
            return 0, None
        if len(candidates) != 1:
            return 0, 'exact_repetition_recipient_ambiguous'
        if self.used + cost > self.limit:
            return 0, 'exact_repetition_limit_reached'
        return cost, None


def exact_button_budgets(goal):
    budgets = []
    for match in _PATTERN.finditer(goal):
        label = match.group('label').strip()
        quotes = {'"':'"', "'":"'", '“':'”', '‘':'’'}
        if label[:1] in quotes:
            if label[-1:] != quotes[label[0]]:
                raise ValueError('exact_repetition_task_ambiguous')
            label = label[1:-1]
        elif label[-1:] in set(quotes.values()):
            raise ValueError('exact_repetition_task_ambiguous')
        elif label.casefold().endswith(' button'):
            label = label[:-7].strip()
        label = normalized_label(label)
        if any(b.label == label for b in budgets):
            raise ValueError('exact_repetition_task_ambiguous')
        budgets.append(ExactButtonBudget(label, int(match.group('count'))))
    # Authority comes solely from the original task. A page label or model note
    # cannot create or enlarge this quota. An acknowledged validation rejection
    # still consumed the attempt; only proven pre-dispatch failure restores it.
    submissions = re.findall(r'\bsubmit(?:\s+(?:it|the\s+form))?\s+(once|exactly\s+\d{1,6}\s+times)\b', goal, re.I)
    if len(submissions) > 1:
        raise ValueError('exact_submission_task_ambiguous')
    if submissions:
        count = 1 if submissions[0].casefold() == 'once' else int(re.search(r'\d+', submissions[0])[0])
        budgets.append(ExactSubmissionBudget(limit=count))
    return budgets
