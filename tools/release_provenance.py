"""Deterministic source and release-manifest provenance helpers."""

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

from tools.write_checksums import ARTIFACTS, ChecksumError, verify_checksums


SHA256 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_EXCLUSIONS = {"RELEASE_REPORT.md", "VERIFICATION_EVIDENCE.json"}
REPORT_BEGIN = "<!-- BEGIN GENERATED VERIFICATION EVIDENCE -->"
REPORT_END = "<!-- END GENERATED VERIFICATION EVIDENCE -->"
MAX_RELEASE_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_CORE_ARCHIVE_BYTES = 500 * 1024
MAX_RELEASE_MANIFEST_BYTES = 1024 * 1024


class ProvenanceError(RuntimeError):
    pass


def _identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_regular_bytes(path: Path, label: str, *, root: Path | None = None, max_bytes: int | None = None):
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProvenanceError("cannot inspect %s" % label) from exc
    if root is not None:
        try:
            parent.relative_to(root)
        except ValueError as exc:
            raise ProvenanceError("%s escapes the repository" % label) from exc
        if parent != candidate.parent:
            raise ProvenanceError("%s must not contain symlinked directories" % label)
    candidate = parent / candidate.name
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ProvenanceError("cannot open %s" % label) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ProvenanceError("%s must be a single regular file" % label)
        if max_bytes is not None and before.st_size > max_bytes:
            raise ProvenanceError("%s exceeds %d bytes" % (label, max_bytes))
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        try:
            current = candidate.lstat()
        except OSError as exc:
            raise ProvenanceError("%s changed while it was read" % label) from exc
        if _identity(before) != _identity(after) or _identity(before) != _identity(current):
            raise ProvenanceError("%s changed while it was read" % label)
        if len(payload) != before.st_size:
            raise ProvenanceError("%s changed while it was read" % label)
        return payload, before
    finally:
        os.close(descriptor)


def read_regular_provenance_bytes(path: Path, label: str, *, max_bytes: int | None = None) -> bytes:
    payload, _ = _read_regular_bytes(path, label, max_bytes=max_bytes)
    return payload


def _source_paths(project_root: Path):
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise ProvenanceError("cannot enumerate repository source files")
    try:
        names = [item.decode("utf-8") for item in completed.stdout.split(b"\0") if item]
    except UnicodeDecodeError as exc:
        raise ProvenanceError("repository contains a non-UTF-8 source path") from exc
    selected = []
    for name in names:
        relative = PurePosixPath(name)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ProvenanceError("repository source path is unsafe")
        if name in SOURCE_EXCLUSIONS or relative.parts[0] == "dist":
            continue
        selected.append(name)
    return sorted(selected)


def repository_source_sha256(project_root: Path) -> str:
    root = Path(project_root).resolve()
    digest = hashlib.sha256()
    for name in _source_paths(root):
        path = root.joinpath(*PurePosixPath(name).parts)
        try:
            payload, info = _read_regular_bytes(
                path,
                "repository source %s" % name,
                root=root,
            )
        except ProvenanceError as exc:
            raise ProvenanceError("cannot safely read repository source %s" % name) from exc
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(b"x" if info.st_mode & 0o111 else b"-")
        digest.update(hashlib.sha256(payload).digest())
        digest.update(b"\n")
    return digest.hexdigest()


def release_checksum_records(project_root: Path):
    dist = Path(project_root) / "dist"
    try:
        payload = verify_checksums(dist)
        text = payload.decode("ascii")
    except (ChecksumError, UnicodeDecodeError) as exc:
        raise ProvenanceError("release checksum manifest is unreadable") from exc
    if not text.endswith("\n"):
        raise ProvenanceError("release checksum manifest must end with a newline")
    records = []
    for line in text.splitlines():
        parts = line.split("  ")
        if len(parts) != 2 or SHA256.fullmatch(parts[0]) is None:
            raise ProvenanceError("release checksum manifest is malformed")
        records.append({"name": parts[1], "sha256": parts[0]})
    if [item["name"] for item in records] != list(ARTIFACTS):
        raise ProvenanceError("release checksum manifest artifact order is invalid")
    return records, hashlib.sha256(payload).hexdigest()


