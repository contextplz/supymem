from tests.company_os_harness import load_company_os_app


def test_inbox_briefing_actions_and_controls_are_content_free_for_unrelated_domains():
    load_company_os_app()
    from app.analytics.feature_catalog import ACTION_SLUG, CONTROL_SLUG

    for domain in ("warehouse.example", "hiking.example"):
        for action, slug in (("inbox.briefing.consent", "settings"), ("inbox.briefing.settings", "settings"), ("inbox.item.feedback", "feedback")):
            assert ACTION_SLUG[action] == slug
            assert domain not in action
        for control in ("consent", "settings_save", "window", "window_add", "window_remove", "quiet_start", "quiet_end", "email_copy"):
            assert CONTROL_SLUG[f"inbox_briefing_{control}"] == "settings"
        assert CONTROL_SLUG["inbox_briefing_feedback"] == "feedback"
        assert CONTROL_SLUG["inbox_briefing_action"] == CONTROL_SLUG["inbox_briefing_open_gmail"] == "chat"
