import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from conductor_runtime.errors import ValidationError
from tools.release_provenance import ProvenanceError, report_template_sha256, repository_source_sha256
from tools.render_release_report import BEGIN, END, ReportError, generated_section, load_evidence, render_report
from tools.verify import (
    ARTIFACT_REFRESH_COMMANDS,
    COVERAGE_MINIMUM_PERCENT,
    DIST_ARTIFACTS,
    VerificationError,
    _evidence_payload,
    _expected_checks,
    _require_unchanged_source,
    _run_coverage_tests,
    _validate_written_evidence,
    _verify_artifacts,
    _write_evidence,
    main as verify_main,
)


class VerificationToolTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "timezone packaging coverage requires POSIX TZ")
    def test_release_archives_are_timezone_independent(self):
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builds = []
            for index, timezone in enumerate(("UTC0", "PST8")):
                dist = root / ("dist-%d" % index)
                environment = dict(os.environ)
                environment["TZ"] = timezone
                commands = (
                    [sys.executable, "-B", "tools/package_runtime.py", str(dist)],
                    [sys.executable, "-B", "tools/package_extras.py", str(dist)],
                    [
                        sys.executable,
                        "-B",
                        "tools/package_skill.py",
                        "codex-conductor",
                        str(dist),
                    ],
                    [sys.executable, "-B", "tools/write_checksums.py", str(dist)],
                )
                for command in commands:
                    completed = subprocess.run(
                        command,
                        cwd=project_root,
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                builds.append(dist)

            for name in DIST_ARTIFACTS:
                self.assertEqual(
                    (builds[0] / name).read_bytes(),
                    (builds[1] / name).read_bytes(),
                    name,
                )
            for name in (
                "conductor-runtime.pyz",
                "conductor-extras.pyz",
                "codex-conductor-marketplace.zip",
            ):
                with zipfile.ZipFile(builds[0] / name) as archive:
                    self.assertEqual(
                        {item.date_time for item in archive.infolist()},
                        {(1980, 1, 2, 0, 0, 0)},
                    )

    def test_release_report_tool_starts_as_a_direct_script(self):
        project_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-B", str(project_root / "tools" / "render_release_report.py"), "--help"],
            cwd=project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Render the generated verification section", result.stdout)

    def test_quick_evidence_is_machine_readable_and_no_clobber(self):
        evidence = _evidence_payload(SimpleNamespace(quick=True, skip_artifacts=True))
        self.assertEqual(evidence["schema"], "conductor.verification_evidence.v1")
        self.assertEqual(evidence["mode"], "quick")
        self.assertFalse(evidence["artifacts_checked"])
        self.assertTrue(all(item["status"] == "passed" for item in evidence["checks"]))

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp).resolve() / "evidence.json"
            _write_evidence(output, evidence, replace=False)
            self.assertEqual(json.loads(output.read_text()), evidence)
            _validate_written_evidence(output, evidence)
            with self.assertRaisesRegex(ValidationError, "already exists"):
                _write_evidence(output, evidence, replace=False)
            _write_evidence(output, evidence, replace=True)

    def test_evidence_contract_names_shard_gate_and_enforces_coverage_floor(self):
        source_sha256 = "a" * 64
        evidence = _evidence_payload(
            SimpleNamespace(quick=True, skip_artifacts=True),
            source_sha256=source_sha256,
            report_template_digest="c" * 64,
        )
        self.assertEqual(evidence["source_sha256"], source_sha256)
        self.assertEqual(evidence["report_template_sha256"], "c" * 64)
        self.assertIn("test-shard-ownership", _expected_checks(quick=True, skip_artifacts=True))
        self.assertEqual(evidence["coverage_minimum_percent"], COVERAGE_MINIMUM_PERCENT)

        with patch("tools.verify._run") as run:
            _run_coverage_tests(["coverage"])
        report_command = run.call_args_list[-1].args[0]
        self.assertIn("--fail-under=%d" % COVERAGE_MINIMUM_PERCENT, report_command)

    def test_verification_rejects_source_drift_after_checks(self):
        before = "a" * 64
        after = "b" * 64
        with patch("tools.verify.repository_source_sha256", return_value=after):
            with self.assertRaisesRegex(VerificationError, "changed during verification"):
                _require_unchanged_source(before)

        args = SimpleNamespace(
            quick=True,
            skip_artifacts=True,
            evidence=None,
            replace_evidence=False,
        )
        errors = StringIO()
        with (
            patch("tools.verify.parse_args", return_value=args),
            patch("tools.verify.repository_source_sha256", side_effect=[before, after]),
            patch("tools.verify._tool_prefix", return_value=["tool"]),
            patch("tools.verify._jsonschema_python", return_value=["python"]),
            patch("tools.verify._run"),
            patch("tools.verify._verify_documentation"),
            patch("tools.verify._verify_workflow_examples"),
            patch("tools.verify._run_tests"),
            redirect_stderr(errors),
        ):
            self.assertEqual(verify_main([]), 1)
        self.assertIn("source changed during verification", errors.getvalue())

    def test_stale_artifact_error_lists_copy_pasteable_refresh_commands(self):
        with (
            patch("tools.verify._build_artifacts"),
            patch(
                "tools.verify._compare_artifacts",
                side_effect=[None, VerificationError("tracked release artifacts differ")],
            ),
        ):
            with self.assertRaises(VerificationError) as caught:
                _verify_artifacts()
        for command in ARTIFACT_REFRESH_COMMANDS:
            self.assertIn(command, str(caught.exception))

    def test_release_report_section_is_generated_and_idempotent(self):
        evidence = _evidence_payload(SimpleNamespace(quick=True, skip_artifacts=True))
        source = "# Release\n\n%s\nold\n%s\n\nTail.\n" % (BEGIN, END)
        rendered = render_report(source, evidence)

        self.assertEqual(rendered, render_report(rendered, evidence))
        self.assertIn("Generated verification evidence", rendered)
        self.assertIn("`warning-strict-core-tests`", rendered)
        self.assertEqual(rendered.count(BEGIN), 1)
        self.assertEqual(rendered.count(END), 1)
        self.assertEqual(generated_section(evidence).count("|"), rendered.count("|"))

    def test_release_evidence_loader_rejects_failed_or_malformed_claims(self):
        evidence = _evidence_payload(SimpleNamespace(quick=True, skip_artifacts=True))
        evidence["checks"][0]["status"] = "failed"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            path.write_text(json.dumps(evidence), encoding="utf-8")
            with self.assertRaisesRegex(ReportError, "checks are invalid"):
                load_evidence(path)

    def test_release_evidence_loader_rejects_duplicate_json_and_markdown_injection(self):
        evidence = _evidence_payload(SimpleNamespace(quick=True, skip_artifacts=True))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = root / "evidence.json"
            payload = json.dumps(evidence)
            path.write_text(payload.replace("{", '{"schema":"forged",', 1), encoding="utf-8")
            with self.assertRaisesRegex(ReportError, "not valid UTF-8 JSON"):
                load_evidence(path)

            evidence["environment"]["machine"] = "arm64\n| forged release claim |"
            path.write_text(json.dumps(evidence), encoding="utf-8")
            with self.assertRaisesRegex(ReportError, "typed metadata"):
                load_evidence(path)

    def test_release_evidence_loader_rejects_source_drift_during_validation(self):
        evidence = _evidence_payload(SimpleNamespace(quick=True, skip_artifacts=True))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / "evidence.json"
            path.write_text(json.dumps(evidence), encoding="utf-8")
            with patch(
                "tools.render_release_report.repository_source_sha256",
                side_effect=[evidence["source_sha256"], "b" * 64],
            ):
                with self.assertRaisesRegex(ReportError, "inputs changed during validation"):
                    load_evidence(path)

    def test_provenance_inputs_reject_symlinks_and_reversed_report_markers(self):
        evidence = _evidence_payload(SimpleNamespace(quick=True, skip_artifacts=True))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            real_evidence = root / "evidence.json"
            evidence_link = root / "evidence-link.json"
            real_evidence.write_text(json.dumps(evidence), encoding="utf-8")
            evidence_link.symlink_to(real_evidence)
            with self.assertRaisesRegex(ReportError, "not valid UTF-8 JSON"):
                load_evidence(evidence_link)

            report = root / "report.md"
            reversed_markers = "%s\n%s\n" % (END, BEGIN)
            report.write_text(reversed_markers, encoding="utf-8")
            with self.assertRaisesRegex(ProvenanceError, "marker order"):
                report_template_sha256(report)
            with self.assertRaisesRegex(ReportError, "out of order"):
                render_report(reversed_markers, evidence)

            source = root / "source"
            source.mkdir()
            (source / "real.txt").write_text("trusted", encoding="utf-8")
            (source / "linked.txt").symlink_to(source / "real.txt")
            subprocess.run(
                ["git", "init", "-q", str(source)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "-C", str(source), "add", "real.txt", "linked.txt"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with self.assertRaisesRegex(ProvenanceError, "cannot safely read"):
                repository_source_sha256(source)


if __name__ == "__main__":
    unittest.main()
