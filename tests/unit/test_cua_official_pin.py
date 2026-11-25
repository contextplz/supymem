"""Admission and failed paired publication, without executing a native driver."""
import hashlib
import io
import tarfile
from types import SimpleNamespace

import pytest

from tests.unit.test_cu_vendor_modal_build import module


@pytest.fixture
def official(tmp_path, monkeypatch):
    mod = module("prepare_cua_driver")
    binaries = {"cua-driver": b"signed-driver", "cua-cursor-theme": b"signed-cursor"}
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as package:
        for name, value in binaries.items():
            info = tarfile.TarInfo(name)
            info.size = len(value)
            package.addfile(info, io.BytesIO(value))
    archive = data.getvalue()
    monkeypatch.setattr(mod, "ARCHIVE_SHA256", hashlib.sha256(archive).hexdigest())
    monkeypatch.setattr(mod, "BINARIES_SHA256", {name: hashlib.sha256(value).hexdigest() for name, value in binaries.items()})
    monkeypatch.setattr(mod, "OUTPUT", tmp_path / "cua-driver")
    monkeypatch.setattr(mod, "CURSOR_OUTPUT", tmp_path / "cua-cursor-theme")
    for path in (mod.OUTPUT, mod.CURSOR_OUTPUT):
        path.write_bytes(b"old-" + path.name.encode())
        path.chmod(0o700)
    calls = []
    def signature(args, **_kwargs):
        calls.append(args)
        assert all(path.read_bytes().startswith(b"old-") for path in (mod.OUTPUT, mod.CURSOR_OUTPUT))
        return SimpleNamespace(returncode=0, stderr="TeamIdentifier=" + mod.TEAM_ID)
    def version(args, **_kwargs):
        calls.append(args)
        assert len(calls) == 5 and all("codesign" == row[0] for row in calls[:4])
        assert all(path.read_bytes().startswith(b"old-") for path in (mod.OUTPUT, mod.CURSOR_OUTPUT))
        return "cua-driver 0.33.3\n"
    monkeypatch.setattr(mod.subprocess, "run", signature)
    monkeypatch.setattr(mod.subprocess, "check_output", version)
    return mod, archive, binaries, calls


def unchanged(mod):
    for path in (mod.OUTPUT, mod.CURSOR_OUTPUT):
        assert path.read_bytes() == b"old-" + path.name.encode()
        assert path.stat().st_mode & 0o777 == 0o700


def test_both_verified_before_publication(official):
    mod, archive, binaries, calls = official
    mod.stage_official_archive(archive)
    assert {path.name: path.read_bytes() for path in (mod.OUTPUT, mod.CURSOR_OUTPUT)} == binaries
    assert all(path.stat().st_mode & 0o777 == 0o755 for path in (mod.OUTPUT, mod.CURSOR_OUTPUT))
    assert [row[1] for row in calls[:4]] == ["--verify", "-dv", "--verify", "-dv"]
    assert not list(mod.OUTPUT.parent.glob(".cua-stage-*"))


@pytest.mark.parametrize("failure", ["archive", "driver", "cursor", "signature", "team", "version"])
def test_untrusted_pair_never_changes_either_destination(official, monkeypatch, failure):
    mod, archive, _binaries, calls = official
    if failure == "archive":
        archive += b"changed"
    elif failure in {"driver", "cursor"}:
        mod.BINARIES_SHA256["cua-driver" if failure == "driver" else "cua-cursor-theme"] = "0" * 64
    elif failure in {"signature", "team"}:
        original = mod.subprocess.run
        def refuse(args, **kwargs):
            result = original(args, **kwargs)
            if args[-1].endswith("cua-cursor-theme"):
                if failure == "signature" and args[1] == "--verify":
                    return SimpleNamespace(returncode=1, stderr="")
                if failure == "team" and args[1] == "-dv":
                    return SimpleNamespace(returncode=0, stderr="TeamIdentifier=OTHER")
            return result
        monkeypatch.setattr(mod.subprocess, "run", refuse)
    else:
        monkeypatch.setattr(mod.subprocess, "check_output", lambda *_a, **_k: "cua-driver 0.33.30\n")
    with pytest.raises(ValueError):
        mod.stage_official_archive(archive)
    unchanged(mod)
    if failure in {"archive", "driver", "cursor"}:
        assert calls == []
    assert not list(mod.OUTPUT.parent.glob(".cua-stage-*"))


