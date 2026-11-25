import stat
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


def private_test_install(monkeypatch, directory):
    lstat = Path.lstat
    ancestors = {directory, *directory.parents, Path("/usr"), Path("/usr/bin")}

    def permissions(path):
        info = lstat(path)
        if path not in ancestors or not stat.S_ISDIR(info.st_mode):
            return info
        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_gid=0,
                               st_dev=info.st_dev, st_ino=info.st_ino)

    monkeypatch.setattr(Path, "lstat", permissions)


@contextmanager
def installed_test_tool(monkeypatch, provider, binary):
    from desktop_runtime import agent_status

    private_test_install(monkeypatch, binary.parent)
    with monkeypatch.context() as probe:
        probe.setattr(agent_status, "provider_binary", lambda name: str(binary) if name == provider else None)
        probe.setattr(agent_status, "_status", lambda *args, **kwargs: {"available": True})
        yield


def pin_test_tool(monkeypatch, provider, binary):
    from desktop_runtime import agent_status

    with installed_test_tool(monkeypatch, provider, binary):
        agent_status.pin_provider(provider)
