#!/usr/bin/env python3
"""Build the optional compatibility and development zipapp."""

import os
import shutil
import sys
import tempfile
import zipapp
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPRODUCIBLE_MTIME = 315619200


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    dist = Path(args[0]).resolve() if args else PROJECT_ROOT / "dist"
    output = dist / "conductor-extras.pyz"
    dist.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.is_symlink():
        output.unlink()
    with tempfile.TemporaryDirectory(prefix="conductor-extras-") as tmp:
        staging = Path(tmp)
        shutil.copytree(
            PROJECT_ROOT / "conductor_runtime",
            staging / "conductor_runtime",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        shutil.copytree(
            PROJECT_ROOT / "conductor_extras",
            staging / "conductor_extras",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        tools = staging / "tools"
        tools.mkdir()
        for name in (
            "__init__.py",
            "evaluate_implementation_canary.py",
            "evaluate_readonly_diagnostic.py",
        ):
            shutil.copy2(PROJECT_ROOT / "tools" / name, tools / name)
        (staging / "__main__.py").write_text(
            "from conductor_extras.__main__ import entrypoint\n\nraise SystemExit(entrypoint())\n",
            encoding="utf-8",
        )
        for path in sorted(staging.rglob("*"), reverse=True):
            os.utime(path, (REPRODUCIBLE_MTIME, REPRODUCIBLE_MTIME), follow_symlinks=False)
        os.utime(staging, (REPRODUCIBLE_MTIME, REPRODUCIBLE_MTIME), follow_symlinks=False)
        zipapp.create_archive(
            staging,
            target=output,
            interpreter="/usr/bin/env python3",
            compressed=True,
        )
    os.utime(output, (REPRODUCIBLE_MTIME, REPRODUCIBLE_MTIME))
    print("OK: wrote %s (%d bytes)" % (output, output.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
