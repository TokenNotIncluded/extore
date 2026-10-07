"""Fixed offline processor policy; merchants configure values, never commands."""

import functools
import json
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

DEFAULT_RUNTIME = {
    "timeout_seconds": 120,
    "memory_mb": 256,
    "cpu_seconds": 120,
    "max_output_bytes": 1000000,
}
MAX_ENV_VALUE_BYTES = 8192
MAX_ENV_TOTAL_BYTES = 65536
MAX_INPUT_BYTES = 200000
MAX_PROGRESS_EVENTS = 100
WORKSPACE_BYTES = 16 * 1024 * 1024
ENV_PREFIX = "EXTORE_WORKFLOW_"
_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_RESERVED = {
    "PATH",
    "LANG",
    "LANGUAGE",
    "HOME",
    "SHELL",
    "ENV",
    "IFS",
    "CDPATH",
    "FPATH",
    "PWD",
    "OLDPWD",
    "LIBRARY_PATH",
    "CPATH",
    "C_INCLUDE_PATH",
    "CPLUS_INCLUDE_PATH",
    "OBJC_INCLUDE_PATH",
    "LOCPATH",
    "TZ",
    "TZDIR",
    "RES_OPTIONS",
    "LOCALDOMAIN",
    "HOSTALIASES",
    "DISPLAY",
    "PROMPT_COMMAND",
    "PS4",
    "SHELLOPTS",
    "BASHOPTS",
    "PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
}
_RESERVED_PREFIXES = (
    "EXTORE_",
    "PYTHON",
    "LD",
    "DYLD",
    "BWRAP_",
    "TMP",
    "TEMP",
    "SSL",
    "OPENSSL",
    "BASH",
    "ZSH",
    "GLIBC",
    "GCONV",
    "XDG_",
    "DBUS_",
    "LC_",
    "VIRTUAL_ENV",
    "CONDA",
    "PIP",
    "UV_",
    "GIT_",
    "NODE",
    "RUBY",
    "PERL",
    "JAVA",
    "SSH_",
    "GPG_",
)


