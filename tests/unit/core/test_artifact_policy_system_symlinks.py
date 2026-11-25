import os
import tempfile
from pathlib import Path

import pytest

from core.artifact_policy import ArtifactRejected, write_file


def test_temp_dir_behind_system_symlink_is_writable(tmp_path, monkeypatch):
    real = tmp_path / "private_tmp"
    real.mkdir()
    alias = tmp_path / "tmp_alias"
    alias.symlink_to(real)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(alias))
    target = alias / "landing-source-x"
    (real / "landing-source-x").mkdir()
    write_file(target, "index.html", b"<p>ok</p>")
    assert (real / "landing-source-x" / "index.html").read_bytes() == b"<p>ok</p>"


def test_symlink_below_the_root_is_still_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(ArtifactRejected):
        write_file(tmp_path / "link", "index.html", b"x")


@pytest.mark.parametrize("root_name", ["HOME", "COMPANY_OS_DATA_DIR"])
@pytest.mark.parametrize("domain", ["Freight invoice review", "Family recipe planner"])
def test_local_root_alias_preserves_child_link_rejection(tmp_path, monkeypatch, root_name, domain):
    from core.artifact_policy import extract_zip, read_file, replace_file, tree_manifest
    import io
    import zipfile

    root = tmp_path / "real"
    root.mkdir()
    alias = tmp_path / "Application Support"
    alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv(root_name, str(alias))
    write_file(alias, "source/index.html", domain.encode())
    replace_file(alias, "source/index.html", (domain + " updated").encode())
    assert read_file(alias, "source/index.html") == (domain + " updated").encode()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("index.html", domain)
    assert extract_zip(archive.getvalue(), alias / "export") == tree_manifest(alias / "export")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "child").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ArtifactRejected):
        write_file(alias / "child", "index.html", domain.encode())
    assert not (outside / "index.html").exists()


def test_macos_style_var_temp_dir_round_trip():
    with tempfile.TemporaryDirectory(prefix="landing-source-") as raw:
        write_file(Path(raw), "index.html", b"<p>ok</p>")
        assert (Path(os.path.realpath(raw)) / "index.html").exists()
