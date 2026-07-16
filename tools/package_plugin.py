#!/usr/bin/env python3
"""Build a reproducible local Codex marketplace containing Codex Conductor."""

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

try:
    from .install_bundle import build_skill_manifest, sha256_file
except ImportError:  # Direct script execution.
    from install_bundle import build_skill_manifest, sha256_file


PLUGIN_NAME = "codex-conductor"
MARKETPLACE_NAME = "codex-conductor-local"
MARKETPLACE_DIR_NAME = "codex-conductor-marketplace"
PLUGIN_BUNDLE_SCHEMA = "conductor.plugin_bundle.v1"
REPRODUCIBLE_MTIME = 315619200
REPRODUCIBLE_ZIP_DATETIME = (1980, 1, 2, 0, 0, 0)
MAX_ARCHIVE_BYTES = 25 * 1024 * 1024


def runtime_version(project_root: Path) -> str:
    source = (project_root / "conductor_runtime" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$', source, re.MULTILINE)
    if match is None:
        raise ValueError("runtime version is missing or invalid")
    return match.group(1)


def plugin_manifest(version: str):
    return {
        "name": PLUGIN_NAME,
        "version": version,
        "description": "Lightweight model-first Codex orchestration with verified staged writes.",
        "author": {"name": "Codex Conductor Project"},
        "license": "MIT",
        "keywords": ["codex", "orchestration", "workflows", "verification"],
        "skills": "./skills/",
        "interface": {
            "displayName": "Codex Conductor",
            "shortDescription": "Lightweight, verified Codex workflow orchestration.",
            "longDescription": (
                "Execute directly by default, or opt into bounded map-to-synthesis workflows, "
                "with explicit safety gates, verification, recovery, and external state."
            ),
            "developerName": "Codex Conductor Project",
            "category": "Productivity",
            "capabilities": ["Interactive", "Write"],
            "defaultPrompt": "Use Codex Conductor to execute this repository task with bounded verification.",
        },
    }


def marketplace_manifest():
    return {
        "name": MARKETPLACE_NAME,
        "interface": {"displayName": "Codex Conductor Local"},
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "source": {"source": "local", "path": "./plugins/%s" % PLUGIN_NAME},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            }
        ],
    }


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _record(path: str, data: bytes):
    return {
        "path": path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }


def build_plugin_marketplace(project_root: Path, runtime_path: Path, output: Path) -> Path:
    project_root = project_root.resolve()
    runtime_path = runtime_path.resolve()
    skill_root = project_root / "codex-conductor"
    license_path = project_root / "LICENSE"
    version = runtime_version(project_root)
    skill = build_skill_manifest(skill_root)
    if not runtime_path.is_file() or runtime_path.is_symlink():
        raise ValueError("packaged runtime is missing or symlinked")
    if not license_path.is_file() or license_path.is_symlink():
        raise ValueError("license is missing or symlinked")

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.is_symlink():
        output.unlink()

    with tempfile.TemporaryDirectory(prefix="conductor-plugin-") as tmp:
        staging = Path(tmp)
        marketplace_root = staging / MARKETPLACE_DIR_NAME
        plugin_root = marketplace_root / "plugins" / PLUGIN_NAME
        marketplace_destination = marketplace_root / ".agents" / "plugins" / "marketplace.json"
        skill_destination = plugin_root / "skills" / PLUGIN_NAME
        scripts_destination = plugin_root / "scripts"
        manifest_destination = plugin_root / ".codex-plugin" / "plugin.json"
        marketplace_destination.parent.mkdir(parents=True)
        skill_destination.mkdir(parents=True)
        scripts_destination.mkdir(parents=True)
        manifest_destination.parent.mkdir(parents=True)

        marketplace_bytes = _json_bytes(marketplace_manifest())
        marketplace_destination.write_bytes(marketplace_bytes)
        plugin_bytes = _json_bytes(plugin_manifest(version))
        manifest_destination.write_bytes(plugin_bytes)

        for item in skill["files"]:
            source = skill_root / item["path"]
            destination = skill_destination / item["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            os.chmod(destination, 0o644)

        embedded_runtime = scripts_destination / "conductor-runtime.pyz"
        shutil.copy2(runtime_path, embedded_runtime)
        os.chmod(embedded_runtime, 0o755)
        shutil.copy2(license_path, plugin_root / "LICENSE")
        os.chmod(plugin_root / "LICENSE", 0o644)

        integrity = {
            "schema": PLUGIN_BUNDLE_SCHEMA,
            "version": version,
            "plugin_manifest": _record(".codex-plugin/plugin.json", plugin_bytes),
            "runtime": {
                "path": "scripts/conductor-runtime.pyz",
                "sha256": sha256_file(embedded_runtime),
                "size_bytes": embedded_runtime.stat().st_size,
            },
            "skill": {
                "path": "skills/%s" % PLUGIN_NAME,
                "tree_sha256": skill["tree_sha256"],
                "files": skill["files"],
            },
        }
        (plugin_root / "plugin-integrity.json").write_bytes(_json_bytes(integrity))
        validate_plugin_integrity(plugin_root)
        _normalize_tree_mtimes(staging)

        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(staging).as_posix())

    os.utime(output, (REPRODUCIBLE_MTIME, REPRODUCIBLE_MTIME))
    if output.stat().st_size >= MAX_ARCHIVE_BYTES:
        output.unlink()
        raise ValueError("plugin marketplace archive exceeds the 25 MiB release limit")
    return output


