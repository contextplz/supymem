import json
import os
import socket
import tempfile

from desktop_runtime import agent_cursor as ac


def test_event_schema():
    assert ac.build_event("click", 10, 20.26, "Clicking Save") == {
        "v": 1, "kind": "click", "x": 10.0, "y": 20.3, "label": "Clicking Save"}
    assert ac.build_event("begin") == {"v": 1, "kind": "begin"}
    assert ac.build_event("read") == {"v": 1, "kind": "read"}  # no target: cursor stays put


def test_invalid_events_are_dropped():
    for args in [("explode", 1, 1), ("click",), ("move", 1), ("move", "1", 2), ("move", float("nan"), 1),
                 ("move", 1e9, 1), ("move", True, 1)]:
        assert ac.build_event(*args) is None, args


def test_label_is_cleaned_and_bounded():
    assert ac.build_event("read", label="a\nb\x00c")["label"] == "a b c"
    assert len(ac.build_event("read", label="x" * 200)["label"]) == ac.MAX_LABEL
    assert "label" not in ac.build_event("read", label="   ")


def test_emit_is_noop_without_listener(monkeypatch):
    monkeypatch.delenv(ac.SOCKET_ENV, raising=False)
    assert ac.emit("click", 1, 1) is False
    monkeypatch.setenv(ac.SOCKET_ENV, "/nonexistent/supymem.sock")
    assert ac.emit("click", 1, 1) is False  # never raises


def test_emit_delivers_datagram(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(dir="/tmp"), "c.sock")  # sun_path is ~104 bytes
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(path)
    monkeypatch.setenv(ac.SOCKET_ENV, path)
    ac._sock = None
    assert ac.emit("type", 5, 6, "Typing…") is True
    assert json.loads(server.recv(2048)) == {"v": 1, "kind": "type", "x": 5.0, "y": 6.0, "label": "Typing…"}
    server.close()
    ac._sock = None


def test_failed_delivery_explicitly_closes_owned_socket(monkeypatch):
    """Retained instrumentation references must not defer resource release."""
    created = []

    class FailedSocket:
        closed = False

        def setblocking(self, value):
            assert value is False

        def sendto(self, body, path):
            raise OSError('listener unavailable')

        def close(self):
            self.closed = True

    def create(*args):
        item = FailedSocket()
        created.append(item)
        return item

    monkeypatch.setattr(ac, '_sock', None)
    monkeypatch.setattr(ac.socket, 'socket', create)
    monkeypatch.setenv(ac.SOCKET_ENV, '/tmp/owned-unavailable-cursor.sock')
    for _ in range(3):
        assert ac.emit('read') is False
    assert len(created) == 3
    assert all(item.closed for item in created)
    assert ac._sock is None


def test_concurrent_visual_hint_drops_without_waiting(monkeypatch):
    monkeypatch.setenv(ac.SOCKET_ENV, '/tmp/owned-cursor.sock')
    assert ac._socket_lock.acquire(blocking=False)
    try:
        assert ac.emit('read') is False
    finally:
        ac._socket_lock.release()


