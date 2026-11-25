"""Linux check isolation and a host-to-private-loopback preview relay."""
from __future__ import annotations

import ctypes
import fcntl
import os
from pathlib import Path
import resource
import shutil
import socket
import struct
import subprocess
import sys
import threading


def backend():
    return shutil.which("bwrap") or shutil.which("unshare")


def command(tool, workspace, temporary, *, node=None, readonly=False, probe=False):
    helper = Path(__file__).resolve()
    if Path(tool).name == "bwrap":
        args = [tool, "--unshare-all", "--die-with-parent", "--new-session"]
        for root in ("/usr", "/lib", "/lib64"):
            if Path(root).exists():
                args.extend(["--ro-bind", root, root])
        args.extend(["--proc", "/proc", "--dev", "/dev", "--ro-bind", str(helper), "/checks.py"])
        if node and not Path(node).is_relative_to(Path("/usr")):
            args.extend(["--ro-bind", node, node])
        if not probe:
            args.extend(["--bind", str(temporary), str(temporary),
                "--ro-bind" if readonly else "--bind", str(workspace), str(workspace),
                "--chdir", str(workspace)])
            if (workspace / ".git").is_dir():
                args.extend(["--tmpfs", str(workspace / ".git")])
        for root in ("/", "/dev", "/proc"):
            args.extend(["--remount-ro", root])
        args.extend(["/usr/bin/python3", "-I", "/checks.py", "bwrap"])
    else:
        args = [tool, "--user", "--map-root-user", "--net", "--mount", "--pid", "--fork",
            "--kill-child", "--mount-proc", "/usr/bin/python3", "-I", str(helper), "restricted"]
    return [*args, str(workspace), str(temporary), str(int(readonly)), "probe" if probe else "run"]


def available():
    tool = backend()
    if not tool or not Path("/usr/bin/python3").is_file():
        return False
    try:
        return subprocess.run(command(tool, Path("/"), Path("/"), probe=True),
            env={"PATH": "/usr/bin:/bin", "OPENSSL_CONF": "/dev/null"},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, start_new_session=True).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _landlock(workspace, temporary, readonly, *, node=None, probe=False):
    library = ctypes.CDLL(None, use_errno=True)
    abi = library.syscall(444, 0, 0, 1)
    if abi < 3:
        raise OSError("Filesystem isolation requires Landlock ABI 3")
    handled = (1 << 15) - 1
    ruleset = library.syscall(444, ctypes.byref(ctypes.c_uint64(handled)), 8, 0)
    if ruleset < 0:
        raise OSError("Filesystem isolation could not start")
    read = (1 << 0) | (1 << 2) | (1 << 3)
    try:
        roots = [(Path(root), read) for root in ("/usr", "/lib", "/lib64", "/proc")]
        roots.extend((Path(root), 1 << 2) for root in ("/dev/zero", "/dev/random", "/dev/urandom"))
        if node:
            roots.append((Path(node), (1 << 0) | (1 << 2)))
        if not probe:
            roots.extend([(workspace, read if readonly else handled), (temporary, handled)])
        for path, access in roots:
            if not path.exists():
                continue
            descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = struct.pack("=Qi", access, descriptor)
                if library.syscall(445, ruleset, 1, ctypes.c_char_p(rule), 0) < 0:
                    raise OSError("Filesystem isolation rule failed")
            finally:
                os.close(descriptor)
        descriptor = os.open("/dev/null", os.O_PATH | os.O_CLOEXEC)
        try:
            rule = struct.pack("=Qi", (1 << 1) | (1 << 2), descriptor)
            if library.syscall(445, ruleset, 1, ctypes.c_char_p(rule), 0) < 0:
                raise OSError("Filesystem isolation rule failed")
        finally:
            os.close(descriptor)
        if library.prctl(38, 1, 0, 0, 0) or library.syscall(446, ruleset, 0):
            raise OSError("Filesystem isolation could not be enforced")
    finally:
        os.close(ruleset)


