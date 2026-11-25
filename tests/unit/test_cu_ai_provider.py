"""Provider routing uses local transports only; uncertainty must never replay."""
import httpx
import pytest

from coding_agents import cu_ai_provider as provider

BASE = "https://example-resource.openai.azure.com/openai/v1"
BODY = {"model": "gpt-6-luna", "service_tier": "default", "reasoning": {"effort": "none"},
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "observe"},
                  {"type": "input_image", "image_url": "data:image/png;base64,eA=="}]}]}


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_CUAI_API_KEY", "synthetic-azure-key")
    monkeypatch.setenv("OPENAI_DEVICE_VISION_API_KEY", "synthetic-openai-key")


@pytest.mark.parametrize("failure", [401, 403, 404, 429, 500, 503, "refused"])
def test_definite_no_output_failure_falls_back_once_with_same_model(keys, failure):
    calls = []
    def send(url, headers, body):
        calls.append((url, headers, body))
        if len(calls) == 1:
            if failure == "refused":
                raise httpx.ConnectError("connection refused")
            return failure, {"error": {"code": "unavailable"}}
        return 200, {"model": "gpt-6-luna"}
    value, served = provider.responses_call(send, BODY, provider="azure", endpoint=BASE, deployment="bound-deployment")
    assert served == "openai" and value["model"] == "gpt-6-luna" and len(calls) == 2
    assert calls[0] == (BASE + "/responses", {"api-key": "synthetic-azure-key"},
                        {key: value for key, value in {**BODY, "model": "bound-deployment"}.items() if key != "service_tier"})
    assert calls[1] == (provider.OPENAI_ENDPOINT, {"Authorization": "Bearer synthetic-openai-key"}, BODY)
    assert BODY["service_tier"] == "default"


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError,
                                  httpx.RemoteProtocolError, httpx.ConnectTimeout])
def test_uncertain_transport_never_falls_back(keys, error):
    sent = []
    def send(*args):
        sent.append(args)
        raise error("uncertain")
    with pytest.raises(error):
        provider.responses_call(send, BODY, provider="azure", endpoint=BASE, deployment="gpt-6-luna")
    assert len(sent) == 1


@pytest.mark.parametrize("value", [{"output": [{"text": "partial"}]}, {"usage": {"output_tokens": 1}},
                                  {"status": "incomplete"}, {"id": "resp_accepted"}, "unparseable"])
def test_http_error_with_possible_paid_output_never_falls_back(keys, value):
    sent = []
    def send(*args):
        sent.append(args)
        return 503, value
    with pytest.raises(ValueError, match="provider_failed"):
        provider.responses_call(send, BODY, provider="azure", endpoint=BASE, deployment="gpt-6-luna")
    assert len(sent) == 1


def test_azure_success_uses_api_key_and_does_not_fallback(keys):
    sent = []
    def send(*args):
        sent.append(args)
        return 200, {"model": "gpt-6-luna", "usage": {"output_tokens": 1}}
    assert provider.responses_call(send, BODY, provider="azure", endpoint=BASE, deployment="gpt-6-luna")[1] == "azure"
    assert len(sent) == 1


@pytest.mark.parametrize("error", [{"code": "content_filter"}, {"code": "ResponsibleAIPolicyViolation"},
                                  {"code": "invalid_request_error", "innererror": {"code": "content_filter"}},
                                  {"innererror": {"code": "ResponsibleAIPolicyViolation"}}])
def test_azure_filter_400_falls_back_once_with_reason(keys, error):
    sent, fallback = [], {}
    def send(url, headers, body):
        sent.append((url, headers, body))
        return (400, {"error": error}) if len(sent) == 1 else (200, {"model": "gpt-6-luna"})
    value, served = provider.responses_call(send, BODY, provider="azure", endpoint=BASE,
        deployment="gpt-6-luna", fallback_metadata=fallback)
    assert len(sent) == 2 and served == "openai" and value["model"] == BODY["model"]
    assert fallback == {"fallback_reason": "azure_content_filter"}


@pytest.mark.parametrize("value", [{"error": {"code": "invalid_request_error"}}, None,
    {"error": {"code": "not_content_filter"}}, {"error": {"code": ["content_filter"]}},
    *[{"error": {"code": "content_filter"}, **evidence} for evidence in (
        {"output": [{"text": "partial"}]}, {"usage": {"input_tokens": 1}}, {"id": "resp_x"}, {"status": "incomplete"})]])
def test_other_or_uncertain_400_never_falls_back(keys, value):
    sent, fallback = [], {}
    def send(*args):
        sent.append(args)
        return 400, value
    with pytest.raises(ValueError, match="provider_failed"):
        provider.responses_call(send, BODY, provider="azure", endpoint=BASE, deployment="gpt-6-luna", fallback_metadata=fallback)
    assert len(sent) == 1 and not fallback


def test_openai_filter_400_never_replays(keys):
    sent = []
    def send(*args):
        sent.append(args)
        return 400, {"error": {"code": "content_filter"}}
    with pytest.raises(ValueError, match="provider_failed"):
        provider.responses_call(send, BODY)
    assert len(sent) == 1


