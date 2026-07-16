#!/usr/bin/env python3
"""Run the canonical local and CI verification contract."""

import argparse
import filecmp
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from conductor_runtime.errors import ConductorError  # noqa: E402
from tools.release_provenance import release_report_template_sha256, repository_source_sha256  # noqa: E402
from tools.write_checksums import ChecksumError, regular_file_record, regular_files_equal  # noqa: E402


RUFF_VERSION = "0.15.21"
MYPY_VERSION = "2.3.0"
COVERAGE_VERSION = "7.15.0"
JSONSCHEMA_VERSION = "4.26.0"
COVERAGE_MINIMUM_PERCENT = 80
CORE_TEST_MODULES = (
    "tests.test_core_runtime",
    "tests.test_core_adversarial",
    "tests.test_core_cli",
    "tests.test_core_policy_security",
    "tests.test_core_stage_lifecycle",
    "tests.test_bundle_installer",
    "tests.test_plugin_package",
    "tests.test_python_support",
    "tests.test_release_checksums",
    "tests.test_verification_tools",
)
HASH_SEEDS = ("0", "1", "42", "random")
DIST_ARTIFACTS = (
    "conductor-runtime.pyz",
    "conductor-extras.pyz",
    "codex-conductor-bundle.zip",
    "codex-conductor-marketplace.zip",
    "release-manifest.json",
    "skill.zip",
    "SHA256SUMS",
)
ARTIFACT_REFRESH_COMMANDS = (
    "python3 -B tools/package_runtime.py dist",
    "python3 -B tools/package_extras.py dist",
    "python3 -B tools/package_skill.py codex-conductor dist",
    "python3 -B tools/write_checksums.py dist",
)


class VerificationError(RuntimeError):
    pass


def _run(argv, *, env=None) -> None:
    command = [str(value) for value in argv]
    print("$ %s" % shlex.join(command), flush=True)
    completed = subprocess.run(command, cwd=PROJECT_ROOT, env=env, check=False)
    if completed.returncode != 0:
        raise VerificationError("command failed with exit %d: %s" % (completed.returncode, shlex.join(command)))


def _test_environment(hash_seed=None):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if hash_seed is not None:
        env["PYTHONHASHSEED"] = hash_seed
    return env


