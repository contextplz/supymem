import asyncio
import csv
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from tests.unit.lead_library.test_service import entry_id, journey_db, library


@pytest.fixture()
def reclassifier(library):
    path = Path(__file__).resolve().parents[3] / "scripts" / "leads" / "reclassify.py"
    spec = importlib.util.spec_from_file_location("lead_reclassify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _two_rows(data):
    from app.lead_library.models import LeadLibraryEntry as Entry, LeadLibraryTag as Tag
    cases = [(1, "Cipher Security", "ciphercloud.test", "Security Director", "cybersecurity"),
             (9, "Meadow Apparel", "meadowwear.test", "Marketing Manager", "d2c_consumer_goods")]
    with data["factory"]() as session:
        ids = [entry_id(case[0]) for case in cases]
        session.execute(delete(Tag).where(Tag.entry_id.not_in(ids)))
        session.execute(delete(Entry).where(Entry.id.not_in(ids)))
        for number, company, domain, role, industry in cases:
            entry = session.get(Entry, entry_id(number))
            entry.company, entry.company_domain, entry.role_title = company, domain, role
            entry.function, entry.seniority = "other", "unknown"
            entry.provenance_json = json.dumps({"description": "", "original_license": "retained"})
            session.execute(delete(Tag).where(Tag.entry_id == entry.id, Tag.tag_type == "industry"))
            session.add(Tag(entry_id=entry.id, tag_type="industry", tag="fintech"))
        session.commit()
    return cases


def _csv(path):
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _save(path, module, rows):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=module.COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def test_reclassify_review_is_dry_run_safe_batched_and_idempotent(library, reclassifier, tmp_path, monkeypatch):
    from app.credits import metered
    from app.lead_library.models import LeadLibraryEntry as Entry, LeadLibraryTag as Tag

    cases = _two_rows(library)
    monkeypatch.setattr(metered, "llm", lambda *args, **kwargs: pytest.fail("Deterministic rows called the model"))
    path = tmp_path / "review.csv"
    summary = asyncio.run(reclassifier.propose(library["factory"], path, batch_size=1, workspace_id=library["wid"]))
    assert summary == dict(rows=2, proposed=2, unchanged=0, unresolved=0, llm_calls=0, errors=0, applied=0)
    rows = _csv(path)
    expected = {entry_id(case[0]): case[4] for case in cases}
    for row in rows:
        assert json.loads(row["after"])["industry"] == [expected[row["entry_id"]]]
        assert row["method"] == "deterministic" and row["approved"] == ""
        assert not any(field in json.loads(row["evidence"]) for field in ("email", "person_name", "category", "search_text"))
    assert reclassifier.review(library["factory"], path, apply=True)["applied"] == 0
    for row in rows:
        row["approved"] = "yes"
    _save(path, reclassifier, rows)
    with library["factory"]() as session:
        protected = {entry.id: (entry.email, entry.email_hash, entry.status, entry.quality_flags, entry.provenance_json, entry.content_hash)
            for entry in session.scalars(select(Entry))}
        other_tags = {(tag.entry_id, tag.tag_type, tag.tag) for tag in session.scalars(select(Tag).where(Tag.tag_type != "industry"))}
    assert reclassifier.review(library["factory"], path, batch_size=1)["proposed"] == 2
    with library["factory"]() as session:
        assert set(session.scalars(select(Tag.tag).where(Tag.tag_type == "industry"))) == {"fintech"}
    assert reclassifier.review(library["factory"], path, apply=True, batch_size=1)["applied"] == 2
    assert reclassifier.review(library["factory"], path, apply=True, batch_size=1)["unchanged"] == 2
    with library["factory"]() as session:
        for entry in session.scalars(select(Entry)):
            assert (entry.email, entry.email_hash, entry.status, entry.quality_flags, entry.provenance_json, entry.content_hash) == protected[entry.id]
            assert expected[entry.id] in entry.search_text and "fintech" not in entry.search_text
            assert entry.function != "other" and entry.seniority != "unknown"
        assert {(tag.entry_id, tag.tag_type, tag.tag) for tag in session.scalars(select(Tag).where(Tag.tag_type != "industry"))} == other_tags
    with pytest.raises(FileExistsError):
        asyncio.run(reclassifier.propose(library["factory"], path))


@pytest.mark.parametrize("industry,title", [("cybersecurity", "Security Director"), ("d2c_consumer_goods", "Marketing Manager")])
def test_only_ambiguous_row_evidence_uses_metered_classifier(library, reclassifier, monkeypatch, industry, title):
    from app.credits import metered
    evidence = dict(company="Northline", domain="northline.test", title=title, description="")
    before = dict(industry=["fintech"], function="other", seniority="unknown")
    calls = []

    async def model(messages, **kwargs):
        calls.append((messages, kwargs))
        assert json.loads(messages[-1]["content"]) == evidence
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"industry": [industry]})))])

    monkeypatch.setattr(metered, "llm", model)
    unchanged, method = asyncio.run(reclassifier.classify(evidence, before))
    assert unchanged["industry"] == before["industry"] and method == "unresolved" and not calls
    after, method = asyncio.run(reclassifier.classify(evidence, before, workspace_id=library["wid"], request_id="fixture-request"))
    assert after["industry"] == [industry] and method == "metered"
    assert calls[0][1] == dict(purpose="lead_library.reclassify", workspace_id=library["wid"], request_id="fixture-request", max_tokens=256)
    for bad in ({"industry": ["invented_label"]}, {"industry": [industry], "email": "not_allowed"}, {"industry": "not_an_array"}):
        async def invalid(*args, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(bad)))])
        monkeypatch.setattr(metered, "llm", invalid)
        with pytest.raises(ValueError):
            asyncio.run(reclassifier.classify(evidence, before, workspace_id=library["wid"]))


