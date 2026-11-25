"""Exercise Playwright's actual profile ownership without a model or database."""
import gc
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()
from app.pagebuilder import rendering  # noqa: E402


@pytest.fixture
def linux_worker(monkeypatch, tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        executable = Path(pw.firefox.executable_path)
    # Exercise the renderer's Linux policy without changing Playwright's host OS.
    monkeypatch.setattr(rendering, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(rendering.os, "geteuid", lambda: 10001)
    monkeypatch.setattr(rendering.time, "sleep", lambda _: None)
    home = tmp_path / "unwritable-home"
    home.mkdir(mode=0o500)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", "/tmp")
    return SimpleNamespace(home=home, executable=executable)


def test_native_profiles_survive_cache_writes_and_are_removed_after_failed_launches(linux_worker, tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    receipt = tmp_path / "profiles.jsonl"
    executable = tmp_path / "cache-writer"
    executable.write_text(f"""#!{sys.executable}
import json
from pathlib import Path
import sys
profile = Path(sys.argv[sys.argv.index('-profile') + 1])
cache = profile / 'startupCache'
cache.mkdir()
(cache / 'shutdown-write').write_text('synthetic cache')
with open({str(receipt)!r}, 'a') as output:
    output.write(json.dumps({{'path': str(profile), 'mode': profile.stat().st_mode & 0o777,
                             'cache_written': (cache / 'shutdown-write').exists()}}) + '\\n')
""")
    executable.chmod(0o700)
    with playwright.sync_playwright() as pw:
        def launch(profile, **kwargs):
            assert profile == ""
            return pw.firefox.launch_persistent_context(profile, executable_path=str(executable), **kwargs)
        with pytest.raises(playwright.Error):
            rendering._launch_browser(SimpleNamespace(firefox=SimpleNamespace(launch_persistent_context=launch)))
    profiles = [json.loads(line) for line in receipt.read_text().splitlines()]
    assert len(profiles) == 2 and len({item["path"] for item in profiles}) == 2
    for item in profiles:
        assert item["mode"] == 0o700 and item["cache_written"]
        assert Path(item["path"]).parent == Path("/tmp")
        assert not Path(item["path"]).exists()
    assert rendering.os.environ["HOME"] == str(linux_worker.home)


@pytest.mark.parametrize("attested", [True, False])
def test_real_firefox_close_removes_profile_with_unwritable_worker_home(linux_worker, monkeypatch, capfd, attested):
    playwright = pytest.importorskip("playwright.sync_api")
    monkeypatch.setenv("DEBUG", "pw:browser")
    monkeypatch.setattr(rendering, "firefox_content_sandbox_verified", lambda: attested)
    with playwright.sync_playwright() as pw:
        if not linux_worker.executable.exists():
            pytest.skip("Playwright Firefox is not installed")
        launch = pw.firefox.launch_persistent_context
        monkeypatch.setattr(pw.firefox, "launch_persistent_context", lambda profile, **kwargs:
                            launch(profile, executable_path=str(linux_worker.executable), **kwargs))
        if attested:
            browser = rendering._launch_browser(pw)
            try:
                gc.collect()
                output = capfd.readouterr().err
                profiles = re.findall(r"-profile (/tmp/playwright_firefoxdev_profile-[^\s]+)", output)
                assert len(profiles) == 1
                profile = Path(profiles[0])
                assert profile.exists() and profile.stat().st_mode & 0o777 == 0o700
                (profile / "startupCache").mkdir(exist_ok=True)
                (profile / "startupCache" / "synthetic-shutdown-cache").write_text("cache")
            finally:
                browser.close()
        else:
            with pytest.raises(ValueError, match="pagebuilder_renderer_unavailable"):
                rendering._launch_browser(pw)
            output = capfd.readouterr().err
            profiles = re.findall(r"-profile (/tmp/playwright_firefoxdev_profile-[^\s]+)", output)
            assert len(profiles) == 1
            profile = Path(profiles[0])
        assert not profile.exists()
        assert rendering.os.environ["HOME"] == str(linux_worker.home)
        assert rendering.os.environ["TMPDIR"] == "/tmp"