def test_socket_reconnects_after_owned_listener_restart(monkeypatch):
    with tempfile.TemporaryDirectory(dir='/tmp') as directory:
        path = os.path.join(directory, 'cursor.sock')
        monkeypatch.setenv(ac.SOCKET_ENV, path)
        monkeypatch.setattr(ac, '_sock', None)
        assert ac.emit('begin') is False
        assert ac._sock is None
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
            server.bind(path)
            server.settimeout(1)
            assert ac.emit('read', -100, 50) is True
            assert json.loads(server.recv(2048)) == {'v': 1, 'kind': 'read', 'x': -100.0, 'y': 50.0}
        failed = ac._sock
        assert ac.emit('end') is False
        assert failed.fileno() == -1
        assert ac._sock is None
        os.unlink(path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
            server.bind(path)
            server.settimeout(1)
            try:
                assert ac.emit('begin') is True
                assert json.loads(server.recv(2048)) == {'v': 1, 'kind': 'begin'}
            finally:
                if ac._sock is not None:
                    ac._sock.close()
                    ac._sock = None


def test_interleaved_execution_hints_drop_stale_end_update_and_legacy(monkeypatch):
    import contextvars
    with tempfile.TemporaryDirectory(dir='/tmp') as directory:
        path = os.path.join(directory, 'cursor.sock')
        monkeypatch.setenv(ac.SOCKET_ENV, path)
        monkeypatch.setattr(ac, '_sock', None)
        monkeypatch.setattr(ac, '_active_owner', None)
        monkeypatch.setattr(ac, '_published_owner', None)
        monkeypatch.setattr(ac, '_listener_identity', None)
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
            server.bind(path)
            server.settimeout(0.05)
            with ac.execution() as first:
                old = contextvars.copy_context()
                with ac.execution() as second:
                    assert first != second
                    for kind in ['end', 'done', 'begin']:
                        assert old.run(ac.emit, kind) is False
                    assert old.run(ac.emit, 'click', 4, 5) is False
                    assert contextvars.Context().run(ac.emit, 'end') is False
                    assert ac.emit('type', 7, 8) is True
                    ended = contextvars.copy_context()
                assert ended.run(ac.emit, 'begin') is False
                assert ended.run(ac.emit, 'read') is False
                assert ac.emit('read') is False
            rows = [json.loads(server.recv(2048)) for _ in range(4)]
            assert [(r['kind'], r['owner']) for r in rows] == [
                ('begin', first), ('begin', second), ('type', second), ('end', second)]
            try:
                server.recv(2048)
                assert False, 'stale execution must not send a datagram'
            except socket.timeout:
                pass
        if ac._sock is not None:
            ac._sock.close()
            ac._sock = None


def test_owned_feed_restates_begin_after_listener_replacement(monkeypatch):
    with tempfile.TemporaryDirectory(dir='/tmp') as directory:
        path = os.path.join(directory, 'cursor.sock')
        monkeypatch.setenv(ac.SOCKET_ENV, path)
        monkeypatch.setattr(ac, '_sock', None)
        monkeypatch.setattr(ac, '_active_owner', None)
        monkeypatch.setattr(ac, '_published_owner', None)
        monkeypatch.setattr(ac, '_listener_identity', None)
        with ac.execution() as owner:
            # Initial begin has no listener; later hints must not invent receiver ownership.
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
                server.bind(path)
                server.settimeout(1)
                assert ac.emit('read') is True
                assert json.loads(server.recv(2048)) == {'v': 1, 'kind': 'begin', 'owner': owner}
                assert json.loads(server.recv(2048)) == {'v': 1, 'kind': 'read', 'owner': owner}
            os.unlink(path)
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
                server.bind(path)
                server.settimeout(1)
                assert ac.emit('read') is True
                assert json.loads(server.recv(2048)) == {'v': 1, 'kind': 'begin', 'owner': owner}
                assert json.loads(server.recv(2048)) == {'v': 1, 'kind': 'read', 'owner': owner}
        if ac._sock is not None:
            ac._sock.close()
            ac._sock = None


def test_busy_begin_still_reserves_new_lease_and_requires_fresh_begin(monkeypatch):
    import contextvars
    with tempfile.TemporaryDirectory(dir='/tmp') as directory:
        path = os.path.join(directory, 'cursor.sock')
        monkeypatch.setenv(ac.SOCKET_ENV, path)
        monkeypatch.setattr(ac, '_sock', None)
        monkeypatch.setattr(ac, '_active_owner', None)
        monkeypatch.setattr(ac, '_published_owner', None)
        monkeypatch.setattr(ac, '_listener_identity', None)
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
            server.bind(path)
            server.settimeout(0.05)
            with ac.execution() as first:
                old = contextvars.copy_context()
                assert ac._socket_lock.acquire(blocking=False)
                newer = ac.execution()
                second = newer.__enter__()  # Its initial begin is dropped, never blocks.
                ac._socket_lock.release()
                try:
                    assert old.run(ac.emit, 'end') is False
                    assert old.run(ac.emit, 'read') is False
                    assert ac.emit('read') is True
                finally:
                    newer.__exit__(None, None, None)
            rows = [json.loads(server.recv(2048)) for _ in range(4)]
            assert [(r['kind'], r['owner']) for r in rows] == [
                ('begin', first), ('begin', second), ('read', second), ('end', second)]
        if ac._sock is not None:
            ac._sock.close()
            ac._sock = None

def test_listener_replacement_with_reused_inode_reestablishes_owned_begin(monkeypatch):
    from types import SimpleNamespace
    # Real datagrams, deterministic inode reuse: Linux can immediately reuse the
    # removed socket inode. ctime changes when the new listener is created.
    with tempfile.TemporaryDirectory(dir='/tmp') as directory:
        path = os.path.join(directory, 'cursor.sock')
        monkeypatch.setenv(ac.SOCKET_ENV, path)
        for name in ('_sock', '_active_owner', '_published_owner', '_listener_identity'):
            monkeypatch.setattr(ac, name, None)
        real_stat = os.stat
        stamp = [1]
        def reused_inode(candidate, *args, **kwargs):
            metadata = real_stat(candidate, *args, **kwargs)
            if candidate == path:
                return SimpleNamespace(st_dev=metadata.st_dev, st_ino=123, st_ctime_ns=stamp[0])
            return metadata
        monkeypatch.setattr(ac.os, 'stat', reused_inode)
        try:
            with ac.execution() as owner:
                for generation in (1, 2):
                    stamp[0] = generation
                    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
                        server.bind(path)
                        server.settimeout(1)
                        assert ac.emit('read') is True
                        rows = [json.loads(server.recv(2048)) for _ in range(2)]
                        assert rows == [{'v': 1, 'kind': 'begin', 'owner': owner},
                                        {'v': 1, 'kind': 'read', 'owner': owner}]
                    os.unlink(path)
        finally:
            if ac._sock is not None:
                ac._sock.close()
                ac._sock = None
