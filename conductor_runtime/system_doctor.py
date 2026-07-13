import os
import shutil
import stat
from pathlib import Path
from typing import Dict, Iterable, Optional

from .errors import PolicyError, ValidationError


CODEX_HOST_BASENAME = "codex-code-mode-host"
CODEX_HOST_REPAIR_APPROVAL = "codex-host-repair"
CODEX_INSTALLATION_SCHEMA = "conductor.codex_installation.v1"


def inspect_codex_installation(codex_path: Optional[str] = None) -> Dict:
    discovered = codex_path or shutil.which("codex")
    if not discovered:
        return _report("codex-not-found", healthy=False)

    launcher = Path(os.path.abspath(os.path.expanduser(discovered)))
    if not _is_executable_file(launcher):
        return _report(
            "codex-launcher-invalid",
            healthy=False,
            codex_launcher=str(launcher),
        )

    try:
        canonical_launcher = launcher.resolve(strict=True)
        source_launcher = _immediate_link_target(launcher) if launcher.is_symlink() else canonical_launcher
    except (OSError, RuntimeError) as exc:
        return _report(
            "codex-launcher-invalid",
            healthy=False,
            codex_launcher=str(launcher),
            detail=_bounded_error(exc),
        )

    expected_host = launcher.parent / CODEX_HOST_BASENAME
    source_host = source_launcher.parent / CODEX_HOST_BASENAME
    base = {
        "codex_launcher": str(launcher),
        "canonical_codex": str(canonical_launcher),
        "expected_host": str(expected_host),
        "host_source": str(source_host),
    }

    if not _is_executable_file(source_host):
        return _report("codex-host-source-unavailable", healthy=False, **base)
    try:
        canonical_source = source_host.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return _report(
            "codex-host-source-unavailable",
            healthy=False,
            detail=_bounded_error(exc),
            **base,
        )

    if expected_host.is_symlink() and not expected_host.exists():
        return _report("codex-host-link-broken", healthy=False, **base)
    if not expected_host.exists():
        return _report("codex-host-missing", healthy=False, **base)
    if not _is_executable_file(expected_host):
        return _report("codex-host-not-executable", healthy=False, **base)

    try:
        canonical_expected = expected_host.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return _report(
            "codex-host-invalid",
            healthy=False,
            detail=_bounded_error(exc),
            **base,
        )
    if canonical_expected != canonical_source:
        return _report(
            "codex-host-version-mismatch",
            healthy=False,
            resolved_host=str(canonical_expected),
            resolved_source=str(canonical_source),
            **base,
        )
    return _report(
        "ready",
        healthy=True,
        resolved_host=str(canonical_expected),
        resolved_source=str(canonical_source),
        **base,
    )


def repair_codex_host_link(
    *,
    approvals: Iterable[str],
    codex_path: Optional[str] = None,
    home: Optional[Path] = None,
) -> Dict:
    approval_set = set(approvals)
    if CODEX_HOST_REPAIR_APPROVAL not in approval_set and "all" not in approval_set:
        raise PolicyError(
            "Codex host repair requires --approve %s" % CODEX_HOST_REPAIR_APPROVAL
        )

    report = inspect_codex_installation(codex_path)
    if report["status"] == "ready":
        return dict(report, repaired=False)
    if report["status"] != "codex-host-missing":
        raise ValidationError(
            "Codex host repair only creates a missing sibling link; current status is %s"
            % report["status"]
        )

    destination = Path(report["expected_host"])
    source = Path(report["host_source"])
    allowed_home = Path.home() if home is None else Path(home)
    try:
        resolved_home = allowed_home.expanduser().resolve(strict=True)
        resolved_parent = destination.parent.resolve(strict=True)
        resolved_parent.relative_to(resolved_home)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PolicyError("Codex host repair destination must be inside the current user home") from exc

    if destination.exists() or destination.is_symlink():
        raise ValidationError("Codex host repair destination already exists")
    if not _is_executable_file(source):
        raise ValidationError("Codex host repair source is not an executable regular file")

    try:
        os.symlink(str(source), str(destination))
    except FileExistsError as exc:
        raise ValidationError("Codex host repair destination already exists") from exc
    except OSError as exc:
        raise ValidationError("Codex host repair could not create the sibling link") from exc

    repaired = inspect_codex_installation(str(Path(report["codex_launcher"])))
    if repaired["status"] != "ready":
        try:
            if destination.is_symlink():
                destination.unlink()
        except OSError:
            pass
        raise ValidationError("Codex host repair did not produce a healthy installation")
    return dict(repaired, repaired=True)


def _immediate_link_target(path: Path) -> Path:
    target = Path(os.readlink(path))
    if not target.is_absolute():
        target = path.parent / target
    return Path(os.path.abspath(str(target)))


def _is_executable_file(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(mode) and os.access(path, os.X_OK)


def _report(status: str, *, healthy: bool, **fields) -> Dict:
    return {
        "schema": CODEX_INSTALLATION_SCHEMA,
        "status": status,
        "healthy": healthy,
        **fields,
    }


def _bounded_error(exc: BaseException) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ")
    return text[:240]
