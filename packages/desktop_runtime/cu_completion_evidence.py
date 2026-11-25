"""Closed, content-free browser evidence admission for independent verification."""
import re


def validated_evidence(value):
    if not isinstance(value, dict) or set(value) != {'v', 'source', 'actions', 'responses'}:
        return None
    if type(value['v']) is not int or value['v'] != 1 or value['source'] != 'owned_browser_backend':
        return None
    if any(not isinstance(value[k], list) or len(value[k]) > 16 for k in ('actions', 'responses')):
        return None
    def integer(x):
        return type(x) is int and 0 < x <= 1_000_000_000
    def scope(x):
        return isinstance(x, str) and re.fullmatch('[a-f0-9]{32}', x)
    for item in value['actions']:
        if not isinstance(item, dict) or set(item) - {'seq', 'page_scope', 'action', 'status', 'element_index', 'before', 'after', 'action_input_matched', 'input_readback'}:
            return None
        if not integer(item.get('seq')) or not scope(item.get('page_scope')):
            return None
        if item.get('action') not in {'navigate', 'click', 'double_click', 'right_click', 'type', 'press', 'scroll', 'select', 'new_tab', 'switch_tab', 'close_tab', 'wait', 'extract', 'upload', 'back', 'forward', 'screenshot', 'other'} or item.get('status') not in {'ok', 'captcha', 'refused', 'error'}:
            return None
        if 'element_index' in item and (type(item['element_index']) is not int or not 0 <= item['element_index'] <= 1_000_000_000):
            return None
        if 'action_input_matched' in item and type(item['action_input_matched']) is not bool:
            return None
        if 'input_readback' in item and item['input_readback'] != 'unavailable':
            return None
        for field in ('before', 'after'):
            if field not in item:
                continue
            state = item[field]
            if not isinstance(state, dict) or set(state) != {'field_type', 'checked'} or state['field_type'] not in {'checkbox', 'radio', 'text', 'email', 'password', 'search', 'number', 'textarea', 'select-one', 'other'} or (state['checked'] is not None and type(state['checked']) is not bool):
                return None
    for item in value['responses']:
        if not isinstance(item, dict) or set(item) != {'action_seq', 'page_scope', 'method', 'status', 'resource'}:
            return None
        if not integer(item['action_seq']) or not scope(item['page_scope']) or item['method'] not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'OTHER'} or type(item['status']) is not int or not 100 <= item['status'] <= 599 or item['resource'] not in {'document', 'xhr', 'fetch', 'other'}:
            return None
    return value
