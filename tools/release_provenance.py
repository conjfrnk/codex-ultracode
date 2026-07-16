"""Deterministic source and release-manifest provenance helpers."""

import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath

from tools.write_checksums import ARTIFACTS, ChecksumError, verify_checksums


SHA256 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_EXCLUSIONS = {"RELEASE_REPORT.md", "VERIFICATION_EVIDENCE.json"}
REPORT_BEGIN = "<!-- BEGIN GENERATED VERIFICATION EVIDENCE -->"
REPORT_END = "<!-- END GENERATED VERIFICATION EVIDENCE -->"


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