def test_review_rejects_invalid_csv_before_any_write_and_skips_stale_rows(library, reclassifier, tmp_path):
    from app.lead_library.models import LeadLibraryEntry as Entry, LeadLibraryTag as Tag
    _two_rows(library)
    path = tmp_path / "review.csv"
    asyncio.run(reclassifier.propose(library["factory"], path))
    rows = _csv(path)
    for row in rows:
        row["approved"] = "yes"
    invalid = [row.copy() for row in rows]
    invalid[-1]["after"] = json.dumps(dict(industry=["invalid"], function="other", seniority="unknown"))
    _save(path, reclassifier, invalid)
    with pytest.raises(ValueError, match="Invalid industry"):
        reclassifier.review(library["factory"], path, apply=True, batch_size=1)
    with library["factory"]() as session:
        assert set(session.scalars(select(Tag.tag).where(Tag.tag_type == "industry"))) == {"fintech"}
        session.get(Entry, rows[0]["entry_id"]).company_domain = "changed.test"
        session.commit()
    _save(path, reclassifier, rows)
    summary = reclassifier.review(library["factory"], path, apply=True, batch_size=1)
    assert summary["conflicts"] == 1 and summary["applied"] == 1


def test_conflicting_evidence_and_meter_budget_leave_industry_for_review(library, reclassifier, tmp_path, monkeypatch):
    from app.credits import metered
    from app.lead_library.models import LeadLibraryEntry as Entry
    _two_rows(library)
    with library["factory"]() as session:
        for entry in session.scalars(select(Entry)):
            entry.provenance_json = json.dumps({"description": "Apparel" if entry.id == entry_id(1) else "Cybersecurity"})
        session.commit()
    calls = []

    async def fail(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("Provider unavailable")

    monkeypatch.setattr(metered, "llm", fail)
    path = tmp_path / "unresolved.csv"
    summary = asyncio.run(reclassifier.propose(library["factory"], path, workspace_id=library["wid"], llm_limit=1))
    assert summary["llm_calls"] == len(calls) == summary["errors"] == 1
    assert summary["unresolved"] == 2
    for row in _csv(path):
        assert row["method"] == "unresolved" and json.loads(row["after"])["industry"] == ["fintech"]


def test_cli_requires_review_for_apply_and_reports_real_dry_run(library, reclassifier, tmp_path, capsys, monkeypatch):
    from app import state
    _two_rows(library)
    monkeypatch.setattr(state, "db", lambda: pytest.fail("CLI must not initialize application schema"))
    path = tmp_path / "cli.csv"
    with pytest.raises(SystemExit) as error:
        reclassifier.main(["--output", str(path), "--apply"])
    assert error.value.code == 2 and not path.exists()
    assert reclassifier.main(["--output", str(path), "--batch-size", "1"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["dry_run"] is True and summary["proposed"] == 2 and summary["applied"] == 0
    rows = _csv(path)
    for row in rows:
        row["approved"] = "yes"
    _save(path, reclassifier, rows)
    assert reclassifier.main(["--review", str(path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["dry_run"] is True and summary["proposed"] == 2 and summary["applied"] == 0
    assert reclassifier.main(["--review", str(path), "--apply", "--batch-size", "1"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["dry_run"] is False and summary["applied"] == 2
