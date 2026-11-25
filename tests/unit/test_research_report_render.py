"""Unit tests for research report rendering (no raw markdown litter)."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.research.report_render import markdown_to_html, markdown_to_pdf_bytes


SAMPLE = """# Validation dossier: Test idea

## Executive summary

This is a **solid** wedge for solo founders ([source](https://example.com)).

## Risks

- Pricing unknown
- Distribution still unproven

***

## Next tests

1. Interview 5 buyers
2. Concierge one paid pilot
"""


def test_html_preview_has_no_raw_markdown_markers():
    html = markdown_to_html(SAMPLE, title="Validation research", subtitle="Test idea")
    assert "<h1>Validation research</h1>" in html
    assert "<strong>solid</strong>" in html
    assert "https://example.com" in html
    body = html.split('<article class="sheet">', 1)[1]
    assert "* Pricing" not in body
    assert "## Risks" not in body
    assert "- Pricing unknown" not in body
    assert "<li>Pricing unknown</li>" in body


def test_pdf_bytes_look_like_pdf():
    pdf = markdown_to_pdf_bytes(SAMPLE, title="Validation research", subtitle="Test idea")
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 200


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_report_links_cannot_execute_or_open_files(monkeypatch, domain):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    for target in ("javascript:alert", "data:text/html,unsafe", "file:///etc/passwd", f"https://{domain}:invalid", f"https://user@{domain}"):
        rendered = markdown_to_html(f"[Your source]({target})", title=domain)
        assert '<a href=' not in rendered
        assert "Your source" in rendered
    for target in (f"https://{domain}/source", f"http://{domain}/source", f"mailto:help@{domain}", "#source"):
        assert f'<a href="{target}">' in markdown_to_html(f"[Your source]({target})", title=domain)
    monkeypatch.delenv("SUPYMEM_MODE")
    assert '<a href="javascript:alert">' in markdown_to_html("[Source](javascript:alert)", title=domain)
