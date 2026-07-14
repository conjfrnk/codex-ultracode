#!/usr/bin/env python3
"""Install an extracted Codex Conductor release bundle without dependencies."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple


MANIFEST_SCHEMA = "conductor.release_bundle.v1"
RECEIPT_SCHEMA = "conductor.bundle_install_receipt.v1"
INSTALL_APPROVAL = "conductor-install"
UPDATE_APPROVAL = "conductor-update"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_BUNDLE_FILE_BYTES = 25 * 1024 * 1024
MAX_SKILL_FILES = 128
ALLOWED_SKILL_SUFFIXES = {".md", ".txt", ".yaml", ".yml"}
DENIED_SKILL_NAMES = {".env", ".npmrc", ".pypirc", "id_ed25519", "id_rsa"}
SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_PATH_PART = re.compile(r"^[A-Za-z0-9._-]+$")


class InstallError(Exception):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(_read_regular_bytes(path, "file", MAX_BUNDLE_FILE_BYTES)).hexdigest()


def skill_tree_sha256(files: List[Dict]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(item["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(item["sha256"].encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["size_bytes"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_skill_manifest(skill_root: Path) -> Dict:
    _require_real_directory(skill_root, "Skill source")
    files = []
    for path in sorted(skill_root.rglob("*")):
        relative = path.relative_to(skill_root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise InstallError("Skill source contains a symlink: %s" % relative)
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise InstallError("Skill source contains a non-regular file: %s" % relative)
        if path.name in DENIED_SKILL_NAMES or path.suffix.lower() not in ALLOWED_SKILL_SUFFIXES:
            raise InstallError("Skill source contains a non-text or sensitive path: %s" % relative)
        data = _read_regular_bytes(path, "Skill source file %s" % relative, MAX_BUNDLE_FILE_BYTES)
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InstallError("Skill source is not valid UTF-8 text: %s" % relative) from exc
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
        )
    if not files or len(files) > MAX_SKILL_FILES:
        raise InstallError("Skill source file count is outside the supported range")
    if files != sorted(files, key=lambda item: item["path"]):
        raise InstallError("Skill source files are not canonically ordered")
    return {"files": files, "tree_sha256": skill_tree_sha256(files)}


def validate_bundle(bundle_root: Path, installer_path: Optional[Path] = None) -> Tuple[Dict, str]:
    _require_real_directory(bundle_root, "bundle root")
    manifest_path = bundle_root / "release-manifest.json"
    manifest_bytes = _read_regular_bytes(manifest_path, "release manifest", MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError("release manifest is not valid UTF-8 JSON") from exc
    _require_fields(manifest, {"schema", "version", "runtime", "installer", "skill"}, "release manifest")
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise InstallError("release manifest schema is unsupported")
    version = manifest["version"]
    if not isinstance(version, str) or SEMVER.fullmatch(version) is None:
        raise InstallError("release manifest version is not strict semantic versioning")

    runtime = _validate_file_record(manifest["runtime"], "runtime")
    installer = _validate_file_record(manifest["installer"], "installer")
    skill = manifest["skill"]
    _require_fields(skill, {"path", "tree_sha256", "files"}, "Skill manifest")
    skill_path = _safe_relative_path(skill["path"], "Skill path")
    if not isinstance(skill["tree_sha256"], str) or SHA256.fullmatch(skill["tree_sha256"]) is None:
        raise InstallError("Skill tree hash is invalid")
    if not isinstance(skill["files"], list) or not 1 <= len(skill["files"]) <= MAX_SKILL_FILES:
        raise InstallError("Skill file list is outside the supported range")
    expected_files = []
    for index, item in enumerate(skill["files"]):
        expected_files.append(_validate_file_record(item, "Skill file %d" % index))
    if expected_files != sorted(expected_files, key=lambda item: item["path"]):
        raise InstallError("Skill file list is not canonically ordered")
    if len({item["path"] for item in expected_files}) != len(expected_files):
        raise InstallError("Skill file list contains duplicate paths")
    if skill_tree_sha256(expected_files) != skill["tree_sha256"]:
        raise InstallError("Skill tree hash does not match its file records")

    _verify_record(bundle_root, runtime, "runtime")
    _verify_record(bundle_root, installer, "installer")
    actual_installer = installer_path.resolve() if installer_path is not None else None
    expected_installer = _safe_join(bundle_root, installer["path"], "installer").resolve()
    if actual_installer is not None and actual_installer != expected_installer:
        raise InstallError("installer must run from the validated bundle root")

    actual_skill_root = _safe_join(bundle_root, skill_path.as_posix(), "Skill")
    actual_skill = build_skill_manifest(actual_skill_root)
    if actual_skill["files"] != expected_files or actual_skill["tree_sha256"] != skill["tree_sha256"]:
        raise InstallError("Skill source does not match the release manifest")
    return manifest, hashlib.sha256(manifest_bytes).hexdigest()


def install_bundle(
    bundle_root: Path,
    codex_home: Path,
    conductor_home: Path,
    *,
    allow_writes: bool,
    approvals: List[str],
    replace: bool,
    dry_run: bool,
    installer_path: Optional[Path] = None,
) -> Dict:
    bundle_root = _resolve_configured_root(bundle_root, "bundle root")
    codex_home = _resolve_configured_root(codex_home, "Codex home")
    conductor_home = _resolve_configured_root(conductor_home, "Conductor home")
    manifest, manifest_sha256 = validate_bundle(bundle_root, installer_path=installer_path)
    version = manifest["version"]
    source_runtime = _safe_join(bundle_root, manifest["runtime"]["path"], "runtime")
    source_installer = _safe_join(bundle_root, manifest["installer"]["path"], "installer")
    source_skill = _safe_join(bundle_root, manifest["skill"]["path"], "Skill")

    release_dir = conductor_home / "releases" / version / "installed"
    active_runtime = conductor_home / "bin" / "conductor-runtime.pyz"
    active_skill = codex_home / "skills" / "codex-conductor"
    receipt_path = conductor_home / "installations" / "current.json"
    _reject_destination_overlaps(
        {
            "release": release_dir,
            "runtime": active_runtime,
            "skill": active_skill,
            "receipt": receipt_path,
        }
    )
    _reject_symlink_components(conductor_home, release_dir.parent)
    _reject_symlink_components(conductor_home, active_runtime.parent)
    _reject_symlink_components(conductor_home, receipt_path.parent)
    _reject_symlink_components(codex_home, active_skill.parent)
    statuses = {
        "release": _release_matches(release_dir, manifest, manifest_sha256),
        "runtime": _file_matches(active_runtime, manifest["runtime"]),
        "skill": _skill_matches(active_skill, manifest["skill"]),
    }
    changes = [name for name, matches in statuses.items() if not matches]
    conflicts = [
        name
        for name, path in (("release", release_dir), ("runtime", active_runtime), ("skill", active_skill))
        if name in changes and _lexists(path)
    ]
    result = {
        "schema": RECEIPT_SCHEMA,
        "version": version,
        "status": "already-installed" if not changes else ("dry-run" if dry_run else "installed"),
        "changes": changes,
        "conflicts": conflicts,
        "manifest_sha256": manifest_sha256,
        "runtime_sha256": manifest["runtime"]["sha256"],
        "skill_tree_sha256": manifest["skill"]["tree_sha256"],
        "destinations": {
            "release": str(release_dir),
            "runtime": str(active_runtime),
            "skill": str(active_skill),
            "receipt": str(receipt_path),
        },
    }
    if not changes or dry_run:
        return result
    if conflicts and not replace:
        raise InstallError(
            "existing differing installation requires --replace and --approve %s: %s"
            % (UPDATE_APPROVAL, ", ".join(conflicts))
        )
    if not allow_writes or INSTALL_APPROVAL not in set(approvals):
        raise InstallError("installation requires --allow-writes and --approve %s" % INSTALL_APPROVAL)
    if conflicts and UPDATE_APPROVAL not in set(approvals):
        raise InstallError("replacement requires --approve %s" % UPDATE_APPROVAL)

    release_stage = None
    runtime_stage = None
    skill_stage = None
    receipt_stage = None
    try:
        if "release" in changes:
            release_stage = _stage_directory(release_dir.parent, ".conductor-release-")
            _copy_regular_file(source_runtime, release_stage / "conductor-runtime.pyz", executable=True)
            _copy_skill(source_skill, release_stage / "codex-conductor", manifest["skill"])
            _copy_regular_file(source_installer, release_stage / "install.py", executable=True)
            _copy_regular_file(bundle_root / "release-manifest.json", release_stage / "release-manifest.json")
        if "runtime" in changes:
            runtime_stage = _stage_file(active_runtime.parent, ".conductor-runtime-")
            _copy_regular_file(source_runtime, runtime_stage, executable=True)
        if "skill" in changes:
            skill_stage = _stage_directory(active_skill.parent, ".codex-conductor-skill-")
            _copy_skill(source_skill, skill_stage, manifest["skill"], destination_is_root=True)

        if release_stage is not None and not _release_matches(release_stage, manifest, manifest_sha256):
            raise InstallError("staged release failed verification")
        if runtime_stage is not None and not _file_matches(runtime_stage, manifest["runtime"]):
            raise InstallError("staged runtime failed verification")
        if skill_stage is not None and not _skill_matches(skill_stage, manifest["skill"]):
            raise InstallError("staged Skill failed verification")

        receipt = dict(result)
        receipt["status"] = "installed"
        receipt["installed_at_utc"] = _utc_now()
        receipt_stage = _stage_file(receipt_path.parent, ".conductor-receipt-")
        receipt_stage.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(receipt_stage, 0o600)

        replacements = []
        if release_stage is not None:
            replacements.append((release_stage, release_dir))
        if runtime_stage is not None:
            replacements.append((runtime_stage, active_runtime))
        if skill_stage is not None:
            replacements.append((skill_stage, active_skill))
        replacements.append((receipt_stage, receipt_path))
        _commit_replacements(replacements)
    finally:
        for path in (release_stage, runtime_stage, skill_stage, receipt_stage):
            if path is not None and _lexists(path):
                _remove_path(path)

    if not _release_matches(release_dir, manifest, manifest_sha256):
        raise InstallError("installed release failed post-commit verification")
    if not _file_matches(active_runtime, manifest["runtime"]):
        raise InstallError("installed runtime failed post-commit verification")
    if not _skill_matches(active_skill, manifest["skill"]):
        raise InstallError("installed Skill failed post-commit verification")
    return result


def _validate_file_record(value, label: str) -> Dict:
    _require_fields(value, {"path", "sha256", "size_bytes"}, label)
    path = _safe_relative_path(value["path"], "%s path" % label).as_posix()
    digest = value["sha256"]
    size = value["size_bytes"]
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise InstallError("%s SHA-256 is invalid" % label)
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_BUNDLE_FILE_BYTES:
        raise InstallError("%s size is outside the supported range" % label)
    return {"path": path, "sha256": digest, "size_bytes": size}


def _require_fields(value, fields, label: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise InstallError("%s must contain exactly the supported fields" % label)


def _safe_relative_path(value, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise InstallError("%s is invalid" % label)
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        part in {"", ".", ".."} or SAFE_PATH_PART.fullmatch(part) is None for part in path.parts
    ):
        raise InstallError("%s must be a safe relative path" % label)
    return path


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise InstallError("release manifest contains a duplicate JSON key: %s" % key)
        value[key] = item
    return value


def _verify_record(bundle_root: Path, record: Dict, label: str) -> None:
    path = _safe_join(bundle_root, record["path"], label)
    data = _read_regular_bytes(path, label, MAX_BUNDLE_FILE_BYTES)
    if len(data) != record["size_bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise InstallError("%s does not match the release manifest" % label)


def _read_regular_bytes(path: Path, label: str, limit: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise InstallError("%s is missing" % label) from exc
    except OSError as exc:
        raise InstallError("%s must be a readable regular non-symlink file" % label) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise InstallError("%s must be a regular non-symlink file" % label)
        if before.st_size > limit:
            raise InstallError("%s exceeds the supported size" % label)
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(data) > limit:
            raise InstallError("%s exceeds the supported size" % label)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or len(data) != after.st_size:
            raise InstallError("%s changed while it was being read" % label)
        return data
    finally:
        os.close(descriptor)


def _resolve_configured_root(path: Path, label: str) -> Path:
    expanded = Path(os.path.abspath(os.fspath(path.expanduser())))
    current = Path(expanded.anchor)
    for part in expanded.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            if current == expanded:
                raise InstallError("%s must not be a symlink" % label)
            if not hasattr(info, "st_uid") or info.st_uid != 0:
                raise InstallError("%s path contains an untrusted symlink" % label)
    return expanded.resolve()


def _reject_destination_overlaps(destinations: Dict[str, Path]) -> None:
    items = list(destinations.items())
    for index, (left_label, left) in enumerate(items):
        for right_label, right in items[index + 1 :]:
            if left == right or _is_relative_to(left, right) or _is_relative_to(right, left):
                raise InstallError(
                    "installation destinations overlap: %s and %s" % (left_label, right_label)
                )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_real_directory(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise InstallError("%s is missing" % label) from exc
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        raise InstallError("%s must be a real directory" % label)


def _file_matches(path: Path, record: Dict) -> bool:
    try:
        data = _read_regular_bytes(path, "installed file", MAX_BUNDLE_FILE_BYTES)
    except InstallError:
        return False
    return len(data) == record["size_bytes"] and hashlib.sha256(data).hexdigest() == record["sha256"]


def _skill_matches(path: Path, skill: Dict) -> bool:
    try:
        actual = build_skill_manifest(path)
    except InstallError:
        return False
    return actual["files"] == skill["files"] and actual["tree_sha256"] == skill["tree_sha256"]


def _release_matches(path: Path, manifest: Dict, manifest_sha256: str) -> bool:
    if not _file_matches(path / "conductor-runtime.pyz", manifest["runtime"]):
        return False
    if not _file_matches(path / "install.py", manifest["installer"]):
        return False
    if not _skill_matches(path / "codex-conductor", manifest["skill"]):
        return False
    try:
        installed_manifest = _read_regular_bytes(
            path / "release-manifest.json", "installed release manifest", MAX_MANIFEST_BYTES
        )
    except InstallError:
        return False
    return hashlib.sha256(installed_manifest).hexdigest() == manifest_sha256


def _copy_regular_file(source: Path, destination: Path, executable: bool = False) -> None:
    data = _read_regular_bytes(source, "bundle file", MAX_BUNDLE_FILE_BYTES)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    os.chmod(destination, 0o755 if executable else 0o644)


def _copy_skill(source: Path, destination: Path, skill: Dict, destination_is_root: bool = False) -> None:
    root = destination if destination_is_root else destination
    if destination_is_root:
        _require_real_directory(root, "staged Skill directory")
        if any(root.iterdir()):
            raise InstallError("staged Skill directory is not empty")
    else:
        root.mkdir(parents=True, exist_ok=False)
    for item in skill["files"]:
        relative = _safe_relative_path(item["path"], "Skill file path")
        target = root.joinpath(*relative.parts)
        _copy_regular_file(source.joinpath(*relative.parts), target)
    if not _skill_matches(root, skill):
        raise InstallError("staged Skill failed verification")


def _stage_directory(parent: Path, prefix: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))


def _stage_file(parent: Path, prefix: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=prefix, dir=str(parent))
    os.close(descriptor)
    return Path(name)


def _commit_replacements(replacements: List[Tuple[Path, Path]]) -> None:
    committed = []
    try:
        for staged, destination in replacements:
            destination.parent.mkdir(parents=True, exist_ok=True)
            backup = destination.parent / (".%s.backup-%s" % (destination.name, uuid.uuid4().hex))
            had_existing = _lexists(destination)
            if had_existing:
                os.replace(destination, backup)
            try:
                os.replace(staged, destination)
            except Exception:
                if had_existing and _lexists(backup):
                    os.replace(backup, destination)
                raise
            committed.append((destination, backup if had_existing else None))
    except Exception:
        for destination, backup in reversed(committed):
            if _lexists(destination):
                _remove_path(destination)
            if backup is not None and _lexists(backup):
                os.replace(backup, destination)
        raise
    for _, backup in committed:
        if backup is not None and _lexists(backup):
            _remove_path(backup)


def _lexists(path: Path) -> bool:
    return os.path.lexists(str(path))


def _safe_join(root: Path, relative: str, label: str) -> Path:
    parts = _safe_relative_path(relative, "%s path" % label).parts
    current = root
    for part in parts:
        current = current / part
        if _lexists(current) and current.is_symlink():
            raise InstallError("%s path contains a symlink" % label)
    return current


def _reject_symlink_components(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise InstallError("installation destination escapes its configured root") from exc
    current = root
    if _lexists(current) and current.is_symlink():
        raise InstallError("installation root must not be a symlink")
    for part in relative.parts:
        current = current / part
        if not _lexists(current):
            break
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise InstallError("installation destination contains a symlink: %s" % current)
        if not stat.S_ISDIR(mode):
            raise InstallError("installation destination parent is not a directory: %s" % current)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _default_codex_home() -> Path:
    value = os.environ.get("CODEX_HOME")
    return Path(value).expanduser() if value else Path.home() / ".codex"


def _default_conductor_home() -> Path:
    value = os.environ.get("CODEX_CONDUCTOR_HOME")
    return Path(value).expanduser() if value else Path.home() / ".codex" / "conductor"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install one extracted Codex Conductor release bundle.")
    parser.add_argument("--bundle-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--codex-home", type=Path, default=_default_codex_home())
    parser.add_argument("--conductor-home", type=Path, default=_default_conductor_home())
    parser.add_argument("--dry-run", action="store_true", help="Validate and print the install plan without writes.")
    parser.add_argument("--allow-writes", action="store_true")
    parser.add_argument("--replace", action="store_true", help="Replace an existing differing installation.")
    parser.add_argument("--approve", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = install_bundle(
            args.bundle_root,
            args.codex_home,
            args.conductor_home,
            allow_writes=args.allow_writes,
            approvals=args.approve,
            replace=args.replace,
            dry_run=args.dry_run,
            installer_path=Path(__file__),
        )
    except (InstallError, OSError) as exc:
        print("Install failed: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Codex Conductor %s: %s" % (result["version"], result["status"]))
        print("Runtime: %s" % result["destinations"]["runtime"])
        print("Skill: %s" % result["destinations"]["skill"])
        print("Release: %s" % result["destinations"]["release"])
        if result["status"] == "installed":
            print("Restart Codex to refresh installed skills.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
