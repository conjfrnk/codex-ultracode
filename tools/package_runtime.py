#!/usr/bin/env python3
from pathlib import Path
import json
import os
import re
import shutil
import sys
import tempfile
import zipapp
import zipfile

from install_bundle import MANIFEST_SCHEMA, build_skill_manifest, sha256_file
from package_plugin import build_plugin_marketplace


project_root = Path(__file__).resolve().parent.parent
runtime_root = project_root / "conductor_runtime"
skill_root = project_root / "codex-conductor"
dist = Path(sys.argv[1]) if len(sys.argv) > 1 else project_root / "dist"
runtime_output = dist / "conductor-runtime.pyz"
bundle_output = dist / "codex-conductor-bundle.zip"
manifest_output = dist / "release-manifest.json"
plugin_output = dist / "codex-conductor-marketplace.zip"
installer_source = project_root / "tools" / "install_bundle.py"
REPRODUCIBLE_MTIME = 315619200  # 1980-01-02 UTC, portable across local ZIP time zones.
MAX_DEFAULT_RUNTIME_BYTES = 500 * 1024
CORE_RUNTIME_FILES = (
    "__init__.py",
    "__main__.py",
    "cli.py",
    "errors.py",
    "redaction.py",
)


def runtime_version() -> str:
    source = (runtime_root / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$', source, re.MULTILINE)
    if match is None:
        raise SystemExit("Package failed: runtime version is missing or invalid")
    return match.group(1)


def normalize_tree_mtimes(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        os.utime(path, (REPRODUCIBLE_MTIME, REPRODUCIBLE_MTIME), follow_symlinks=False)
    os.utime(root, (REPRODUCIBLE_MTIME, REPRODUCIBLE_MTIME), follow_symlinks=False)

if not runtime_root.is_dir() or not (runtime_root / "cli.py").is_file():
    print("Package failed: conductor_runtime is missing")
    raise SystemExit(1)
if not skill_root.is_dir() or not (skill_root / "SKILL.md").is_file():
    print("Package failed: codex-conductor skill is missing")
    raise SystemExit(1)
if not installer_source.is_file():
    print("Package failed: bundle installer is missing")
    raise SystemExit(1)

dist.mkdir(parents=True, exist_ok=True)
for output in (runtime_output, bundle_output, manifest_output, plugin_output):
    if output.exists():
        output.unlink()

with tempfile.TemporaryDirectory(prefix="conductor-zipapp-") as tmp:
    staging = Path(tmp)
    package = staging / "conductor_runtime"
    package.mkdir()
    for relative in CORE_RUNTIME_FILES:
        shutil.copy2(runtime_root / relative, package / relative)
    shutil.copytree(runtime_root / "core", package / "core", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (staging / "__main__.py").write_text(
        "from conductor_runtime.__main__ import entrypoint\n\nraise SystemExit(entrypoint())\n",
        encoding="utf-8",
    )
    normalize_tree_mtimes(staging)
    zipapp.create_archive(
        staging,
        target=runtime_output,
        interpreter="/usr/bin/env python3",
        compressed=True,
    )
os.utime(runtime_output, (REPRODUCIBLE_MTIME, REPRODUCIBLE_MTIME))
if runtime_output.stat().st_size >= MAX_DEFAULT_RUNTIME_BYTES:
    raise SystemExit("Package failed: default runtime exceeds 500 KiB")

skill_manifest = build_skill_manifest(skill_root)
runtime_size = runtime_output.stat().st_size
installer_size = installer_source.stat().st_size
manifest = {
    "schema": MANIFEST_SCHEMA,
    "version": runtime_version(),
    "runtime": {
        "path": runtime_output.name,
        "sha256": sha256_file(runtime_output),
        "size_bytes": runtime_size,
    },
    "installer": {
        "path": "install.py",
        "sha256": sha256_file(installer_source),
        "size_bytes": installer_size,
    },
    "skill": {
        "path": skill_root.name,
        "tree_sha256": skill_manifest["tree_sha256"],
        "files": skill_manifest["files"],
    },
}
manifest_output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.utime(manifest_output, (REPRODUCIBLE_MTIME, REPRODUCIBLE_MTIME))
build_plugin_marketplace(project_root, runtime_output, plugin_output)

bundle_entries = [
    ("README.md", project_root / "README.md"),
    ("LICENSE", project_root / "LICENSE"),
    (runtime_output.name, runtime_output),
    ("install.py", installer_source),
    (manifest_output.name, manifest_output),
]
for directory in [
    skill_root,
]:
    bundle_entries.extend(
        (path.relative_to(project_root).as_posix(), path)
        for path in directory.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    )

with zipfile.ZipFile(bundle_output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for arcname, path in sorted(bundle_entries):
        archive.write(path, arcname)

for output in (runtime_output, bundle_output, manifest_output, plugin_output):
    if output.stat().st_size >= 25 * 1024 * 1024:
        print("Package failed: %s exceeds the 25 MiB release limit" % output)
        raise SystemExit(1)

print("OK: wrote %s (%d bytes)" % (runtime_output, runtime_output.stat().st_size))
print("OK: wrote %s (%d bytes)" % (manifest_output, manifest_output.stat().st_size))
print("OK: wrote %s (%d bytes)" % (plugin_output, plugin_output.stat().st_size))
print("OK: wrote %s (%d bytes)" % (bundle_output, bundle_output.stat().st_size))
