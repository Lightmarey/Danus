"""Run the human-summary service as a stdio MCP server:
``python -m danus.human_summary``.

Launched by the installed ``human-summary-mcp`` entry point (normally
``uv run human-summary-mcp``); Python loads the shared env-file chain first.
"""

def main() -> int:
    from danus import runtime

    runtime.configure_environment()
    from .server import build_app

    build_app().run()
    return 0

if __name__ == "__main__":
    main()
