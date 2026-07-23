"""Run the human-summary service as a stdio MCP server:
``python -m danus.human_summary``.

Launched by ``bin/human-summary-mcp`` (which exports
DANUS_HUMAN_SUMMARY_SKILL_DIR and the codex/project env).
"""

from .server import build_app

def main() -> int:
    build_app().run()
    return 0

if __name__ == "__main__":
    main()
