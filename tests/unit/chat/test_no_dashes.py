"""Founders never see em or en dashes: model-authored rows are cleaned on write, user rows are not."""
import json
import re
from pathlib import Path

from tests.company_os_harness import load_company_os_app
from tests.unit.chat.test_assistant_routing import env  # noqa: F401

DASH = re.compile("[\u2013\u2014]")


def test_assistant_rows_stored_without_dashes_user_rows_untouched(env):
    _, vid = env
    from app.db.models import ChatMessage
    from app.state import db
    from core.ids import new_id
    from core.clock import utcnow

    rows = {}
    with db()() as s:
        for role in ("assistant", "user"):
            s.add(ChatMessage(id=new_id("msg"), venture_id=vid, role=role, kind="text",
                body_md="Lost orders upset customers\u2014arguably the messiest, 5\u201310 days",
                actions_json=json.dumps([{"label": "Pick this \u2014 it fits", "href": "/v/x"}]), created_at=utcnow()))
        s.commit()
        for m in s.query(ChatMessage).filter(ChatMessage.venture_id == vid).all():
            rows[m.role] = m
        assert not DASH.search(rows["assistant"].body_md) and "5-10 days" in rows["assistant"].body_md
        assert not DASH.search(rows["assistant"].actions_json)
        assert DASH.search(rows["user"].body_md)


def test_email_templates_and_chat_builders_have_no_dash_literals():
    load_company_os_app()
    root = Path(__file__).resolve().parents[3] / "services/company_os/app"
    files = [root / "email/templates.py", *sorted((root / "chat").glob("*.py"))]
    import ast
    bad = []
    for f in files:
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and DASH.search(node.value):
                if re.search(r"\\[sSdD]|\[[^\]]*[\u2013\u2014]|\(\?", node.value) or not re.search("[A-Za-z]", node.value) or node.lineno == 1:
                    continue  # regex or character-class literals match dashes, they do not emit them
                bad.append(f"{f.name}:{node.lineno}")
    assert not bad, bad
