"""Existing-product context saved for founder-facing GTM use."""

from tests.company_os_harness import load_company_os_app

load_company_os_app()
from app.entry.product_context import product_context


def test_summary_is_saved_without_questionnaire_codes():
    answers = {"what": "app", "where": "unsure", "want_first": "gtm", "team": "solo", "product_summary": "It helps rehearsal studios manage bookings"}
    assert product_context("Existing product: It helps rehearsal studios manage bookings. app · unsure · gtm · solo", answers) == (
        "It helps rehearsal studios manage bookings", "It helps rehearsal studios manage bookings"
    )


def test_legacy_answer_only_payload_has_readable_fallback():
    assert product_context("Existing product: app · unsure · gtm · solo", {"what": "app"}) == ("Existing app", "Existing app")


def test_freeform_product_description_is_preserved():
    description = "We have a SaaS product for repair shops to track customer orders"
    assert product_context(description, {"want_first": "gtm"}) == (description, description)


def test_chat_first_generic_product_request_does_not_become_a_venture_name():
    request = "I already have a product. What should I do next to get users? Do not start paid work."
    assert product_context(request, {"want_first": "gtm"}) == (
        request, "Existing product"
    )
    assert product_context("I already have a product. It helps rehearsal studios manage bookings", {"want_first": "gtm"}) == (
        "It helps rehearsal studios manage bookings", "It helps rehearsal studios manage bookings"
    )
