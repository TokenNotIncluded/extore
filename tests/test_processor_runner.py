"""Independent child processes exercise hard limits and real Linux seccomp."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from extore import processor_runner as runner

RUNNER = Path(runner.__file__).resolve()
SECRET_MARKER = "runner-private-value-must-not-be-printed"


def limits(**values):
    return {**runner.DEFAULT_LIMITS, **values}


def test_limit_json_rejects_duplicate_fields_and_requires_complete_normalized_limits():
    with pytest.raises(runner.RunnerError):
        runner.parse_limits('{"timeout_seconds":120,"timeout_seconds":10}')
    with pytest.raises(runner.RunnerError):
        runner.parse_limits({"memory_mb": 256})
    with pytest.raises(runner.RunnerError):
        runner.parse_limits({**limits(), "command": SECRET_MARKER})


@pytest.mark.parametrize(
    "field,value",
    [
        ("memory_mb", 63),
        ("memory_mb", 513),
        ("memory_mb", True),
        ("cpu_seconds", 0),
        ("cpu_seconds", 121),
        ("cpu_seconds", 1.0),
        ("timeout_seconds", 9),
        ("timeout_seconds", 121),
        ("timeout_seconds", "120"),
        ("max_output_bytes", 65535),
        ("max_output_bytes", 1000001),
        ("max_output_bytes", False),
    ],
)
def test_out_of_policy_runtime_values_never_reach_exec(
    field, value, monkeypatch, capsys
):
    called = []
    monkeypatch.setattr(runner.os, "execve", lambda *args: called.append(args))
    assert (
        runner.main(
            [
                "--launch",
                json.dumps(limits(**{field: value})),
                runner.BWRAP_PATH,
                "--version",
            ]
        )
        == 2
    )
    assert not called
    assert capsys.readouterr().err == runner.ERROR_MESSAGE


def test_launcher_replaces_process_only_after_all_soft_and_hard_limits_are_set(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        runner.os,
        "stat",
        lambda path: SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0),
    )
    monkeypatch.setattr(
        runner.resource, "setrlimit", lambda kind, pair: calls.append((kind, pair))
    )
    monkeypatch.setenv("EXTORE_WORKFLOW_PASSWORD", SECRET_MARKER)

    def execve(path, argv, environment):
        calls.append((path, argv, environment))
        raise SystemExit(0)

    monkeypatch.setattr(runner.os, "execve", execve)
    assert (
        runner.main(
            [
                "--launch",
                json.dumps(limits(memory_mb=64, cpu_seconds=1)),
                runner.BWRAP_PATH,
                "--unshare-net",
                "/usr/bin/python",
            ]
        )
        == 0
    )
    assert len(calls) == 6
    assert calls[:-1] == [
        (runner.resource.RLIMIT_AS, (64 * 1024 * 1024,) * 2),
        (runner.resource.RLIMIT_CPU, (1, 1)),
        (runner.resource.RLIMIT_CORE, (0, 0)),
        (runner.resource.RLIMIT_NOFILE, (64, 64)),
        (runner.resource.RLIMIT_FSIZE, (1048576, 1048576)),
    ]
    path, argv, environment = calls[-1]
    assert path == runner.BWRAP_PATH
    assert argv == [runner.BWRAP_PATH, "--unshare-net", "/usr/bin/python"]
    assert SECRET_MARKER not in json.dumps(argv)
    assert environment["EXTORE_WORKFLOW_PASSWORD"] == SECRET_MARKER


@pytest.mark.parametrize(
    "mode,uid",
    [
        (stat.S_IFREG | 0o777, 0),
        (stat.S_IFREG | 0o755, 1000),
        (stat.S_IFREG | 0o644, 0),
        (stat.S_IFDIR | 0o755, 0),
    ],
)
def test_untrusted_bubblewrap_file_fails_closed(mode, uid, monkeypatch, capsys):
    monkeypatch.setattr(
        runner.os, "stat", lambda path: SimpleNamespace(st_mode=mode, st_uid=uid)
    )
    monkeypatch.setattr(
        runner,
        "set_limits",
        lambda value: pytest.fail("untrusted executable reached resource setup"),
    )
    assert (
        runner.main(["--launch", json.dumps(limits()), runner.BWRAP_PATH, "--version"])
        == 2
    )
    assert capsys.readouterr().err == runner.ERROR_MESSAGE


def test_resource_setup_failure_is_generic_and_never_executes(monkeypatch, capsys):
    monkeypatch.setattr(
        runner.os,
        "stat",
        lambda path: SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0),
    )

    def failure(*args):
        raise OSError(SECRET_MARKER)

    monkeypatch.setattr(runner.resource, "setrlimit", failure)
    monkeypatch.setattr(
        runner.os, "execve", lambda *args: pytest.fail("failed limits reached execve")
    )
    assert (
        runner.main(["--launch", json.dumps(limits()), runner.BWRAP_PATH, "--version"])
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == runner.ERROR_MESSAGE
    assert SECRET_MARKER not in captured.err


class Function:
    def __init__(self, name, calls, result=0):
        self.name, self.calls, self.result = name, calls, result

    def __call__(self, *args):
        self.calls.append((self.name, args))
        return self.result(*args) if callable(self.result) else self.result


def fake_seccomp(monkeypatch, *, failure=None):
    calls = []
    library = SimpleNamespace(
        seccomp_init=Function("init", calls, None if failure == "init" else 123),
        seccomp_release=Function("release", calls),
        seccomp_syscall_resolve_name=Function(
            "resolve", calls, -1 if failure == "resolve" else 42
        ),
        seccomp_rule_add=Function("add", calls, -1 if failure == "add" else 0),
        seccomp_rule_add_array=Function(
            "add_array", calls, -1 if failure == "add_array" else 0
        ),
        seccomp_load=Function("load", calls, -1 if failure == "load" else 0),
    )
    libc = SimpleNamespace(
        prctl=Function("prctl", calls, -1 if failure == "prctl" else 0)
    )

    def cdll(path, **kwargs):
        if failure == "library":
            raise OSError(SECRET_MARKER)
        assert path in (runner.SECCOMP_PATH, None)
        return library if path == runner.SECCOMP_PATH else libc

    monkeypatch.setattr(runner.ctypes, "CDLL", cdll)
    return calls


def test_every_denied_syscall_is_resolved_added_before_no_new_privs_and_load(
    monkeypatch,
):
    calls = fake_seccomp(monkeypatch)
    runner.install_seccomp()
    assert calls[0] == ("init", (runner.SCMP_ACT_ALLOW,))
    assert [args[0].decode("ascii") for name, args in calls if name == "resolve"] == [
        *runner.DENIED_SYSCALLS,
        "open",
        "openat",
    ]
    assert all(
        args == (123, runner.SCMP_ACT_ERRNO, 42, 0)
        for name, args in calls
        if name == "add"
    )
    assert len([name for name, _ in calls if name == "add"]) == len(
        runner.DENIED_SYSCALLS
    )
    comparisons = [args[4]._obj for name, args in calls if name == "add_array"]
    tmpfile_bit = os.O_TMPFILE & ~os.O_DIRECTORY
    assert [
        (value.arg, value.op, value.datum_a, value.datum_b) for value in comparisons
    ] == [
        (1, runner.SCMP_CMP_MASKED_EQ, os.O_CREAT, os.O_CREAT),
        (1, runner.SCMP_CMP_MASKED_EQ, tmpfile_bit, tmpfile_bit),
        (2, runner.SCMP_CMP_MASKED_EQ, os.O_CREAT, os.O_CREAT),
        (2, runner.SCMP_CMP_MASKED_EQ, tmpfile_bit, tmpfile_bit),
    ]
    assert calls[-3:] == [
        ("prctl", (runner.PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)),
        ("load", (123,)),
        ("release", (123,)),
    ]


@pytest.mark.parametrize(
    "failure", ["library", "init", "resolve", "add", "add_array", "prctl", "load"]
)
def test_failed_guard_never_imports_processor_and_never_reflects_values(
    failure, monkeypatch, capsys
):
    fake_seccomp(monkeypatch, failure=failure)
    original_flags = sys.flags

    class Flags:
        isolated = 1
        no_site = 1

        def __getattr__(self, name):
            return getattr(original_flags, name)

    monkeypatch.setattr(runner.sys, "flags", Flags())
    monkeypatch.setattr(runner.sys, "dont_write_bytecode", True)
    monkeypatch.setattr(runner.resource, "setrlimit", lambda *args: None)
    monkeypatch.setattr(
        runner.runpy,
        "run_module",
        lambda *args, **kwargs: pytest.fail("failed guard imported processor"),
    )
    monkeypatch.setenv("EXTORE_WORKFLOW_PASSWORD", SECRET_MARKER)
    assert runner.main(["--execute", "probe"]) == 2
    assert capsys.readouterr().err == runner.ERROR_MESSAGE


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--execute"],
        ["--execute", "../secrets"],
        ["--execute", "probe", SECRET_MARKER],
        ["--launch", "{}", "/tmp/evil", "run"],
    ],
)
def test_standalone_invalid_arguments_return_only_generic_error(argv):
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-u", "-X", "utf8", str(RUNNER), *argv],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr.decode() == runner.ERROR_MESSAGE


def test_execute_requires_isolated_interpreter_without_site_or_bytecode():
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--execute", "probe"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert result.stderr.decode() == runner.ERROR_MESSAGE


CHILD_IMPORT = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location('trusted_runner', sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
"""


