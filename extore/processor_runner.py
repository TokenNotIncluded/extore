"""Standalone trusted launcher and in-sandbox syscall guard.

The worker supplies the fixed bubblewrap command.  Merchants supply validated
workflow values through its environment, never executable paths or arguments.
This file intentionally imports neither Extore nor its database/configuration.
"""

import ctypes
import errno
import json
import os
import re
import resource
import runpy
import stat
import sys

DEFAULT_LIMITS = {
    "timeout_seconds": 120,
    "memory_mb": 256,
    "cpu_seconds": 120,
    "max_output_bytes": 1000000,
}
LIMIT_RANGES = {
    "timeout_seconds": (10, 120),
    "memory_mb": (64, 512),
    "cpu_seconds": (1, 120),
    "max_output_bytes": (65536, 1000000),
}
BWRAP_PATH = "/usr/bin/bwrap"
SECCOMP_PATH = "/usr/lib/libseccomp.so.2"
DENIED_SYSCALLS = (
    "fork",
    "vfork",
    "clone",
    "clone3",
    "execve",
    "execveat",
    "setns",
    "unshare",
    "mount",
    "umount2",
    "pivot_root",
    "ptrace",
    "bpf",
    "perf_event_open",
    "keyctl",
    "add_key",
    "request_key",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    "memfd_create",
    "shmget",
    "shmat",
    "shmdt",
    "shmctl",
    "semget",
    "semop",
    "semtimedop",
    "semctl",
    "msgget",
    "msgsnd",
    "msgrcv",
    "msgctl",
    "socket",
    "socketpair",
    "openat2",
    "creat",
    "mkdir",
    "mkdirat",
    "link",
    "linkat",
    "symlink",
    "symlinkat",
    "mknod",
    "mknodat",
    "pipe",
    "pipe2",
    "io_setup",
    "io_destroy",
    "io_submit",
    "io_cancel",
    "io_getevents",
    "io_pgetevents",
)
SCMP_ACT_ALLOW = 0x7FFF0000
SCMP_ACT_ERRNO = 0x00050000 | errno.EPERM
PR_SET_NO_NEW_PRIVS = 38
SCMP_CMP_MASKED_EQ = 7
ERROR_MESSAGE = "processor_runtime_failed\n"


class ArgumentComparison(ctypes.Structure):
    _fields_ = (
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    )


class RunnerError(Exception):
    """Intentionally contains no submitted argument or environment value."""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RunnerError
        result[key] = value
    return result


def parse_limits(value):
    """Accept the worker's complete normalized limits, including strict ints."""
    if isinstance(value, str):
        if len(value) > 2048:
            raise RunnerError
        try:
            value = json.loads(value, object_pairs_hook=_pairs)
        except (TypeError, ValueError, RecursionError):
            raise RunnerError from None
    if not isinstance(value, dict) or set(value) != set(LIMIT_RANGES):
        raise RunnerError
    for name, (low, high) in LIMIT_RANGES.items():
        if type(value[name]) is not int or not low <= value[name] <= high:
            raise RunnerError
    return dict(value)


def set_limits(limits):
    """Set soft and hard bounds in this independent launcher process."""
    limits = parse_limits(limits)
    for kind, maximum in (
        (resource.RLIMIT_AS, limits["memory_mb"] * 1024 * 1024),
        (resource.RLIMIT_CPU, limits["cpu_seconds"]),
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_NOFILE, 64),
        # This is a per-file fallback.  The worker separately bounds its
        # stdout protocol and the runtime mounts bound the writable workspace.
        (resource.RLIMIT_FSIZE, 1048576),
    ):
        resource.setrlimit(kind, (maximum, maximum))


def launch(limits, bwrap_path, arguments):
    """Replace this limited process with the trusted host-generated sandbox."""
    limits = parse_limits(limits)
    if sys.platform != "linux" or bwrap_path != BWRAP_PATH:
        raise RunnerError
    if (
        not isinstance(arguments, (list, tuple))
        or not arguments
        or len(arguments) > 4096
        or any(not isinstance(v, str) or "\0" in v for v in arguments)
        or sum(len(v) for v in arguments) > 1048576
    ):
        raise RunnerError
    executable = os.stat(bwrap_path)
    if (
        not stat.S_ISREG(executable.st_mode)
        or executable.st_uid != 0
        or executable.st_mode & 0o022
        or not executable.st_mode & 0o111
    ):
        raise RunnerError
    set_limits(limits)
    os.execve(bwrap_path, [bwrap_path, *arguments], dict(os.environ))
    raise RunnerError  # execve never returns after a successful launch.


def install_seccomp():
    """Fail closed if any syscall cannot be resolved or its rule installed."""
    if sys.platform != "linux":
        raise RunnerError
    library = ctypes.CDLL(SECCOMP_PATH, use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_release.restype = None
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(ArgumentComparison),
    ]
    library.seccomp_rule_add_array.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    libc.prctl.restype = ctypes.c_int
    context = library.seccomp_init(SCMP_ACT_ALLOW)
    if not context:
        raise RunnerError
    try:
        for name in DENIED_SYSCALLS:
            syscall = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if syscall < 0 or library.seccomp_rule_add(
                context, SCMP_ACT_ERRNO, syscall, 0
            ):
                raise RunnerError
        # O_TMPFILE also contains O_DIRECTORY.  Block only its creation bits,
        # preserving ordinary read-only directory opens used by Python.
        creation_mask = os.O_CREAT | (os.O_TMPFILE & ~os.O_DIRECTORY)
        if not creation_mask or not (os.O_TMPFILE & ~os.O_DIRECTORY):
            raise RunnerError
        for name, flags_arg in (("open", 1), ("openat", 2)):
            syscall = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if syscall < 0:
                raise RunnerError
            remaining = creation_mask
            while remaining:
                bit = remaining & -remaining
                comparison = ArgumentComparison(flags_arg, SCMP_CMP_MASKED_EQ, bit, bit)
                if library.seccomp_rule_add_array(
                    context, SCMP_ACT_ERRNO, syscall, 1, ctypes.byref(comparison)
                ):
                    raise RunnerError
                remaining &= ~bit
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0):
            raise RunnerError
        if library.seccomp_load(context):
            raise RunnerError
    finally:
        library.seccomp_release(context)


def execute(processor_id):
    """Import only the mounted catalog after installing the process guard."""
    if (
        not isinstance(processor_id, str)
        or not re.fullmatch(r"[a-z][a-z0-9_-]{0,99}", processor_id)
        or not sys.flags.isolated
        or not sys.flags.no_site
        or not sys.dont_write_bytecode
    ):
        raise RunnerError
    sys.path.insert(0, "/code")
    # Bubblewrap's fixed setup needs its own few trusted forks.  Applying
    # NPROC on the host would count unrelated processes of that real UID and
    # can prevent namespace creation before the processor has even started.
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    install_seccomp()
    sys.argv = ["extore_processors", processor_id]
    runpy.run_module("extore_processors", run_name="__main__")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) >= 4 and argv[0] == "--launch":
            launch(argv[1], argv[2], argv[3:])
            return 0
        if len(argv) == 2 and argv[0] == "--execute":
            execute(argv[1])
            return 0
        raise RunnerError
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if type(exc.code) is int and 0 <= exc.code <= 125:
            return exc.code
    except Exception:
        pass
    # Neither exception text, arguments nor environment belongs in logs.
    sys.stderr.write(ERROR_MESSAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
