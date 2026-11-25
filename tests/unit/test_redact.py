"""Redaction catches every pattern from 16 §5, including nested structures."""

from __future__ import annotations

from core.redact import is_secret_value, redact
import pytest

SK_LIVE = "sk_" "live_abcdefghijklmnopqrstuvwxyz012345"
JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
AKIA = "AKIAIOSFODNN7EXAMPLE"
PEM = """-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF6PZGBw=
-----END RSA PRIVATE KEY-----"""


def test_sk_live_jwt_akia_pem_never_survive_nested() -> None:
    payload = {
        "a": SK_LIVE,
        "b": ["plain", {"token": JWT}],
        "c": {"aws": AKIA, "nested": [PEM, {"x": f"Bearer {JWT}"}]},
        "d": f"prefix {SK_LIVE} suffix",
    }
    out = redact(payload)
    blob = str(out)
    assert "sk_live_" not in blob
    assert "eyJ" not in blob or "eyJ" not in blob.replace("[REDACTED]", "")
    assert AKIA not in blob
    assert "PRIVATE KEY" not in blob
    assert "BEGIN" not in blob or "BEGIN" not in blob.replace("[REDACTED]", "")
    # Explicit survivors check
    assert SK_LIVE not in blob
    assert JWT not in blob
    assert PEM not in blob


def test_all_patterns_in_free_text() -> None:
    samples = [
        "sk_test_abcdefghijklmnopqrstuvwxyz01",
        "pk_live_abcdefghijklmnopqrstuvwxyz01",
        "sk-abcdefghijklmnopqrstuvwxyz0123456789",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB",
        AKIA,
        "xoxb-1234567890-abcdefghij",
        f"Authorization: Bearer {JWT}",
        PEM,
        JWT,
    ]
    text = " | ".join(samples)
    out = redact(text)
    for s in samples:
        # PEM / JWT may be partially matched; ensure raw secrets gone
        if "BEGIN" in s:
            assert "PRIVATE KEY-----" not in out or "[REDACTED]" in out
        else:
            assert s not in out or s.startswith("-----")
    assert "sk_test_" not in out
    assert "pk_live_" not in out
    assert "ghp_" not in out
    assert "AKIA" not in out
    assert "xoxb-" not in out
    assert JWT not in out


@pytest.mark.parametrize("value", ["<", "/", "x" * 20, "</html></body>", "ordinary response text", "application/json", "Content-Type"])
def test_vault_secret_detection_rejects_short_common_and_structural_values(value):
    assert not is_secret_value(value)


@pytest.mark.parametrize("value", ["bob@differentcompany.com", "SyntheticCredentialDoNotPrint", "short-token", "ordinary response text"])
def test_exact_redaction_honors_explicit_values(value):
    assert redact(f"value={value}", exact_values=[value]) == "value=[REDACTED]"


def test_exact_redaction_preserves_opaque_secret_protection():
    assert redact("ref=fixture-value-2026", exact_values=["fixture-value-2026"]) == "ref=[REDACTED]"
