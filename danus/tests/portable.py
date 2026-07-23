from __future__ import annotations

import os
import stat
from pathlib import Path

from danus import runtime

def write_python_launcher(dirpath: Path, name: str, body: str) -> Path:
    script = dirpath / f"{name}.py"
    script.write_text(body, encoding="utf-8")
    if os.name == "nt":
        launcher = dirpath / f"{name}.cmd"
        launcher.write_text(
            f'@echo off\r\n"{runtime.current_python()}" "{script}" %*\r\n',
            encoding="utf-8",
        )
    else:
        launcher = dirpath / name
        launcher.write_text(
            f'#!/bin/sh\nexec "{runtime.current_python()}" "{script}" "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return launcher
