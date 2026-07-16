import sys


def entrypoint() -> int:
    from conductor_runtime import require_supported_python

    try:
        require_supported_python()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if len(sys.argv) > 1 and sys.argv[1] == "_skill-mcp-stdio-proxy":
        from conductor_extras.runtime.skill_mcp_stdio import proxy_main

        return proxy_main(sys.argv[2:])
    from .cli import main

    return main()


if __name__ == "__main__":
    raise SystemExit(entrypoint())
