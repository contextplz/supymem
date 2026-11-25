import json

from scripts.generate_chat_contract import FIXTURES, action_shapes, fixtures
from tests.company_os_harness import load_company_os_app


def test_console_chat_fixtures_cover_server_wire_contract(isolate_app_imports):
    load_company_os_app(reset=False)
    committed = json.loads(FIXTURES.read_text())
    current = json.loads(json.dumps(fixtures()))
    kinds, shapes = set(current["kinds"]), {tuple(shape) for shape in current["action_shapes"]}
    hint = "Add a wire fixture and regenerate with scripts/generate_chat_contract.py"
    assert kinds == set(committed["kinds"]), hint
    assert shapes == {tuple(shape) for shape in committed["action_shapes"]}, hint
    messages = [case["message"] for case in committed["cases"]]
    assert kinds <= {message["kind"] for message in messages}, hint
    assert shapes <= set().union(*(action_shapes(message) for message in messages)), hint
    assert committed == current, hint