def _isolate(kind, workspace, temporary, readonly, probe, node=None):
    library = ctypes.CDLL(None, use_errno=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        flags = fcntl.ioctl(control, 0x8913, struct.pack("16sH14s", b"lo", 0, b""))
        current = struct.unpack_from("H", flags, 16)[0]
        if not current & 1:
            fcntl.ioctl(control, 0x8914, struct.pack("16sH14s", b"lo", current | 1, b""))
    if kind == "restricted":
        if not probe and (workspace / ".git").is_dir():
            library = ctypes.CDLL(None, use_errno=True)
            if library.mount(b"tmpfs", os.fsencode(workspace / ".git"), b"tmpfs", 0, b"size=4096"):
                raise OSError("Private repository metadata could not be isolated")
        _landlock(workspace, temporary, readonly, node=node, probe=probe)
        for capability in range(64):
            if library.prctl(24, capability, 0, 0, 0) and ctypes.get_errno() != 22:
                raise OSError("Process privileges could not be restricted")
        header = struct.pack("=II", 0x20080522, 0)
        if library.capset(ctypes.c_char_p(header), ctypes.c_char_p(bytes(24))):
            raise OSError("Process privileges could not be restricted")
    if library.prctl(38, 1, 0, 0, 0) or library.prctl(4, 0, 0, 0, 0):
        raise OSError("Process privileges could not be restricted")
    _seccomp()
    for limit, value in ((resource.RLIMIT_CPU, 120), (resource.RLIMIT_AS, 16 * 1024**3),
            (resource.RLIMIT_DATA, 512 * 1024**2),
            (resource.RLIMIT_FSIZE, 4 * 1024**2), (resource.RLIMIT_NOFILE, 128), (resource.RLIMIT_CORE, 0)):
        resource.setrlimit(limit, (value, value))


def _seccomp():
    class Comparison(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
            ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    context = library.seccomp_init(0x7fff0000)
    if not context:
        raise OSError("Network isolation could not start")
    try:
        domain = Comparison(0, 4, socket.AF_UNIX, 0)
        syscall = library.seccomp_syscall_resolve_name(b"socket")
        if syscall < 0 or library.seccomp_rule_add(context, 0x50001, syscall, 1, domain):
            raise OSError("Network isolation rule failed")
        syscall = library.seccomp_syscall_resolve_name(b"socketpair")
        for kind in (socket.SOCK_DGRAM, socket.SOCK_SEQPACKET):
            if syscall < 0 or library.seccomp_rule_add(context, 0x50001, syscall, 2,
                    domain, Comparison(1, 7, 15, kind)):
                raise OSError("Network isolation rule failed")
        for name in (b"ptrace", b"process_vm_readv", b"process_vm_writev", b"pidfd_getfd"):
            syscall = library.seccomp_syscall_resolve_name(name)
            if syscall < 0 or library.seccomp_rule_add(context, 0x50001, syscall, 0):
                raise OSError("Process isolation rule failed")
        if library.seccomp_load(context):
            raise OSError("Network isolation could not be enforced")
    finally:
        library.seccomp_release(context)


class LoopbackRelay:
    def __init__(self, channel, port):
        from desktop_runtime.local_http import LoopbackHTTPRelay

        self.channel, self.lock = channel, threading.Lock()
        self.http = LoopbackHTTPRelay(port, self.connection)

    def connection(self):
        with self.lock:
            self.channel.settimeout(2)
            self.channel.sendall(b"c")
            data, descriptors, _, _ = socket.recv_fds(self.channel, 1, 1)
            if data != b"1" or len(descriptors) != 1:
                for descriptor in descriptors:
                    os.close(descriptor)
                raise OSError("Local check server is not listening")
            return socket.socket(fileno=descriptors[0])

    def listening(self):
        return self.http.listening()

    def close(self):
        self.http.close()
        self.channel.close()


def _main():
    kind, workspace, temporary, readonly, mode = sys.argv[1:6]
    _isolate(kind, Path(workspace), Path(temporary), readonly == "1", mode == "probe",
        sys.argv[7] if mode != "probe" else None)
    if mode == "probe":
        return 0
    descriptor = int(sys.argv[6])
    argv = sys.argv[7:]
    if descriptor < 0:
        os.execv(argv[0], argv)
    with socket.socket(fileno=descriptor) as channel:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, close_fds=True)
        channel.settimeout(.1)
        while child.poll() is None:
            try:
                if channel.recv(1) != b"c":
                    break
                try:
                    with socket.create_connection(("127.0.0.1", int(os.environ["PORT"])), timeout=1) as connection:
                        socket.send_fds(channel, [b"1"], [connection.fileno()])
                except OSError:
                    channel.sendall(b"0")
            except socket.timeout:
                continue
        if child.poll() is None:
            child.terminate()
        return child.wait(timeout=3)


if __name__ == "__main__":
    try:
        sys.exit(_main())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print("Install bubblewrap with your Linux package manager, allow user namespaces, then open Build activity and retry your build.", file=sys.stderr)
        sys.exit(1)