def _reject_duplicate_object_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _release_bundle_members(payload: bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = [item.filename for item in archive.infolist()]
            if len(names) != len(set(names)):
                raise ProvenanceError("release bundle contains duplicate members")
            members = {}
            for name, limit in (
                ("conductor-runtime.pyz", MAX_CORE_ARCHIVE_BYTES),
                ("release-manifest.json", MAX_RELEASE_MANIFEST_BYTES),
            ):
                matches = [item for item in archive.infolist() if item.filename == name]
                if len(matches) != 1:
                    raise ProvenanceError("release bundle member is missing: %s" % name)
                info = matches[0]
                mode = info.external_attr >> 16
                if info.is_dir() or stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                    raise ProvenanceError("release bundle member is not a regular file: %s" % name)
                if info.file_size > limit:
                    raise ProvenanceError("release bundle member is too large: %s" % name)
                with archive.open(info) as source:
                    data = source.read(limit + 1)
                if len(data) != info.file_size or len(data) > limit:
                    raise ProvenanceError("release bundle member changed while reading: %s" % name)
                members[name] = data
            return members
    except ProvenanceError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ProvenanceError("release bundle is not a valid ZIP archive") from exc


def release_artifact_binding(project_root: Path):
    """Bind checksummed public artifacts to the core runtime inside the bundle."""

    root = Path(project_root).resolve()
    records, checksum_digest = release_checksum_records(root)
    expected_bundle_sha256 = next(
        item["sha256"] for item in records if item["name"] == "codex-conductor-bundle.zip"
    )
    bundle_payload, _ = _read_regular_bytes(
        root / "dist" / "codex-conductor-bundle.zip",
        "release bundle",
        root=root,
        max_bytes=MAX_RELEASE_ARCHIVE_BYTES,
    )
    if hashlib.sha256(bundle_payload).hexdigest() != expected_bundle_sha256:
        raise ProvenanceError("release bundle does not match the checksum manifest")
    members = _release_bundle_members(bundle_payload)
    runtime_payload = members["conductor-runtime.pyz"]
    runtime_record = {
        "sha256": hashlib.sha256(runtime_payload).hexdigest(),
        "size_bytes": len(runtime_payload),
    }
    try:
        manifest = json.loads(
            members["release-manifest.json"].decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object_keys,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProvenanceError("release manifest is not valid UTF-8 JSON") from exc
    expected_runtime = {
        "path": "conductor-runtime.pyz",
        **runtime_record,
    }
    if not isinstance(manifest, dict) or manifest.get("runtime") != expected_runtime:
        raise ProvenanceError("release manifest does not bind the bundled runtime")
    if release_checksum_records(root) != (records, checksum_digest):
        raise ProvenanceError("release artifacts changed while provenance was assembled")
    return records, checksum_digest, runtime_record


def release_report_template_sha256(project_root: Path) -> str:
    return report_template_sha256(Path(project_root) / "RELEASE_REPORT.md")


def report_template_sha256(path: Path) -> str:
    try:
        payload, _ = _read_regular_bytes(path, "release report", max_bytes=4 * 1024 * 1024)
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProvenanceError("release report is not readable UTF-8") from exc
    if text.count(REPORT_BEGIN) != 1 or text.count(REPORT_END) != 1:
        raise ProvenanceError("release report generated marker pair is invalid")
    start = text.index(REPORT_BEGIN)
    end = text.index(REPORT_END)
    if end < start:
        raise ProvenanceError("release report generated marker order is invalid")
    finish = end + len(REPORT_END)
    template = text[:start] + REPORT_BEGIN + "\n" + REPORT_END + text[finish:]
    return hashlib.sha256(template.encode("utf-8")).hexdigest()