def _tool_prefix(name: str, version: str):
    executable = shutil.which(name)
    if executable is not None:
        prefix = [executable]
    else:
        uvx = shutil.which("uvx")
        if uvx is None:
            raise VerificationError(
                "%s %s is required; install it or bootstrap the documented uvx tool cache" % (name, version)
            )
        prefix = [uvx, "--offline", "%s==%s" % (name, version)]
    completed = subprocess.run(
        [*prefix, "--version"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    observed = completed.stdout.strip()
    if completed.returncode != 0 or version not in observed.replace(",", " ").split():
        raise VerificationError("expected %s %s, observed %r" % (name, version, observed[:200]))
    return prefix


def _jsonschema_python():
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib.metadata as m; print(m.version('jsonschema'))",
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if probe.returncode == 0 and probe.stdout.strip() == JSONSCHEMA_VERSION:
        return [sys.executable]
    uv = shutil.which("uv")
    if uv is None:
        raise VerificationError(
            "jsonschema %s is required; install it or bootstrap the documented uv cache"
            % JSONSCHEMA_VERSION
        )
    return [
        uv,
        "run",
        "--offline",
        "--no-project",
        "--with",
        "jsonschema==%s" % JSONSCHEMA_VERSION,
        "python",
    ]


def _run_coverage_tests(coverage_prefix, *, hash_seed=None) -> None:
    with tempfile.TemporaryDirectory(prefix="conductor-coverage-") as tmp:
        env = _test_environment(hash_seed)
        env["PYTHONWARNINGS"] = "error"
        env["COVERAGE_FILE"] = str(Path(tmp) / ".coverage")
        _run(
            [
                *coverage_prefix,
                "run",
                "--branch",
                "-m",
                "unittest",
                "-q",
                *CORE_TEST_MODULES,
            ],
            env=env,
        )
        _run(
            [*coverage_prefix, "report", "--fail-under=%d" % COVERAGE_MINIMUM_PERCENT],
            env=env,
        )


def _run_tests(*, quick: bool, coverage_prefix) -> None:
    if quick:
        _run_coverage_tests(coverage_prefix)
        return
    _run(
        [sys.executable, "-B", "-W", "error", "-m", "unittest", "discover", "-s", "tests", "-q"],
        env=_test_environment(),
    )
    _run_coverage_tests(coverage_prefix, hash_seed=HASH_SEEDS[0])
    for seed in HASH_SEEDS[1:]:
        print("Core hash-seed matrix: %s" % seed, flush=True)
        _run(
            [sys.executable, "-B", "-W", "error", "-m", "unittest", "-q", *CORE_TEST_MODULES],
            env=_test_environment(seed),
        )


def _verify_workflow_examples() -> None:
    _run(
        [
            sys.executable,
            "-B",
            "-m",
            "conductor_runtime",
            "validate",
            "conductor-workflows/core",
        ]
    )
    legacy_root = PROJECT_ROOT / "conductor-workflows"
    extras_root = legacy_root / "extras"
    legacy_names = {path.name for path in legacy_root.glob("*.json")}
    extras_names = {path.name for path in extras_root.glob("*.json")}
    if legacy_names != extras_names:
        raise VerificationError("deprecated root and extras workflow fixture sets differ")
    for name in sorted(legacy_names):
        if not filecmp.cmp(legacy_root / name, extras_root / name, shallow=False):
            raise VerificationError("deprecated root workflow fixture drifted from extras/%s" % name)
    _run(
        [
            sys.executable,
            "-B",
            "-m",
            "conductor_extras",
            "workflow",
            "validate",
            "conductor-workflows/extras",
        ]
    )
    with tempfile.TemporaryDirectory(prefix="conductor-workflow-fixtures-") as tmp:
        generated = Path(tmp).resolve()
        for template, name in (("read-only", "read-only-review.json"), ("staged-write", "staged-change.json")):
            destination = generated / name
            _run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "conductor_runtime",
                    "init",
                    destination,
                    "--template",
                    template,
                ]
            )
            if not filecmp.cmp(destination, PROJECT_ROOT / "conductor-workflows" / "core" / name, shallow=False):
                raise VerificationError("generated core workflow fixture is stale: %s" % name)


def _verify_documentation() -> None:
    canonical = PROJECT_ROOT / "docs" / "runtime.md"
    packaged = PROJECT_ROOT / "codex-conductor" / "references" / "runtime.md"
    if not filecmp.cmp(canonical, packaged, shallow=False):
        raise VerificationError(
            "packaged runtime reference drifted; copy docs/runtime.md to codex-conductor/references/runtime.md"
        )


def _build_artifacts(destination: Path) -> None:
    _run([sys.executable, "-B", "tools/package_runtime.py", destination])
    _run([sys.executable, "-B", "tools/package_extras.py", destination])
    _run([sys.executable, "-B", "tools/package_skill.py", "codex-conductor", destination])
    _run([sys.executable, "-B", "tools/write_checksums.py", destination])


def _compare_artifacts(left: Path, right: Path, label: str) -> None:
    drift = []
    for name in DIST_ARTIFACTS:
        left_path = left / name
        right_path = right / name
        try:
            equal = regular_files_equal(left_path, right_path)
        except ChecksumError:
            equal = False
        if not equal:
            drift.append(name)
    if drift:
        raise VerificationError("%s differ: %s" % (label, ", ".join(drift)))


