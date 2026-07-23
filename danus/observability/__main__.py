"""``python -m danus.observability`` entrypoint — launch the read-only dashboard."""

from __future__ import annotations

def main() -> None:
    from danus import runtime

    runtime.configure_environment()
    from .app import main as app_main

    app_main()

if __name__ == "__main__":
    main()
