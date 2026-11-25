import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(os.environ.get("LOCAL_API_TEST_ROOT", Path(__file__).resolve().parents[2]))


@pytest.mark.parametrize("name,idea", [
    ("Inventory planner", "Forecast parts demand for independent repair shops"),
    ("Garden journal", "Help apartment gardeners track watering and plant growth"),
])
def test_first_open_without_optional_providers(tmp_path, name, idea):
    environment = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
    environment.update(
        HOME=str(tmp_path), SUPYMEM_MODE="local", APP_ENV="local",
        COMPANY_OS_DATA_DIR=str(tmp_path / "data"),
        COMPANY_OS_DEPLOYMENT_CONFIG=str(ROOT / "config/deployment.example.json"),
        GUARDRAILS_MODE="deterministic_only", LLM_OFFLINE="1", LLM_CASSETTE_MODE="replay",
    )
    result = subprocess.run(
        [os.environ.get("LOCAL_API_TEST_PYTHON", sys.executable), "-c", """
import importlib
from pathlib import Path
import sys

root, name, idea = sys.argv[1:]
sys.path[:0] = [str(Path(root) / 'services/company_os'), str(Path(root) / 'packages')]
for package in ('llm_adapter', 'cursor_sdk', 'agent_reach', 'gpt_researcher',
                'chromadb', 'stripe', 'google', 'facebook_business'):
    for module in list(sys.modules):
        if module == package or module.startswith(package + '.'):
            del sys.modules[module]
    sys.modules[package] = None

from app.main import create_app
from app.api import HOSTED_ONLY_MODULES, ROUTER_MODULES
application = create_app()
for module in ROUTER_MODULES + ('local_settings', 'local_capabilities_api'):
    if module not in HOSTED_ONLY_MODULES:
        importlib.import_module('app.api.' + module)

from fastapi.testclient import TestClient
from app.core.local_owner import ensure_owner, _ensure_proxy_secret
from core.local_security import _proxy_secret, console_origin
from app.workspaces.service import create_workspace
from app.db.models import Venture
from app.state import db
from core.clock import utcnow
from core.ids import new_id

owner = ensure_owner()
workspace = create_workspace(kind='venture', name=name, owner_id=owner.id)
venture_id = new_id('ven')
with db()() as session:
    session.add(Venture(id=venture_id, workspace_id=workspace.id, name=name,
                       raw_idea=idea, stage='idea', created_at=utcnow(), updated_at=utcnow()))
    session.commit()
_ensure_proxy_secret()
with TestClient(application, base_url='http://127.0.0.1:3300', client=('127.0.0.1', 50000),
                headers={'X-Local-Proxy-Secret': _proxy_secret().decode('ascii'),
                         'Origin': console_origin()}) as client:
    for route, expected in [
        ('/ventures', 200), (f'/ventures/{venture_id}', 200),
        (f'/ventures/{venture_id}/chat', 200), ('/local/capabilities', 200),
        ('/coding/agents', 200), ('/local-tools/coding-agents', 200),
        (f'/ventures/{venture_id}/device-work-orders', 200),
        (f'/workspaces/{workspace.id}/site', 404),
        (f'/ventures/{venture_id}/dashboard', 200),
        (f'/ventures/{venture_id}/onboarding', 200),
        (f'/ventures/{venture_id}/idea/sessions', 200),
        (f'/ventures/{venture_id}/plans', 200), (f'/ventures/{venture_id}/apps', 200),
        (f'/workspaces/{workspace.id}/jobs?venture_id={venture_id}&active_only=true&limit=10', 200),
    ]:
        response = client.get(route)
        assert response.status_code == expected, (route, response.status_code)
        print(route, response.status_code, flush=True)
    assert client.get(f'/ventures/{venture_id}').json()['name'] == name
    assert client.get(f'/ventures/{venture_id}/chat').json()['items'] == []

    from app.chat.store import append_message
    from app.firstlook.serve import site_dir
    from app.firstlook.versions import persist
    from hashlib import sha256
    append_message(venture_id, role='user', body_md=idea)
    page = site_dir(workspace_id=workspace.id) / 'index.html'
    page.parent.mkdir(parents=True, exist_ok=True)
    html = '<!doctype html><html><title>' + name + '</title><body>' + idea + '</body></html>'
    page.write_text(html)
    digest = sha256(html.encode()).hexdigest()
    persist(workspace_id=workspace.id, venture_id=venture_id, stage='first_look',
            template='external', archetype='tool', brandkit_hash=digest, copy_hash=digest,
            copy={}, brandkit={}, variant_index=0, path=str(page), url=None, sha256=digest)
    for route in ['/ventures', f'/ventures/{venture_id}', f'/ventures/{venture_id}/dashboard',
                  f'/workspaces/{workspace.id}/site?venture_id={venture_id}',
                  f'/ventures/{venture_id}/chat']:
        response = client.get(route)
        assert response.status_code == 200, (route, response.status_code)
    assert client.get(f'/ventures/{venture_id}/chat').json()['items'][0]['body_md'] == idea
""", str(ROOT), name, idea],
        env=environment, text=True, capture_output=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