def _verify_artifacts() -> None:
    with tempfile.TemporaryDirectory(prefix="conductor-verify-") as tmp:
        root = Path(tmp)
        first = root / "first"
        second = root / "second"
        _build_artifacts(first)
        _build_artifacts(second)
        _compare_artifacts(first, second, "two clean package builds")
        try:
            _compare_artifacts(first, PROJECT_ROOT / "dist", "fresh and tracked release artifacts")
        except VerificationError as exc:
            raise VerificationError(
                "%s; refresh dist by running:\n%s"
                % (exc, "\n".join(ARTIFACT_REFRESH_COMMANDS))
            ) from exc


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run the focused core matrix once instead of the full suite and four hash seeds.",
    )
    parser.add_argument(
        "--skip-artifacts",
        action="store_true",
        help="Skip reproducibility and tracked-dist checks during an intermediate local edit.",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        help="Write machine-readable evidence after every requested check passes.",
    )
    parser.add_argument(
        "--replace-evidence",
        action="store_true",
        help="Replace an existing --evidence file.",
    )
    return parser.parse_args(argv)


def _evidence_payload(args, *, source_sha256=None, report_template_digest=None):
    from conductor_runtime import __version__
    from tools.release_provenance import (
        release_checksum_records,
        repository_source_sha256,
    )

    checks = _expected_checks(quick=args.quick, skip_artifacts=args.skip_artifacts)
    artifacts = []
    checksums_sha256 = None
    core_archive_bytes = None
    if not args.skip_artifacts:
        artifacts, checksums_sha256 = release_checksum_records(PROJECT_ROOT)
        core_record = regular_file_record(PROJECT_ROOT / "dist" / "conductor-runtime.pyz")
        expected_core_sha256 = next(
            item["sha256"] for item in artifacts if item["name"] == "conductor-runtime.pyz"
        )
        if core_record["sha256"] != expected_core_sha256:
            raise VerificationError("core archive changed while verification evidence was assembled")
        core_archive_bytes = core_record["size_bytes"]
    return {
        "schema": "conductor.verification_evidence.v1",
        "runtime_version": __version__,
        "mode": "quick" if args.quick else "full",
        "artifacts_checked": not args.skip_artifacts,
        "source_sha256": source_sha256 or repository_source_sha256(PROJECT_ROOT),
        "report_template_sha256": report_template_digest or release_report_template_sha256(PROJECT_ROOT),
        "checksums_sha256": checksums_sha256,
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": sys.platform,
            "machine": platform.machine(),
        },
        "tools": {
            "ruff": RUFF_VERSION,
            "mypy": MYPY_VERSION,
            "coverage": COVERAGE_VERSION,
            "jsonschema": JSONSCHEMA_VERSION,
        },
        "warning_strict": True,
        "coverage_minimum_percent": COVERAGE_MINIMUM_PERCENT,
        "hash_seeds": [] if args.quick else list(HASH_SEEDS),
        "test_counts": _current_test_counts(),
        "core_archive_bytes": core_archive_bytes,
        "checks": [{"id": check, "status": "passed"} for check in checks],
        "artifacts": artifacts,
    }


def _expected_checks(*, quick: bool, skip_artifacts: bool):
    checks = [
        "ruff",
        "mypy-core",
        "local-skill-audit",
        "test-shard-ownership",
        "documentation-sync",
        "workflow-contract-fixtures",
        "published-schema-runtime-parity",
        "warning-strict-core-tests",
        "core-branch-coverage",
    ]
    if not quick:
        checks.extend(["warning-strict-full-suite", "core-hash-seed-matrix"])
    if not skip_artifacts:
        checks.extend(
            [
                "two-build-reproducibility",
                "tracked-artifact-freshness",
                "external-artifact-checksums",
            ]
        )
    return checks


def _current_test_counts():
    repository_tests = unittest.defaultTestLoader.discover(str(PROJECT_ROOT / "tests"), pattern="test_*.py")
    core_tests = unittest.defaultTestLoader.loadTestsFromNames(CORE_TEST_MODULES)
    return {
        "repository": repository_tests.countTestCases(),
        "core_gate": core_tests.countTestCases(),
    }


