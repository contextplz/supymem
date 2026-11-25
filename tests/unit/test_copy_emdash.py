from pathlib import Path

import pytest

from core.copy import founder_copy, strip_emdash


def test_strip_emdash_replaces_em_and_en():
    assert "—" not in strip_emdash("Hello — world")
    assert strip_emdash("Hello — world") == "Hello, world"
    assert "–" not in strip_emdash("range 1–3")
    assert strip_emdash("") == ""


@pytest.mark.parametrize("text,expected", [
    ("The founder already knows X", "You already know X"),
    ("The founder still has X", "You still have X"),
    ("The founder just wants X", "You just want X"),
    ("The founder also needs X", "You also need X"),
    ("The founder only does X", "You only do X"),
    ("The founder never plans X", "You never plan X"),
    ("The founder has not", "You have not"),
    ("The founder is here", "You are here"),
    ("The founder was here", "You were here"),
    ("The founder knows X", "You know X"),
    ("The founder wants X", "You want X"),
    ("The founder needs X", "You need X"),
    ("The founder does X", "You do X"),
    ("The founder doesn't know", "You don't know"),
    ("The founder hasn't tried", "You haven't tried"),
    ("The founder isn't sure", "You aren't sure"),
    ("The founder wasn't sure", "You weren't sure"),
    ("The founder still hasn’t tried", "You still haven’t tried"),
    ("The founder builds X", "You build X"),
    ("the founder's hypothesis", "your hypothesis"),
    ("The founder’s plans", "Your plans"),
])
def test_founder_copy_agrees_with_subject_without_changing_possessives(text, expected):
    assert founder_copy(text) == expected


@pytest.mark.parametrize("path", [
    "apps/console/src/components/planning/NextStepCard.tsx",
    "services/company_os/app/ventures/next_step_catalogue.py",
])
def test_founder_next_step_copy_has_no_emdash(path):
    assert "—" not in (Path(__file__).resolve().parents[2] / path).read_text()
