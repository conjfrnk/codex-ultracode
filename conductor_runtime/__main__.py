import sys


def entrypoint() -> int:
    from . import require_supported_python

    try:
        require_supported_python()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    from .cli import main

    return main()


if __name__ == "__main__":
    raise SystemExit(entrypoint())
