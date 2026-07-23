"""Shared Python/runtime/process helpers for native Windows + POSIX.

This module centralizes the small bits of environment, process, lock, and
filesystem behavior that Danus needs across worker orchestration, MCP launchers,
and scaffolding. The goal is one Python implementation that works on Linux and
Windows without parallel shell logic in the application code.
"""

from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional

if os.name == "nt":
    import msvcrt
else:
    import fcntl

DEFAULT_PYTHON_ENV = ("DANUS_PYTHON_BIN", "DANUS_PY")
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_SIGKILL = getattr(signal, "SIGKILL", 9)


def _configure_windows_api(kernel32) -> object:
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _kernel32():
    return _configure_windows_api(ctypes.windll.kernel32)


def is_windows() -> bool:
    return os.name == "nt"


def current_python(*override_env_names: str) -> str:
    """Resolve the current Python executable for subprocess/module launches."""
    names = override_env_names or DEFAULT_PYTHON_ENV
    for name in names:
        val = os.environ.get(name)
        if val:
            return shutil.which(val) or val
    if sys.executable:
        return sys.executable
    return shutil.which("python") or "python"


def module_cmd(module: str, *args: str, python: Optional[str] = None) -> List[str]:
    return [python or current_python(), "-m", module, *args]


def load_env_files(root: str | Path, environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Load Danus KEY=value files without executing shell code.

    Existing environment values win. Files are read in the same order as the
    legacy env.sh: codex, danus, then machine-derived runtime settings.
    """
    root = Path(root)
    base = dict(os.environ if environ is None else environ)
    protected = set(base)
    loaded: Dict[str, str] = {}
    for path in (
        root / "config" / "codex.env",
        root / "config" / "danus.env",
        root / "runtime" / "runtime.env",
    ):
        if not path.is_file():
            continue
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                raise ValueError(f"{path}:{number}: expected KEY=value")
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if not key or not (key[0].isalpha() or key[0] == "_") or not all(
                c.isalnum() or c == "_" for c in key
            ):
                raise ValueError(f"{path}:{number}: invalid environment name")
            if value[:1] in ("'", '"'):
                quote = value[0]
                if len(value) < 2 or value[-1] != quote:
                    raise ValueError(f"{path}:{number}: unterminated quote")
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].rstrip()
            value = re.sub(
                r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))",
                lambda match: base.get(match.group(1) or match.group(2), ""),
                value,
            )
            if key not in protected:
                loaded[key] = value
                base[key] = value
    return loaded


def mcp_server_spec(module: str, *, env: Optional[Dict[str, str]] = None,
                    python: Optional[str] = None) -> Dict[str, object]:
    spec: Dict[str, object] = {
        "command": python or current_python(),
        "args": ["-m", module],
    }
    if env:
        spec["env"] = dict(env)
    return spec


def symlink_or_copy(target: Path, link: Path) -> None:
    """Create a symlink when possible; copy as a portable fallback."""
    if link.is_symlink() or link.exists():
        return
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
        return
    except OSError:
        pass
    if target.is_dir():
        shutil.copytree(target, link)
    else:
        link.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, link)


def sync_symlink_or_copy(target: Path, link: Path) -> None:
    """Create an asset link, or refresh its copied fallback."""
    if link.is_symlink():
        return
    if not link.exists():
        symlink_or_copy(target, link)
    elif target.is_dir():
        shutil.rmtree(link)
        shutil.copytree(target, link)
    else:
        shutil.copy2(target, link)


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[Optional[object]]:
    """Try to take an exclusive non-blocking file lock.

    Yields the open handle on success, or ``None`` when another process already
    holds the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if is_windows():
            handle.seek(0)
            if handle.tell() == 0 and handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield None
                return
        else:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield None
                return
        yield handle
    finally:
        try:
            if is_windows():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    if is_windows():
        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        raise
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        state = stat.rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (OSError, IndexError):
        return True


def process_identity(pid: Optional[int]) -> Optional[str]:
    """Return a token that changes when an OS PID is reused."""
    if not pid:
        return None
    if is_windows():
        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exit_time),
                ctypes.byref(kernel), ctypes.byref(user),
            ):
                return None
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return f"windows:{ticks}"
        finally:
            kernel32.CloseHandle(handle)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
        return f"linux:{fields[19]}"
    except (OSError, IndexError):
        pass
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
        started = completed.stdout.strip()
        return f"posix:{started}" if completed.returncode == 0 and started else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def spawn_detached(cmd: List[str], *, cwd: str | Path, env: Dict[str, str],
                   stdout, stderr=None, stdin=None) -> subprocess.Popen:
    return spawn_process(
        cmd, cwd=cwd, env=env, stdout=stdout, stderr=stderr, stdin=stdin,
        new_process_group=True,
    )


