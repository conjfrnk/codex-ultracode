import hashlib
import tempfile
import unittest
from pathlib import Path

from tools.write_checksums import (
    ARTIFACTS,
    ChecksumError,
    regular_files_equal,
    verify_checksums,
    write_checksums,
)


class ReleaseChecksumTests(unittest.TestCase):
    def test_checksums_cover_every_artifact_in_canonical_order_and_detect_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, name in enumerate(ARTIFACTS):
                (root / name).write_bytes(("artifact-%d" % index).encode("ascii"))

            checksum_file = write_checksums(root)
            lines = checksum_file.read_text(encoding="ascii").splitlines()
            self.assertEqual([line.split("  ", 1)[1] for line in lines], list(ARTIFACTS))
            self.assertEqual(
                lines[0].split("  ", 1)[0],
                hashlib.sha256((root / ARTIFACTS[0]).read_bytes()).hexdigest(),
            )
            self.assertEqual(verify_checksums(root), checksum_file.read_bytes())

            (root / ARTIFACTS[-1]).write_bytes(b"tampered")
            with self.assertRaisesRegex(ChecksumError, "stale"):
                verify_checksums(root)

    def test_missing_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ChecksumError, "missing release artifacts"):
                write_checksums(Path(tmp))

    def test_unexpected_top_level_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ARTIFACTS:
                (root / name).write_bytes(b"artifact")
            (root / "skill.zip").write_bytes(b"retired")

            with self.assertRaisesRegex(ChecksumError, "unexpected release entries: skill.zip"):
                write_checksums(root)

    def test_symlinked_artifact_and_checksum_manifest_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, name in enumerate(ARTIFACTS):
                (root / name).write_bytes(("artifact-%d" % index).encode("ascii"))

            artifact = root / ARTIFACTS[0]
            target = root / "outside-artifact"
            artifact.replace(target)
            artifact.symlink_to(target)
            with self.assertRaisesRegex(ChecksumError, "cannot open release artifact"):
                write_checksums(root)

            artifact.unlink()
            artifact.write_bytes(target.read_bytes())
            target.unlink()
            checksum_file = write_checksums(root)
            manifest_target = root / "manifest-target"
            checksum_file.replace(manifest_target)
            checksum_file.symlink_to(manifest_target)
            with self.assertRaisesRegex(ChecksumError, "cannot read SHA256SUMS"):
                verify_checksums(root)

    def test_regular_file_comparison_is_literal_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            left = root / "left"
            right = root / "right"
            left.write_bytes(b"same bytes")
            right.write_bytes(b"same bytes")
            self.assertTrue(regular_files_equal(left, right))

            right.write_bytes(b"other byte")
            self.assertFalse(regular_files_equal(left, right))

            right.unlink()
            right.symlink_to(left)
            with self.assertRaisesRegex(ChecksumError, "cannot open release artifact"):
                regular_files_equal(left, right)


if __name__ == "__main__":
    unittest.main()
