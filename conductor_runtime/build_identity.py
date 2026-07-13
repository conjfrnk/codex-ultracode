import hashlib
from pathlib import Path

from .errors import ValidationError
from .security import read_regular_file_bytes_no_follow


MAX_RUNTIME_BUILD_BYTES = 25 * 1024 * 1024
MAX_RUNTIME_BUILD_FILES = 1000


def current_runtime_build_sha256() -> str:
    """Hash the exact zipapp, or the executable source set in source mode."""
    archive = getattr(globals().get("__loader__"), "archive", None)
    if isinstance(archive, str) and archive:
        payload = read_regular_file_bytes_no_follow(
            Path(archive),
            "Conductor runtime archive",
            MAX_RUNTIME_BUILD_BYTES,
        )
        return hashlib.sha256(payload).hexdigest()

    project_root = Path(__file__).resolve().parent.parent
    runtime_root = project_root / "conductor_runtime"
    candidates = list(runtime_root.rglob("*.py"))
    candidates.extend(
        project_root / relative
        for relative in (
            "tools/__init__.py",
            "tools/evaluate_implementation_canary.py",
            "tools/evaluate_readonly_diagnostic.py",
        )
    )
    paths = sorted(candidates, key=lambda path: path.relative_to(project_root).as_posix())
    if not paths or len(paths) > MAX_RUNTIME_BUILD_FILES:
        raise ValidationError("Conductor runtime source set has an invalid file count")

    digest = hashlib.sha256()
    total = 0
    for path in paths:
        if path.is_symlink():
            raise ValidationError("Conductor runtime source files must not be symlinks")
        relative = path.relative_to(project_root).as_posix().encode("utf-8")
        payload = read_regular_file_bytes_no_follow(
            path,
            "Conductor runtime source",
            MAX_RUNTIME_BUILD_BYTES,
        )
        total += len(payload)
        if total > MAX_RUNTIME_BUILD_BYTES:
            raise ValidationError("Conductor runtime source set exceeds the supported size")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()
