#!/usr/bin/env python3
"""Build the public Conductor bundle and local marketplace archive."""

import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import zipapp
import zipfile
from pathlib import Path

if __package__:
    from .install_bundle import MANIFEST_SCHEMA, build_agent_manifest, build_skill_manifest, sha256_file
else:  # Direct script execution.
    from install_bundle import MANIFEST_SCHEMA, build_agent_manifest, build_skill_manifest, sha256_file


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPRODUCIBLE_ZIP_DATETIME = (1980, 1, 2, 0, 0, 0)
REPRODUCIBLE_MTIME = 315619200  # Fixed mtime for the output files themselves.
MAX_DEFAULT_RUNTIME_BYTES = 500 * 1024
MAX_RELEASE_BYTES = 25 * 1024 * 1024
CORE_RUNTIME_FILES = (
    "__init__.py",
    "__main__.py",
    "cli.py",
    "errors.py",
    "redaction.py",
)
PUBLIC_OUTPUTS = (
    "codex-conductor-bundle.zip",
    "codex-conductor-marketplace.zip",
)
RETIRED_TOP_LEVEL_OUTPUTS = (
    ".gitkeep",
    "conductor-runtime.pyz",
    "release-manifest.json",
    "skill.zip",
)


def runtime_version(project_root: Path) -> str:
    source = (project_root / "conductor_runtime" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$', source, re.MULTILINE)
    if match is None:
        raise ValueError("runtime version is missing or invalid")
    return match.group(1)


def normalize_tree_mtimes(root: Path) -> None:
    # zipfile records filesystem mtimes as local wall-clock values. Derive the
    # epoch for the fixed ZIP timestamp in the builder's current timezone.
    zip_mtime = time.mktime((*REPRODUCIBLE_ZIP_DATETIME, 0, 0, -1))
    for path in sorted(root.rglob("*"), reverse=True):
        os.utime(path, (zip_mtime, zip_mtime), follow_symlinks=False)
    os.utime(root, (zip_mtime, zip_mtime), follow_symlinks=False)


def write_reproducible_file(
    archive: zipfile.ZipFile,
    arcname: str,
    path: Path,
    mode: int,
) -> None:
    info = zipfile.ZipInfo(arcname, date_time=REPRODUCIBLE_ZIP_DATETIME)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | mode) << 16
    archive.writestr(info, path.read_bytes())


def _require_project_inputs(project_root: Path) -> None:
    runtime_root = project_root / "conductor_runtime"
    skill_root = project_root / "codex-conductor"
    agent_root = project_root / "codex-agents"
    installer_source = project_root / "tools" / "install_bundle.py"
    if not runtime_root.is_dir() or not (runtime_root / "cli.py").is_file():
        raise ValueError("conductor_runtime is missing")
    if not skill_root.is_dir() or not (skill_root / "SKILL.md").is_file():
        raise ValueError("codex-conductor skill is missing")
    if not agent_root.is_dir():
        raise ValueError("codex-agents is missing")
    if not installer_source.is_file():
        raise ValueError("bundle installer is missing")


