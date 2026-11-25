"""Pause classifier fixtures for general browser G1."""
from coding_agents.browser_pause import (
    classify_control,
    field_kind,
    founder_prompt,
    is_safe_type_target,
    page_requires_pause,
)


def test_password_and_otp_and_captcha_pause():
    assert classify_control({"tag": "INPUT", "input_type": "password", "label": "Password"}) == "password"
    assert classify_control({"tag": "INPUT", "input_type": "text", "label": "Authenticator code",
                             "autocomplete": "one-time-code"}) == "otp_2fa"
    assert classify_control({"tag": "IFRAME", "label": "recaptcha challenge", "captcha": True}) == "captcha"
    assert founder_prompt("password").startswith("Type your password")


def test_terms_payment_and_send_pause():
    assert classify_control({"tag": "INPUT", "input_type": "checkbox", "label": "I agree to the Terms of Service",
                             "terms_checkbox": True}) == "terms"
    assert classify_control({"tag": "INPUT", "input_type": "text", "autocomplete": "cc-number",
                             "label": "Card number"}) == "payment"
    assert classify_control({"tag": "BUTTON", "label": "Create account", "submit": True}) == "send"
    assert classify_control({"tag": "BUTTON", "label": "Send invite", "submit": True}) == "send"


def test_false_positives_stay_clickable():
    # Plain navigation and non-secret fields are not pauses.
    assert classify_control({"tag": "A", "label": "Forgot password?", "href": "https://example.com/reset"}) is None
    assert classify_control({"tag": "A", "label": "Pricing", "href": "https://example.com/pricing"}) is None
    assert classify_control({"tag": "INPUT", "input_type": "email", "label": "Work email"}) is None
    assert field_kind({"tag": "INPUT", "input_type": "email", "label": "Work email"}) == "email"
    assert is_safe_type_target({"tag": "INPUT", "input_type": "email", "label": "Work email"})


def test_page_priority_prefers_payment_then_password():
    controls = [
        {"tag": "INPUT", "input_type": "password", "label": "Password"},
        {"tag": "INPUT", "input_type": "text", "autocomplete": "cc-number", "label": "Card"},
        {"tag": "BUTTON", "label": "Submit", "submit": True},
    ]
    assert page_requires_pause(controls) == "payment"
    assert page_requires_pause([
        {"tag": "INPUT", "input_type": "email", "label": "Email"},
        {"tag": "INPUT", "input_type": "password", "label": "Password"},
    ]) == "password"
