from __future__ import annotations

import ctypes
import os
import subprocess
import tempfile
from pathlib import Path

from danus import runtime
from danus.tests.portable import write_python_launcher


def test_current_python_defaults_to_running_interpreter():
    got = runtime.current_python()
    assert got
    assert Path(got).name.lower().startswith("python") or got == os.environ.get("DANUS_PYTHON_BIN")


def test_module_cmd_uses_current_python():
    cmd = runtime.module_cmd("danus.gateway", "--help")
    assert cmd[0] == runtime.current_python()
    assert cmd[1:] == ["-m", "danus.gateway", "--help"]


def test_configure_environment_loads_repo_files_with_process_precedence(
    tmp_path,
):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "codex.env").write_text(
        "FROM_CODEX=codex\nEXPLICIT=file\nCODEX_BACKEND=api\n", encoding="utf-8"
    )
    (tmp_path / "config" / "danus.env").write_text(
        "FROM_DANUS=$FROM_CODEX-danus\n", encoding="utf-8"
    )
    configured = {"EXPLICIT": "process"}

    assert runtime.configure_environment(tmp_path, configured) == tmp_path.resolve()
    assert configured["FROM_CODEX"] == "codex"
    assert configured["FROM_DANUS"] == "codex-danus"
    assert configured["EXPLICIT"] == "process"
    assert configured["CODEX_HOME"] == str((tmp_path / "runtime" / "codex-home").resolve())


def test_chatgpt_backend_uses_normal_codex_home(tmp_path):
    configured = {"CODEX_BACKEND": "chatgpt"}
    runtime.configure_environment(tmp_path, configured)
    assert "CODEX_HOME" not in configured


def test_symlink_or_copy_falls_back_to_copy():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        src = root / "src"
        dst = root / "dst"
        src.mkdir()
        (src / "x.txt").write_text("ok", encoding="utf-8")
        real = runtime.os.symlink
        runtime.os.symlink = lambda *a, **k: (_ for _ in ()).throw(OSError("no symlink"))  # type: ignore[assignment]
        try:
            runtime.symlink_or_copy(src, dst)
        finally:
            runtime.os.symlink = real  # type: ignore[assignment]
        assert (dst / "x.txt").read_text(encoding="utf-8") == "ok"


def test_sync_symlink_or_copy_refreshes_copies_and_removes_deleted_files():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        src = root / "src"
        dst = root / "dst"
        src.mkdir()
        (src / "keep.txt").write_text("old", encoding="utf-8")
        (src / "removed.txt").write_text("remove", encoding="utf-8")
        runtime.shutil.copytree(src, dst)

        (src / "keep.txt").write_text("new", encoding="utf-8")
        (src / "removed.txt").unlink()
        runtime.sync_symlink_or_copy(src, dst)

        assert (dst / "keep.txt").read_text(encoding="utf-8") == "new"
        assert not (dst / "removed.txt").exists()


def test_file_lock_is_exclusive():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "x.lock"
        with runtime.file_lock(path) as one:
            assert one is not None
            with runtime.file_lock(path) as two:
                assert two is None


def test_spawn_detached_runs_child():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        marker = root / "done.txt"
        launcher = write_python_launcher(
            root,
            "child",
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ok', encoding='utf-8')\n",
        )
        log = open(root / "child.log", "w", encoding="utf-8")
        try:
            proc = runtime.spawn_detached([str(launcher)], cwd=root, env=os.environ.copy(), stdout=log)
            proc.wait(timeout=10)
        finally:
            log.close()
        assert marker.read_text(encoding="utf-8") == "ok"


def test_process_identity_posix_ps_fallback(monkeypatch):
    class _Completed:
        returncode = 0
        stdout = "Mon Jul 23 16:00:00 2026\n"

    monkeypatch.setattr(runtime, "is_windows", lambda: False)
    monkeypatch.setattr(
        runtime.Path, "read_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no procfs")),
    )
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: _Completed())
    assert runtime.process_identity(123) == "posix:Mon Jul 23 16:00:00 2026"