def spawn_process(cmd: List[str], *, cwd: str | Path, env: Dict[str, str],
                  stdout, stderr=None, stdin=None,
                  new_process_group: bool = False) -> subprocess.Popen:
    kwargs = {
        "cwd": str(cwd),
        "env": env,
        "stdout": stdout,
        "stderr": stderr if stderr is not None else subprocess.STDOUT,
        "stdin": stdin if stdin is not None else subprocess.DEVNULL,
    }
    if is_windows():
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if new_process_group:
            creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
        if creationflags:
            kwargs["creationflags"] = creationflags
    elif new_process_group:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _windows_descendant_pids(pid: int) -> List[int]:
    if not is_windows():
        return []

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        return []

    parents: Dict[int, List[int]] = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            parent = int(entry.th32ParentProcessID)
            child = int(entry.th32ProcessID)
            parents.setdefault(parent, []).append(child)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)

    descendants: List[int] = []
    stack = list(parents.get(pid, ()))
    seen = set()
    while stack:
        child = stack.pop()
        if child in seen:
            continue
        seen.add(child)
        descendants.append(child)
        stack.extend(parents.get(child, ()))
    return descendants


def process_tree_pids(pid: int) -> List[int]:
    """Return the known process tree rooted at pid (used during PID handoff)."""
    return [pid, *_windows_descendant_pids(pid)] if is_windows() else [pid]


def _wait_for_dead_pids(pids: List[int], *, timeout_seconds: float) -> List[int]:
    deadline = time_monotonic() + max(timeout_seconds, 0.0)
    remaining = {pid for pid in pids if pid > 0}
    while remaining and time_monotonic() < deadline:
        remaining = {pid for pid in remaining if pid_alive(pid)}
        if remaining:
            sleep(0.05)
    return sorted(remaining)


def _taskkill_windows_pid(pid: int, *, force: bool) -> None:
    cmd = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        cmd.append("/F")
    subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _terminate_windows_pid(pid: int) -> None:
    if not is_windows():
        return
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)


def wait_until_path_releasable(path: Path, *, timeout_seconds: float) -> bool:
    if not is_windows():
        return True
    if not path.exists():
        return True
    probe = path.with_name(f"{path.name}.delete-probe")
    with contextlib.suppress(OSError):
        probe.unlink()
    deadline = time_monotonic() + max(timeout_seconds, 0.0)
    while time_monotonic() < deadline:
        try:
            os.replace(path, probe)
            os.replace(probe, path)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            sleep(0.05)
        finally:
            if probe.exists() and not path.exists():
                with contextlib.suppress(OSError):
                    os.replace(probe, path)
    return False


def terminate_process_tree(pid: int, *, force: bool, wait_seconds: float = 5.0) -> None:
    """Best-effort stop of a detached worker loop and its descendants."""
    if is_windows():
        tracked_pids = [pid, *_windows_descendant_pids(pid)]
        _taskkill_windows_pid(pid, force=force)
        survivors = _wait_for_dead_pids(tracked_pids, timeout_seconds=wait_seconds)
        if force:
            for survivor in survivors:
                _taskkill_windows_pid(survivor, force=True)
            survivors = _wait_for_dead_pids(survivors, timeout_seconds=wait_seconds)
            for survivor in survivors:
                _terminate_windows_pid(survivor)
            if survivors:
                _wait_for_dead_pids(survivors, timeout_seconds=wait_seconds)
        return

    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return
    try:
        os.killpg(pgid, _SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    if force:
        return
    end = time_monotonic() + wait_seconds
    while time_monotonic() < end and pid_alive(pid):
        sleep(0.1)
    if pid_alive(pid):
        try:
            os.killpg(os.getpgid(pid), _SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def stop_process(proc: subprocess.Popen, *, wait_seconds: float = 5.0,
                 force: bool = False) -> Optional[int]:
    """Stop a process and its descendants, then reap the parent process."""
    if proc.poll() is not None:
        return proc.returncode
    terminate_process_tree(proc.pid, force=force, wait_seconds=wait_seconds)
    try:
        return proc.wait(timeout=wait_seconds)
    except subprocess.TimeoutExpired:
        if force:
            with contextlib.suppress(OSError):
                proc.kill()
            try:
                return proc.wait(timeout=wait_seconds)
            except subprocess.TimeoutExpired:
                return None
        terminate_process_tree(proc.pid, force=True)
        try:
            return proc.wait(timeout=wait_seconds)
        except subprocess.TimeoutExpired:
            return None


def time_monotonic() -> float:
    import time

    return time.monotonic()


def sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)
