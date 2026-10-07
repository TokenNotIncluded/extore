"""Bounded native probes, independent of the processor runtime implementation.

These fixtures are test-owned catalog code. They never inspect production
files or keys, create more than one attempted child, or write more than 17 MiB.
"""

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from extore.processor_runtime import (
    launch_command,
    process_environment,
    sandbox_command,
)


def _command(tmp_path, source, *, memory=64, cpu=2, mount_probe=False):
    package = tmp_path / "catalog" / "extore_processors"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(source, encoding="utf-8")
    workflow = {
        "variables": {"MARKER": "synthetic-value"},
        "secrets": {"TOKEN": "synthetic-secret"},
        "runtime": {"memory_mb": memory, "cpu_seconds": cpu},
    }
    native = sandbox_command(
        "probe", SimpleNamespace(__file__=str(package / "__init__.py"))
    )
    if mount_probe:
        # Test the mount quota independently of the stricter production guard.
        # This trusted, finite writer stays inside the exact same namespaces,
        # mounts, capabilities and hard resource limits. No merchant API can
        # change the fixed production entry point in this way.
        native[native.index("--") + 1 :] = [
            "/python/bin/python3",
            "-I",
            "-S",
            "-B",
            "-c",
            source,
        ]
    return launch_command(native, workflow), process_environment(workflow)


def _run(
    tmp_path, source, *, payload=None, memory=64, cpu=2, timeout=8, mount_probe=False
):
    command, environment = _command(
        tmp_path, source, memory=memory, cpu=cpu, mount_probe=mount_probe
    )
    return subprocess.run(
        command,
        input=json.dumps(payload or {}).encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        close_fds=True,
        timeout=timeout,
        check=False,
    )


def _json_result(result):
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert not result.stderr
    return json.loads(result.stdout)


def test_native_caps_namespace_host_canary_and_readonly_mounts(tmp_path):
    host_canary = tmp_path / "host-only-canary"
    host_canary.write_text("synthetic-host-only", encoding="utf-8")
    source = """
import json, os, sys
from pathlib import Path
payload = json.load(sys.stdin)
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
mounts = {line.split()[1]: line.split()[3] for line in Path('/proc/mounts').read_text().splitlines()}
checks = {}
for path in (payload['canary'], '/proc/1/root' + payload['canary'], '/code/extore_processors/__main__.py', '/dev/shm/probe', '/proc/probe', '/new-host-root'):
    try:
        with open(path, 'ab') as file:
            file.write(b'x')
        checks[path] = 'allowed'
    except OSError as error:
        checks[path] = error.errno
print(json.dumps({'pid': os.getpid(), 'uid': os.getuid(), 'status': status, 'mounts': mounts, 'checks': checks, 'netns': os.readlink('/proc/self/ns/net'), 'environment': dict(os.environ), 'third_party_visible': any('site-packages' in p for p in sys.path)}))
"""
    value = _json_result(_run(tmp_path, source, payload={"canary": str(host_canary)}))
    assert value["pid"] < 10 and value["uid"] == 1000
    for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        assert int(value["status"][name].strip(), 16) == 0
    assert value["status"]["NoNewPrivs"].strip() == "1"
    assert value["status"]["Seccomp"].strip() == "2"
    assert value["netns"] != os.readlink("/proc/self/ns/net")
    assert value["checks"][str(host_canary)] != "allowed"
    assert all(result != "allowed" for result in value["checks"].values())
    assert host_canary.read_text(encoding="utf-8") == "synthetic-host-only"
    for mount in ("/", "/dev", "/proc", "/code/extore_processors"):
        assert "ro" in value["mounts"][mount].split(",")
    assert not value["third_party_visible"]
    assert value["environment"]["EXTORE_WORKFLOW_TOKEN"] == "synthetic-secret"
    assert (
        "TOKEN" not in value["environment"] and "PYTHONPATH" not in value["environment"]
    )


def test_native_fork_exec_clone_and_new_namespace_denied(tmp_path):
    # The safety fallback creates at most one child, which exits immediately.
    source = """
import ctypes, errno, json, os
libc = ctypes.CDLL(None, use_errno=True)
checks = {}
for name, args in [('fork', ()), ('vfork', ()), ('unshare', (0x10000000,)), ('mount', (b'none', b'/tmp', b'tmpfs', 0, None)), ('ptrace', (0, 0, None, None))]:
    if name == 'vfork':
        continue  # A vfork child cannot safely return into Python.
    ctypes.set_errno(0)
    result = getattr(libc, name)(*args)
    if name == 'fork' and result == 0:
        os._exit(42)
    if name == 'fork' and result > 0:
        os.waitpid(result, 0)
    checks[name] = [result, ctypes.get_errno()]
try:
    os.execv('/python/bin/python3', ['python3', '-c', 'raise SystemExit(77)'])
    checks['execve'] = 'allowed'
except OSError as error:
    checks['execve'] = [-1, error.errno]
print(json.dumps(checks))
"""
    value = _json_result(_run(tmp_path, source))
    assert set(value) == {"fork", "unshare", "mount", "ptrace", "execve"}
    assert all(result == [-1, 1] for result in value.values())