def validate_plugin_integrity(plugin_root: Path):
    try:
        integrity = json.loads((plugin_root / "plugin-integrity.json").read_text(encoding="utf-8"))
        manifest_bytes = (plugin_root / ".codex-plugin" / "plugin.json").read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("plugin integrity metadata is missing or invalid") from exc
    if not isinstance(integrity, dict) or set(integrity) != {
        "schema",
        "version",
        "plugin_manifest",
        "runtime",
        "skill",
    }:
        raise ValueError("plugin integrity metadata has unsupported fields")
    if integrity["schema"] != PLUGIN_BUNDLE_SCHEMA:
        raise ValueError("plugin integrity schema is unsupported")
    if manifest.get("name") != PLUGIN_NAME or manifest.get("version") != integrity["version"]:
        raise ValueError("plugin manifest identity does not match integrity metadata")
    expected_manifest = integrity["plugin_manifest"]
    if expected_manifest != _record(".codex-plugin/plugin.json", manifest_bytes):
        raise ValueError("plugin manifest does not match integrity metadata")
    runtime = plugin_root / "scripts" / "conductor-runtime.pyz"
    if not runtime.is_file() or runtime.is_symlink():
        raise ValueError("plugin runtime is missing or symlinked")
    runtime_bytes = runtime.read_bytes()
    if integrity["runtime"] != _record("scripts/conductor-runtime.pyz", runtime_bytes):
        raise ValueError("plugin runtime does not match integrity metadata")
    skill_root = plugin_root / "skills" / PLUGIN_NAME
    skill = build_skill_manifest(skill_root)
    expected_skill = integrity["skill"]
    if not isinstance(expected_skill, dict) or set(expected_skill) != {
        "path",
        "tree_sha256",
        "files",
    }:
        raise ValueError("plugin Skill integrity metadata has unsupported fields")
    if (
        expected_skill["path"] != "skills/%s" % PLUGIN_NAME
        or expected_skill["tree_sha256"] != skill["tree_sha256"]
        or expected_skill["files"] != skill["files"]
    ):
        raise ValueError("plugin Skill does not match integrity metadata")
    return integrity


def _normalize_tree_mtimes(root: Path) -> None:
    zip_mtime = time.mktime((*REPRODUCIBLE_ZIP_DATETIME, 0, 0, -1))
    for path in sorted(root.rglob("*"), reverse=True):
        os.utime(path, (zip_mtime, zip_mtime), follow_symlinks=False)
    os.utime(root, (zip_mtime, zip_mtime), follow_symlinks=False)


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    project_root = Path(__file__).resolve().parent.parent
    dist = Path(args[0]).resolve() if args else project_root / "dist"
    runtime_path = dist / "conductor-runtime.pyz"
    output = dist / "codex-conductor-marketplace.zip"
    try:
        build_plugin_marketplace(project_root, runtime_path, output)
    except (OSError, ValueError) as exc:
        print("Plugin package failed: %s" % exc, file=sys.stderr)
        return 1
    print("OK: wrote %s (%d bytes)" % (output, output.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