def build_runtime_archive(project_root: Path, output: Path) -> Path:
    runtime_root = project_root / "conductor_runtime"
    with tempfile.TemporaryDirectory(prefix="conductor-zipapp-") as tmp:
        staging = Path(tmp)
        package = staging / "conductor_runtime"
        package.mkdir()
        for relative in CORE_RUNTIME_FILES:
            shutil.copy2(runtime_root / relative, package / relative)
        shutil.copytree(
            runtime_root / "core",
            package / "core",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (staging / "__main__.py").write_text(
            "from conductor_runtime.__main__ import entrypoint\n\nraise SystemExit(entrypoint())\n",
            encoding="utf-8",
        )
        normalize_tree_mtimes(staging)
        zipapp.create_archive(
            staging,
            target=output,
            interpreter="/usr/bin/env python3",
            compressed=True,
        )
    os.utime(output, (REPRODUCIBLE_MTIME, REPRODUCIBLE_MTIME))
    if output.stat().st_size >= MAX_DEFAULT_RUNTIME_BYTES:
        raise ValueError("default runtime exceeds 500 KiB")
    return output


def _remove_owned_output(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        raise ValueError("release output path is a directory: %s" % path.name)
    path.unlink()


def build_release_artifacts(project_root: Path, dist: Path):
    if __package__:
        from .package_plugin import build_plugin_marketplace
    else:  # Direct script execution.
        from package_plugin import build_plugin_marketplace

    project_root = project_root.resolve()
    dist = dist.resolve()
    _require_project_inputs(project_root)
    dist.mkdir(parents=True, exist_ok=True)
    for name in (*PUBLIC_OUTPUTS, *RETIRED_TOP_LEVEL_OUTPUTS):
        _remove_owned_output(dist / name)

    bundle_output = dist / PUBLIC_OUTPUTS[0]
    plugin_output = dist / PUBLIC_OUTPUTS[1]
    skill_root = project_root / "codex-conductor"
    agent_root = project_root / "codex-agents"
    installer_source = project_root / "tools" / "install_bundle.py"

    with tempfile.TemporaryDirectory(prefix="conductor-release-") as tmp:
        private = Path(tmp)
        runtime_output = private / "conductor-runtime.pyz"
        manifest_output = private / "release-manifest.json"
        build_runtime_archive(project_root, runtime_output)

        skill_manifest = build_skill_manifest(skill_root)
        agent_manifest = build_agent_manifest(agent_root)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "version": runtime_version(project_root),
            "runtime": {
                "path": runtime_output.name,
                "sha256": sha256_file(runtime_output),
                "size_bytes": runtime_output.stat().st_size,
            },
            "installer": {
                "path": "install.py",
                "sha256": sha256_file(installer_source),
                "size_bytes": installer_source.stat().st_size,
            },
            "skill": {
                "path": skill_root.name,
                "tree_sha256": skill_manifest["tree_sha256"],
                "files": skill_manifest["files"],
            },
            "agents": {
                "path": agent_root.name,
                "tree_sha256": agent_manifest["tree_sha256"],
                "files": agent_manifest["files"],
            },
        }
        manifest_output.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.utime(manifest_output, (REPRODUCIBLE_MTIME, REPRODUCIBLE_MTIME))
        build_plugin_marketplace(project_root, runtime_output, plugin_output)

        bundle_entries = [
            ("README.md", project_root / "README.md", 0o644),
            ("LICENSE", project_root / "LICENSE", 0o644),
            (runtime_output.name, runtime_output, 0o755),
            ("install.py", installer_source, 0o755),
            (manifest_output.name, manifest_output, 0o644),
        ]
        for directory in (
            skill_root,
            project_root / "docs",
            project_root / "conductor-workflows",
        ):
            bundle_entries.extend(
                (path.relative_to(project_root).as_posix(), path, 0o644)
                for path in directory.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
            )
        bundle_entries.extend(
            (
                "%s/%s" % (agent_root.name, item["path"]),
                agent_root / item["path"],
                0o644,
            )
            for item in agent_manifest["files"]
        )

        with zipfile.ZipFile(bundle_output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for arcname, path, mode in sorted(bundle_entries):
                write_reproducible_file(archive, arcname, path, mode)
        os.utime(bundle_output, (REPRODUCIBLE_MTIME, REPRODUCIBLE_MTIME))

        for output in (runtime_output, manifest_output, bundle_output, plugin_output):
            if output.stat().st_size >= MAX_RELEASE_BYTES:
                raise ValueError("%s exceeds the 25 MiB release limit" % output.name)

    return bundle_output, plugin_output


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) > 1:
        print("Package failed: expected at most one output directory", file=sys.stderr)
        return 2
    dist = Path(args[0]) if args else PROJECT_ROOT / "dist"
    try:
        outputs = build_release_artifacts(PROJECT_ROOT, dist)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print("Package failed: %s" % exc, file=sys.stderr)
        return 1
    for output in outputs:
        print("OK: wrote %s (%d bytes)" % (output, output.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
