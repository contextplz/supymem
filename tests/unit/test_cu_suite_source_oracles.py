"""Owned source/output oracle contracts; no desktop or provider calls."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import urllib.request

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/desktop'))
import cu_ai_suite as suite
from cu_browser_source_fixture import BrowserSourceFixture
import cu_browser_round1 as round1


def test_research_four_unrelated_lines_do_not_prove_source_grounding(tmp_path, monkeypatch):
    research = tmp_path / 'research.txt'
    research.write_text('Invented title\n- Fiction one\n- Fiction two\n- Fiction three\n')
    monkeypatch.setattr(suite, 'RESEARCH', research)
    assert suite.TRIALS['long']['verify']()[0] is not True


def test_browser_summary_placeholder_cannot_pass_without_owned_readback():
    assert suite.TRIALS['browser']['verify']()[0] is not True


def delivered():
    fixture = BrowserSourceFixture()
    fixture.server = SimpleNamespace(server_address=('127.0.0.1', 43210))
    fixture.source_reads = 1
    return fixture


def backend(fixture, *, url=None, text=None):
    return SimpleNamespace(b=SimpleNamespace(observe=lambda: SimpleNamespace(
        url=fixture.source_url if url is None else url,
        text='\n'.join((fixture.headline, *fixture.features)) if text is None else text)))


def test_browser_requires_owned_fresh_source_and_existing_backend():
    fixture = delivered()
    assert fixture.verify_browser(None)[0] is None
    assert fixture.verify_browser(backend(fixture, url=fixture.source_url + '/foreign'))[0] is False
    assert fixture.verify_browser(backend(fixture, text='\n'.join((fixture.headline, *fixture.features[:-1]))))[0] is False
    stale = delivered()
    assert fixture.verify_browser(backend(stale))[0] is False
    fixture.source_reads = 0
    assert fixture.verify_browser(backend(fixture))[0] is False
    fixture.source_reads = 1
    assert fixture.verify_browser(backend(fixture))[0] is True
    assert fixture.final_verdict() is None  # cleanup is also required
    fixture.closed = fixture.cleanup_verified = True
    assert fixture.final_verdict() is True


def test_exact_research_requires_distinct_source_lines_and_actual_move(tmp_path):
    fixture = delivered()
    source, output = tmp_path / 'research.txt', tmp_path / 'organized.txt'
    output.write_text(fixture.research_text + '\n')
    source.write_text(fixture.research_text)
    assert fixture.verify_file(output, source=source)[0] is False  # copied is not moved
    source.unlink()
    assert fixture.verify_file(output, source=source)[0] is True
    for text in ['Title\n- a\n- b\n- c', fixture.headline + '\n' + ('- ' + fixture.features[0] + '\n') * 3,
                 fixture.research_text.replace(fixture.features[2], 'invented'),
                 fixture.research_text + '\nextra']:
        output.write_text(text)
        assert fixture.verify_file(output, source=source)[0] is False
    output.unlink()
    output.symlink_to(tmp_path / 'foreign')
    assert fixture.verify_file(output, source=source)[0] is False


def test_headline_bytes_and_receipt_both_required(tmp_path):
    fixture = delivered()
    output = tmp_path / 'headline.txt'
    output.write_text(fixture.headline)
    assert fixture.verify_file(output)[0] is True
    fixture.source_reads = 0
    assert fixture.verify_file(output)[0] is False
    fixture.source_reads = 1
    output.write_text(delivered().headline)
    assert fixture.verify_file(output)[0] is False


def test_owned_source_local_server_receipt_and_bounded_cleanup():
    fixture = BrowserSourceFixture().start()
    try:
        with urllib.request.urlopen(fixture.source_url, timeout=2) as response:
            data = response.read(8192).decode()
        assert fixture.headline in data and all(feature in data for feature in fixture.features)
        assert fixture.source_reads == 1
        assert fixture.verify_browser(backend(fixture))[0] is True
    finally:
        fixture.close()
    assert fixture.cleanup_verified is True and not fixture.thread.is_alive()
    assert fixture.final_verdict() is True
    assert fixture.public_receipt()['counts_as_real_user'] is False
    assert 'headline' not in fixture.public_receipt()


def test_duplicate_note_count_cannot_qualify_as_owned_exact_note(monkeypatch):
    monkeypatch.setattr(suite, "osa", lambda _, **kwargs: "2")
    assert suite.verify_notes_app()[0] is not True


def test_owned_notes_exact_fresh_identity_and_edit_without_scoped_text_leaks():
    from cu_notes_readback import OwnedNotesReadback

    title, edited, body, added = (
        "Owned UUID title",
        "Owned UUID title edited",
        "random nonce body",
        "random nonce edit",
    )
    records = {}
    scripts = []

    def read(script):
        scripts.append(script)
        name = edited if '"' + edited + '"' in script else title
        values = records.get(name, [])
        return (
            str(len(values))
            if len(values) != 1
            else "1\x1e" + values[0][0] + "\x1e" + values[0][1]
        )

    oracle = OwnedNotesReadback(read, title, edited, body, added)
    assert oracle.verify_create()[0] is False  # no arming or existing matching data
    oracle.prepare_create()
    records[title] = [("owned-id", title + "\n" + body + "\n")]
    assert oracle.verify_create()[0] is True
    records[title] = [("owned-id", title + "\n" + body + "\nextra")]
    assert oracle.verify_create()[0] is False
    records[title] = [
        ("owned-id", title + "\n" + body),
        ("duplicate-id", title + "\n" + body),
    ]
    assert oracle.verify_create()[0] is False
    records[title] = [("different-id", title + "\n" + body)]
    assert oracle.verify_create()[0] is False
    records[title] = [("owned-id", title + "\n" + body)]
    oracle.prepare_edit()
    records.clear()
    records[edited] = [("foreign-id", edited + "\n" + body + "\n" + added)]
    assert oracle.verify_edit()[0] is False
    records[edited] = [("owned-id", edited + "\n" + body + "\n" + added)]
    assert oracle.verify_edit()[0] is True
    assert all(
        "plaintext of n" in script and "contains" not in script for script in scripts
    )
    assert body not in repr(oracle.verify_edit())  # closed public diagnostic only


def test_notes_missing_permissions_are_unknown_and_reuse_is_refused():
    import pytest
    from cu_notes_readback import OwnedNotesReadback

    unavailable = OwnedNotesReadback(lambda _: None, "title", "edited", "body", "added")
    assert unavailable.verify_create()[0] is None
    with pytest.raises(RuntimeError, match="scope_not_fresh"):
        unavailable.prepare_create()
    existing = OwnedNotesReadback(
        lambda _: "1\x1eexisting\x1etitle\nbody", "title", "edited", "body", "added"
    )
    with pytest.raises(RuntimeError, match="scope_not_fresh"):
        existing.prepare_create()


def test_notes_raw_readback_preserves_extra_trailing_content(monkeypatch):
    import subprocess
    monkeypatch.setattr(suite.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess([], 0, '1\x1eowned-id\x1etitle\nbody \n', ''))
    assert suite.osa('owned scoped readback', preserve_output=True).endswith('body ')
    assert suite.osa('ordinary scalar readback').endswith('body')


@pytest.fixture
def research_oracle(monkeypatch):
    facts = {
        "requests": {"name": "requests", "version": "2.32.7", "license_expression": "Apache-2.0", "requires_python": ">=3.9"},
        "httpx": {"name": "httpx", "version": "0.29.3", "license": "BSD-3-Clause", "requires_python": ">=3.10,<4"},
        "aiohttp": {"name": "aiohttp", "version": "3.14.2", "license": "Apache 2.0", "requires_python": ">=3.11"},
    }
    stars = {"psf/requests": 20, "encode/httpx": 50, "aio-libs/aiohttp": 30}
    calls = []
    def read(url):
        calls.append(url)
        if url.startswith("https://pypi.org/pypi/"):
            return SimpleNamespace(text=json.dumps({"info": facts[url.split("/")[-2]]}))
        assert url.startswith("https://api.github.com/repos/")
        return SimpleNamespace(text=json.dumps({"stargazers_count": stars[url.removeprefix("https://api.github.com/repos/")]}))
    monkeypatch.setattr(round1, "get", read)
    rows = [[name, info["version"], info.get("license_expression") or info["license"], info["requires_python"]]
            for name, info in facts.items()]
    return rows, stars, calls


def research_answer(rows, winner="Most GitHub stars: httpx"):
    return {"summary": "| Client | Version | License | Requires Python |\n| --- | --- | --- | --- |\n"
            + "\n".join("| " + " | ".join(row) + " |" for row in rows) + "\n" + winner}


def test_research_named_rows_and_current_stars_with_exact_eleven_cell_bar(research_oracle):
    rows, stars, calls = research_oracle
    assert round1.o_l01(None, research_answer(rows))
    assert len(calls) == 6
    rows[0][2] = "Apache License, Version 2.0"
    rows[1][3] = "<4, ≥3.10"
    assert round1.o_l01(None, research_answer(rows, "httpx has the most GitHub stars."))
    assert round1.o_l01(None, research_answer(rows, "httpx has the most GitHub stars, ahead of requests and aiohttp."))
    assert round1.o_l01(None, research_answer(rows, "Most GitHub stars: httpx (50 stars)"))
    rows[0][1] = "wrong"  # Exactly eleven of twelve cells remains the declared pass bar.
    assert round1.o_l01(None, research_answer(rows))
    rows[2][1] = "wrong"
    assert not round1.o_l01(None, research_answer(rows))
    rows[0][1], rows[2][1] = "2.32.7", "3.14.2"
    stars["psf/requests"] = 100
    assert not round1.o_l01(None, research_answer(rows))
    assert round1.o_l01(None, research_answer(rows, "Most GitHub stars: requests"))


@pytest.mark.parametrize("column", [1, 2, 3])
def test_research_swapped_rows_cannot_borrow_correct_global_tokens(research_oracle, column):
    rows, _stars, _calls = research_oracle
    correct = "\nOther facts: " + " ".join(cell for row in rows for cell in row)
    rows[0][column], rows[1][column] = rows[1][column], rows[0][column]
    answer = research_answer(rows)
    answer["summary"] += correct
    assert not round1.o_l01(None, answer)


@pytest.mark.parametrize("winner", ["Most GitHub stars: requests", "Not the most GitHub stars: httpx",
    "httpx does not have the most GitHub stars", "Most GitHub stars: httpx?",
    "Most GitHub stars: httpx or requests", "Most GitHub stars: httpx\nMost GitHub stars: requests",
    "httpx has the most GitHub stars; requests has the most GitHub stars",
    "Most GitHub stars: httpx is incorrect; the winner is requests.",
    "httpx has the most GitHub stars, but actually requests has more.",
    "Most GitHub stars: httpx (99 stars)"])
def test_research_correct_name_elsewhere_cannot_repair_wrong_or_ambiguous_stars_line(research_oracle, winner):
    rows, _stars, _calls = research_oracle
    assert not round1.o_l01(None, research_answer(rows, winner))


def test_research_duplicate_rows_and_unqualified_python_versions_refuse(research_oracle):
    rows, _stars, _calls = research_oracle
    assert not round1.o_l01(None, research_answer(rows + [rows[0]]))
    for row in rows:
        row[3] = row[3].split(",")[0].removeprefix(">=")
    assert not round1.o_l01(None, research_answer(rows))


def test_research_literal_markdown_labels_bind_their_own_package_and_values(research_oracle):
    rows, _stars, _calls = research_oracle
    for row in rows:
        row[0] = f"[{row[0]}](https://pypi.org/project/{row[0]}/)"
    rows[1][2] = "[BSD-3-Clause](https://example.invalid/license)"
    assert round1.o_l01(None, research_answer(rows))
    rows[0][1], rows[1][1] = rows[1][1], rows[0][1]
    assert not round1.o_l01(None, research_answer(rows))


def test_research_ascii_python_comparators_keep_exact_eleven_cell_bar(research_oracle):
    rows, _stars, _calls = research_oracle
    rows[0][1] = "wrong"
    rows[1][3] = "<4,>=3.10"
    assert round1.o_l01(None, research_answer(rows))
    rows[1][3] = "<=4,>=3.10"  # A different bound is not an equivalent formatting change.
    assert not round1.o_l01(None, research_answer(rows))


@pytest.fixture
def wizard_oracle():
    from cu_tasks.fixture_server import FixtureServer, post_body_digest
    from cu_tasks.round1_fixture import Fx2
    fixture = object.__new__(Fx2)
    FixtureServer.__init__(fixture, "owned-wizard-oracle")
    fixture.wizard = {"steps_ok": {}, "rejections": 0, "backs": 0}
    fixture.confirm_code = "OWNED-FINAL-NONCE"
    def deliver(path, body):
        fixture.posts.append((path, body))
        receipt = {"schema_version": 1, "request_id": len(fixture.posts), "method": "POST", "path": path,
                   "body_sha256": post_body_digest(body), "response": None, "handler_error": None}
        fixture.post_receipts.append(receipt)
        def out(status, response, _ctype):
            receipt["response"] = {"status": status, "complete": True}
        fixture._wizard(SimpleNamespace(out=out), path, body)
        return receipt
    deliver("/wizard2/step/2", {"region": "EU", "size": "twenty five"})
    deliver("/wizard2/nav", {"back": 2})
    final = {"name": "Northwind", "region": "EU", "size": "25", "billing": "finance@example.invalid", "plan": "Team"}
    return fixture, deliver, final, {"summary": fixture.confirm_code}


def test_wizard_exactly_one_commit_with_known_validation_noncommits(wizard_oracle):
    fixture, deliver, final, answer = wizard_oracle
    assert not round1.o_l02(fixture, answer)
    deliver("/wizard2/finish", {**final, "plan": "Unknown"})
    deliver("/wizard2/finish", final)
    assert round1.o_l02(fixture, answer)
    deliver("/wizard2/finish", final)
    assert not round1.o_l02(fixture, answer)


@pytest.mark.parametrize("corruption", ["missing", "unknown", "incomplete", "handler", "body", "status"])
def test_wizard_unknown_or_unbound_finish_receipt_never_qualifies(wizard_oracle, corruption):
    fixture, deliver, final, answer = wizard_oracle
    receipt = deliver("/wizard2/finish", final)
    if corruption == "missing":
        fixture.post_receipts.pop()
    elif corruption == "unknown":
        receipt["response"] = None
    elif corruption == "incomplete":
        receipt["response"]["complete"] = False
    elif corruption == "handler":
        receipt["handler_error"] = "synthetic"
    elif corruption == "body":
        receipt["body_sha256"] = "0" * 64
    else:
        receipt["response"]["status"] = 400
    assert not round1.o_l02(fixture, answer)