def _write_evidence(path: Path, payload, *, replace: bool) -> None:
    from conductor_runtime.core.safe import canonical_json_bytes, replace_bytes, write_new_bytes

    writer = replace_bytes if replace else write_new_bytes
    writer(path, canonical_json_bytes(payload), "verification evidence", mode=0o644)


def _require_unchanged_source(expected_sha256: str) -> None:
    observed = repository_source_sha256(PROJECT_ROOT)
    if observed != expected_sha256:
        raise VerificationError("repository source changed during verification")


def _require_unchanged_report_template(expected_sha256: str) -> None:
    observed = release_report_template_sha256(PROJECT_ROOT)
    if observed != expected_sha256:
        raise VerificationError("release report template changed during verification")


def _validate_written_evidence(path: Path, expected) -> None:
    from tools.render_release_report import load_evidence

    if load_evidence(path) != expected:
        raise VerificationError("written verification evidence does not match its payload")


def _verify_committed_release_evidence() -> None:
    from tools.render_release_report import load_evidence, render_report
    from tools.release_provenance import read_regular_provenance_bytes

    evidence = load_evidence(PROJECT_ROOT / "VERIFICATION_EVIDENCE.json")
    if evidence["mode"] != "full" or not evidence["artifacts_checked"]:
        raise VerificationError("committed release evidence must represent the full artifact gate")
    report_path = PROJECT_ROOT / "RELEASE_REPORT.md"
    observed = read_regular_provenance_bytes(
        report_path,
        "release report",
        max_bytes=4 * 1024 * 1024,
    ).decode("utf-8")
    if observed != render_report(observed, evidence):
        raise VerificationError("RELEASE_REPORT.md generated evidence section is stale")


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if args.replace_evidence and args.evidence is None:
            raise VerificationError("--replace-evidence requires --evidence")
        if args.evidence is not None and (args.quick or args.skip_artifacts):
            raise VerificationError("--evidence requires the full verification and artifact gate")
        source_sha256 = repository_source_sha256(PROJECT_ROOT)
        report_template_digest = release_report_template_sha256(PROJECT_ROOT)
        ruff = _tool_prefix("ruff", RUFF_VERSION)
        mypy = _tool_prefix("mypy", MYPY_VERSION)
        coverage = _tool_prefix("coverage", COVERAGE_VERSION)
        jsonschema_python = _jsonschema_python()
        _run([*ruff, "check", "."])
        _run([*mypy, "conductor_runtime"])
        _run([sys.executable, "-B", "tools/run_local_audit.py"])
        _run([sys.executable, "-B", "-W", "error", "tools/run_test_shard.py", "--validate"])
        _verify_documentation()
        _verify_workflow_examples()
        _run([*jsonschema_python, "-B", "tools/check_core_schema.py"])
        _run_tests(quick=args.quick, coverage_prefix=coverage)
        if not args.skip_artifacts:
            _verify_artifacts()
            if args.evidence is None:
                _verify_committed_release_evidence()
        _require_unchanged_source(source_sha256)
        _require_unchanged_report_template(report_template_digest)
        if args.evidence is not None:
            payload = _evidence_payload(
                args,
                source_sha256=source_sha256,
                report_template_digest=report_template_digest,
            )
            _require_unchanged_source(source_sha256)
            _require_unchanged_report_template(report_template_digest)
            _write_evidence(args.evidence, payload, replace=args.replace_evidence)
            _validate_written_evidence(args.evidence, payload)
            _require_unchanged_source(source_sha256)
            _require_unchanged_report_template(report_template_digest)
    except (ConductorError, OSError, RuntimeError) as exc:
        print("VERIFICATION FAILED: %s" % exc, file=sys.stderr)
        return 1
    print("OK: canonical verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