@pytest.mark.parametrize("directory", ["/work", "/tmp"])
def test_native_workspace_cumulative_cap_across_small_files(tmp_path, directory):
    source = """
import json, os, sys
directory = json.load(sys.stdin)['directory']
written = 0
error = None
for number in range(17):
    try:
        with open(f'{directory}/piece-{number}', 'wb') as file:
            for chunk in range(16):
                written += file.write(b'x' * 65536)
    except OSError as exception:
        error = exception.errno
        break
print(json.dumps({'written': written, 'error': error}))
"""
    value = _json_result(
        _run(tmp_path, source, payload={"directory": directory}, mount_probe=True)
    )
    assert value["written"] <= 16 * 1024 * 1024
    assert value["error"] == 28


def test_native_socket_memfd_ipc_and_creation_denied(tmp_path):
    source = """
import ctypes, json, os, socket
libc = ctypes.CDLL(None, use_errno=True)
checks = {}
for family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX):
    try:
        with socket.socket(family, socket.SOCK_STREAM):
            checks[str(family)] = 'allowed'
    except OSError as error:
        checks[str(family)] = error.errno
try:
    left, right = socket.socketpair()
    left.close(); right.close()
    checks['socketpair'] = 'allowed'
except OSError as error:
    checks['socketpair'] = error.errno
ctypes.set_errno(0)
descriptor = libc.memfd_create(b'synthetic-probe', 0)
checks['memfd'] = ctypes.get_errno() if descriptor < 0 else 'allowed'
if descriptor >= 0:
    os.close(descriptor)
for name, args, cleanup in [('shmget', (0, 1024, 0o1600), 'shmctl'), ('semget', (0, 1, 0o1600), 'semctl'), ('msgget', (0, 0o1600), 'msgctl')]:
    ctypes.set_errno(0)
    result = getattr(libc, name)(*args)
    checks[name] = ctypes.get_errno() if result < 0 else 'allowed'
    if result >= 0:
        getattr(libc, cleanup)(result, 0, None)
for name, call in [('file', lambda: open('/tmp/new-file', 'wb')), ('directory', lambda: os.mkdir('/tmp/new-directory')), ('tmpfile', lambda: os.open('/tmp', os.O_TMPFILE | os.O_RDWR, 0o600))]:
    try:
        result = call()
        if hasattr(result, 'close'): result.close()
        elif isinstance(result, int): os.close(result)
        checks[name] = 'allowed'
    except OSError as error:
        checks[name] = error.errno
print(json.dumps(checks))
"""
    value = _json_result(_run(tmp_path, source))
    assert len(value) == 11
    assert all(result == 1 for result in value.values())


def test_native_clone3_execveat_io_uring_syscalls_denied_before_argument_validation(
    tmp_path,
):
    source = """
import ctypes, json
resolver = ctypes.CDLL('/usr/lib/libseccomp.so.2')
resolver.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
resolver.seccomp_syscall_resolve_name.restype = ctypes.c_int
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
checks = {}
for name in ('clone3', 'execveat', 'io_uring_setup', 'setns', 'openat2'):
    number = resolver.seccomp_syscall_resolve_name(name.encode())
    ctypes.set_errno(0)
    result = libc.syscall(number, -1, 0, 0, 0, 0, 0)
    checks[name] = [result, ctypes.get_errno()]
print(json.dumps(checks))
"""
    value = _json_result(_run(tmp_path, source))
    assert all(result == [-1, 1] for result in value.values())


def test_native_legacy_clone_cannot_create_child(tmp_path):
    source = """
import ctypes, json, os
resolver = ctypes.CDLL('/usr/lib/libseccomp.so.2')
resolver.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
resolver.seccomp_syscall_resolve_name.restype = ctypes.c_int
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
number = resolver.seccomp_syscall_resolve_name(b'clone')
ctypes.set_errno(0)
result = libc.syscall(number, 0, 0, 0, 0, 0)
error = ctypes.get_errno()
if result == 0: os._exit(42)
if result > 0: os.waitpid(result, 0)
print(json.dumps({'result': result, 'error': error}))
"""
    value = _json_result(_run(tmp_path, source))
    assert value == {"result": -1, "error": 1}


def test_native_new_pipe_and_native_aio_denied(tmp_path):
    source = """
import ctypes, json, os
resolver = ctypes.CDLL('/usr/lib/libseccomp.so.2')
resolver.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
resolver.seccomp_syscall_resolve_name.restype = ctypes.c_int
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
checks = {}
for name in ('pipe', 'pipe2'):
    descriptors = (ctypes.c_int * 2)(-1, -1)
    ctypes.set_errno(0)
    result = getattr(libc, name)(descriptors, 0)
    checks[name] = [result, ctypes.get_errno()]
    if result == 0:
        for descriptor in descriptors: os.close(descriptor)
for name in ('io_setup', 'io_destroy', 'io_getevents', 'io_submit', 'io_cancel', 'io_pgetevents'):
    number = resolver.seccomp_syscall_resolve_name(name.encode())
    context = ctypes.c_ulong(0)
    ctypes.set_errno(0)
    args = (1, ctypes.byref(context)) if name == 'io_setup' else (0, 0, 0, 0, 0, 0)
    result = libc.syscall(number, *args)
    checks[name] = [result, ctypes.get_errno()]
    if name == 'io_setup' and result == 0:
        libc.syscall(resolver.seccomp_syscall_resolve_name(b'io_destroy'), context)
print(json.dumps(checks))
"""
    value = _json_result(_run(tmp_path, source))
    assert len(value) == 8
    assert all(result == [-1, 1] for result in value.values())


