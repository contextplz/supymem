from tests.company_os_harness import load_company_os_app

load_company_os_app()


def test_trace_tallies_drop_reasons_without_urls():
    from app.gtm.reachable import trace
    payload = {}
    trace(payload, "https://a.example", "dropped", "judge_rejected")
    trace(payload, "https://b.example", "dropped", "judge_rejected")
    trace(payload, "https://c.example", "dropped", "no_relevant_need_passage", track="signal")
    assert payload["_trace_counts"] == {"seed:dropped:judge_rejected": 2, "signal:dropped:no_relevant_need_passage": 1}
    assert "example" not in str(payload["_trace_counts"])


def test_trace_tally_drops_private_reason_text():
    from app.gtm.reachable import trace
    payload = {}
    trace(payload, "https://p.example", "dropped", "search_failed:private exception text https://p.example", track="https://x")
    assert payload["_trace_counts"] == {"seed:dropped:search_failed": 1}


def test_trace_tallies_candidate_domains_only():
    from app.gtm.reachable import trace
    payload = {}
    trace(payload, "https://www.reddit.com/r/sleeptrain/comments/abc/private-title", "candidate")
    trace(payload, "https://www.reddit.com/r/x/comments/def", "dropped", "no_relevant_need_passage")
    assert payload["_trace_hosts"] == {"candidate:reddit.com": 1, "dropped:reddit.com": 1}
    assert "private" not in str(payload["_trace_hosts"])


def test_trace_logs_each_source_decision_without_contact_data(caplog):
    import logging

    from app.gtm.reachable import trace

    with caplog.at_level(logging.INFO, logger="app.gtm.journey"):
        for source, stage, reason in (
            ("https://inventory.example/contact?token=secret#address", "kept", ""),
            ("https://walks.example/about", "dropped", "quote_ungrounded:private detail"),
            ("http://127.0.0.1/private", "dropped", "invalid_reach_url"),
            ("https://user:secret@walks.example/contact", "dropped", "invalid_reach_url"),
        ):
            payload = {}
            trace(payload, source, stage, reason, quote="private detail", email="person@example.test")
            if reason == "invalid_reach_url":
                assert "_trace_hosts" not in payload and "_trace_drops" not in payload
    decisions = [record.lead_trace for record in caplog.records if record.message == "gtm_lead_trace"]
    assert decisions == [
        {"url": "https://inventory.example/contact", "stage": "kept", "reason": "qualified_public_source"},
        {"url": "https://walks.example/about", "stage": "dropped", "reason": "quote_ungrounded"},
        {"url": "", "stage": "dropped", "reason": "invalid_reach_url"},
        {"url": "", "stage": "dropped", "reason": "invalid_reach_url"},
    ]
