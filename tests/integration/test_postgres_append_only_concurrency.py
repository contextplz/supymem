"""Concurrent processes installing append-only triggers must not race."""
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine

URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="needs TEST_POSTGRES_URL")


def test_parallel_trigger_installs_do_not_fail():
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.db.append_only import install_append_only_triggers
    from app.db.models import Base
    engine = create_engine(URL)
    Base.metadata.create_all(engine)
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda _: install_append_only_triggers(create_engine(URL)), range(16)))
