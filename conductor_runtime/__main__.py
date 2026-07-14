def entrypoint() -> int:
    from .cli import main

    return main()


if __name__ == "__main__":
    raise SystemExit(entrypoint())