def test_real_child_has_all_five_host_hard_limits_before_exec_and_preserves_env_only_secret():
    script = (
        CHILD_IMPORT
        + """
import json, os, resource
def replacement(path, argv, environment):
    output = {str(kind): list(resource.getrlimit(kind)) for kind in (
        resource.RLIMIT_AS, resource.RLIMIT_CPU, resource.RLIMIT_CORE,
        resource.RLIMIT_NOFILE, resource.RLIMIT_FSIZE)}
    output['secret_env_only'] = environment.get('EXTORE_WORKFLOW_PASSWORD') == 'runner-private-value-must-not-be-printed' and all('runner-private-value' not in v for v in argv)
    print(json.dumps(output))
    raise SystemExit(0)
m.os.execve = replacement
raise SystemExit(m.main(['--launch', json.dumps({
    'timeout_seconds': 10, 'memory_mb':64, 'cpu_seconds':2,
    'max_output_bytes':65536}), '/usr/bin/bwrap', '--unshare-net', '/usr/bin/python']))
"""
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-u",
            "-X",
            "utf8",
            "-c",
            script,
            str(RUNNER),
        ],
        capture_output=True,
        timeout=10,
        check=False,
        env={**os.environ, "EXTORE_WORKFLOW_PASSWORD": SECRET_MARKER},
    )
    assert result.returncode == 0, result.stderr.decode()
    output = json.loads(result.stdout)
    assert output == {
        str(runner.resource.RLIMIT_AS): [64 * 1024 * 1024] * 2,
        str(runner.resource.RLIMIT_CPU): [2, 2],
        str(runner.resource.RLIMIT_CORE): [0, 0],
        str(runner.resource.RLIMIT_NOFILE): [64, 64],
        str(runner.resource.RLIMIT_FSIZE): [1048576, 1048576],
        "secret_env_only": True,
    }
    assert SECRET_MARKER.encode() not in result.stdout + result.stderr


