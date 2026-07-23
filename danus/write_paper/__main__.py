"""Run the write-paper service as a stdio MCP server:
``python -m danus.write_paper``.

Launched by ``bin/write-paper-mcp`` (which exports DANUS_WRITE_PAPER_SKILL_DIR and the
codex/project env).
"""

def main() -> int:
    from danus import runtime

    runtime.configure_environment()
    from .server import build_app

    build_app().run()
    return 0

if __name__ == "__main__":
    main()
