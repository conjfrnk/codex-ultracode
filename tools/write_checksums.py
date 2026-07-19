#!/usr/bin/env python3
"""Write or verify external SHA-256 checksums for release artifacts."""

import argparse
import hashlib
import os
import stat
import tempfile
from pathlib import Path


ARTIFACTS = (
    "codex-conductor-bundle.zip",
    "codex-conductor-marketplace.zip",
    "conductor-extras.pyz",
)
CHECKSUM_FILE = "SHA256SUMS"


class ChecksumError(RuntimeError):
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


def _open_regular_file(path: Path):
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ChecksumError("cannot open release artifact %s" % path.name) from exc
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        os.close(descriptor)
        raise ChecksumError("release artifact must be a single regular file: %s" % path.name)
    return descriptor, before


def _require_stable_path(path: Path, before, after) -> None:
    try:
        current = path.lstat()
    except OSError as exc:
        raise ChecksumError("release artifact changed while reading: %s" % path.name) from exc
    if _identity(before) != _identity(after) or _identity(before) != _identity(current):
        raise ChecksumError("release artifact changed while reading: %s" % path.name)


def _sha256_regular_file(path: Path):
    descriptor, before = _open_regular_file(path)
    try:
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        _require_stable_path(path, before, after)
        return digest.hexdigest(), before
    finally:
        os.close(descriptor)


def sha256_regular_file(path: Path) -> str:
    digest, _ = _sha256_regular_file(path)
    return digest


def regular_file_record(path: Path):
    digest, info = _sha256_regular_file(path)
    return {"sha256": digest, "size_bytes": info.st_size}


def regular_files_equal(left: Path, right: Path) -> bool:
    left_descriptor, left_before = _open_regular_file(left)
    try:
        right_descriptor, right_before = _open_regular_file(right)
        try:
            if left_before.st_size != right_before.st_size:
                return False
            while True:
                left_chunk = os.read(left_descriptor, 1024 * 1024)
                right_chunk = os.read(right_descriptor, 1024 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    break
            left_after = os.fstat(left_descriptor)
            right_after = os.fstat(right_descriptor)
            _require_stable_path(left, left_before, left_after)
            _require_stable_path(right, right_before, right_after)
            return True
        finally:
            os.close(right_descriptor)
    finally:
        os.close(left_descriptor)


def read_regular_file(path: Path, *, max_bytes: int) -> bytes:
    descriptor, before = _open_regular_file(path)
    try:
        if before.st_size > max_bytes:
            raise ChecksumError("release artifact is too large: %s" % path.name)
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ChecksumError("release artifact is too large: %s" % path.name)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        _require_stable_path(path, before, after)
        if len(payload) != before.st_size:
            raise ChecksumError("release artifact changed while reading: %s" % path.name)
        return payload
    finally:
        os.close(descriptor)


def checksum_payload(directory: Path) -> bytes:
    root = Path(directory)
    missing = [name for name in ARTIFACTS if not (root / name).exists()]
    if missing:
        raise ChecksumError("missing release artifacts: %s" % ", ".join(missing))
    records = [(name, *_sha256_regular_file(root / name)) for name in ARTIFACTS]
    allowed = {*ARTIFACTS, CHECKSUM_FILE}
    unexpected = sorted(path.name for path in root.iterdir() if path.name not in allowed)
    if unexpected:
        raise ChecksumError("unexpected release entries: %s" % ", ".join(unexpected))
    for name, _, identity in records:
        try:
            current = (root / name).lstat()
        except OSError as exc:
            raise ChecksumError("release artifact changed while hashing: %s" % name) from exc
        if _identity(identity) != _identity(current):
            raise ChecksumError("release artifact changed while hashing: %s" % name)
    lines = ["%s  %s\n" % (digest, name) for name, digest, _ in records]
    return "".join(lines).encode("ascii")


def write_checksums(directory: Path) -> Path:
    root = Path(directory)
    payload = checksum_payload(root)
    destination = root / CHECKSUM_FILE
    descriptor, temporary = tempfile.mkstemp(prefix=".SHA256SUMS.", dir=root)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o644)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ChecksumError("could not write %s" % CHECKSUM_FILE)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary_path, destination)
        return destination
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def verify_checksums(directory: Path) -> bytes:
    root = Path(directory)
    destination = root / CHECKSUM_FILE
    try:
        observed = read_regular_file(destination, max_bytes=1024 * 1024)
    except ChecksumError as exc:
        raise ChecksumError("cannot read %s" % CHECKSUM_FILE) from exc
    expected = checksum_payload(root)
    if observed != expected:
        raise ChecksumError("%s is missing, malformed, or stale" % CHECKSUM_FILE)
    return observed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=Path(__file__).resolve().parent.parent / "dist")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.check:
            verify_checksums(args.directory)
            print("OK: %s" % (args.directory / CHECKSUM_FILE))
        else:
            output = write_checksums(args.directory)
            print("OK: wrote %s" % output)
    except (ChecksumError, OSError) as exc:
        print("Checksum failed: %s" % exc, file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
