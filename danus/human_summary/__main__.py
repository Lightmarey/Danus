"""Run the human-summary service as a stdio MCP server:
``python -m danus.human_summary``.

Launched by ``bin/human-summary-mcp`` (which exports
DANUS_HUMAN_SUMMARY_SKILL_DIR and the codex/project env).
"""

def main() -> int:
    from danus import runtime

    runtime.configure_environment()
    from .server import build_app

    build_app().run()
    return 0

if __name__ == "__main__":
    main()