def test_real_linux_guard_blocks_new_process_exec_and_namespace_control():
    # A test-only Python harness replaces runpy's catalog callback, not the
    # production CLI or its fixed /code path.  The real irreversible filter
    # runs in a fresh child, and every blocked operation is attempted there.
    script = (
        CHILD_IMPORT
        + """
import ctypes, errno, json, os, resource, subprocess
def catalog_probe(module, run_name):
    checked = {'argv': sys.argv == ['extore_processors','probe'],
        'module': module == 'extore_processors' and run_name == '__main__',
        'code_path': sys.path[0] == '/code'}
    libc = ctypes.CDLL(None, use_errno=True)
    checked['no_new_privs'] = libc.prctl(39,0,0,0,0) == 1
    checked['nproc_hard'] = resource.getrlimit(resource.RLIMIT_NPROC) == (64,64)
    try:
        pid = os.fork()
        if pid == 0: os._exit(5)
        os.waitpid(pid,0)
        checked['fork'] = False
    except OSError as e: checked['fork'] = e.errno == errno.EPERM
    try:
        os.execve('/usr/bin/true',['/usr/bin/true'],{})
    except OSError as e: checked['execve'] = e.errno == errno.EPERM
    try:
        subprocess.run(['/usr/bin/true'],check=False)
        checked['subprocess'] = False
    except OSError as e: checked['subprocess'] = e.errno == errno.EPERM
    library = ctypes.CDLL('/usr/lib/libseccomp.so.2')
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    for name in ('setns','unshare','mount','umount2','pivot_root','ptrace','bpf','perf_event_open','keyctl','add_key','request_key','io_uring_setup','io_uring_enter','io_uring_register','pipe','pipe2','io_setup','io_destroy','io_submit','io_cancel','io_getevents','io_pgetevents'):
        number = library.seccomp_syscall_resolve_name(name.encode())
        ctypes.set_errno(0)
        returned = libc.syscall(number,-1,-1,-1,-1,-1,-1)
        checked[name] = returned == -1 and ctypes.get_errno() == errno.EPERM
    print(json.dumps(checked),flush=True)
m.runpy.run_module = catalog_probe
raise SystemExit(m.main(['--execute','probe']))
"""
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-u",
            "-X",
            "utf8",
            "-c",
            script,
            str(RUNNER),
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    output = json.loads(result.stdout)
    assert len(output) == 30
    assert all(output.values()), output
    assert result.stderr == b""


def test_real_filter_blocks_file_and_memory_creation_but_preserves_file_and_directory_reads(
    tmp_path,
):
    fixture = tmp_path / "existing.txt"
    fixture.write_text("fixture", encoding="utf-8")
    script = (
        CHILD_IMPORT
        + """
import ctypes, errno, json, os, socket
fixture = sys.argv[2]
directory = os.path.dirname(fixture)
def catalog_probe(module, run_name):
    checked = {'read_file': open(fixture,'rb').read() == b'fixture'}
    fd = os.open(directory,os.O_RDONLY | os.O_DIRECTORY)
    checked['read_directory'] = fd >= 0
    library = ctypes.CDLL('/usr/lib/libseccomp.so.2')
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    libc = ctypes.CDLL(None,use_errno=True)
    def create_memfd():
        ctypes.set_errno(0)
        number = library.seccomp_syscall_resolve_name(b'memfd_create')
        returned = libc.syscall(number,ctypes.c_char_p(b'processor'),0)
        if returned < 0: raise OSError(ctypes.get_errno(),'blocked syscall')
        return returned
    attempts = {
        'create': lambda: os.open(directory + '/created',os.O_WRONLY | os.O_CREAT,0o600),
        'tmpfile': lambda: os.open(directory,os.O_RDWR | os.O_TMPFILE,0o600),
        'openat_create': lambda: os.open('created-at',os.O_WRONLY | os.O_CREAT,0o600,dir_fd=fd),
        'mkdir': lambda: os.mkdir(directory + '/created-dir'),
        'link': lambda: os.link(fixture,directory + '/created-link'),
        'symlink': lambda: os.symlink(fixture,directory + '/created-symlink'),
        'memfd': create_memfd,
        'socket': lambda: socket.socket(),
        'socketpair': lambda: socket.socketpair(),
    }
    for name, attempt in attempts.items():
        try:
            returned = attempt()
            if isinstance(returned,int): os.close(returned)
            elif hasattr(returned,'close'): returned.close()
            elif isinstance(returned,tuple):
                for value in returned: value.close()
            checked[name] = False
        except OSError as error: checked[name] = error.errno == errno.EPERM
    # Test both actual syscall layouts, independent of libc's choice to
    # implement open() with openat().  Only temporary fixture paths are used.
    library = ctypes.CDLL('/usr/lib/libseccomp.so.2')
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    libc = ctypes.CDLL(None,use_errno=True)
    for name in ('open','openat'):
        number = library.seccomp_syscall_resolve_name(name.encode())
        for flag_name, flags in (('create',os.O_WRONLY | os.O_CREAT),('tmpfile',os.O_RDWR | os.O_TMPFILE)):
            path = ctypes.c_char_p((directory if flag_name == 'tmpfile' else directory + '/raw-' + name).encode())
            ctypes.set_errno(0)
            if name == 'open': returned = libc.syscall(number,path,flags,0o600)
            else: returned = libc.syscall(number,-100,path,flags,0o600)
            if returned >= 0: os.close(returned)
            checked['raw_' + name + '_' + flag_name] = returned == -1 and ctypes.get_errno() == errno.EPERM
    os.close(fd)
    print(json.dumps(checked),flush=True)
m.runpy.run_module = catalog_probe
raise SystemExit(m.main(['--execute','probe']))
"""
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-u",
            "-X",
            "utf8",
            "-c",
            script,
            str(RUNNER),
            str(fixture),
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    output = json.loads(result.stdout)
    assert len(output) == 15
    assert all(output.values()), output
    assert [path.name for path in tmp_path.iterdir()] == [fixture.name]
    assert result.stderr == b""
