import sys
from pathlib import Path
from typing import List


def runtime_self_command() -> List[str]:
    argv0 = Path(sys.argv[0])
    if argv0.suffix == ".pyz" and argv0.is_file():
        return [sys.executable, str(argv0.resolve())]
    return [sys.executable, "-B", "-m", "conductor_runtime"]