def test_configure_windows_api_sets_explicit_signatures():
    class _Fn:
        def __init__(self):
            self.argtypes = None
            self.restype = None

    class _Kernel32:
        def __init__(self):
            self.CreateToolhelp32Snapshot = _Fn()
            self.Process32FirstW = _Fn()
            self.Process32NextW = _Fn()
            self.OpenProcess = _Fn()
            self.GetExitCodeProcess = _Fn()
            self.GetProcessTimes = _Fn()
            self.TerminateProcess = _Fn()
            self.CloseHandle = _Fn()

    kernel32 = _Kernel32()
    runtime._configure_windows_api(kernel32)
    assert kernel32.CreateToolhelp32Snapshot.restype is runtime.wintypes.HANDLE
    assert kernel32.CreateToolhelp32Snapshot.argtypes == [runtime.wintypes.DWORD, runtime.wintypes.DWORD]
    assert kernel32.Process32FirstW.restype is runtime.wintypes.BOOL
    assert kernel32.Process32FirstW.argtypes == [runtime.wintypes.HANDLE, ctypes.c_void_p]
    assert kernel32.Process32NextW.argtypes == [runtime.wintypes.HANDLE, ctypes.c_void_p]
    assert kernel32.OpenProcess.argtypes == [runtime.wintypes.DWORD, runtime.wintypes.BOOL, runtime.wintypes.DWORD]
    assert kernel32.OpenProcess.restype is runtime.wintypes.HANDLE
    assert kernel32.GetExitCodeProcess.restype is runtime.wintypes.BOOL
    assert kernel32.GetProcessTimes.restype is runtime.wintypes.BOOL
    assert kernel32.TerminateProcess.argtypes == [runtime.wintypes.HANDLE, runtime.wintypes.UINT]
    assert kernel32.CloseHandle.argtypes == [runtime.wintypes.HANDLE]


def test_terminate_process_tree_waits_for_windows_descendants():
    if not runtime.is_windows():
        return

    calls = []
    terminated = []
    waits = [[101], []]
    real_descendants = runtime._windows_descendant_pids
    real_wait = runtime._wait_for_dead_pids
    real_taskkill = runtime._taskkill_windows_pid
    real_terminate = runtime._terminate_windows_pid
    try:
        runtime._windows_descendant_pids = lambda pid: [101]  # type: ignore[assignment]
        runtime._taskkill_windows_pid = lambda pid, force: calls.append((pid, force))  # type: ignore[assignment]
        runtime._wait_for_dead_pids = lambda pids, timeout_seconds: waits.pop(0)  # type: ignore[assignment]
        runtime._terminate_windows_pid = lambda pid: terminated.append(pid)  # type: ignore[assignment]
        runtime.terminate_process_tree(100, force=True, wait_seconds=1.0)
    finally:
        runtime._windows_descendant_pids = real_descendants  # type: ignore[assignment]
        runtime._wait_for_dead_pids = real_wait  # type: ignore[assignment]
        runtime._taskkill_windows_pid = real_taskkill  # type: ignore[assignment]
        runtime._terminate_windows_pid = real_terminate  # type: ignore[assignment]
    assert calls == [(100, True)]  # one native tree kill; no per-PID process startup
    assert terminated == [101]
    assert waits == []


def test_terminate_process_tree_force_uses_sigkill_on_posix():
    calls = []
    real_is_windows = runtime.is_windows
    real_getpgid = getattr(runtime.os, "getpgid", None)
    real_killpg = getattr(runtime.os, "killpg", None)
    try:
        runtime.is_windows = lambda: False  # type: ignore[assignment]
        runtime.os.getpgid = lambda pid: 77  # type: ignore[assignment]
        runtime.os.killpg = lambda pgid, sig: calls.append((pgid, sig))  # type: ignore[assignment]
        runtime.terminate_process_tree(42, force=True)
    finally:
        runtime.is_windows = real_is_windows  # type: ignore[assignment]
        if real_getpgid is None:
            del runtime.os.getpgid
        else:
            runtime.os.getpgid = real_getpgid  # type: ignore[assignment]
        if real_killpg is None:
            del runtime.os.killpg
        else:
            runtime.os.killpg = real_killpg  # type: ignore[assignment]
    assert calls == [(77, runtime._SIGKILL)]


