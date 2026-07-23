"""Run the verify service: ``python -m danus.verify`` (default 127.0.0.1:8091)."""

from __future__ import annotations

import os

def main() -> int:
    from danus import runtime

    runtime.configure_environment()
    import uvicorn

    from .service import app

    os.environ.setdefault("CODEX_TIMEOUT_SECONDS", "900")
    host = os.getenv("VERIFY_HOST", "127.0.0.1")
    port = int(os.getenv("VERIFY_PORT", os.getenv("PORT", "8091")))
    uvicorn.run(app, host=host, port=port)
    return 0

if __name__ == "__main__":
    main()
