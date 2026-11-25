import pytest


@pytest.fixture
def client(base_client):
    from app.core.local_mode import is_local
    from app.core.ratelimit import limiter

    limiter.reset()
    if is_local():
        base_client.headers["Origin"] = str(base_client.base_url).rstrip("/")
    yield base_client
    limiter.reset()
