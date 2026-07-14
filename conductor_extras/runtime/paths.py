import hashlib
import os
import re
from pathlib import Path


CONDUCTOR_HOME_ENV = "CODEX_CONDUCTOR_HOME"


def conductor_home() -> Path:
    configured = os.environ.get(CONDUCTOR_HOME_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex" / "conductor"


def workspace_state_dir(workspace: Path = None) -> Path:
    root = Path("." if workspace is None else workspace).expanduser().resolve()
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", root.name).strip(".-") or "workspace"
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return conductor_home() / "workspaces" / ("%s-%s" % (name, digest))


def default_runs_dir(workspace: Path = None) -> Path:
    return workspace_state_dir(workspace) / "runs"


def default_goals_dir(workspace: Path = None) -> Path:
    return workspace_state_dir(workspace) / "goals"


def default_benchmarks_dir(workspace: Path = None) -> Path:
    return workspace_state_dir(workspace) / "benchmarks"


def default_routines_dir(workspace: Path = None) -> Path:
    return workspace_state_dir(workspace) / "routines"


def default_generated_dir(workspace: Path = None) -> Path:
    return workspace_state_dir(workspace) / "generated"


def default_agent_memory_dir(workspace: Path = None) -> Path:
    return workspace_state_dir(workspace) / "agent-memory"


def default_cloud_dir(workspace: Path = None) -> Path:
    return workspace_state_dir(workspace) / "cloud"


def default_dashboard_path(workspace: Path = None) -> Path:
    return workspace_state_dir(workspace) / "dashboard.html"


def home_display_path(path: Path) -> str:
    home = Path.home()
    try:
        return "~/%s" % path.resolve().relative_to(home.resolve()).as_posix()
    except ValueError:
        configured = os.environ.get(CONDUCTOR_HOME_ENV)
        if configured:
            try:
                relative = path.resolve().relative_to(Path(configured).expanduser().resolve())
                return "$%s/%s" % (CONDUCTOR_HOME_ENV, relative.as_posix())
            except ValueError:
                pass
        return path.as_posix()


def default_live_parity_report_dir(workspace: Path = None) -> str:
    return home_display_path(default_benchmarks_dir(workspace) / "live-parity")


def default_live_parity_workflow_dir(workspace: Path = None) -> str:
    return home_display_path(default_generated_dir(workspace) / "live-parity")
