"""Lead Library loader: dry run, upsert, manifest refusal."""
import gzip
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from tests.company_os_harness import load_company_os_app

pytestmark = pytest.mark.hosted_only


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "lead_library" / "synthetic_leads.jsonl"


@pytest.fixture()
def library(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/library.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.lead_library import loader
    from app.state import db
    yield {"factory": db(), "loader": loader, "root": tmp_path}


def _lines(raw: bytes) -> list[str]:
    return [line for line in raw.decode().splitlines() if line.strip()]


def _pack(root: Path, raw: bytes | None = None) -> Path:
    if raw is None:
        shutil.copy(FIXTURE, root / "synthetic_leads.jsonl")
        raw = (root / "synthetic_leads.jsonl").read_bytes()
    else:
        (root / "synthetic_leads.jsonl").write_bytes(raw)
    gz = gzip.compress(raw)
    path = root / "synthetic_leads.jsonl.gz"
    path.write_bytes(gz)
    rows = len(_lines(raw))
    (root / "manifest.json").write_text(json.dumps({"sha256": hashlib.sha256(gz).hexdigest(), "rows": rows}))
    return path


def _rewrite(root: Path, mutate) -> Path:
    rows = [json.loads(line) for line in _lines((root / "synthetic_leads.jsonl").read_bytes())]
    mutate(rows)
    raw = "".join(json.dumps(row) + "\n" for row in rows).encode()
    return _pack(root, raw)


def _invoke(library, capsys, *args):
    code = library["loader"].main(list(args))
    captured = capsys.readouterr()
    return code, json.loads(captured.out) if captured.out.strip() else None, captured.err


def _totals(factory):
    from sqlalchemy import func, select
    from app.lead_library.models import LeadLibraryEntry, LeadLibraryTag
    with factory() as session:
        return (session.scalar(select(func.count()).select_from(LeadLibraryEntry)),
            session.scalar(select(func.count()).select_from(LeadLibraryTag)))


def _one(factory, external):
    from sqlalchemy import select
    from app.lead_library.models import LeadLibraryEntry, LeadLibraryTag
    with factory() as session:
        entry = session.scalar(select(LeadLibraryEntry).where(LeadLibraryEntry.external_lead_id == external))
        tags = set()
        data = None
        if entry is not None:
            tags = {(tag.tag_type, tag.tag) for tag in session.scalars(select(LeadLibraryTag).where(LeadLibraryTag.entry_id == entry.id))}
            data = {name: getattr(entry, name) for name in (
                "id", "status", "status_reason", "email_status", "email_hash", "company", "content_hash",
                "imported_at", "search_text", "usable_channels", "provenance_json")}
        return data, tags


def test_dry_run_writes_nothing(library, capsys):
    path = _pack(library["root"])
    code, counts, _err = _invoke(library, capsys, "--file", str(path))
    assert code == 0
    assert counts == {"rows": 20, "insert": 20, "update": 0, "unchanged": 0, "invalid": 0, "tags": 96,
        "email_hash_unavailable": 0}
    assert _totals(library["factory"]) == (0, 0)


def test_apply_inserts_entries_and_tags(library, capsys):
    from app.gtm.email_learning import opaque
    path = _pack(library["root"])
    code, counts, _err = _invoke(library, capsys, "--file", str(path), "--apply")
    assert code == 0
    assert counts["insert"] == 20 and counts["tags"] == 96 and counts["unchanged"] == 0
    assert _totals(library["factory"]) == (20, 96)
    data, tags = _one(library["factory"], "synthetic_001")
    assert data["id"] == "ll_" + hashlib.sha256(b"synthetic_001").hexdigest()[:24]
    assert data["status"] == "active"
    assert data["email_hash"] == opaque("avery@loopforge.test")
    assert data["imported_at"] is not None
    assert "avery quillwheel" in data["search_text"] and "devtools" in data["search_text"] and "san francisco" in data["search_text"]
    assert json.loads(data["usable_channels"]) == ["email", "linkedin"]
    assert "lead_type_raw" in json.loads(data["provenance_json"])
    assert ("industry", "devtools") in tags and ("buyer_tag", "ai_agents") in tags and ("niche_raw", "coding_agents") in tags
    missing, _tags = _one(library["factory"], "synthetic_003")
    assert missing["email_hash"] is None


def test_reapply_reports_unchanged(library, capsys):
    path = _pack(library["root"])
    assert _invoke(library, capsys, "--file", str(path), "--apply")[0] == 0
    code, counts, _err = _invoke(library, capsys, "--file", str(path), "--apply")
    assert code == 0
    assert counts["unchanged"] == 20 and counts["insert"] == 0 and counts["update"] == 0 and counts["tags"] == 0
    assert _totals(library["factory"]) == (20, 96)


def test_changed_row_updates_and_replaces_tags(library, capsys):
    path = _pack(library["root"])
    assert _invoke(library, capsys, "--file", str(path), "--apply")[0] == 0
    from app.lead_library.models import LeadLibraryEntry
    from sqlalchemy import select
    with library["factory"]() as session:
        session.scalar(select(LeadLibraryEntry).where(LeadLibraryEntry.external_lead_id == "synthetic_001")).email_status = "delivered"
        session.commit()

    def mutate(rows):
        row = rows[0]
        row["company"] = "Changed Forge"
        row["content_hash"] = "a" * 64
        row["email_status"] = "none"
        row["tags"] = {"industry": ["fintech"], "buyer_tag": ["finance_software"], "category_raw": ["fintech"], "niche_raw": ["payments"]}

    path = _rewrite(library["root"], mutate)
    code, counts, _err = _invoke(library, capsys, "--file", str(path), "--apply")
    assert code == 0
    assert counts["update"] == 1 and counts["unchanged"] == 19 and counts["insert"] == 0 and counts["tags"] == 4
    data, tags = _one(library["factory"], "synthetic_001")
    assert data["company"] == "Changed Forge"
    assert data["email_status"] == "delivered"
    assert data["status"] == "active"
    assert tags == {("industry", "fintech"), ("buyer_tag", "finance_software"), ("category_raw", "fintech"), ("niche_raw", "payments")}
    assert "changed forge" in data["search_text"]
    assert _totals(library["factory"])[0] == 20


def test_suppressed_entry_stays_suppressed(library, capsys):
    path = _pack(library["root"])
    assert _invoke(library, capsys, "--file", str(path), "--apply")[0] == 0
    from app.lead_library.models import LeadLibraryEntry
    from sqlalchemy import select
    with library["factory"]() as session:
        entry = session.scalar(select(LeadLibraryEntry).where(LeadLibraryEntry.external_lead_id == "synthetic_002"))
        entry.status = "suppressed"
        entry.status_reason = "held"
        session.commit()
    before, _tags = _one(library["factory"], "synthetic_002")

    def mutate(rows):
        row = next(item for item in rows if item["external_lead_id"] == "synthetic_002")
        row["company"] = "Held Labs"
        row["content_hash"] = "b" * 64
        row["tags"] = {"industry": ["devtools"], "buyer_tag": ["devtools"], "category_raw": [], "niche_raw": []}

    path = _rewrite(library["root"], mutate)
    code, counts, _err = _invoke(library, capsys, "--file", str(path), "--apply")
    assert code == 0 and counts["update"] == 1
    data, tags = _one(library["factory"], "synthetic_002")
    assert data["status"] == "suppressed" and data["status_reason"] == "held"
    assert data["imported_at"] == before["imported_at"]
    assert data["company"] == "Held Labs"
    assert tags == {("industry", "devtools"), ("buyer_tag", "devtools")}


def test_manifest_sha256_mismatch_refuses_without_writes(library, capsys):
    path = _pack(library["root"])
    manifest = library["root"] / "manifest.json"
    body = json.loads(manifest.read_text())
    body["sha256"] = "0" * 64
    manifest.write_text(json.dumps(body))
    code, counts, err = _invoke(library, capsys, "--file", str(path), "--manifest", str(manifest), "--apply")
    assert code != 0 and counts is None
    assert "sha256" in err
    assert _totals(library["factory"]) == (0, 0)


def test_missing_manifest_refuses_without_writes(library, capsys):
    path = _pack(library["root"])
    (library["root"] / "manifest.json").unlink()
    code, counts, err = _invoke(library, capsys, "--file", str(path), "--apply")
    assert code != 0 and counts is None
    assert "missing" in err
    assert _totals(library["factory"]) == (0, 0)


def test_manifest_rows_mismatch_refuses(library, capsys):
    path = _pack(library["root"])
    manifest = library["root"] / "manifest.json"
    body = json.loads(manifest.read_text())
    body["rows"] = 19
    manifest.write_text(json.dumps(body))
    code, counts, err = _invoke(library, capsys, "--file", str(path), "--apply")
    assert code != 0 and counts is None
    assert "rows" in err
    assert _totals(library["factory"]) == (0, 0)


def test_unknown_taxonomy_is_invalid_and_skipped(library, capsys):
    raw = FIXTURE.read_bytes()
    bad = json.loads(_lines(raw)[0])
    bad["external_lead_id"] = "synthetic_bad"
    bad["lead_type"] = "not_a_lead_type"
    bad["content_hash"] = "c" * 64
    packed = raw if raw.endswith(b"\n") else raw + b"\n"
    path = _pack(library["root"], packed + (json.dumps(bad) + "\n").encode())
    code, counts, err = _invoke(library, capsys, "--file", str(path), "--apply")
    assert code == 0
    assert counts["rows"] == 21 and counts["invalid"] == 1 and counts["insert"] == 20
    assert "synthetic_bad" in err
    assert _totals(library["factory"])[0] == 20
    assert _one(library["factory"], "synthetic_bad")[0] is None


def test_missing_shared_secret_leaves_email_hash_empty(library, capsys, monkeypatch):
    def unavailable(value, purpose="address"):
        raise ValueError("gtm_shared_secret_unavailable")

    monkeypatch.setattr(library["loader"], "opaque", unavailable)
    path = _pack(library["root"])
    code, counts, _err = _invoke(library, capsys, "--file", str(path), "--apply")
    assert code == 0
    assert counts["email_hash_unavailable"] == 15 and counts["insert"] == 20
    data, _tags = _one(library["factory"], "synthetic_001")
    assert data["email_hash"] is None
