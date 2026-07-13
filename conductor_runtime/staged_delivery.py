import hashlib
from pathlib import Path
from typing import Dict

from .benchmark import load_benchmark_report
from .codex_staged_repair import load_codex_staged_repair_evidence
from .errors import ValidationError
from .security import read_regular_file_bytes_no_follow


STAGED_DELIVERY_PROVIDER_STATUSES = {
    "success",
    "budget-exceeded",
    "token-budget-exceeded",
}


def verified_stage_delivery(result: Dict, patch_output: Path) -> bool:
    if not isinstance(result, dict):
        return False
    provider = result.get("provider_evidence")
    staged = result.get("staged_evidence")
    summary = result.get("completion_summary")
    if not isinstance(provider, dict) or not isinstance(staged, dict) or not isinstance(summary, dict):
        return False
    if provider.get("status") not in STAGED_DELIVERY_PROVIDER_STATUSES:
        return False
    source = staged.get("source")
    stage = staged.get("stage")
    changes = staged.get("changes")
    verification = staged.get("verification")
    policy = staged.get("policy")
    structurally_accepted = bool(
        staged.get("status") == "success"
        and isinstance(source, dict)
        and source.get("unchanged") is True
        and isinstance(stage, dict)
        and stage.get("verifier_mutated_files") is False
        and isinstance(changes, dict)
        and changes.get("change_count", 0) > 0
        and changes.get("patch_written") is True
        and isinstance(verification, dict)
        and verification.get("configured") is True
        and verification.get("status") == "passed"
        and verification.get("returncode") == 0
        and verification.get("timed_out") is False
        and isinstance(policy, dict)
        and policy.get("automatic_apply") is False
        and summary.get("verification_configured") is True
        and summary.get("verification_performed") is True
        and summary.get("verification_status") == "passed"
        and summary.get("verification_returncode") == 0
        and summary.get("verification_timed_out") is False
        and summary.get("change_count") == changes.get("change_count")
    )
    if not structurally_accepted:
        return False
    patch_bytes = changes.get("patch_bytes")
    patch_sha256 = changes.get("patch_sha256")
    if not isinstance(patch_bytes, int) or isinstance(patch_bytes, bool) or patch_bytes <= 0:
        return False
    if not isinstance(patch_sha256, str) or len(patch_sha256) != 64:
        return False
    try:
        retained_patch = read_regular_file_bytes_no_follow(
            Path(patch_output),
            "verified staged patch",
            max_bytes=patch_bytes,
        )
    except (FileNotFoundError, OSError, ValidationError):
        return False
    return len(retained_patch) == patch_bytes and hashlib.sha256(retained_patch).hexdigest() == patch_sha256


def verified_repair_stage_delivery(evidence: Dict, repair_dir: Path) -> bool:
    if not isinstance(evidence, dict) or evidence.get("status") not in {
        "success",
        "token-budget-exceeded",
    }:
        return False
    source = evidence.get("source")
    attempts = evidence.get("attempts")
    final = evidence.get("final")
    policy = evidence.get("policy")
    if not isinstance(source, dict) or not isinstance(attempts, list) or not attempts:
        return False
    if not isinstance(final, dict) or not isinstance(policy, dict):
        return False
    last_attempt = attempts[-1]
    structurally_accepted = bool(
        source.get("unchanged") is True
        and source.get("scan_error") is None
        and policy.get("source_mutated") is False
        and policy.get("automatic_apply") is False
        and isinstance(last_attempt, dict)
        and last_attempt.get("chain_scan_error") is None
        and last_attempt.get("provider_status") in {"success", "token-budget-exceeded"}
        and last_attempt.get("staged_status") == "success"
        and last_attempt.get("verification_status") == "passed"
        and final.get("change_count", 0) > 0
        and final.get("binary") == []
        and final.get("mode_changed") == []
        and final.get("unpatchable") == []
        and final.get("verification_status") == "passed"
        and final.get("patch_written") is True
        and final.get("patch_name") == "final.patch"
        and final.get("patch_error") is None
    )
    if not structurally_accepted:
        return False

    repair_path = Path(repair_dir)
    try:
        validated = load_codex_staged_repair_evidence(repair_path / "repair.json")
        if validated != evidence:
            return False
        report = load_benchmark_report(repair_path / last_attempt["report_name"])
        if len(report.get("results", [])) != 1:
            return False
        attempt_patch = repair_path / ("attempt-%03d.patch" % last_attempt["index"])
        if not verified_stage_delivery(report["results"][0], attempt_patch):
            return False
        patch_bytes = final.get("patch_bytes")
        patch_sha256 = final.get("patch_sha256")
        if not isinstance(patch_bytes, int) or isinstance(patch_bytes, bool) or patch_bytes <= 0:
            return False
        if not isinstance(patch_sha256, str) or len(patch_sha256) != 64:
            return False
        retained_patch = read_regular_file_bytes_no_follow(
            repair_path / final["patch_name"],
            "verified staged repair patch",
            max_bytes=patch_bytes,
        )
    except (FileNotFoundError, OSError, ValidationError):
        return False
    return len(retained_patch) == patch_bytes and hashlib.sha256(retained_patch).hexdigest() == patch_sha256
