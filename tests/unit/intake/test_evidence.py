"""Synthetic founder files only: local extraction, custody, limits and bounded crawling."""
import io
import json
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app
from tests.unit.chat.test_chat_request_status import chat_request


@pytest.fixture(scope="module")
def modules():
    load_company_os_app()
    from app.intake import documents, evidence
    return documents, evidence


@pytest.fixture
def store(modules, tmp_path, monkeypatch):
    from app.config import settings
    from app import state
    from app.db.models import VentureDocument
    from app.db.supymem_models import EntryChatProposal

    monkeypatch.setattr(settings, "new_user_flow", "on")
    engine = create_engine(f"sqlite:///{tmp_path}/evidence.db")
    VentureDocument.__table__.create(engine)
    EntryChatProposal.__table__.create(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr(state, "db", lambda: factory)
    monkeypatch.setattr(modules[1], "db", lambda: factory)
    yield modules[1], factory
    engine.dispose()


def office(parts):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, xml in parts.items():
            archive.writestr(name, xml)
    return output.getvalue()


@pytest.mark.parametrize("name,data,expected", [
    ("folder/brief.md", b"# Product\nFor tutors", "For tutors"),
    ("notes.txt", "Founder café".encode(), "Founder café"),
    ("notes.txt", "Unicode notes".encode("utf-16"), "Unicode notes"),
    ("src/app.py", b"print('Founder code')\n", "Founder code"),
    ("table.csv", b'name,description\nTutors,"bookings, invoices"', "Tutors\tbookings, invoices"),
    ("brief.docx", office({"word/document.xml": '<w:document xmlns:w="w"><w:p><w:r><w:t>Founder </w:t></w:r><w:r><w:t>brief</w:t></w:r></w:p><w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p></w:document>'}), "Founder brief\nSecond paragraph"),
    ("pitch.pptx", office({"ppt/slides/slide2.xml": '<a:sp xmlns:a="a"><a:p><a:r><a:t>Second slide</a:t></a:r></a:p></a:sp>',
                           "ppt/slides/slide1.xml": '<a:sp xmlns:a="a"><a:p><a:r><a:t>First slide</a:t></a:r></a:p></a:sp>',
                           "ppt/notesSlides/notesSlide1.xml": '<a:sp xmlns:a="a"><a:p><a:r><a:t>Speaker notes</a:t></a:r></a:p></a:sp>'}), "Speaker notes"),
    ("budget.xlsx", office({"xl/sharedStrings.xml": '<sst xmlns="x"><si><t>Customers</t></si></sst>',
        "xl/worksheets/sheet1.xml": '<worksheet xmlns="x"><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="B1"><v>12</v></c><c r="C1" t="inlineStr"><is><t>Tutors</t></is></c></row></sheetData></worksheet>'}), "A1: Customers\tB1: 12\tC1: Tutors"),
])
def test_formats(modules, name, data, expected):
    assert expected in modules[0].extract_text(name, data)
    assert expected in modules[0].read_document(name, data)


def test_pdf_text_and_scanned_password_fallbacks(modules):
    from fpdf import FPDF
    from pypdf import PdfWriter

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(text="Founder PDF evidence")
    data = bytes(pdf.output())
    assert "Founder PDF evidence" in modules[0].read_document("brief.pdf", data)
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(ValueError, match="Export.*text.*paste.*chat"):
        modules[0].extract_text("scan.pdf", output.getvalue())
    writer.encrypt("secret")
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(ValueError, match="Password-protected"):
        modules[0].read_document("locked.pdf", output.getvalue())




@pytest.mark.parametrize("name,data,reason", [
    ("logo.png", b"fake image", "Images and binaries"), ("binary.bin", b"\x00\x01", "Binary"),
    ("empty.md", b"", "No readable"), ("../escape.txt", b"hello", "relative name"),
    ("corrupt.docx", b"not a zip", "Could not read"),
    ("unsafe.docx", office({"word/document.xml": '<!DOCTYPE x [<!ENTITY external SYSTEM "file:///etc/passwd">]><x>&external;</x>'}), "safely"),
    ("empty.pptx", office({"ppt/slides/slide1.xml": '<sp xmlns="a"><p/></sp>'}), "No readable"),
    ("empty.xlsx", office({"xl/worksheets/sheet1.xml": '<worksheet xmlns="x"><sheetData><row><c r="A1"/></row></sheetData></worksheet>'}), "No readable"),
])
def test_skip_with_working_alternative(modules, name, data, reason):
    with pytest.raises(ValueError, match=reason):
        modules[0].read_document(name, data)


def client(monkeypatch):
    from app.api import entry_api

    monkeypatch.setattr(entry_api, "ensure_subject_person", lambda subject: subject or "founder")
    app = FastAPI()
    app.include_router(entry_api.router)
    return TestClient(app)


def test_multi_folder_api_and_custody(store, monkeypatch):
    evidence, _ = store
    with client(monkeypatch) as http:
        result = http.post("/entry/evidence/files", headers={"X-Subject": "founder"}, files=[
            ("files", ("folder/brief.md", b"Founder notes", "text/markdown")),
            ("files", ("folder/src/app.py", b"print('code')", "text/plain")),
            ("files", ("folder/logo.png", b"image", "image/png"))])
    assert result.status_code == 200
    files = result.json()["files"]
    assert [file["status"] for file in files] == ["read", "read", "skipped"]
    assert "instead" in files[2]["note"]
    identifiers = [file["evidence_id"] for file in files[:2]]
    with pytest.raises(ValueError, match="attachments"):
        evidence.adopt_entry_evidence("ven_other", "other", identifiers)
    evidence.adopt_entry_evidence("ven_owned", "founder", identifiers)
    evidence.adopt_entry_evidence("ven_owned", "founder", identifiers)
    saved = evidence.venture_evidence("ven_owned")
    assert len(saved) == 2
    assert {row["source_label"] for row in saved} == {"folder/brief.md", "folder/src/app.py"}
    assert all(row["source_kind"] == "file" for row in saved)
    assert evidence.venture_evidence("ven_other") == []


def test_file_count_individual_total_limits_allow_remaining_files(store, modules, monkeypatch):
    monkeypatch.setattr(modules[0], "MAX_FILES", 4)
    monkeypatch.setattr(modules[0], "MAX_FILE_BYTES", 10)
    monkeypatch.setattr(modules[0], "MAX_TOTAL_BYTES", 12)
    with client(monkeypatch) as http:
        result = http.post("/entry/evidence/files", files=[("files", (name, data, "text/plain")) for name, data in [
            ("large.txt", b"x" * 11), ("first.txt", b"a" * 8), ("second.txt", b"b" * 8),
            ("small.txt", b"ok"), ("extra.txt", b"extra")]])
    files = result.json()["files"]
    assert [file["status"] for file in files] == ["skipped", "read", "skipped", "skipped", "skipped"]
    assert "25 MB" in files[0]["note"] and "100 MB" in files[2]["note"] and "50 files" in files[4]["note"]


def test_venture_upload_checks_access_before_read(store, monkeypatch):
    def forbidden(*args):
        from fastapi import HTTPException
        raise HTTPException(403, "forbidden")
    monkeypatch.setattr("app.authz.workspace.assert_venture_access", forbidden)
    with client(monkeypatch) as http:
        response = http.post("/entry/evidence/files", data={"venture_id": "ven_other"}, files=[("files", ("brief.md", b"private"))])
    assert response.status_code == 403


def test_mac_sanitized_filename_preserves_validated_folder_label(store, monkeypatch):
    with client(monkeypatch) as http:
        response = http.post("/entry/evidence/files", data={"source_labels": "folder/src/app.py"},
            files=[("files", ("app.py", b"print('founder')", "text/plain"))])
    assert response.status_code == 200
    result = response.json()["files"][0]
    assert result["status"] == "read" and result["source_label"] == "folder/src/app.py"
    with client(monkeypatch) as http:
        response = http.post("/entry/evidence/files", data={"source_labels": "../outside.txt"},
            files=[("files", ("safe.txt", b"notes", "text/plain"))])
    assert response.json()["files"][0]["status"] == "skipped"


def test_evidence_chunks_labels_order_and_dedup(store):
    evidence, _ = store
    text = "a" * 8000 + "b" * 8000 + "last"
    evidence.save_evidence("ven_one", "typed", "typed", text, evidence_id="known")
    evidence.save_evidence("ven_one", "typed", "typed", text, evidence_id="known")
    evidence.save_evidence("ven_one", "voice", "voice", "Founder voice", evidence_id="voice")
    rows = evidence.venture_evidence("ven_one")
    assert len(rows) == 4 and "".join(row["text"] for row in rows[:3]) == text
    assert rows[-1]["source_kind"] == "voice"
    assert all(set(row) == {"source_kind", "source_label", "text", "captured_at"} for row in rows)
    assert all(row["captured_at"].endswith("+00:00") for row in rows)


def test_entry_input_keeps_all_answers_but_removed_files_do_not_transfer(store):
    from app.db.supymem_models import EntryChatProposal
    from core.clock import utcnow
    evidence, factory = store
    now = utcnow()
    with factory() as session:
        session.add(EntryChatProposal(id="proposal", actor_id="founder", text="Product", route="clarify",
            state="clarify", skipped_steps_json="[]", message="Who is it for?", next_step="clarify",
            created_at=now, updated_at=now, expires_at=now, receipt_json="{}"))
        session.commit()
    identifier = evidence.save_evidence("entry:founder", "file", "brief.md", "private file", actor_id="founder")
    result = {"status": "clarify", "id": "proposal"}
    evidence.record_entry_input(result, "founder", "Product description", "typed", [identifier])
    assert result["files"][0]["evidence_id"] == identifier
    evidence.record_entry_input(result, "founder", "For independent tutors", "voice", [])
    assert "files" not in result
    evidence.record_entry_input(result, "founder", "For independent tutors", "voice", [])
    evidence.adopt_entry_evidence("ven_one", "founder", result["evidence_ids"])
    rows = evidence.venture_evidence("ven_one")
    assert {row["source_kind"] for row in rows} == {"typed", "voice"}
    assert len(rows) == 2


@pytest.mark.parametrize("label", ["Audience", "Clarification", "Context"])
@pytest.mark.parametrize("answer", ["Independent tutors", "Manufacturing procurement teams"])
def test_separate_intake_proposals_keep_exact_typed_and_voice_answers(store, label, answer):
    from app.db.supymem_models import EntryChatProposal
    from core.clock import utcnow
    evidence, factory = store
    now = utcnow()
    with factory() as session:
        for identifier, text in [("first", "My typed idea"), ("second", f"My typed idea\n{label}: {answer}")]:
            session.add(EntryChatProposal(id=identifier, actor_id="founder", text=text, route="clarify",
                state="clarify", skipped_steps_json="[]", message="Next?", next_step="clarify",
                created_at=now, updated_at=now, expires_at=now, receipt_json="{}"))
        session.commit()
    evidence.record_entry_input({"id": "first"}, "founder", "My typed idea", "typed", [])
    result = evidence.record_entry_input({"id": "second"}, "founder", f"My typed idea\n{label}: {answer}", "voice", [], previous_proposal_id="first")
    evidence.adopt_entry_evidence("ven_one", "founder", result["evidence_ids"])
    assert {(row["source_kind"], row["text"]) for row in evidence.venture_evidence("ven_one")} == {
        ("typed", "My typed idea"), ("voice", answer)}


@pytest.mark.parametrize("idea,answer", [
    ("A product for print shops", "A camera scans print samples and software flags colour drift before staff ship an order."),
    ("A product for home gardeners", "A soil probe records moisture and an app reminds you which beds need watering."),
])
def test_offer_continuation_advances_and_records_only_the_original_answer(store, idea, answer):
    from app.db.supymem_models import EntryChatProposal
    from app.intake.smart import _prefill
    from core.clock import utcnow

    evidence, factory = store
    now = utcnow()
    with factory() as session:
        session.add(EntryChatProposal(id="opening", actor_id="founder", text=idea, route="clarify",
            state="clarify", skipped_steps_json="[]", message="Describe a task your product does.", next_step="clarify",
            created_at=now, updated_at=now, expires_at=now, receipt_json='{"onboarding": {"field": "offer"}}'))
        session.commit()
    evidence.record_entry_input({"id": "opening"}, "founder", idea, "typed", [])
    joined, target = evidence.continued_entry(answer, "opening", "founder", None)
    assert target is None and joined == f"{idea}\nOffer: {answer}"
    assert _prefill(joined)["offer"] == answer
    with factory() as session:
        session.add(EntryChatProposal(id="continued", actor_id="founder", text=joined, route="clarify",
            state="pending", skipped_steps_json="[]", message="Review your idea.", next_step="continue",
            created_at=now, updated_at=now, expires_at=now, receipt_json="{}"))
        session.commit()
    result = evidence.record_entry_input({"id": "continued"}, "founder", joined, "typed", [], previous_proposal_id="opening")
    evidence.adopt_entry_evidence("ven_offer", "founder", result["evidence_ids"])
    assert {row["text"] for row in evidence.venture_evidence("ven_offer")} == {idea, answer}


def test_staged_removal_persists_across_proposal_reload(store):
    from app.db.supymem_models import EntryChatProposal
    from core.clock import utcnow
    evidence, factory = store
    now = utcnow()
    identifier = evidence.save_evidence("entry:founder", "file", "brief.md", "notes", actor_id="founder")
    with factory() as session:
        session.add(EntryChatProposal(id="pending", actor_id="founder", text="My idea", route="clarify", state="clarify",
            skipped_steps_json="[]", message="Who?", next_step="clarify", created_at=now, updated_at=now,
            expires_at=now, receipt_json=json.dumps({"evidence_ids": [identifier]})))
        session.commit()
    evidence.remove_file(identifier, "founder")
    with factory() as session:
        assert json.loads(session.get(EntryChatProposal, "pending").receipt_json)["evidence_ids"] == []
    assert evidence.entry_files([identifier], "founder") == []
    evidence.record_entry_input({"id": "pending"}, "founder", "My idea", "typed", [])


def test_remove_venture_file_does_not_touch_other_evidence(store, monkeypatch):
    evidence, _ = store
    monkeypatch.setattr("app.authz.workspace.assert_venture_access", lambda *args: None)
    identifier = evidence.save_evidence("ven_one", "file", "folder/brief.md", "a" * 16001, actor_id="founder")
    evidence.save_statement("ven_one", "typed description")
    with client(monkeypatch) as http:
        response = http.delete(f"/entry/evidence/files/{identifier}?venture_id=ven_one")
        assert response.status_code == 200
    assert [row["source_kind"] for row in evidence.venture_evidence("ven_one")] == ["typed"]


def test_statement_voice_context_and_caller_transaction(store):
    from app.intake.evidence import INPUT_KIND
    evidence, factory = store
    token = INPUT_KIND.set("voice")
    try:
        with factory() as session:
            evidence.save_statement("ven_one", "Voice idea", session=session)
            session.rollback()
        assert evidence.venture_evidence("ven_one") == []
        with factory() as session:
            evidence.save_statement("ven_one", "Voice idea", session=session)
            session.commit()
        assert evidence.venture_evidence("ven_one")[0]["source_kind"] == "voice"
    finally:
        INPUT_KIND.reset(token)


def test_legacy_statement_does_not_query_or_write_evidence(store, monkeypatch):
    from app.config import settings
    evidence, _ = store
    monkeypatch.setattr(settings, "new_user_flow", "off")
    monkeypatch.setattr(evidence, "save_evidence", lambda *args, **kwargs: pytest.fail("legacy evidence access"))
    evidence.save_statement("ven_one", "Legacy chat message")


@pytest.mark.parametrize("policy", [None, "User-agent: *\nDisallow: /"])
def test_crawl_robots_deny_and_unavailable_never_reads(modules, monkeypatch, policy):
    from app.intake.crawl import crawl_site
    monkeypatch.setattr("app.gtm.scrapy_leads._fetch", lambda *args, **kwargs: policy)
    monkeypatch.setattr("app.intake.assets._allowed_page", lambda *args: pytest.fail("robots forbids this"))
    assert crawl_site("https://founder.example") == []


@pytest.mark.parametrize("prefetched", [False, True])
def test_crawl_preserves_supplied_path_and_query_before_expanding(modules, monkeypatch, prefetched):
    from app.intake.crawl import crawl_site
    url = "https://founder.example/products?plan=Pro"
    home = "https://founder.example/"
    html = "<h1>Pricing for Pro</h1>"
    calls = []
    monkeypatch.setattr("app.gtm.scrapy_leads._fetch", lambda *args, **kwargs: "User-agent: *\nAllow: /")
    def read(target, robots, deadline):
        calls.append(target)
        return html if target == url else "<h1>Home</h1>" if target == home else None
    monkeypatch.setattr("app.intake.assets._allowed_page", read)
    pages = crawl_site(url, home_html=html if prefetched else None)
    assert calls[0] == (home if prefetched else url)
    assert calls.count(url) == (0 if prefetched else 1)
    assert (pages[0]["url"], pages[0]["text"]) == (url, "Pricing for Pro")
    assert pages[0]["website_answers"]["offer"] == "Pricing for Pro"
    assert pages[1]["url"] == home


def test_crawl_same_domain_key_pages_limit_and_storage(store, monkeypatch):
    from app.intake.crawl import crawl_site, store_pages
    calls = []
    monkeypatch.setattr("app.gtm.scrapy_leads._fetch", lambda *args, **kwargs: "User-agent: *\nDisallow: /team")
    home = '<h1>Founder product</h1><a href="https://competitor.example/about">Competitor</a><a href="https://sub.founder.example/team">Other domain</a><a href="/about-us">About</a><a href="/pricing">Pricing</a><a href="/team">Team</a><a href="/secret">Not key</a>' + ''.join(f'<a href="/products/{number}">Product</a>' for number in range(20))
    def read(url, robots, deadline):
        calls.append(url)
        return home if url == "https://founder.example/" else "<p>Evidence for " + url + "</p><script>hidden</script>"
    monkeypatch.setattr("app.intake.assets._allowed_page", read)
    pages = crawl_site("https://founder.example/")
    assert len(calls) <= 10 and len(pages) <= 10
    assert "https://founder.example/about-us" in calls and "https://founder.example/pricing" in calls
    assert all(url.startswith("https://founder.example/") for url in calls)
    assert not any(url.endswith(("/team", "/secret")) for url in calls)
    assert "https://founder.example/blog" in calls and "https://founder.example/customers" in calls
    store_pages("ven_one", pages)
    store_pages("ven_one", pages)
    assert len(store[0].venture_evidence("ven_one")) == len(pages)
    assert all(row["source_kind"] == "website" and "hidden" not in row["text"] for row in store[0].venture_evidence("ven_one"))


def test_redirect_to_other_domain_is_refused_before_network(modules, monkeypatch):
    from app.intake.assets import _allowed_page
    from urllib.robotparser import RobotFileParser
    robots = RobotFileParser()
    robots.parse([])
    def guarded(client, url, **kwargs):
        return client.get("https://8.8.8.8/pricing", headers={"Host": "competitor.example"})
    monkeypatch.setattr("core.research_web.guard.get_checked", guarded)
    assert _allowed_page("https://founder.example/", robots, float("inf")) is None


def test_local_text_documents_upload_and_search_by_venture(chat_request, monkeypatch):
    import asyncio
    from xml.sax.saxutils import escape
    from fastapi import UploadFile
    from fpdf import FPDF
    from app.db.models import Venture
    from app.intake import evidence
    from app.tenancy import bind
    from core.clock import utcnow

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    statements = ["Freight estimates include customs duties.", "Family recipes include allergen substitutions."]
    for index, statement in enumerate(statements):
        venture_id = f"ven_files_{index}"
        with chat_request["factory"]() as session:
            session.add(Venture(id=venture_id, workspace_id=chat_request["wid"], name=statement,
                raw_idea=statement, stage="draft", created_at=utcnow(), updated_at=utcnow()))
            session.commit()
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.cell(text=statement)
        sources = [("notes.txt", statement.encode()), ("notes.md", ("# Notes\n" + statement).encode()),
            ("notes.pdf", bytes(pdf.output())), ("notes.docx", office({"word/document.xml":
                '<w:document xmlns:w="w"><w:p><w:r><w:t>' + escape(statement) + '</w:t></w:r></w:p></w:document>'}))]
        result = asyncio.run(evidence.upload_files([UploadFile(io.BytesIO(data), filename=name)
            for name, data in sources], chat_request["actor"], venture_id))
        assert [file["status"] for file in result["files"]] == ["read"] * 4
        with bind(workspace_id=chat_request["wid"], venture_id=venture_id, actor=chat_request["actor"]):
            for file in result["files"]:
                hits = evidence.search_files(venture_id, file["source_label"] + " " + statement)
                hit = next(hit for hit in hits if hit["source_label"] == file["source_label"])
                assert statement in hit["text"] and hit["id"].startswith(file["evidence_id"])
                assert statements[1 - index] not in hit["text"]
            with pytest.raises(PermissionError):
                evidence.search_files(f"ven_files_{1 - index}", statement)
        assert len(evidence.venture_evidence(venture_id)) == 4


def test_document_search_returns_matching_late_excerpt_and_removed_files_disappear(chat_request, monkeypatch):
    from app.intake import evidence
    from app.tenancy import bind
    for statement in ["Freight estimates include customs duties.", "Family recipes include allergen substitutions."]:
        identifier = evidence.save_evidence(chat_request["vid"], "file", "notes.md", "intro " * 600 + statement,
            actor_id=chat_request["actor"])
        with bind(workspace_id=chat_request["wid"], venture_id=chat_request["vid"], actor=chat_request["actor"]):
            hits = evidence.search_files(chat_request["vid"], statement)
            assert statement in next(hit["text"] for hit in hits if hit["id"].startswith(identifier))
            assert all(len(hit["text"]) <= 2000 for hit in hits)
            evidence.remove_file(identifier, chat_request["actor"], chat_request["vid"])
            assert not any(hit["id"].startswith(identifier) for hit in evidence.search_files(chat_request["vid"], statement))


def test_credential_uploads_give_secure_settings_step_without_saving(chat_request):
    import asyncio
    from fastapi import UploadFile
    from app.intake import evidence
    for statement in ["Freight estimates include customs duties.", "Family recipes include allergen substitutions."]:
        text = statement + "\napi_key=sk-proj-" + "x" * 40
        result = asyncio.run(evidence.upload_files([UploadFile(io.BytesIO(text.encode()), filename="notes.txt")],
            chat_request["actor"], chat_request["vid"]))
        assert result["files"][0]["status"] == "skipped"
        assert "Settings > Keys and connections" in result["files"][0]["note"]
        assert evidence.venture_evidence(chat_request["vid"]) == []


def test_document_search_withholds_legacy_credentials_across_chunk_boundaries(chat_request):
    from app.db.models import VentureDocument
    from app.intake import evidence
    from app.tenancy import bind
    from core.clock import utcnow
    for index, statement in enumerate(["Freight estimates include customs duties.", "Family recipes include allergen substitutions."]):
        prefix = (statement + "\n" + "notes " * 2000)[:7997] + " "
        text = prefix + "sk-proj-" + "x" * 40
        with chat_request["factory"]() as session:
            for chunk, offset in enumerate(range(0, len(text), 8000)):
                session.add(VentureDocument(id=f"legacy_{index}:{chunk}", venture_id=chat_request["vid"],
                    kind=evidence.KIND, title="notes.txt", body_md=text[offset:offset + 8000],
                    created_at=utcnow(), updated_at=utcnow(), provenance_json=json.dumps({
                        "source_kind": "file", "evidence_id": f"legacy_{index}", "chunk": chunk})))
            session.commit()
        with bind(workspace_id=chat_request["wid"], venture_id=chat_request["vid"], actor=chat_request["actor"]):
            assert evidence.search_files(chat_request["vid"], statement) == []