@pytest.mark.parametrize("phase", ["backup", "publish"])
def test_second_replacement_failure_restores_old_pair(official, monkeypatch, phase):
    mod, archive, _binaries, _calls = official
    replace = mod.os.replace
    def fail_once(source, destination):
        if ((phase == "backup" and destination.name == "cua-cursor-theme.previous")
                or (phase == "publish" and source.name == "cua-cursor-theme" and destination == mod.CURSOR_OUTPUT)):
            raise OSError("owned fixture replacement failure")
        return replace(source, destination)
    monkeypatch.setattr(mod.os, "replace", fail_once)
    with pytest.raises(OSError):
        mod.stage_official_archive(archive)
    unchanged(mod)
    assert not list(mod.OUTPUT.parent.glob(".cua-stage-*"))


@pytest.mark.parametrize("phase", ["first_backup", "second_backup", "first_publish", "second_publish"])
def test_interrupt_immediately_after_each_rename_preserves_old_pair(official, monkeypatch, phase):
    mod, archive, _binaries, _calls = official
    replace, interrupted = mod.os.replace, False
    def interrupt_after_move(source, destination):
        nonlocal interrupted
        result = replace(source, destination)
        match = {"first_backup": destination.name == "cua-driver.previous",
            "second_backup": destination.name == "cua-cursor-theme.previous",
            "first_publish": source.name == "cua-driver" and destination == mod.OUTPUT,
            "second_publish": source.name == "cua-cursor-theme" and destination == mod.CURSOR_OUTPUT}[phase]
        if match and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("owned fixture signal after rename")
        return result
    monkeypatch.setattr(mod.os, "replace", interrupt_after_move)
    with pytest.raises(KeyboardInterrupt):
        mod.stage_official_archive(archive)
    assert interrupted
    unchanged(mod)
    assert not list(mod.OUTPUT.parent.glob(".cua-stage-*"))


def test_failed_rollback_retains_recovery_bytes(official, monkeypatch):
    mod, archive, _binaries, _calls = official
    replace = mod.os.replace
    def fail(source, destination):
        if ((source.name == "cua-cursor-theme" and destination == mod.CURSOR_OUTPUT)
                or source.name == "cua-driver.previous"):
            raise OSError("owned fixture unavailable destination")
        return replace(source, destination)
    monkeypatch.setattr(mod.os, "replace", fail)
    with pytest.raises(RuntimeError, match="recovery retained"):
        mod.stage_official_archive(archive)
    directories = list(mod.OUTPUT.parent.glob(".cua-stage-*"))
    assert len(directories) == 1
    assert (directories[0] / "cua-driver.previous").read_bytes() == b"old-cua-driver"
    assert (directories[0] / "cua-cursor-theme.previous").read_bytes() == b"old-cua-cursor-theme"


@pytest.mark.parametrize("kind", ["duplicate", "symlink"])
def test_ambiguous_or_link_members_refused(official, monkeypatch, kind):
    mod, _archive, binaries, calls = official
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as package:
        for name, value in binaries.items():
            info = tarfile.TarInfo(name)
            if kind == "symlink" and name == "cua-driver":
                info.type, info.linkname = tarfile.SYMTYPE, "outside"
                package.addfile(info)
            else:
                info.size = len(value)
                package.addfile(info, io.BytesIO(value))
                if kind == "duplicate" and name == "cua-driver":
                    package.addfile(info, io.BytesIO(value))
    archive = data.getvalue()
    monkeypatch.setattr(mod, "ARCHIVE_SHA256", hashlib.sha256(archive).hexdigest())
    with pytest.raises(ValueError, match="members_invalid"):
        mod.stage_official_archive(archive)
    unchanged(mod)
    assert calls == []
