#!/usr/bin/env python3
"""Render the generated verification section of RELEASE_REPORT.md."""

import argparse
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from conductor_runtime import __version__  # noqa: E402
from conductor_runtime.core.safe import replace_bytes, strict_json_bytes  # noqa: E402
from conductor_runtime.errors import ValidationError  # noqa: E402
from tools.release_provenance import (  # noqa: E402
    ProvenanceError,
    REPORT_BEGIN,
    REPORT_END,
    read_regular_provenance_bytes,
    release_artifact_binding,
    release_report_template_sha256,
    report_template_sha256,
    repository_source_sha256,
)
from tools.verify import (  # noqa: E402
    CODEX_VERSION,
    COVERAGE_VERSION,
    COVERAGE_MINIMUM_PERCENT,
    JSONSCHEMA_VERSION,
    MYPY_VERSION,
    EVIDENCE_SCHEMA,
    RUFF_VERSION,
    _current_test_counts,
    _expected_checks,
)


BEGIN = REPORT_BEGIN
END = REPORT_END
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReportError(RuntimeError):
    pass


def _release_binding():
    try:
        return release_artifact_binding(PROJECT_ROOT)
    except ProvenanceError as exc:
        raise ReportError("verification evidence artifact binding is unreadable") from exc


def load_evidence(path: Path):
    try:
        payload = read_regular_provenance_bytes(
            path,
            "verification evidence",
            max_bytes=1024 * 1024,
        )
        value = strict_json_bytes(payload, "verification evidence")
    except (ProvenanceError, ValidationError) as exc:
        raise ReportError("verification evidence is not valid UTF-8 JSON") from exc
    required = {
        "schema",
        "runtime_version",
        "codex_version",
        "mode",
        "artifacts_checked",
        "source_sha256",
        "report_template_sha256",
        "checksums_sha256",
        "environment",
        "tools",
        "warning_strict",
        "coverage_minimum_percent",
        "hash_seeds",
        "test_counts",
        "core_archive_bytes",
        "checks",
        "artifacts",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("schema") != EVIDENCE_SCHEMA:
        raise ReportError("verification evidence fields or schema are invalid")
    if (
        value["mode"] not in {"quick", "full"}
        or type(value["artifacts_checked"]) is not bool
        or type(value["warning_strict"]) is not bool
        or not isinstance(value["checks"], list)
        or not isinstance(value["artifacts"], list)
        or not isinstance(value["source_sha256"], str)
        or SHA256.fullmatch(value["source_sha256"]) is None
    ):
        raise ReportError("verification evidence mode or checks are invalid")
    if not _safe_display_text(value["codex_version"]) or (
        value["mode"] == "full" and CODEX_VERSION.fullmatch(value["codex_version"]) is None
    ):
        raise ReportError("verification evidence Codex version is invalid")
    if any(
        not isinstance(item, dict)
        or set(item) != {"id", "status"}
        or not isinstance(item["id"], str)
        or item["status"] != "passed"
        for item in value["checks"]
    ):
        raise ReportError("verification evidence checks are invalid")
    check_ids = [item["id"] for item in value["checks"]]
    if not check_ids or len(check_ids) != len(set(check_ids)) or any(not item for item in check_ids):
        raise ReportError("verification evidence checks are duplicated or empty")
    environment = value["environment"]
    tools = value["tools"]
    test_counts = value["test_counts"]
    if (
        not isinstance(environment, dict)
        or set(environment) != {"python", "implementation", "platform", "machine"}
        or not all(_safe_display_text(item) for item in environment.values())
        or not isinstance(tools, dict)
        or set(tools) != {"ruff", "mypy", "coverage", "jsonschema"}
        or not all(isinstance(item, str) and item for item in tools.values())
        or not isinstance(test_counts, dict)
        or set(test_counts) != {"repository", "core_gate"}
        or any(type(item) is not int or item <= 0 for item in test_counts.values())
        or type(value["coverage_minimum_percent"]) is not int
        or not 0 < value["coverage_minimum_percent"] <= 100
        or not isinstance(value["hash_seeds"], list)
        or not all(isinstance(item, str) and item for item in value["hash_seeds"])
    ):
        raise ReportError("verification evidence typed metadata is invalid")
    if value["mode"] == "full" and value["hash_seeds"] != ["0", "1", "42", "random"]:
        raise ReportError("full verification evidence hash-seed matrix is invalid")
    if value["mode"] == "quick" and value["hash_seeds"]:
        raise ReportError("quick verification evidence cannot claim a hash-seed matrix")
    if value["runtime_version"] != __version__:
        raise ReportError("verification evidence runtime version is stale")
    observed_source_sha256 = repository_source_sha256(PROJECT_ROOT)
    if value["source_sha256"] != observed_source_sha256:
        raise ReportError("verification evidence source binding is stale")
    observed_report_template_sha256 = release_report_template_sha256(PROJECT_ROOT)
    if value["report_template_sha256"] != observed_report_template_sha256:
        raise ReportError("verification evidence report-template binding is stale")
    expected_tools = {
        "ruff": RUFF_VERSION,
        "mypy": MYPY_VERSION,
        "coverage": COVERAGE_VERSION,
        "jsonschema": JSONSCHEMA_VERSION,
    }
    if (
        value["tools"] != expected_tools
        or value["warning_strict"] is not True
        or value["coverage_minimum_percent"] != COVERAGE_MINIMUM_PERCENT
        or value["test_counts"] != _current_test_counts()
        or [item["id"] for item in value["checks"]]
        != _expected_checks(quick=value["mode"] == "quick", skip_artifacts=not value["artifacts_checked"])
    ):
        raise ReportError("verification evidence claims do not match the current contract")
    artifact_binding = None
    if value["artifacts_checked"]:
        if (
            not isinstance(value["checksums_sha256"], str)
            or SHA256.fullmatch(value["checksums_sha256"]) is None
            or type(value["core_archive_bytes"]) is not int
            or value["core_archive_bytes"] <= 0
        ):
            raise ReportError("verification evidence release metadata is invalid")
        artifact_binding = _release_binding()
        records, checksum_digest, core_record = artifact_binding
        if (
            value["checksums_sha256"] != checksum_digest
            or value["artifacts"] != records
            or value["core_archive_bytes"] != core_record["size_bytes"]
        ):
            raise ReportError("verification evidence artifact binding is stale")
    elif value["checksums_sha256"] is not None or value["core_archive_bytes"] is not None or value["artifacts"]:
        raise ReportError("verification evidence claims unchecked release metadata")
    if (
        repository_source_sha256(PROJECT_ROOT) != observed_source_sha256
        or release_report_template_sha256(PROJECT_ROOT) != observed_report_template_sha256
        or (artifact_binding is not None and _release_binding() != artifact_binding)
    ):
        raise ReportError("verification evidence inputs changed during validation")
    return value


def generated_section(evidence) -> str:
    environment = evidence["environment"]
    tools = evidence["tools"]
    lines = [
        BEGIN,
        "## Generated verification evidence",
        "",
        "The table below is rendered from `VERIFICATION_EVIDENCE.json` rather than maintained by hand.",
        "",
        "| Field | Value |",
        "| --- | --- |",
        "| Evidence schema | `%s` |" % evidence["schema"],
        "| Runtime | `%s` |" % evidence["runtime_version"],
        "| Codex CLI | `%s` |" % evidence["codex_version"],
        "| Mode | `%s` |" % evidence["mode"],
        "| Python | `%s` (`%s`, `%s`, `%s`) |"
        % (
            environment["python"],
            environment["implementation"],
            environment["platform"],
            environment["machine"],
        ),
        "| Tools | Ruff `%s`; mypy `%s`; coverage `%s`; jsonschema `%s` |"
        % (tools["ruff"], tools["mypy"], tools["coverage"], tools["jsonschema"]),
        "| Warning strict | `%s` |" % str(evidence["warning_strict"]).lower(),
        "| Core coverage floor | `%s%%` |" % evidence["coverage_minimum_percent"],
        "| Hash seeds | `%s` |" % (", ".join(evidence["hash_seeds"]) or "not run"),
        "| Release artifacts checked | `%s` |" % str(evidence["artifacts_checked"]).lower(),
        "| Source SHA-256 | `%s` |" % evidence["source_sha256"],
        "| Report-template SHA-256 | `%s` |" % evidence["report_template_sha256"],
        "| Checksum-manifest SHA-256 | `%s` |" % (evidence["checksums_sha256"] or "not run"),
        "| Repository tests | `%s` |" % evidence["test_counts"]["repository"],
        "| Core-gate tests | `%s` |" % evidence["test_counts"]["core_gate"],
        "| Core archive bytes | `%s` |" % (evidence["core_archive_bytes"] or "not built"),
        "",
        "### Passed checks",
        "",
    ]
    lines.extend("- `%s`" % item["id"] for item in evidence["checks"])
    if evidence["artifacts"]:
        lines.extend(["", "### Release artifact SHA-256", "", "| Artifact | SHA-256 |", "| --- | --- |"])
        lines.extend("| `%s` | `%s` |" % (item["name"], item["sha256"]) for item in evidence["artifacts"])
    lines.extend([END, ""])
    return "\n".join(lines)


def render_report(report_text: str, evidence) -> str:
    if report_text.count(BEGIN) != 1 or report_text.count(END) != 1:
        raise ReportError("release report must contain exactly one generated evidence marker pair")
    start = report_text.index(BEGIN)
    end = report_text.index(END)
    if end < start:
        raise ReportError("release report generated evidence markers are out of order")
    finish = end + len(END)
    return report_text[:start] + generated_section(evidence).rstrip() + report_text[finish:]


def replace_text(path: Path, text: str) -> None:
    replace_bytes(path, text.encode("utf-8"), "release report", mode=0o644)


def _safe_display_text(value) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
        and not any(char in value for char in "`|")
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=PROJECT_ROOT / "VERIFICATION_EVIDENCE.json")
    parser.add_argument("--report", type=Path, default=PROJECT_ROOT / "RELEASE_REPORT.md")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        evidence = load_evidence(args.evidence)
        if evidence["mode"] != "full" or not evidence["artifacts_checked"]:
            raise ReportError("release report requires full verification with artifact checks")
        if report_template_sha256(args.report) != evidence["report_template_sha256"]:
            raise ReportError("release report template does not match verification evidence")
        observed = read_regular_provenance_bytes(
            args.report,
            "release report",
            max_bytes=4 * 1024 * 1024,
        ).decode("utf-8")
        expected = render_report(observed, evidence)
        if args.check:
            if observed != expected:
                raise ReportError("release report generated evidence section is stale")
        else:
            if report_template_sha256(args.report) != evidence["report_template_sha256"]:
                raise ReportError("release report template changed before update")
            replace_text(args.report, expected)
            print("OK: updated %s" % args.report)
    except (OSError, UnicodeDecodeError, ProvenanceError, ValidationError, ReportError) as exc:
        print("Release report failed: %s" % exc, file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
