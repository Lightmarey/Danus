"""danus.orchestration — the ``danus`` CLI verbs (the main agent's control surface).

The verbs scaffold projects, manage v2 research control, assign workers, and
start / monitor / stop bounded exploration. The loop + on-disk layout they drive
live in ``danus.execution``; this module is the verbs/UX only.

Run as ``python -m danus.orchestration`` (this is what ``bin/danus`` execs).
"""

from __future__ import annotations

from .cli import (
    build_parser,
    do_assign,
    do_control_rebuild,
    do_control_taint,
    do_list,
    do_new,
    do_obligation,
    do_route,
    do_start,
    do_status,
    do_stop,
    do_target,
    main,
    worker_status,
)

__all__ = [
    "do_new",
    "do_target",
    "do_obligation",
    "do_route",
    "do_control_rebuild",
    "do_control_taint",
    "do_assign",
    "do_start",
    "do_status",
    "worker_status",
    "do_list",
    "do_stop",
    "build_parser",
    "main",
]
