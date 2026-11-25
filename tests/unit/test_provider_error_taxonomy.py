from llm_gateway.errors import classify_provider_error


class _Exc(Exception):
    def __init__(self, status_code: int, msg: str = "x"):
        super().__init__(msg)
        self.status_code = status_code


def test_classify_bad_key():
    err = classify_provider_error(_Exc(401))
    assert err.code == "bad_api_key"


def test_classify_rate_limit():
    err = classify_provider_error(_Exc(429))
    assert err.code == "rate_limited"
    assert err.retryable


def test_classify_credits():
    err = classify_provider_error(_Exc(402, "Payment required"))
    assert err.code == "out_of_credits"


def test_quota_text_in_runtime_error_does_not_imply_insufficient_funds():
    err = classify_provider_error(RuntimeError("quota counter bug"))
    assert err.code == "provider_error"
    assert err.status is None
    assert not err.retryable


def test_quota_text_in_http_500_preserves_provider_failure():
    err = classify_provider_error(_Exc(500, "quota stats invalid"))
    assert err.code == "provider_down"
    assert err.status == 500
    assert err.retryable
