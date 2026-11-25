import httpx
import pytest

from tests.integration.test_postgres_auth_security import service_modules


@pytest.fixture()
def client_module(monkeypatch):
    with service_modules("company_os"):
        from app.builder import task_client as m
        monkeypatch.setattr(m.intent_bridge, "_context", lambda *args: ("http://10.91.0.11:8000", "fixture-control-token", {}))
        yield m


def response_client(m, monkeypatch, response):
    def handle(request):
        assert request.url.host == "10.91.0.11"
        assert request.headers["authorization"] == "Bearer fixture-control-token"
        if response.is_stream_consumed:
            return httpx.Response(response.status_code, headers=response.headers, stream=httpx.ByteStream(response.content))
        return response
    monkeypatch.setattr(m.intent_bridge, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))


def test_absolute_task_response_deadline(client_module, monkeypatch):
    m = client_module
    response_client(m, monkeypatch, httpx.Response(200, stream=httpx.ByteStream(b"{}"), headers={"content-type": "application/json"}))
    ticks = iter([0, 31])
    monkeypatch.setattr(m.time, "monotonic", lambda: next(ticks))
    with pytest.raises(m.BuildModelError):
        m.state("bld_fixture", 1)


def test_small_slow_chunks_are_checked_before_large_buffer_fills(client_module, monkeypatch):
    m = client_module
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"{"
            yield b"}"
            raise AssertionError("deadline must stop before another chunk")
    response_client(m, monkeypatch, httpx.Response(200, stream=Stream(), headers={"content-type": "application/json"}))
    ticks = iter([0, 1, 15, 31])
    monkeypatch.setattr(m.time, "monotonic", lambda: next(ticks))
    with pytest.raises(m.BuildModelError):
        m.state("bld_fixture", 1)


@pytest.mark.parametrize("headers", [{"content-length": "262145"}, {"content-length": "-1"}, {"content-length": "8"}, {"content-encoding": "gzip"}])
def test_invalid_length_or_encoding_rejected(client_module, monkeypatch, headers):
    m = client_module
    response_client(m, monkeypatch, httpx.Response(200, stream=httpx.ByteStream(b"{}"), headers={"content-type": "application/json", **headers}))
    with pytest.raises(m.BuildModelError):
        m.state("bld_fixture", 1)


def test_foreign_bound_task_state_is_rejected(client_module, monkeypatch):
    m = client_module
    response_client(m, monkeypatch, httpx.Response(200, json={"build_id": "bld_foreign", "generation": 1, "persistence_available": True, "commands": []}))
    with pytest.raises(m.BuildModelError, match="scope_invalid"):
        m.state("bld_fixture", 1)


@pytest.mark.parametrize("status", [409, 413, 422])
def test_source_seed_rejection_is_deterministic(client_module, monkeypatch, status):
    m = client_module
    client_class = httpx.Client
    monkeypatch.setattr(m.httpx, "Client", lambda **_kwargs: client_class(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"detail": "invalid"}))))
    with pytest.raises(m.BuildModelError, match="build_source_seed_rejected"):
        m.seed_source("bld_fixture", 1, profile="founder_product_v1",
                      source_artifact_id="bar_fixture", sha256="a" * 64,
                      manifest=[{"path": "product/routes.py", "size_bytes": 1, "sha256": "b" * 64}], blob=b"x")
