"""Explicit UTF-8 verification budgets; never split a source receipt envelope."""
import json

MAX_CONTEXT_BYTES = 5000
MAX_REQUEST_BYTES = 10240
# Tasks that visit more than three pages carry their evidence across pages: the budgets widen (the browser adapter marks
# such a readback with WIDE_MARK), still bounded and still with explicit omission markers.
WIDE_MARK = "MULTI-PAGE TASK"
WIDE_CONTEXT_BYTES = 8000
WIDE_REQUEST_BYTES = 14000


def is_wide(*texts):
    return any(WIDE_MARK in str(t or "") for t in texts)


def bounded(text, limit, label):
    text = str(text or '')
    raw = text.encode('utf-8')
    if len(raw) <= limit:
        return text
    marker = '\n[' + label + ' truncated; omitted content is not verified]'
    return raw[:max(0, limit - len(marker.encode()))].decode('utf-8', 'ignore') + marker


def compose(remaining, file_evidence, owned_evidence, memory):
    from desktop_runtime.cu_completion_evidence import validated_evidence
    browser = ''
    if owned_evidence:
        try:
            value = validated_evidence(json.loads(owned_evidence.split('\n')[-1]))
            if value is not None:
                # Consume a copy; never change the source-owned history.
                value = {**value, 'actions': list(value['actions']), 'responses': list(value['responses'])}
                omitted = False
                encoded = json.dumps(value, separators=(',', ':'), sort_keys=True)
                while len(encoded.encode()) > (2600 if is_wide(file_evidence) else 1800):
                    group = 'actions' if len(value['actions']) > len(value['responses']) else 'responses'
                    if not value[group]:
                        break
                    value[group].pop(0)
                    omitted = True
                    encoded = json.dumps(value, separators=(',', ':'), sort_keys=True)
                browser = ('SOURCE-OWNED TRANSPORT/FIELD READBACK (input match is not goal proof; HTTP success is not business acceptance):\n'
                           + encoded + ('\n[older whole browser receipts omitted]' if omitted else ''))
        except (ValueError, TypeError, AttributeError):
            browser = ''
    file_heading = 'FILE EVIDENCE (fresh readback; existence does not prove task history):\n'
    planned = 'REMAINING PLANNED CHECKS (hints, not completion proof):\n' + bounded('\n'.join(remaining), 200, 'planned checks')
    notes = 'AGENT NOTES (untrusted claims, not proof):\n' + bounded(memory, 100, 'agent notes')
    # Receipt sizes vary when whole old rows are omitted. Reserve the complete
    # JSON and all section headings first, then use available capacity for facts.
    wide = is_wide(file_evidence)
    cap = WIDE_CONTEXT_BYTES if wide else MAX_CONTEXT_BYTES
    file_budget = min(4800 if wide else 2600, cap - len('\n'.join([browser, file_heading, planned, notes]).encode()))
    context = '\n'.join([browser, file_heading + bounded(file_evidence, file_budget, 'file evidence'), planned, notes])
    # Each section has its own budget; never truncate the already-valid JSON.
    assert len(context.encode()) <= cap
    return context