@pytest.mark.parametrize("failure", [400, "timeout"])
def test_content_filter_fallback_failure_never_triggers_a_third_call(keys, failure):
    sent, fallback = [], {}
    def send(*args):
        sent.append(args)
        if len(sent) == 1:
            return 400, {"error": {"code": "content_filter"}}
        if failure == "timeout":
            raise httpx.ReadTimeout("uncertain OpenAI output")
        return 400, {"error": {"code": "content_filter"}}
    with pytest.raises(httpx.ReadTimeout if failure == "timeout" else ValueError):
        provider.responses_call(send, BODY, provider="azure", endpoint=BASE, deployment="gpt-6-luna", fallback_metadata=fallback)
    assert len(sent) == 2 and fallback == {"fallback_reason": "azure_content_filter"}


def test_azure_error_metadata_exports_only_closed_codes_categories_and_booleans():
    value = {"error": {"code": "content_filter", "message": "PRIVATE_CONTENT", "innererror": {
        "code": "ResponsibleAIPolicyViolation", "content_filter_result": {
            "jailbreak": {"filtered": True, "detected": False, "message": "PRIVATE_CONTENT"},
            "hate": {"filtered": "PRIVATE_CONTENT", "severity": "PRIVATE_CONTENT"},
            "PRIVATE_CONTENT": {"filtered": True}}}}}
    assert provider.azure_error_metadata(value) == {"error_code": "content_filter",
        "innererror_code": "ResponsibleAIPolicyViolation", "content_filter_result": {
            "jailbreak": {"filtered": True, "detected": False}, "hate": {}}}
    assert provider.azure_error_metadata({"error": {"code": "PRIVATE_CONTENT", "innererror": []}}) == {"error_code": "other"}


@pytest.mark.parametrize("base", ["http://example-resource.openai.azure.com/openai/v1", "https://evil.invalid/openai/v1",
    "https://resource.openai.azure.com.evil.invalid/openai/v1", "https://key@resource.openai.azure.com/openai/v1",
    BASE + "?key=secret", BASE + "#fragment", BASE + "/responses", "https://resource.openai.azure.com:444/openai/v1"])
def test_unapproved_endpoint_refuses_before_transport(keys, base):
    with pytest.raises(ValueError, match="configuration_unavailable"):
        provider.responses_call(lambda *args: pytest.fail("unapproved endpoint called"), BODY,
                                provider="azure", endpoint=base, deployment="gpt-6-luna")


@pytest.mark.parametrize('second_failure', [False, True])
def test_proven_pre_output_timeout_falls_back_once(keys, second_failure):
    sent, metadata = [], {}
    def send(*args):
        sent.append(args)
        if len(sent) == 1:
            raise provider.NoOutputReadTimeout('no bytes')
        if second_failure:
            raise provider.NoOutputReadTimeout('no bytes')
        return 200, {'model': 'gpt-6-luna'}
    if second_failure:
        with pytest.raises(httpx.ReadTimeout):
            provider.responses_call(send, BODY, provider='azure', endpoint=BASE,
                deployment='gpt-6-luna', fallback_metadata=metadata)
    else:
        assert provider.responses_call(send, BODY, provider='azure', endpoint=BASE,
            deployment='gpt-6-luna', fallback_metadata=metadata)[1] == 'openai'
    assert len(sent) == 2
    assert metadata == {'fallback_reason': 'azure_pre_output_timeout', 'azure_usage_unconfirmed': True, 'fallback_attempted': True}


@pytest.mark.parametrize('partial', [False, True])
def test_transport_admits_timeout_fallback_only_before_any_bytes(keys, partial):
    requests, metadata = [], {}
    class Bytes(httpx.SyncByteStream):
        def __iter__(self):
            if partial:
                yield b'{"output":['
            raise httpx.ReadTimeout('private failure')
    def handle(request):
        requests.append(request)
        return httpx.Response(200, stream=Bytes()) if len(requests) == 1 else httpx.Response(200, json={'model':'gpt-6-luna'})
    with httpx.Client(transport=httpx.MockTransport(handle), timeout=httpx.Timeout(120, connect=5)) as client:
        def send(url, headers, body):
            status, raw = provider.read_response(client, url, headers, body)
            return status, __import__('json').loads(raw)
        if partial:
            with pytest.raises(httpx.ReadTimeout):
                provider.responses_call(send, BODY, provider='azure', endpoint=BASE, deployment='gpt-6-luna', fallback_metadata=metadata)
        else:
            assert provider.responses_call(send, BODY, provider='azure', endpoint=BASE, deployment='gpt-6-luna', fallback_metadata=metadata)[1] == 'openai'
    assert len(requests) == (1 if partial else 2)
    assert requests[0].extensions['timeout']['connect'] == 10
    assert requests[0].extensions['timeout']['read'] == 30
    if not partial:
        assert requests[1].extensions['timeout']['read'] == 120
        assert requests[1].extensions['timeout']['connect'] == 5
    else:
        assert not metadata


def test_openai_pre_output_timeout_never_replays_and_keeps_timeout(keys):
    sent = []
    def handle(request):
        sent.append(request)
        raise httpx.ReadTimeout('no response bytes')
    with httpx.Client(transport=httpx.MockTransport(handle), timeout=httpx.Timeout(120, connect=5)) as client:
        with pytest.raises(provider.NoOutputReadTimeout):
            provider.responses_call(lambda url, headers, body: provider.read_response(client, url, headers, body), BODY)
    assert len(sent) == 1 and sent[0].extensions['timeout'] == {'connect': 5, 'read': 120, 'write': 120, 'pool': 120}
