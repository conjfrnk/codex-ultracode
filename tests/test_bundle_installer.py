import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from conductor_extras.runtime.clock import utc_from_timestamp, utc_now
from tools.install_bundle import (
    InstallError,
    _commit_replacements,
    _read_regular_bytes,
    build_skill_manifest,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import pathlib
import sys

args = sys.argv[1:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("README quickstart completed.\n", encoding="utf-8")
print(json.dumps({"type": "thread.started", "thread_id": "readme-quickstart"}))
print(json.dumps({
    "type": "turn.completed",
    "usage": {
        "input_tokens": 1,
        "cached_input_tokens": 0,
        "output_tokens": 1,
        "reasoning_output_tokens": 0,
    },
}))
'''


def markdown_shell_command(markdown: str, introduction: str) -> list[str]:
    start = markdown.index(introduction)
    fence = markdown.index("```sh\n", start) + len("```sh\n")
    finish = markdown.index("\n```", fence)
    lines = markdown[fence:finish].splitlines()
    command = " ".join(line[:-1].strip() if line.rstrip().endswith("\\") else line.strip() for line in lines)
    return shlex.split(command)


def package_and_extract(root: Path) -> Path:
    dist = root / "dist"
    packaged = subprocess.run(
        [sys.executable, "-B", str(PROJECT_ROOT / "tools" / "package_runtime.py"), str(dist)],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    if packaged.returncode != 0:
        raise AssertionError(packaged.stderr or packaged.stdout)
    extracted = root / "extracted"
    with zipfile.ZipFile(dist / "codex-conductor-bundle.zip") as archive:
        names = set(archive.namelist())
        for expected in (
            "README.md",
            "docs/README.md",
            "docs/runtime.md",
            "docs/cli-namespaces.md",
            "conductor-workflows/README.md",
            "conductor-workflows/core/read-only-review.json",
            "conductor-workflows/core/staged-change.json",
        ):
            if expected not in names:
                raise AssertionError("release bundle omits README target %s" % expected)
        archive.extractall(extracted)
    return extracted


def run_installer(extracted: Path, codex_home: Path, conductor_home: Path, *extra: str):
    return subprocess.run(
        [
            sys.executable,
            "-B",
            str(extracted / "install.py"),
            "--bundle-root",
            str(extracted),
            "--codex-home",
            str(codex_home),
            "--conductor-home",
            str(conductor_home),
            *extra,
        ],
        cwd=str(extracted),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )


class ClockTests(unittest.TestCase):
    def test_utc_helpers_preserve_naive_utc_without_deprecation_warnings(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            now = utc_now()
            epoch = utc_from_timestamp(0)
        self.assertIsNone(now.tzinfo)
        self.assertEqual(epoch, datetime(1970, 1, 1))
        self.assertFalse([item for item in caught if issubclass(item.category, DeprecationWarning)])


class BundleInstallerTests(unittest.TestCase):
    def test_bundle_installer_preflights_installs_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extracted = package_and_extract(root)
            codex_home = root / "codex-home"
            conductor_home = root / "conductor-home"

            dry_run = run_installer(extracted, codex_home, conductor_home, "--dry-run", "--json")
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            plan = json.loads(dry_run.stdout)
            self.assertEqual(plan["status"], "dry-run")
            self.assertEqual(plan["changes"], ["release", "runtime", "skill"])
            self.assertFalse(codex_home.exists())
            self.assertFalse(conductor_home.exists())

            denied = run_installer(extracted, codex_home, conductor_home, "--json")
            self.assertEqual(denied.returncode, 2)
            self.assertIn("conductor-install", denied.stderr)
            self.assertFalse(codex_home.exists())
            self.assertFalse(conductor_home.exists())

            installed = run_installer(
                extracted,
                codex_home,
                conductor_home,
                "--allow-writes",
                "--approve",
                "conductor-install",
                "--json",
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            result = json.loads(installed.stdout)
            self.assertEqual(result["status"], "installed")
            active_runtime = conductor_home / "bin" / "conductor-runtime.pyz"
            active_skill = codex_home / "skills" / "codex-conductor"
            self.assertEqual(active_runtime.read_bytes(), (extracted / "conductor-runtime.pyz").read_bytes())
            self.assertEqual(
                sorted(path.relative_to(active_skill) for path in active_skill.rglob("*") if path.is_file()),
                sorted(
                    path.relative_to(extracted / "codex-conductor")
                    for path in (extracted / "codex-conductor").rglob("*")
                    if path.is_file()
                ),
            )
            receipt = json.loads((conductor_home / "installations" / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["schema"], "conductor.bundle_install_receipt.v1")
            self.assertEqual(receipt["status"], "installed")

            invoked = subprocess.run(
                [str(active_runtime), "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            self.assertEqual(invoked.returncode, 0, invoked.stderr)
            self.assertRegex(invoked.stdout.strip(), r"^conductor-runtime \d+\.\d+\.\d+$")

            examples = subprocess.run(
                [
                    str(active_runtime),
                    "validate",
                    str((extracted / "conductor-workflows" / "core").resolve()),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            self.assertEqual(examples.returncode, 0, examples.stderr)
            self.assertIn("read-only-review.json", examples.stdout)
            self.assertIn("staged-change.json", examples.stdout)

            repeated = run_installer(extracted, codex_home, conductor_home, "--json")
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(json.loads(repeated.stdout)["status"], "already-installed")

            displayed = run_installer(extracted, codex_home, conductor_home)
            self.assertEqual(displayed.returncode, 0, displayed.stderr)
            self.assertIn("Next: ", displayed.stdout)
            command = next(line.removeprefix("Next: ") for line in displayed.stdout.splitlines() if line.startswith("Next: "))
            invoked_next = subprocess.run(
                shlex.split(command),
                cwd=str(extracted),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            self.assertIn(invoked_next.returncode, {0, 1}, invoked_next.stderr)
            self.assertIn("status:", invoked_next.stdout)

            readme_command = markdown_shell_command(
                (extracted / "README.md").read_text(encoding="utf-8"),
                "Run one read-only model call:",
            )
            self.assertEqual(
                readme_command[0],
                "~/.codex/conductor/bin/conductor-runtime.pyz",
            )
            readme_command[0] = str(active_runtime)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            fake_codex = fake_bin / "codex"
            fake_codex.write_text(FAKE_CODEX, encoding="utf-8")
            fake_codex.chmod(0o755)
            readme_environment = dict(os.environ)
            readme_environment.update(
                {
                    "CODEX_CONDUCTOR_HOME": str(root / "readme-state"),
                    "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                }
            )
            readme_invoked = subprocess.run(
                readme_command,
                cwd=str(extracted),
                env=readme_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            self.assertEqual(readme_invoked.returncode, 0, readme_invoked.stderr)
            self.assertIn("completed", readme_invoked.stdout)

    def test_bundle_installer_rejects_tampering_and_gates_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extracted = package_and_extract(root)
            codex_home = root / "codex-home"
            conductor_home = root / "conductor-home"
            installed = run_installer(
                extracted,
                codex_home,
                conductor_home,
                "--allow-writes",
                "--approve",
                "conductor-install",
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)

            active_runtime = conductor_home / "bin" / "conductor-runtime.pyz"
            active_runtime.write_bytes(b"corrupt\n")
            denied = run_installer(
                extracted,
                codex_home,
                conductor_home,
                "--allow-writes",
                "--approve",
                "conductor-install",
            )
            self.assertEqual(denied.returncode, 2)
            self.assertIn("--replace", denied.stderr)
            self.assertEqual(active_runtime.read_bytes(), b"corrupt\n")

            missing_update_approval = run_installer(
                extracted,
                codex_home,
                conductor_home,
                "--replace",
                "--allow-writes",
                "--approve",
                "conductor-install",
            )
            self.assertEqual(missing_update_approval.returncode, 2)
            self.assertIn("conductor-update", missing_update_approval.stderr)
            self.assertEqual(active_runtime.read_bytes(), b"corrupt\n")

            replaced = run_installer(
                extracted,
                codex_home,
                conductor_home,
                "--replace",
                "--allow-writes",
                "--approve",
                "conductor-install",
                "--approve",
                "conductor-update",
                "--json",
            )
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            self.assertEqual(active_runtime.read_bytes(), (extracted / "conductor-runtime.pyz").read_bytes())

            skill_file = extracted / "codex-conductor" / "SKILL.md"
            skill_file.write_text(skill_file.read_text(encoding="utf-8") + "\ntampered\n", encoding="utf-8")
            tampered = run_installer(extracted, root / "other-codex", root / "other-conductor", "--dry-run")
            self.assertEqual(tampered.returncode, 2)
            self.assertIn("does not match the release manifest", tampered.stderr)

    def test_bundle_validation_rejects_duplicate_json_and_non_text_skill_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extracted = package_and_extract(root)
            manifest_path = extracted / "release-manifest.json"
            manifest = manifest_path.read_text(encoding="utf-8")
            manifest_path.write_text(manifest.replace('{\n  "installer"', '{\n  "schema": "duplicate",\n  "installer"', 1), encoding="utf-8")
            duplicate = run_installer(extracted, root / "codex", root / "conductor", "--dry-run")
            self.assertEqual(duplicate.returncode, 2)
            self.assertIn("duplicate JSON key", duplicate.stderr)

            skill = root / "unsafe-skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text("safe\n", encoding="utf-8")
            (skill / "run.py").write_text("print('unsafe')\n", encoding="utf-8")
            with self.assertRaisesRegex(InstallError, "non-text or sensitive"):
                build_skill_manifest(skill)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink checks require symlink support")
    def test_bundle_installer_rejects_symlinked_destination_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extracted = package_and_extract(root)
            codex_home = root / "codex-home"
            conductor_home = root / "conductor-home"
            target = root / "redirected-skills"
            target.mkdir()
            codex_home.mkdir()
            (codex_home / "skills").symlink_to(target, target_is_directory=True)
            result = run_installer(extracted, codex_home, conductor_home, "--dry-run")
            self.assertEqual(result.returncode, 2)
            self.assertIn("contains a symlink", result.stderr)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink checks require symlink support")
    def test_bundle_installer_rejects_symlinked_roots_and_bundle_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extracted = package_and_extract(root)
            real_codex_home = root / "real-codex-home"
            real_codex_home.mkdir()
            codex_home = root / "codex-home"
            codex_home.symlink_to(real_codex_home, target_is_directory=True)

            result = run_installer(extracted, codex_home, root / "conductor-home", "--dry-run")
            self.assertEqual(result.returncode, 2)
            self.assertIn("Codex home must not be a symlink", result.stderr)

            regular = root / "regular"
            regular.write_bytes(b"content")
            linked = root / "linked"
            linked.symlink_to(regular)
            with self.assertRaisesRegex(InstallError, "non-symlink"):
                _read_regular_bytes(linked, "linked file", 100)

            redirected = root / "redirected-parent"
            redirected.mkdir()
            parent_link = root / "parent-link"
            parent_link.symlink_to(redirected, target_is_directory=True)
            nested = run_installer(
                extracted,
                parent_link / "nested-codex-home",
                root / "nested-conductor-home",
                "--dry-run",
            )
            self.assertEqual(nested.returncode, 2)
            self.assertIn("untrusted symlink", nested.stderr)

    def test_bundle_installer_rejects_overlapping_destinations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extracted = package_and_extract(root)
            codex_home = root / "codex-home"
            conductor_home = codex_home / "skills" / "codex-conductor"

            result = run_installer(extracted, codex_home, conductor_home, "--dry-run")
            self.assertEqual(result.returncode, 2)
            self.assertIn("destinations overlap", result.stderr)

    def test_atomic_replacement_restores_prior_targets_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            staged_first = root / "staged-first"
            staged_second = root / "staged-second"
            first.write_text("old-first", encoding="utf-8")
            second.write_text("old-second", encoding="utf-8")
            staged_first.write_text("new-first", encoding="utf-8")
            staged_second.write_text("new-second", encoding="utf-8")
            real_replace = os.replace

            def fail_second(source, destination):
                if Path(source) == staged_second:
                    raise OSError("injected commit failure")
                return real_replace(source, destination)

            with patch("tools.install_bundle.os.replace", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "injected commit failure"):
                    _commit_replacements([(staged_first, first), (staged_second, second)])
            self.assertEqual(first.read_text(encoding="utf-8"), "old-first")
            self.assertEqual(second.read_text(encoding="utf-8"), "old-second")
            self.assertFalse(list(root.glob(".*.backup-*")))


if __name__ == "__main__":
    unittest.main()