def test_terminate_process_tree_graceful_still_uses_sigterm_on_posix():
    calls = []
    real_is_windows = runtime.is_windows
    real_getpgid = getattr(runtime.os, "getpgid", None)
    real_killpg = getattr(runtime.os, "killpg", None)
    real_pid_alive = runtime.pid_alive
    try:
        runtime.is_windows = lambda: False  # type: ignore[assignment]
        runtime.os.getpgid = lambda pid: 77  # type: ignore[assignment]
        runtime.os.killpg = lambda pgid, sig: calls.append((pgid, sig))  # type: ignore[assignment]
        runtime.pid_alive = lambda pid: False  # type: ignore[assignment]
        runtime.terminate_process_tree(42, force=False)
    finally:
        runtime.is_windows = real_is_windows  # type: ignore[assignment]
        if real_getpgid is None:
            del runtime.os.getpgid
        else:
            runtime.os.getpgid = real_getpgid  # type: ignore[assignment]
        if real_killpg is None:
            del runtime.os.killpg
        else:
            runtime.os.killpg = real_killpg  # type: ignore[assignment]
        runtime.pid_alive = real_pid_alive  # type: ignore[assignment]
    assert calls == [(77, runtime.signal.SIGTERM)]


def test_stop_process_force_kills_lingering_root():
    class _Proc:
        pid = 42
        returncode = None

        def __init__(self):
            self.wait_calls = 0
            self.killed = False

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout)
            self.returncode = -9
            return self.returncode

        def kill(self):
            self.killed = True

    proc = _Proc()
    calls = []
    real_term = runtime.terminate_process_tree
    try:
        runtime.terminate_process_tree = lambda pid, force, wait_seconds=5.0: calls.append((pid, force, wait_seconds))  # type: ignore[assignment]
        rc = runtime.stop_process(proc, wait_seconds=3.0, force=True)
    finally:
        runtime.terminate_process_tree = real_term  # type: ignore[assignment]
    assert calls == [(42, True, 3.0)]
    assert proc.killed
    assert rc == -9


def test_wait_until_path_releasable_non_windows_is_true():
    real_is_windows = runtime.is_windows
    try:
        runtime.is_windows = lambda: False  # type: ignore[assignment]
        assert runtime.wait_until_path_releasable(Path("anything.log"), timeout_seconds=0.1) is True
    finally:
        runtime.is_windows = real_is_windows  # type: ignore[assignment]


def test_wait_until_path_releasable_missing_path_is_true():
    if not runtime.is_windows():
        return
    assert runtime.wait_until_path_releasable(Path("C:/definitely-missing.log"), timeout_seconds=0.1) is True


def test_wait_until_path_releasable_timeout_is_false():
    if not runtime.is_windows():
        return
    path = Path("C:/locked-loop.log")
    probe = Path("C:/locked-loop.log.delete-probe")
    real_exists = runtime.Path.exists
    real_replace = runtime.os.replace
    real_sleep = runtime.sleep
    try:
        runtime.Path.exists = lambda self: self in {path, probe}  # type: ignore[assignment]
        runtime.os.replace = lambda src, dst: (_ for _ in ()).throw(PermissionError("locked"))  # type: ignore[assignment]
        runtime.sleep = lambda seconds: None  # type: ignore[assignment]
        assert runtime.wait_until_path_releasable(path, timeout_seconds=0.01) is False
    finally:
        runtime.Path.exists = real_exists  # type: ignore[assignment]
        runtime.os.replace = real_replace  # type: ignore[assignment]
        runtime.sleep = real_sleep  # type: ignore[assignment]