def validate_workflow(value=None):
    """Normalize encrypted workflow contents without exposing their values."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"variables", "secrets", "runtime"}:
        raise ValueError("invalid processor workflow")
    result = {}
    total = 0
    for group in ("variables", "secrets"):
        fields = value.get(group, {})
        if not isinstance(fields, dict) or len(fields) > 64:
            raise ValueError("invalid processor environment")
        normalized = {}
        for name, text in fields.items():
            if (
                not isinstance(name, str)
                or not _NAME.fullmatch(name)
                or name in _RESERVED
                or name.startswith(_RESERVED_PREFIXES)
                or name.endswith("_PROXY")
            ):
                raise ValueError("invalid processor environment name")
            if not isinstance(text, str) or "\0" in text:
                raise ValueError("invalid processor environment value")
            try:
                size = len(text.encode("utf-8"))
            except UnicodeError:
                raise ValueError("invalid processor environment value") from None
            if size > MAX_ENV_VALUE_BYTES:
                raise ValueError("invalid processor environment value")
            total += size
            normalized[name] = text
        result[group] = normalized
    if (
        total > MAX_ENV_TOTAL_BYTES
        or result["variables"].keys() & result["secrets"].keys()
    ):
        raise ValueError("invalid processor environment")
    runtime = value.get("runtime", {})
    if not isinstance(runtime, dict) or set(runtime) - DEFAULT_RUNTIME.keys():
        raise ValueError("invalid processor runtime limits")
    limits = {**DEFAULT_RUNTIME, **runtime}
    ranges = {
        "timeout_seconds": (10, 120),
        "memory_mb": (64, 512),
        "cpu_seconds": (1, 120),
        "max_output_bytes": (65536, 1000000),
    }
    if any(
        type(limits[key]) is not int or not low <= limits[key] <= high
        for key, (low, high) in ranges.items()
    ):
        raise ValueError("invalid processor runtime limits")
    result["runtime"] = limits
    return result


def environment(workflow):
    workflow = validate_workflow(workflow)
    return {**workflow["variables"], **workflow["secrets"]}


def process_environment(workflow):
    # Prefixing prevents even a future reserved-name omission from controlling
    # the interpreter, dynamic loader, shell, network routing or main worker.
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": "/work",
        "TMPDIR": "/tmp",
        **{ENV_PREFIX + name: value for name, value in environment(workflow).items()},
    }


def _ldd(path):
    result = subprocess.run(
        ["/usr/bin/ldd", str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        check=False,
    )
    output = result.stdout.decode("utf-8", "strict")
    if "not found" in output:
        raise RuntimeError("processor runtime dependency unavailable")
    paths = []
    for line in output.splitlines():
        match = re.search(r"(?:=>\s+|^\s*)(/[^\s]+)\s+\(", line)
        if match:
            source = Path(match[1]).resolve(strict=True)
            if not source.is_file():
                raise RuntimeError("processor runtime dependency unavailable")
            paths.append((source, Path(match[1])))
    return paths


@functools.lru_cache(maxsize=1)
def runtime_mounts():
    if sys.platform != "linux":
        raise RuntimeError("processor sandbox requires Linux")
    executable = Path(sys.executable).resolve(strict=True)
    stdlib = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
    # The reviewed catalog and lightweight SDK use this fixed standard-library
    # dependency set. Optional GUI/database extensions must not enlarge mounts
    # or make the offline text processors depend on desktop libraries.
    extension_names = {
        "_ctypes",
        "_sqlite3",
        "_ssl",
        "_hashlib",
        "_socket",
        "_struct",
        "_json",
        "_datetime",
        "_random",
        "_sha2",
        "_blake2",
        "unicodedata",
        "math",
        "array",
        "binascii",
        "zlib",
        "select",
        "_posixsubprocess",
        "fcntl",
    }
    inputs = [
        executable,
        *sorted(
            path
            for path in (stdlib / "lib-dynload").glob("*.so")
            if path.name.split(".", 1)[0] in extension_names
        ),
    ]
    ldconfig = shutil.which("ldconfig", path="/usr/sbin:/usr/bin:/sbin:/bin")
    if ldconfig is None:
        raise RuntimeError("processor runtime dependency tool unavailable")
    library = subprocess.run(
        [str(Path(ldconfig).resolve(strict=True)), "-p"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        check=True,
    ).stdout.decode("ascii", "strict")
    seccomp = re.search(
        r"^\s*libseccomp\.so\.2\s+.*?=>\s+(/\S+)\s*$", library, re.MULTILINE
    )
    if seccomp is None:
        raise RuntimeError("processor sandbox requires libseccomp")
    seccomp_path = Path(seccomp[1]).resolve(strict=True)
    inputs.append(seccomp_path)
    mounts = {}
    for path in inputs:
        for source, destination in _ldd(path):
            destination = str(destination)
            if destination.startswith("/lib/"):
                destination = "/usr" + destination
            elif destination.startswith("/lib64/"):
                destination = "/usr" + destination
            mounts[destination] = str(source)
    mounts["/usr/lib/libseccomp.so.2"] = str(seccomp_path)
    return executable, stdlib, sorted(mounts.items())


def sandbox_command(processor_id, processor_package):
    # Source paths are trusted installed code, never product/customer strings.
    bwrap = shutil.which("bwrap", path="/usr/bin:/bin")
    if bwrap is None:
        raise RuntimeError("processor sandbox requires bubblewrap")
    try:
        version_output = subprocess.run(
            [bwrap, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            env={"PATH": "/usr/bin:/bin"},
            check=True,
        ).stdout.decode("ascii", "strict")
        match = re.fullmatch(r"bubblewrap (\d+)\.(\d+)(?:\.\d+)?\s*", version_output)
        if not match or (int(match[1]), int(match[2])) < (0, 12):
            raise ValueError
    except (OSError, ValueError, subprocess.SubprocessError):
        raise RuntimeError(
            "processor sandbox requires bubblewrap 0.12 or newer"
        ) from None
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", processor_id):
        raise ValueError("invalid processor identifier")
    executable, stdlib, libraries = runtime_mounts()
    root = Path(__file__).resolve().parent
    catalog = Path(processor_package.__file__).resolve().parent
    runner = root / "processor_runner.py"
    if not runner.is_file():
        raise RuntimeError("processor sandbox launcher unavailable")
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    args = [
        bwrap,
        "--unshare-user",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-uts",
        "--unshare-cgroup",
        "--uid",
        "1000",
        "--gid",
        "1000",
        "--disable-userns",
        "--assert-userns-disabled",
        "--cap-drop",
        "ALL",
        "--die-with-parent",
        "--new-session",
        "--hostname",
        "extore-processor",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--ro-bind",
        str(executable),
        "/python/bin/python3",
        "--ro-bind",
        str(stdlib),
        "/python/lib/" + version,
    ]
    # No installed third-party packages or editable .pth files enter the task.
    if (stdlib / "site-packages").exists():
        args += [
            "--tmpfs",
            "/python/lib/" + version + "/site-packages",
            "--remount-ro",
            "/python/lib/" + version + "/site-packages",
        ]
    for destination, source in libraries:
        args += ["--ro-bind", source, destination]
    args += [
        "--ro-bind",
        str(catalog),
        "/code/extore_processors",
        "--ro-bind",
        str(root / "__init__.py"),
        "/code/extore/__init__.py",
        "--ro-bind",
        str(root / "variants.py"),
        "/code/extore/variants.py",
        "--ro-bind",
        str(root / "task_flow_definition.py"),
        "/code/extore/task_flow_definition.py",
        "--ro-bind",
        str(root / "task_flow_schema.py"),
        "/code/extore/task_flow_schema.py",
        "--ro-bind",
        str(root / "sdk"),
        "/code/extore/sdk",
        "--ro-bind",
        str(runner),
        "/runner.py",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--remount-ro",
        "/dev",
        "--remount-ro",
        "/proc",
        "--size",
        str(WORKSPACE_BYTES),
        "--tmpfs",
        "/work",
        "--size",
        str(WORKSPACE_BYTES),
        "--tmpfs",
        "/tmp",
        "--chmod",
        "0700",
        "/work",
        "--chmod",
        "0700",
        "/tmp",
        "--chdir",
        "/work",
        "--remount-ro",
        "/",
        "--",
        "/python/bin/python3",
        "-I",
        "-S",
        "-B",
        "-u",
        "-X",
        "utf8",
        "/runner.py",
        "--execute",
        processor_id,
    ]
    return args


def launch_command(sandbox, workflow):
    limits = validate_workflow(workflow)["runtime"]
    return [
        sys.executable,
        "-I",
        str(Path(__file__).with_name("processor_runner.py")),
        "--launch",
        json.dumps(limits, separators=(",", ":")),
        *sandbox,
    ]
