"""Actual filesystem/archive denials at the artifact boundary."""
import io
import os
import stat
import zipfile

import pytest

from core.artifact_policy import (ArtifactLimits, ArtifactRejected, extract_zip, read_file,
                                  replace_file, safe_name, tree_manifest)


def archive(entries, compression=zipfile.ZIP_STORED):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as out:
        for name, blob in entries:
            out.writestr(name, blob)
    return stream.getvalue()


@pytest.mark.parametrize("name", ["/root/key", "../key", "a/../../x", "C:/foo", "a\\b", "a//b", "./x", "a/./b",
                                  "a\x00b", "a\u202eb", ".env", "src/.env.production", ".aws/credentials", "id_rsa",
                                  "a/private.pem", ".git/config", "e\u0301.txt"])
def test_unsafe_paths_are_rejected(name):
    with pytest.raises(ArtifactRejected):
        safe_name(name)


def test_valid_archive_records_real_bytes_and_hash(tmp_path):
    result = extract_zip(archive([("src/main.py", b"print('hello')"), (".gitignore", b"cache/")]), tmp_path / "out")
    assert {item["path"] for item in result} == {"src/main.py", ".gitignore"}
    assert read_file(tmp_path / "out", "src/main.py") == b"print('hello')"
    import hashlib
    assert next(row for row in result if row["path"] == "src/main.py")["sha256"] == hashlib.sha256(b"print('hello')").hexdigest()


@pytest.mark.parametrize("entries", [[("A/x", b"a"), ("a/y", b"b")], [("a", b"file"), ("a/b", b"nested")],
                                    [("x", b"first"), ("x", b"second")], [("safe", b"ok"), ("../outside", b"bad")]])
def test_colliding_or_bad_archives_never_create_partial_destination(tmp_path, entries):
    with pytest.raises(ArtifactRejected):
        extract_zip(archive(entries), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_links_devices_and_secret_bytes_are_rejected(tmp_path):
    for mode in (stat.S_IFLNK | 0o777, stat.S_IFCHR | 0o600, stat.S_IFIFO | 0o600):
        info = zipfile.ZipInfo("source")
        info.create_system = 3
        info.external_attr = mode << 16
        with pytest.raises(ArtifactRejected):
            extract_zip(archive([(info, b"/etc/passwd")]), tmp_path / "out")
    with pytest.raises(ArtifactRejected, match="secret_detected"):
        extract_zip(archive([("src/main.py", b"TOKEN='ghp_" + b"a" * 36 + b"'")]), tmp_path / "out")
    with pytest.raises(ArtifactRejected, match="secret_detected"):
        extract_zip(archive([("src/main.py", b"run-token-secret")]), tmp_path / "out", exact_secrets=("run-token-secret",))
    with pytest.raises(ArtifactRejected, match="secret_detected"):
        extract_zip(archive([("src/main.py", b"TOKEN='short'")]), tmp_path / "out", exact_secrets=("short",))


def test_archive_expansion_and_file_count_are_bounded(tmp_path):
    with pytest.raises(ArtifactRejected, match="expansion_limit"):
        extract_zip(archive([("huge", b"a" * 100_000)], zipfile.ZIP_DEFLATED), tmp_path / "out")
    limits = ArtifactLimits(max_files=1, max_file_bytes=4, max_total_bytes=4)
    for entries in ([('x', b'12345')], [('x', b'1'), ('y', b'2')]):
        with pytest.raises(ArtifactRejected):
            extract_zip(archive(entries), tmp_path / "out", limits=limits)
    assert not (tmp_path / "out").exists()


def test_source_symlink_hardlink_fifo_and_parent_links_are_denied(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.write_text("sensitive")
    (root / "link").symlink_to(outside)
    with pytest.raises(ArtifactRejected):
        tree_manifest(root)
    (root / "link").unlink()
    os.link(outside, root / "hard")
    with pytest.raises(ArtifactRejected, match="file_type"):
        read_file(root, "hard")
    (root / "hard").unlink()
    os.mkfifo(root / "pipe")
    with pytest.raises(ArtifactRejected, match="file_type"):
        read_file(root, "pipe")
    (root / "pipe").unlink()
    (root / "parent").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ArtifactRejected):
        replace_file(root, "parent/outside", b"overwrite")
    assert outside.read_text() == "sensitive"


def test_replace_never_overwrites_a_symlink_target(tmp_path):
    target = tmp_path / "target"
    target.write_text("original")
    (tmp_path / "link").symlink_to(target)
    with pytest.raises(ArtifactRejected):
        replace_file(tmp_path, "link", b"changed")
    assert target.read_text() == "original"
    replace_file(tmp_path, "target", b"accepted")
    assert target.read_bytes() == b"accepted"


def test_tree_limits_and_skip_dirs(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / ".env").write_text("ignored dependency content")
    (tmp_path / "main.py").write_text("pass")
    assert [row["path"] for row in tree_manifest(tmp_path, skip_dirs=frozenset({"node_modules"}))] == ["main.py"]
    with pytest.raises(ArtifactRejected):
        tree_manifest(tmp_path, skip_dirs=frozenset({"node_modules"}), limits=ArtifactLimits(max_file_bytes=2))
