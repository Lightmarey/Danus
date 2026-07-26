"""Run the write-paper service as a stdio MCP server:
``python -m danus.write_paper``.

Launched by the installed ``write-paper-mcp`` entry point (normally
``uv run write-paper-mcp``); Python loads the shared env-file chain first.
"""

def main() -> int:
    from danus import runtime

    runtime.configure_environment()
    from .server import build_app

    build_app().run()
    return 0

if __name__ == "__main__":
    main()
