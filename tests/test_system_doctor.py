import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from conductor_runtime.cli import main as cli_main
from conductor_runtime.errors import PolicyError, ValidationError
from conductor_runtime.security import assess_command
from conductor_runtime.system_doctor import (
    CODEX_HOST_REPAIR_APPROVAL,
    inspect_codex_installation,
    repair_codex_host_link,
)


class SystemDoctorTests(unittest.TestCase):
    def _installation(self, root: Path):
        package_bin = root / "package" / "current" / "bin"
        launcher_bin = root / "home" / ".local" / "bin"
        package_bin.mkdir(parents=True)
        launcher_bin.mkdir(parents=True)
        codex = package_bin / "codex"
        host = package_bin / "codex-code-mode-host"
        for path in (codex, host):
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
        launcher = launcher_bin / "codex"
        launcher.symlink_to(codex)
        return launcher, host, launcher_bin / "codex-code-mode-host"

    def test_inspect_and_repair_missing_codex_host_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher, source, destination = self._installation(root)
            before = inspect_codex_installation(str(launcher))
            self.assertEqual(before["status"], "codex-host-missing")
            self.assertFalse(before["healthy"])

            after = repair_codex_host_link(
                approvals={CODEX_HOST_REPAIR_APPROVAL},
                codex_path=str(launcher),
                home=root / "home",
            )
            self.assertTrue(after["healthy"])
            self.assertTrue(after["repaired"])
            self.assertTrue(destination.is_symlink())
            self.assertEqual(Path(os.readlink(destination)), source)

    def test_repair_requires_explicit_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher, _source, destination = self._installation(root)
            with self.assertRaisesRegex(PolicyError, CODEX_HOST_REPAIR_APPROVAL):
                repair_codex_host_link(
                    approvals=set(),
                    codex_path=str(launcher),
                    home=root / "home",
                )
            self.assertFalse(destination.exists())
            self.assertFalse(destination.is_symlink())

    def test_repair_refuses_broken_or_mismatched_existing_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher, _source, destination = self._installation(root)
            destination.symlink_to(root / "missing-host")
            report = inspect_codex_installation(str(launcher))
            self.assertEqual(report["status"], "codex-host-link-broken")
            with self.assertRaisesRegex(ValidationError, "only creates a missing"):
                repair_codex_host_link(
                    approvals={CODEX_HOST_REPAIR_APPROVAL},
                    codex_path=str(launcher),
                    home=root / "home",
                )

            destination.unlink()
            other = root / "other-host"
            other.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            other.chmod(0o755)
            destination.symlink_to(other)
            report = inspect_codex_installation(str(launcher))
            self.assertEqual(report["status"], "codex-host-version-mismatch")

    def test_repair_destination_must_be_inside_selected_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher, _source, destination = self._installation(root)
            with self.assertRaisesRegex(PolicyError, "inside the current user home"):
                repair_codex_host_link(
                    approvals={CODEX_HOST_REPAIR_APPROVAL},
                    codex_path=str(launcher),
                    home=root / "different-home",
                )
            self.assertFalse(destination.exists())

    def test_doctor_json_reports_missing_host_and_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher, _source, _destination = self._installation(Path(tmp))
            stdout = StringIO()
            with patch(
                "conductor_runtime.system_doctor.shutil.which",
                return_value=str(launcher),
            ), redirect_stdout(stdout):
                code = cli_main(["doctor", "--json"])
            payload = json.loads(stdout.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(payload["codex"]["status"], "codex-host-missing")

    def test_doctor_repair_is_not_classified_read_only(self):
        read = assess_command(["python3", "-B", "-m", "conductor_runtime", "doctor"])
        repair = assess_command(
            [
                "python3",
                "-B",
                "-m",
                "conductor_runtime",
                "doctor",
                "--repair-codex-host",
                "--approve",
                CODEX_HOST_REPAIR_APPROVAL,
            ]
        )
        self.assertFalse(read.writes)
        self.assertFalse(read.network)
        self.assertTrue(repair.writes)
        self.assertTrue(repair.network)
        self.assertTrue(repair.destructive)


if __name__ == "__main__":
    unittest.main()
