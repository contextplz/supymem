"""Provider choices survive concurrent requests on migrated PostgreSQL."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_auth_security import pg_database as pg_database


def test_concurrent_first_service_choices_preserve_both_categories(journey_db):
    from app.chat.service_guides import select_service
    from app.db.models import VentureDocument
    from app.tenancy import bind

    ready = Barrier(2)

    def choose(category, provider):
        with bind(workspace_id=journey_db['wid'], venture_id=journey_db['vid'], actor=journey_db['actor']):
            ready.wait(timeout=5)
            return select_service(journey_db['vid'], {'category': category, 'provider': provider})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(choose, 'hosting', 'netlify')
        second = pool.submit(choose, 'backend', 'supabase')
        assert first.result(timeout=20)['kind'] == 'text'
        assert second.result(timeout=20)['kind'] == 'text'
    with journey_db['factory']() as session:
        row = session.get(VentureDocument, f"service-plan:{journey_db['vid']}")
        assert json.loads(row.body_md) == {'hosting': 'netlify', 'backend': 'supabase'}
        assert json.loads(row.provenance_json)['verified'] is False