def test_native_missing_seccomp_fails_before_catalog_code(tmp_path):
    command, environment = _command(tmp_path, "print('unsafe-catalog-executed')\n")
    missing = command.index("/usr/lib/libseccomp.so.2")
    assert command[missing - 2] == "--ro-bind"
    del command[missing - 2 : missing + 1]
    result = subprocess.run(
        command,
        input=b"{}",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        close_fds=True,
        timeout=8,
        check=False,
    )
    assert result.returncode == 2 and not result.stdout
    assert result.stderr == b"processor_runtime_failed\n"


def test_native_address_space_limit_stops_bounded_allocation(tmp_path):
    source = """
import json, resource
chunks = []
try:
    for number in range(16):
        chunks.append(bytearray(8 * 1024 * 1024))
    limited = False
except MemoryError:
    limited = True
print(json.dumps({'limited': limited, 'chunks': len(chunks), 'as': resource.getrlimit(resource.RLIMIT_AS)}))
"""
    value = _json_result(_run(tmp_path, source, memory=64))
    assert value["limited"] and value["chunks"] < 8
    assert value["as"] == [64 * 1024 * 1024] * 2


def test_native_cpu_limit_kills_busy_loop_without_wall_timeout(tmp_path):
    started = time.monotonic()
    result = _run(tmp_path, "while True: pass\n", cpu=1, timeout=6)
    assert result.returncode != 0 and time.monotonic() - started < 6


def test_native_nofile_and_core_limits_are_hard(tmp_path):
    source = """
import json, resource
files = []
try:
    for number in range(80):
        files.append(open('/dev/null', 'rb'))
    error = None
except OSError as exception:
    error = exception.errno
print(json.dumps({'error': error, 'nofile': resource.getrlimit(resource.RLIMIT_NOFILE), 'core': resource.getrlimit(resource.RLIMIT_CORE)}))
"""
    value = _json_result(_run(tmp_path, source))
    assert value["error"] == 24 and value["nofile"] == [64, 64]
    assert value["core"] == [0, 0]


def _descendants(pid):
    found = []
    pending = [pid]
    while pending:
        parent = pending.pop()
        try:
            children = (
                Path(f"/proc/{parent}/task/{parent}/children").read_text().split()
            )
        except FileNotFoundError:
            continue
        for child in children:
            number = int(child)
            if number not in found:
                found.append(number)
                pending.append(number)
    return found


@pytest.mark.parametrize("change_session", [False, True])
def test_native_terminate_leaves_no_processes_even_after_setsid(
    tmp_path, change_session
):
    source = """
import json, os, sys, time
if json.load(sys.stdin)['setsid']:
    try:
        os.setsid()
    except OSError:
        pass
print('ready', flush=True)
time.sleep(20)
"""
    command, environment = _command(tmp_path, source)
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        close_fds=True,
        start_new_session=True,
    )
    try:
        process.stdin.write(json.dumps({"setsid": change_session}).encode())
        process.stdin.close()
        import selectors

        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5), (
                "sandbox did not start within bounded time"
            )
        assert process.stdout.readline() == b"ready\n"
        descendants = _descendants(process.pid)
        assert descendants
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(
            Path(f"/proc/{pid}").exists() for pid in descendants
        ):
            time.sleep(0.02)
        assert not any(Path(f"/proc/{pid}").exists() for pid in descendants)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def test_native_normal_exit_leaves_no_namespace_processes(tmp_path):
    source = """
import sys
sys.stdin.readline()
print('ready', flush=True)
sys.stdin.readline()
"""
    command, environment = _command(tmp_path, source)
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        close_fds=True,
        start_new_session=True,
    )
    try:
        process.stdin.write(b"start\n")
        process.stdin.flush()
        import selectors

        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5), (
                "sandbox did not start within bounded time"
            )
        assert process.stdout.readline() == b"ready\n"
        descendants = _descendants(process.pid)
        assert descendants
        process.stdin.write(b"finish\n")
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        assert not process.stderr.read()
        # The namespace init can remain a zombie briefly after the bwrap
        # monitor exits; require its reaping within the same bounded deadline
        # as forced termination, without accepting any surviving process.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(
            Path(f"/proc/{pid}").exists() for pid in descendants
        ):
            time.sleep(0.02)
        assert not any(Path(f"/proc/{pid}").exists() for pid in descendants)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
