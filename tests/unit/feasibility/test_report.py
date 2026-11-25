from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.feasibility.extract import extract_requirements
from app.feasibility.report import build_report


def test_report_separates_software_and_human_help():
    tree = extract_requirements("A web app with Stripe checkout and an Instagram account")
    report = build_report("ven_test", tree, competitor_count=3, source_count=4)

    assert report.can_fully
    assert any(node.human_minutes == 3 for node in report.can_with_help)
    assert report.human_minutes_total >= 3
    assert report.phases[0].proves
    assert report.phases[0].proof_metric
    assert report.phases[0].kill_criteria
