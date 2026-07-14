import os
import tempfile
import unittest
from pathlib import Path

from conductor_runtime.errors import ValidationError
from conductor_extras.runtime.staged_workspace import (
    apply_workspace_delta_merge,
    copy_workspace_to_stage,
    plan_workspace_delta_merge,
    reconcile_workspace_delta_merge,
    snapshot_workspace,
    validate_workspace_merge_plan,
    workspace_snapshot_from_manifest,
    workspace_snapshot_manifest,
)


class WorkspaceMergeTests(unittest.TestCase):
    def test_merge_applies_added_binary_modified_mode_and_deleted_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "script.sh").write_text("echo old\n", encoding="utf-8")
            (source / "remove.txt").write_text("remove\n", encoding="utf-8")
            stage = root / "stage"
            base = copy_workspace_to_stage(source, stage)
            (stage / "script.sh").write_text("echo new\n", encoding="utf-8")
            os.chmod(stage / "script.sh", 0o755)
            (stage / "remove.txt").unlink()
            (stage / "binary.dat").write_bytes(b"\x00\xff\x10")
            incoming = snapshot_workspace(stage)
            plan = plan_workspace_delta_merge(base, snapshot_workspace(source), incoming)

            self.assertEqual(plan["status"], "applied")
            self.assertEqual(plan["changed_files"], ["binary.dat", "remove.txt", "script.sh"])
            merged = apply_workspace_delta_merge(source, stage, incoming, plan)
            self.assertEqual(merged.tracked_fingerprint_sha256, plan["source_after_sha256"])
            self.assertEqual((source / "binary.dat").read_bytes(), b"\x00\xff\x10")
            self.assertFalse((source / "remove.txt").exists())
            self.assertEqual((source / "script.sh").read_text(encoding="utf-8"), "echo new\n")
            self.assertTrue(os.access(source / "script.sh", os.X_OK))

    def test_merge_rejects_different_overlap_and_deduplicates_identical_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "shared.txt").write_text("base\n", encoding="utf-8")
            first_stage = root / "first"
            second_stage = root / "second"
            identical_stage = root / "identical"
            base = copy_workspace_to_stage(source, first_stage)
            copy_workspace_to_stage(source, second_stage)
            copy_workspace_to_stage(source, identical_stage)
            (first_stage / "shared.txt").write_text("first\n", encoding="utf-8")
            (second_stage / "shared.txt").write_text("second\n", encoding="utf-8")
            (identical_stage / "shared.txt").write_text("first\n", encoding="utf-8")

            first_snapshot = snapshot_workspace(first_stage)
            first_plan = plan_workspace_delta_merge(base, snapshot_workspace(source), first_snapshot)
            apply_workspace_delta_merge(source, first_stage, first_snapshot, first_plan)

            second_snapshot = snapshot_workspace(second_stage)
            conflict = plan_workspace_delta_merge(base, snapshot_workspace(source), second_snapshot)
            self.assertEqual(conflict["status"], "conflict")
            self.assertEqual(conflict["conflicting_files"], ["shared.txt"])
            with self.assertRaisesRegex(ValidationError, "conflicting.*cannot be applied"):
                apply_workspace_delta_merge(source, second_stage, second_snapshot, conflict)

            identical_snapshot = snapshot_workspace(identical_stage)
            deduplicated = plan_workspace_delta_merge(base, snapshot_workspace(source), identical_snapshot)
            self.assertEqual(deduplicated["status"], "deduplicated")
            self.assertEqual(deduplicated["deduplicated_files"], ["shared.txt"])
            apply_workspace_delta_merge(source, identical_stage, identical_snapshot, deduplicated)
            self.assertEqual((source / "shared.txt").read_text(encoding="utf-8"), "first\n")

    def test_merge_handles_directory_to_file_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            (source / "old").mkdir(parents=True)
            (source / "old" / "nested.txt").write_text("nested\n", encoding="utf-8")
            stage = root / "stage"
            base = copy_workspace_to_stage(source, stage)
            (stage / "old" / "nested.txt").unlink()
            (stage / "old").rmdir()
            (stage / "old").write_text("replacement\n", encoding="utf-8")
            incoming = snapshot_workspace(stage)
            plan = plan_workspace_delta_merge(base, snapshot_workspace(source), incoming)
            apply_workspace_delta_merge(source, stage, incoming, plan)
            self.assertTrue((source / "old").is_file())
            self.assertEqual((source / "old").read_text(encoding="utf-8"), "replacement\n")

    def test_snapshot_manifest_and_partial_merge_recovery_are_hash_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "first.txt").write_text("first-base\n", encoding="utf-8")
            (source / "remove.txt").write_text("remove\n", encoding="utf-8")
            stage = root / "stage"
            base = copy_workspace_to_stage(source, stage)
            source_before = snapshot_workspace(source)
            manifest = workspace_snapshot_manifest(source_before)
            restored = workspace_snapshot_from_manifest(manifest)
            self.assertEqual(restored.entries, source_before.entries)
            (stage / "first.txt").write_text("first-new\n", encoding="utf-8")
            (stage / "second.txt").write_text("second-new\n", encoding="utf-8")
            (stage / "remove.txt").unlink()
            incoming = snapshot_workspace(stage)
            plan = plan_workspace_delta_merge(base, source_before, incoming)
            validate_workspace_merge_plan(plan, base_snapshot=base, incoming_snapshot=incoming)

            (source / "first.txt").write_text("first-new\n", encoding="utf-8")
            (source / "remove.txt").unlink()
            recovered = reconcile_workspace_delta_merge(
                source,
                stage,
                incoming,
                plan,
                restored,
            )
            self.assertEqual(recovered.tracked_fingerprint_sha256, plan["source_after_sha256"])
            self.assertEqual((source / "second.txt").read_text(encoding="utf-8"), "second-new\n")

            tampered = dict(manifest)
            tampered["total_bytes"] += 1
            with self.assertRaisesRegex(ValidationError, "byte count"):
                workspace_snapshot_from_manifest(tampered)
            bad_plan = dict(plan)
            bad_plan["source_after_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValidationError, "plan hash"):
                validate_workspace_merge_plan(bad_plan)


if __name__ == "__main__":
    unittest.main()
