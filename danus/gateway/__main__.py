"""Run the danus gateway as a stdio MCP server: ``python -m danus.gateway``.

Role is taken from ``DANUS_ROLE`` (env). Launched by ``bin/danus-mcp`` (main) and
by each worker's ``.codex/config.toml`` (worker) / the verifier's ``-c`` override.
"""

def main() -> int:
    from danus import runtime

    runtime.configure_environment()
    from .server import build_app

    build_app().run()
    return 0

if __name__ == "__main__":
    main()
