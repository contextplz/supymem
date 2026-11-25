"""The owner mail loop uses real TLS sockets, never a real mailbox."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
DOMAINS = [
    pytest.param("A parts scheduling tool for repair shops", "Repair shop owners",
                 "Parts and technicians arrive at different times", "repairs.test", id="b2b"),
    pytest.param("A bread journal for home bakers", "Adults learning home baking",
                 "Bakers cannot tell why their loaf failed", "baking.test", id="consumer"),
]


@pytest.mark.parametrize("idea,audience,problem,domain", DOMAINS)
def test_local_mail_journey(idea, audience, problem, domain):
    process = subprocess.Popen([
        sys.executable, str(ROOT / "scripts/local_mail_journey.py"),
        "--idea", idea, "--audience", audience, "--problem", problem, "--domain", domain,
    ], cwd=ROOT, env={**os.environ, "PYTHONPATH": f"{ROOT / 'packages'}:{ROOT / 'services/company_os'}"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=180)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=30)
    assert process.returncode == 0, stdout + stderr
    for step in ("compliant SMTP delivery", "daily cap", "background reply",
                 "contact marked replied", "booked call", "encrypted password", "loopback only"):
        assert f"PASS {step}" in stdout


@pytest.mark.parametrize("domain", ["repairs.example", "baking.example"])
def test_mail_journey_refuses_non_test_domains(domain):
    result = subprocess.run([sys.executable, str(ROOT / "scripts/local_mail_journey.py"), "--domain", domain],
        cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "Use a reserved .test domain" in result.stdout
    assert "PASS" not in result.stdout
