#!/usr/bin/env python3
# ruff: noqa: E402
import argparse
import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conductor_extras.runtime.benchmark import load_benchmark_report
from conductor_extras.runtime.claude_staged import _prepare_verifier, _run_verifier
from conductor_runtime.errors import ConductorError, ValidationError
from conductor_extras.runtime.staged_workspace import (
    build_workspace_patch,
    copy_workspace_to_stage,
    snapshot_workspace,
)


EVALUATION_SCHEMA = "conductor.implementation_canary_evaluation.v1"
SCORE_INPUT_SCHEMA = "conductor.benchmark_score_input.v1"
SUITE = "conductor-implementation-canary"
MAX_PATCH_BYTES = 1024 * 1024
SCORABLE_STAGED_STATUSES = {"success", "verification-failed", "verification-timed-out"}
TERMINAL_NO_PATCH_STATUS = "no-changes"
EVALUATOR_IDENTITY = "implementation-canary-evaluator-v4"
WORKSPACE_EVALUATION_SCHEMA = "conductor.implementation_canary_workspace_evaluation.v1"
TASK_SPECS = {
    "slug-normalization": {
        "source": "source",
        "held_out": "held-out",
        "held_out_test": "test_slugify_held_out.py",
        "visible_pattern": "test_slugify.py",
        "held_out_pattern": "test_slugify_held_out.py",
        "visible_tests": 3,
        "held_out_tests": 5,
        "expected_changes": {"added": [], "modified": ["slugify.py"], "deleted": []},
    },
    "retry-policy-migration": {
        "source": "retry-policy/source",
        "held_out": "retry-policy/held-out",
        "held_out_test": "test_policy_held_out.py",
        "visible_pattern": "test_policy.py",
        "held_out_pattern": "test_policy_held_out.py",
        "visible_tests": 4,
        "held_out_tests": 7,
        "expected_changes": {"added": [], "modified": ["policy.py", "service.py"], "deleted": []},
    },
    "event-routing-refactor": {
        "source": "event-routing/source",
        "held_out": "event-routing/held-out",
        "held_out_test": "test_event_routing_held_out.py",
        "visible_pattern": "test_event_routing.py",
        "held_out_pattern": "test_event_routing_held_out.py",
        "visible_tests": 4,
        "held_out_tests": 7,
        "expected_changes": {
            "added": [],
            "modified": ["events/names.py", "events/router.py", "events/subscriptions.py"],
            "deleted": [],
        },
    },
    "backoff-test-authoring": {
        "source": "backoff-tests/source",
        "held_out": "backoff-tests/held-out",
        "mode": "mutation-tests",
        "visible_pattern": "test*.py",
        "minimum_visible_tests": 7,
        "mutation_pattern": "test_backoff_contract.py",
        "minimum_authored_tests": 5,
        "expected_mutants": 5,
        "expected_changes": {
            "added": ["tests/test_backoff_contract.py"],
            "modified": [],
            "deleted": [],
        },
    },
    "quota-ledger-contract": {
        "source": "quota-ledger/source",
        "held_out": "quota-ledger/held-out",
        "held_out_test": "test_quota_held_out.py",
        "visible_pattern": "test_quota.py",
        "held_out_pattern": "test_quota_held_out.py",
        "visible_tests": 5,
        "held_out_tests": 8,
        "expected_changes": {
            "added": [],
            "modified": ["quota/ledger.py", "quota/parser.py"],
            "deleted": [],
        },
    },
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the implementation canary in a fresh sandboxed workspace.")
    parser.add_argument("report", type=Path)
    parser.add_argument("patch", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--terminal-no-patch",
        action="store_true",
        help="Score a validated terminal no-change outcome as zero using an empty patch artifact.",
    )
    args = parser.parse_args(argv)

    result = evaluate_implementation_canary(
        args.report,
        args.patch,
        args.output_dir,
        terminal_no_patch=args.terminal_no_patch,
    )
    print("Evaluation: %s" % (args.output_dir / "evaluation.json"))
    print("Score input: %s" % (args.output_dir / "score-input.json"))
    print("Score: %d/10" % result["score"])
    return 0 if result["passed"] else 1


def evaluate_implementation_canary(
    report_path: Path,
    patch_path: Path,
    output_dir: Path,
    *,
    fixture_root: Path = None,
    terminal_no_patch: bool = False,
):
    fixture_root = Path(fixture_root) if fixture_root is not None else REPO_ROOT / "benchmark-suites" / "implementation-canary"
    report_path = Path(report_path)
    patch_path = Path(patch_path)
    output_dir = Path(output_dir)

    report = load_benchmark_report(report_path)
    result, changes, task_id = _validated_result(report, terminal_no_patch=terminal_no_patch)
    spec = TASK_SPECS.get(task_id)
    if spec is None:
        raise ValidationError("implementation canary task is not supported by the evaluator")
    source = fixture_root / spec["source"]
    held_out = fixture_root / spec["held_out"]
    patch_bytes = _read_regular_bytes(patch_path, "patch", MAX_PATCH_BYTES)
    patch_sha256 = hashlib.sha256(patch_bytes).hexdigest()
    if terminal_no_patch:
        if patch_bytes:
            raise ValidationError("terminal no-patch evaluation requires an empty patch artifact")
    else:
        if patch_sha256 != changes["patch_sha256"]:
            raise ValidationError("patch SHA-256 does not match staged evidence")
        _validate_patch_shape(patch_bytes, changes, spec["expected_changes"])

    output_dir = output_dir.resolve()
    if output_dir.exists() or output_dir.is_symlink():
        raise ValidationError("evaluation output directory already exists")
    output_dir.mkdir(parents=True)
    _validate_fixture_tree(source, "source fixture")
    _validate_fixture_tree(held_out, "held-out fixture")
    if terminal_no_patch:
        visible, held_out_result, scope, structure, reporting = _terminal_zero_results(result, changes)
    else:
        workspace = output_dir / "workspace"
        shutil.copytree(source, workspace)
        _apply_patch(workspace, patch_bytes)

        if spec.get("mode") == "mutation-tests":
            visible = _run_minimum_check(
                workspace,
                spec["visible_pattern"],
                spec["minimum_visible_tests"],
            )
            held_out_result = _run_mutation_check(
                workspace,
                held_out / "mutants",
                spec["mutation_pattern"],
                spec["minimum_authored_tests"],
                spec["expected_mutants"],
            )
        else:
            visible = _run_check(workspace, spec["visible_pattern"], spec["visible_tests"])
            shutil.copy2(held_out / spec["held_out_test"], workspace / "tests" / spec["held_out_test"])
            held_out_result = _run_check(
                workspace,
                spec["held_out_pattern"],
                spec["held_out_tests"],
            )
        structure = _inspect_implementation(workspace, task_id)
        scope = _scope_result(changes, structure, spec["expected_changes"])
        report_files = sorted(
            spec["expected_changes"]["added"]
            + spec["expected_changes"]["modified"]
            + spec["expected_changes"]["deleted"]
        )
        reporting = _reporting_result(result, report_files)

    evaluation = {
        "schema": EVALUATION_SCHEMA,
        "suite": SUITE,
        "task_id": task_id,
        "report_sha256": _sha256_file(report_path, "benchmark report", 4 * 1024 * 1024),
        "patch_sha256": patch_sha256,
        "source_fixture_sha256": _tree_sha256(source),
        "held_out_fixture_sha256": _tree_sha256(held_out),
        "visible": visible,
        "held_out": held_out_result,
        "scope": scope,
        "structure": structure,
        "reporting": reporting,
    }
    evaluation_sha256 = _sha256_json(evaluation)
    evaluation["evaluation_sha256"] = evaluation_sha256
    score_input = _score_input(evaluation, evaluation_sha256, task_id)
    _write_json(output_dir / "evaluation.json", evaluation)
    _write_json(output_dir / "score-input.json", score_input)
    passed = all(
        [visible["passed"], held_out_result["passed"], scope["passed"], structure["maintainable"], reporting["passed"]]
    )
    return {
        "evaluation": evaluation,
        "score_input": score_input,
        "score": sum(item["score"] for item in score_input["tasks"][0]["criteria"]),
        "passed": passed,
    }


def evaluate_implementation_canary_workspace(
    task_id: str,
    workspace: Path,
    *,
    fixture_root: Path = None,
    orchestration_completed: bool,
):
    """Score a completed topology arm without exposing held-out assets to its workspace."""
    if task_id not in TASK_SPECS:
        raise ValidationError("implementation canary task is not supported by the workspace evaluator")
    if not isinstance(orchestration_completed, bool):
        raise ValidationError("workspace evaluation orchestration_completed must be boolean")
    fixture_root = Path(fixture_root) if fixture_root is not None else REPO_ROOT / "benchmark-suites" / "implementation-canary"
    workspace = Path(workspace).resolve()
    spec = TASK_SPECS[task_id]
    source = (fixture_root / spec["source"]).resolve()
    held_out = (fixture_root / spec["held_out"]).resolve()
    _validate_fixture_tree(source, "source fixture")
    _validate_fixture_tree(held_out, "held-out fixture")
    _validate_fixture_tree(workspace, "topology workspace")

    source_snapshot = snapshot_workspace(source)
    workspace_snapshot = snapshot_workspace(workspace)
    changes = build_workspace_patch(source, source_snapshot, workspace, workspace_snapshot)
    public_changes = {
        key: changes[key]
        for key in ["added", "modified", "deleted", "binary", "mode_changed", "unpatchable", "change_count"]
    }

    with tempfile.TemporaryDirectory(prefix="conductor-topology-eval-") as temporary:
        evaluation_workspace = Path(temporary) / "workspace"
        copy_workspace_to_stage(workspace, evaluation_workspace)
        if spec.get("mode") == "mutation-tests":
            visible = _run_minimum_check(
                evaluation_workspace,
                spec["visible_pattern"],
                spec["minimum_visible_tests"],
            )
            held_out_result = _run_mutation_check(
                evaluation_workspace,
                held_out / "mutants",
                spec["mutation_pattern"],
                spec["minimum_authored_tests"],
                spec["expected_mutants"],
            )
        else:
            visible = _run_check(evaluation_workspace, spec["visible_pattern"], spec["visible_tests"])
            shutil.copy2(
                held_out / spec["held_out_test"],
                evaluation_workspace / "tests" / spec["held_out_test"],
            )
            held_out_result = _run_check(
                evaluation_workspace,
                spec["held_out_pattern"],
                spec["held_out_tests"],
            )
        try:
            structure = _inspect_implementation(evaluation_workspace, task_id)
        except (OSError, SyntaxError, UnicodeError, ValidationError) as exc:
            structure = {
                "source_sha256": None,
                "signature_preserved": False,
                "line_count": 0,
                "maintainable": False,
                "error_class": exc.__class__.__name__,
            }

    scope = _scope_result(public_changes, structure, spec["expected_changes"])
    maintainability_scored = bool(public_changes["change_count"] > 0 and structure["maintainable"])
    orchestration_scored = bool(public_changes["change_count"] > 0 and orchestration_completed)
    criteria = [
        _workspace_criterion("held-out-correctness", held_out_result["passed"], 5),
        _workspace_criterion("visible-test-correctness", visible["passed"], 2),
        _workspace_criterion("scope-control", scope["passed"], 1),
        _workspace_criterion("maintainability", maintainability_scored, 1),
        _workspace_criterion("orchestration-completion", orchestration_scored, 1),
    ]
    score = sum(item["score"] for item in criteria)
    evaluation = {
        "schema": WORKSPACE_EVALUATION_SCHEMA,
        "evaluator": {
            "identity": "implementation-canary-workspace-evaluator-v1",
            "independent": True,
        },
        "task_id": task_id,
        "source_fixture_sha256": source_snapshot.fingerprint_sha256,
        "workspace_sha256": workspace_snapshot.fingerprint_sha256,
        "held_out_fixture_sha256": snapshot_workspace(held_out).fingerprint_sha256,
        "changes": public_changes,
        "visible": visible,
        "held_out": held_out_result,
        "scope": scope,
        "structure": structure,
        "orchestration_completed": orchestration_completed,
        "criteria": criteria,
        "score": score,
        "max_score": 10,
        "passed": score == 10,
    }
    evaluation["evaluation_sha256"] = _sha256_json(evaluation)
    return evaluation


def _workspace_criterion(criterion_id: str, passed: bool, max_score: int):
    return {
        "id": criterion_id,
        "score": max_score if passed else 0,
        "max_score": max_score,
        "passed": bool(passed),
    }


def _validated_result(report, *, terminal_no_patch=False):
    if report.get("suite") != SUITE or len(report.get("results", [])) != 1:
        raise ValidationError("benchmark report does not match the implementation canary")
    result = report["results"][0]
    task_id = result.get("id")
    if task_id not in TASK_SPECS:
        raise ValidationError("benchmark report task id does not match a supported implementation canary")
    staged = result.get("staged_evidence")
    allowed_statuses = SCORABLE_STAGED_STATUSES | ({TERMINAL_NO_PATCH_STATUS} if terminal_no_patch else set())
    if not isinstance(staged, dict) or staged.get("status") not in allowed_statuses:
        raise ValidationError("implementation canary requires scorable retained staged evidence")
    changes = staged.get("changes")
    if terminal_no_patch:
        _validate_terminal_no_patch(staged, changes)
    elif not isinstance(changes, dict) or changes.get("patch_written") is not True:
        raise ValidationError("implementation canary requires a retained patch")
    return result, changes, task_id


def _validate_terminal_no_patch(staged, changes):
    verification = staged.get("verification")
    if (
        staged.get("status") != TERMINAL_NO_PATCH_STATUS
        or not isinstance(changes, dict)
        or changes.get("change_count") != 0
        or changes.get("added") != []
        or changes.get("modified") != []
        or changes.get("deleted") != []
        or changes.get("binary") != []
        or changes.get("mode_changed") != []
        or changes.get("unpatchable") != []
        or changes.get("patch_written") is not False
        or changes.get("patch_sha256") is not None
        or not isinstance(verification, dict)
        or verification.get("status") != "skipped-no-changes"
    ):
        raise ValidationError("terminal no-patch evaluation requires validated no-change staged evidence")


def _terminal_zero_results(result, changes):
    output = result.get("stdout", "") if isinstance(result, dict) else ""
    output = output if isinstance(output, str) else ""
    visible = {
        "passed": False,
        "status": "not-run-no-patch",
        "returncode": None,
        "timed_out": False,
        "duration_ms": 0,
        "command_sha256": None,
        "sandbox": "not-run",
        "network_isolated": True,
        "stdout_excerpt": "",
        "stderr_excerpt": "",
    }
    held_out = dict(visible)
    scope = {
        "passed": False,
        "modified": changes.get("modified"),
        "added": changes.get("added"),
        "deleted": changes.get("deleted"),
        "signature_preserved": False,
    }
    structure = {
        "source_sha256": None,
        "signature_preserved": False,
        "line_count": 0,
        "maintainable": False,
    }
    reporting = {
        "passed": False,
        "source": "not-scored-without-patch",
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }
    return visible, held_out, scope, structure, reporting


def _apply_patch(workspace: Path, patch_bytes: bytes) -> None:
    patch_tool = shutil.which("patch")
    if not patch_tool:
        raise ValidationError("patch is required to apply the retained canary patch")
    command = [patch_tool, "--batch", "--forward", "-p1"]
    try:
        checked = subprocess.run(
            [*command, "--dry-run"],
            cwd=workspace,
            input=patch_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        if checked.returncode != 0:
            raise ValidationError("retained canary patch does not apply cleanly")
        applied = subprocess.run(
            command,
            cwd=workspace,
            input=patch_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ValidationError("retained canary patch application timed out")
    if applied.returncode != 0:
        raise ValidationError("retained canary patch could not be applied")


def _validate_patch_shape(patch_bytes: bytes, changes, expected_changes) -> None:
    try:
        text = patch_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("retained canary patch must be UTF-8 text")
    if not isinstance(changes, dict):
        raise ValidationError("retained canary changes must be an object")
    observed_changes = {}
    for field in ["added", "modified", "deleted"]:
        expected = expected_changes[field]
        observed = changes.get(field)
        if (
            not isinstance(observed, list)
            or observed != sorted(set(observed))
            or not set(observed).issubset(set(expected))
        ):
            raise ValidationError(
                "retained canary patch %s files do not match the expected implementation scope" % field
            )
        observed_changes[field] = observed
    changed_files = sorted(
        observed_changes["added"] + observed_changes["modified"] + observed_changes["deleted"]
    )
    if not changed_files:
        raise ValidationError("retained canary patch must have an expected change scope")
    diff_headers = [line for line in text.splitlines() if line.startswith("diff --git ")]
    old_headers = [line for line in text.splitlines() if line.startswith("--- ")]
    new_headers = [line for line in text.splitlines() if line.startswith("+++ ")]
    expected_diff_headers = ["diff --git a/%s b/%s" % (path, path) for path in changed_files]
    expected_old_headers = [
        "--- /dev/null" if path in observed_changes["added"] else "--- a/%s" % path
        for path in changed_files
    ]
    expected_new_headers = [
        "+++ /dev/null" if path in observed_changes["deleted"] else "+++ b/%s" % path
        for path in changed_files
    ]
    if diff_headers != expected_diff_headers:
        raise ValidationError("retained canary patch must modify exactly the expected implementation files")
    if old_headers != expected_old_headers or new_headers != expected_new_headers:
        raise ValidationError("retained canary patch headers are invalid")
    new_mode_headers = [line for line in text.splitlines() if line.startswith("new file mode ")]
    deleted_mode_headers = [line for line in text.splitlines() if line.startswith("deleted file mode ")]
    if new_mode_headers != ["new file mode 100644"] * len(observed_changes["added"]):
        raise ValidationError("retained canary patch new-file metadata is invalid")
    if len(deleted_mode_headers) != len(observed_changes["deleted"]) or any(
        line not in {"deleted file mode 100644", "deleted file mode 100755"}
        for line in deleted_mode_headers
    ):
        raise ValidationError("retained canary patch deleted-file metadata is invalid")
    forbidden_markers = ["GIT binary patch", "rename from ", "rename to "]
    if any(marker in text for marker in forbidden_markers):
        raise ValidationError("retained canary patch contains unsupported metadata")


def _run_check(workspace: Path, pattern: str, expected_tests: int):
    record, observed_tests = _run_test_process(workspace, pattern)
    result = _check_record(record, observed_tests)
    result.update(
        {
            "passed": record["status"] == "passed" and observed_tests == expected_tests,
            "expected_tests": expected_tests,
            "observed_tests": observed_tests,
        }
    )
    return result


def _run_minimum_check(workspace: Path, pattern: str, minimum_tests: int):
    record, observed_tests = _run_test_process(workspace, pattern)
    result = _check_record(record, observed_tests)
    result.update(
        {
            "passed": record["status"] == "passed"
            and observed_tests is not None
            and observed_tests >= minimum_tests,
            "minimum_tests": minimum_tests,
            "observed_tests": observed_tests,
        }
    )
    return result


def _run_mutation_check(
    workspace: Path,
    mutants: Path,
    pattern: str,
    minimum_tests: int,
    expected_mutants: int,
):
    mutant_files = sorted(path for path in mutants.iterdir() if path.is_file() and path.suffix == ".py")
    if len(mutant_files) != expected_mutants:
        raise ValidationError("held-out mutation count does not match the canary specification")
    mutation_root = workspace.parent / "mutation-workspaces"
    mutation_root.mkdir()
    results = []
    for mutant in mutant_files:
        mutant_workspace = mutation_root / mutant.stem
        shutil.copytree(workspace, mutant_workspace)
        shutil.copy2(mutant, mutant_workspace / "backoff.py")
        record, observed_tests = _run_test_process(mutant_workspace, pattern)
        killed = bool(
            record["status"] == "failed"
            and observed_tests is not None
            and observed_tests >= minimum_tests
        )
        item = _check_record(record, observed_tests)
        item.update(
            {
                "id": mutant.stem,
                "killed": killed,
                "minimum_tests": minimum_tests,
                "observed_tests": observed_tests,
            }
        )
        results.append(item)
    killed_count = sum(1 for item in results if item["killed"])
    return {
        "passed": killed_count == expected_mutants,
        "status": "passed" if killed_count == expected_mutants else "failed",
        "expected_mutants": expected_mutants,
        "killed_mutants": killed_count,
        "observed_tests": killed_count,
        "mutants": results,
    }


def _run_test_process(workspace: Path, pattern: str):
    command = [
        "python3",
        "-B",
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-p",
        pattern,
        "-v",
    ]
    verifier = _prepare_verifier(command, workspace)
    record = _run_verifier(verifier, workspace, timeout_seconds=30, output_limit_bytes=256 * 1024)
    combined = record["stdout_excerpt"] + "\n" + record["stderr_excerpt"]
    match = re.search(r"\bRan (\d+) tests?\b", combined)
    observed_tests = int(match.group(1)) if match else None
    return record, observed_tests


def _check_record(record, observed_tests):
    return {
        "status": record["status"],
        "returncode": record["returncode"],
        "timed_out": record["timed_out"],
        "duration_ms": record["duration_ms"],
        "command_sha256": record["command_sha256"],
        "sandbox": record["sandbox"],
        "network_isolated": record["network_isolated"],
        "stdout_excerpt": record["stdout_excerpt"],
        "stderr_excerpt": record["stderr_excerpt"],
    }


def _inspect_implementation(workspace: Path, task_id: str):
    if task_id == "slug-normalization":
        return _inspect_slug_implementation(workspace / "slugify.py")
    if task_id == "retry-policy-migration":
        return _inspect_retry_policy_implementation(workspace)
    if task_id == "event-routing-refactor":
        return _inspect_event_routing_implementation(workspace)
    if task_id == "backoff-test-authoring":
        return _inspect_backoff_tests(workspace)
    if task_id == "quota-ledger-contract":
        return _inspect_quota_implementation(workspace)
    raise ValidationError("implementation canary structure check is not supported")


def _inspect_slug_implementation(path: Path):
    source = _read_regular_bytes(path, "evaluated implementation", 128 * 1024).decode("utf-8")
    tree = ast.parse(source, filename="slugify.py")
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "normalize_slug"]
    signature_preserved = False
    if len(functions) == 1 and isinstance(functions[0], ast.FunctionDef):
        function = functions[0]
        signature_preserved = bool(
            len(function.args.args) == 1
            and function.args.args[0].arg == "value"
            and isinstance(function.args.args[0].annotation, ast.Name)
            and function.args.args[0].annotation.id == "str"
            and isinstance(function.returns, ast.Name)
            and function.returns.id == "str"
            and not function.args.vararg
            and not function.args.kwarg
        )
    imports = []
    forbidden_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
            "compile",
            "eval",
            "exec",
            "__import__",
        }:
            forbidden_calls.append(node.func.id)
    imports = sorted(imports)
    line_count = len(source.splitlines())
    maintainable = bool(
        signature_preserved
        and set(imports).issubset({"re", "unicodedata"})
        and not forbidden_calls
        and line_count <= 80
    )
    return {
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "signature_preserved": signature_preserved,
        "imports": imports,
        "forbidden_calls": sorted(set(forbidden_calls)),
        "line_count": line_count,
        "maintainable": maintainable,
    }


def _inspect_retry_policy_implementation(workspace: Path):
    sources = {}
    trees = {}
    for filename in ["policy.py", "service.py"]:
        source = _read_regular_bytes(
            workspace / filename,
            "evaluated %s implementation" % filename,
            128 * 1024,
        ).decode("utf-8")
        sources[filename] = source
        trees[filename] = ast.parse(source, filename=filename)

    normalize = _named_function(trees["policy.py"], "normalize_policy")
    retry = _named_function(trees["service.py"], "should_retry")
    normalize_signature = _signature_matches(
        normalize,
        [("raw", "dict")],
        "dict",
    )
    retry_signature = _signature_matches(
        retry,
        [("raw_policy", "dict"), ("completed_attempts", "int")],
        "bool",
    )
    signature_preserved = normalize_signature and retry_signature
    imports = []
    forbidden_calls = []
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
                "compile",
                "eval",
                "exec",
                "__import__",
            }:
                forbidden_calls.append(node.func.id)
    imports = sorted(imports)
    line_count = sum(len(source.splitlines()) for source in sources.values())
    allowed_imports = {"copy", "math", "numbers", "policy", "typing"}
    maintainable = bool(
        signature_preserved
        and set(imports).issubset(allowed_imports)
        and not forbidden_calls
        and line_count <= 220
    )
    combined = b"\0".join(sources[name].encode("utf-8") for name in sorted(sources))
    return {
        "source_sha256": hashlib.sha256(combined).hexdigest(),
        "signature_preserved": signature_preserved,
        "signatures": {
            "normalize_policy": normalize_signature,
            "should_retry": retry_signature,
        },
        "imports": imports,
        "forbidden_calls": sorted(set(forbidden_calls)),
        "line_count": line_count,
        "maintainable": maintainable,
    }


def _inspect_event_routing_implementation(workspace: Path):
    paths = {
        "events/names.py": workspace / "events" / "names.py",
        "events/router.py": workspace / "events" / "router.py",
        "events/subscriptions.py": workspace / "events" / "subscriptions.py",
    }
    sources = {
        name: _read_regular_bytes(path, "evaluated %s implementation" % name, 128 * 1024).decode("utf-8")
        for name, path in paths.items()
    }
    trees = {name: ast.parse(source, filename=name) for name, source in sources.items()}
    canonical_signature = _signature_matches(
        _named_function(trees["events/names.py"], "canonical_event_name"),
        [("value", "str")],
        "str",
    )
    dedupe_signature = _signature_matches(
        _named_function(trees["events/subscriptions.py"], "dedupe_subscriptions"),
        [("names", "list")],
        "list",
    )
    router = _named_class(trees["events/router.py"], "EventRouter")
    register_signature = _method_shape_matches(
        _named_method(router, "register"),
        [("self", None), ("name", "str"), ("handler", None)],
    )
    dispatch_signature = _method_shape_matches(
        _named_method(router, "dispatch"),
        [("self", None), ("name", "str"), ("payload", None)],
    )
    shared_imports = {
        "router": _imports_symbol(trees["events/router.py"], "names", "canonical_event_name", level=1)
        or _imports_symbol(trees["events/router.py"], "events.names", "canonical_event_name"),
        "subscriptions": _imports_symbol(
            trees["events/subscriptions.py"], "names", "canonical_event_name", level=1
        )
        or _imports_symbol(
            trees["events/subscriptions.py"], "events.names", "canonical_event_name"
        ),
    }
    duplicate_normalizers = []
    for name in ["events/router.py", "events/subscriptions.py"]:
        for node in ast.walk(trees[name]):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {
                "lower",
                "casefold",
                "replace",
            }:
                duplicate_normalizers.append("%s:%s" % (name, node.func.attr))
    imports, forbidden_calls = _imports_and_forbidden_calls(trees.values())
    signature_preserved = bool(
        canonical_signature and dedupe_signature and register_signature and dispatch_signature
    )
    line_count = sum(len(source.splitlines()) for source in sources.values())
    maintainable = bool(
        signature_preserved
        and all(shared_imports.values())
        and not duplicate_normalizers
        and set(imports).issubset({"events.names", "names", "re", "typing", "unicodedata"})
        and not forbidden_calls
        and line_count <= 260
    )
    combined = b"\0".join(sources[name].encode("utf-8") for name in sorted(sources))
    return {
        "source_sha256": hashlib.sha256(combined).hexdigest(),
        "signature_preserved": signature_preserved,
        "signatures": {
            "canonical_event_name": canonical_signature,
            "dedupe_subscriptions": dedupe_signature,
            "EventRouter.register": register_signature,
            "EventRouter.dispatch": dispatch_signature,
        },
        "shared_helper_imports": shared_imports,
        "duplicate_normalizers": duplicate_normalizers,
        "imports": imports,
        "forbidden_calls": forbidden_calls,
        "line_count": line_count,
        "maintainable": maintainable,
    }


def _inspect_backoff_tests(workspace: Path):
    production_path = workspace / "backoff.py"
    test_path = workspace / "tests" / "test_backoff_contract.py"
    production = _read_regular_bytes(production_path, "evaluated backoff implementation", 128 * 1024).decode(
        "utf-8"
    )
    tests = _read_regular_bytes(test_path, "evaluated backoff tests", 128 * 1024).decode("utf-8")
    production_tree = ast.parse(production, filename="backoff.py")
    test_tree = ast.parse(tests, filename="tests/test_backoff_contract.py")
    signature_preserved = _retry_delay_signature_matches(_named_function(production_tree, "retry_delay"))
    test_methods = sorted(
        node.name
        for node in ast.walk(test_tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    )
    assertion_calls = sum(
        1
        for node in ast.walk(test_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("assert")
    )
    imports, forbidden_calls = _imports_and_forbidden_calls([test_tree])
    line_count = len(tests.splitlines())
    maintainable = bool(
        signature_preserved
        and len(test_methods) >= 5
        and assertion_calls >= 5
        and set(imports).issubset({"backoff", "unittest"})
        and not forbidden_calls
        and line_count <= 220
    )
    return {
        "source_sha256": hashlib.sha256((production + "\0" + tests).encode("utf-8")).hexdigest(),
        "signature_preserved": signature_preserved,
        "test_methods": test_methods,
        "assertion_calls": assertion_calls,
        "imports": imports,
        "forbidden_calls": forbidden_calls,
        "line_count": line_count,
        "maintainable": maintainable,
    }


def _inspect_quota_implementation(workspace: Path):
    relative_paths = [
        "quota/model.py",
        "quota/parser.py",
        "quota/ledger.py",
        "quota/service.py",
    ]
    sources = {
        name: _read_regular_bytes(
            workspace / name,
            "evaluated %s implementation" % name,
            128 * 1024,
        ).decode("utf-8")
        for name in relative_paths
    }
    trees = {name: ast.parse(source, filename=name) for name, source in sources.items()}
    parse_signature = _signature_matches(
        _named_function(trees["quota/parser.py"], "parse_reservation"),
        [("raw", "dict")],
        "Reservation",
    )
    service_signature = _signature_matches(
        _named_function(trees["quota/service.py"], "reserve_quota"),
        [("raw", "dict"), ("ledger", "QuotaLedger")],
        "bool",
    )
    ledger = _named_class(trees["quota/ledger.py"], "QuotaLedger")
    constructor_signature = _method_shape_matches(
        _named_method(ledger, "__init__"),
        [("self", None), ("limits", "dict")],
    )
    reserve_signature = _method_shape_matches(
        _named_method(ledger, "reserve"),
        [("self", None), ("reservation", "Reservation")],
    )
    usage_signature = _method_shape_matches(
        _named_method(ledger, "usage"),
        [("self", None), ("tenant", "str")],
    )
    reservation = _named_class(trees["quota/model.py"], "Reservation")
    frozen_model = _has_frozen_dataclass(reservation)
    imports, forbidden_calls = _imports_and_forbidden_calls(trees.values())
    signature_preserved = bool(
        parse_signature
        and service_signature
        and constructor_signature
        and reserve_signature
        and usage_signature
        and frozen_model
    )
    line_count = sum(len(source.splitlines()) for source in sources.values())
    maintainable = bool(
        signature_preserved
        and set(imports).issubset(
            {"collections.abc", "copy", "dataclasses", "ledger", "model", "numbers", "parser", "typing"}
        )
        and not forbidden_calls
        and line_count <= 360
    )
    combined = b"\0".join(sources[name].encode("utf-8") for name in sorted(sources))
    return {
        "source_sha256": hashlib.sha256(combined).hexdigest(),
        "signature_preserved": signature_preserved,
        "signatures": {
            "parse_reservation": parse_signature,
            "reserve_quota": service_signature,
            "QuotaLedger.__init__": constructor_signature,
            "QuotaLedger.reserve": reserve_signature,
            "QuotaLedger.usage": usage_signature,
            "Reservation.frozen": frozen_model,
        },
        "imports": imports,
        "forbidden_calls": forbidden_calls,
        "line_count": line_count,
        "maintainable": maintainable,
    }


def _named_class(tree, name):
    matches = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
    return matches[0] if len(matches) == 1 else None


def _named_method(class_node, name):
    if class_node is None:
        return None
    matches = [
        node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    return matches[0] if len(matches) == 1 else None


def _method_shape_matches(function, arguments):
    if function is None:
        return False
    observed = []
    for argument in function.args.args:
        annotation = argument.annotation
        observed.append((argument.arg, annotation.id if isinstance(annotation, ast.Name) else None))
    return bool(
        observed == arguments
        and not function.args.posonlyargs
        and not function.args.kwonlyargs
        and not function.args.vararg
        and not function.args.kwarg
        and not function.args.defaults
        and not function.args.kw_defaults
    )


def _imports_symbol(tree, module, symbol, level=0):
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == module
        and node.level == level
        and any(alias.name == symbol for alias in node.names)
        for node in tree.body
    )


def _imports_and_forbidden_calls(trees):
    imports = []
    forbidden_calls = []
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
                "compile",
                "eval",
                "exec",
                "__import__",
            }:
                forbidden_calls.append(node.func.id)
    return sorted(imports), sorted(set(forbidden_calls))


def _retry_delay_signature_matches(function):
    if function is None:
        return False
    observed = []
    for argument in function.args.args:
        annotation = argument.annotation
        observed.append((argument.arg, annotation.id if isinstance(annotation, ast.Name) else None))
    if observed != [("attempt", "int"), ("base_seconds", "float"), ("cap_seconds", "float")]:
        return False
    if function.args.posonlyargs or function.args.kwonlyargs or function.args.vararg or function.args.kwarg:
        return False
    if not isinstance(function.returns, ast.Name) or function.returns.id != "float":
        return False
    if len(function.args.defaults) != 2:
        return False
    try:
        defaults = [ast.literal_eval(value) for value in function.args.defaults]
    except (ValueError, TypeError):
        return False
    return defaults == [0.5, 30.0]


def _has_frozen_dataclass(class_node):
    if class_node is None:
        return False
    for decorator in class_node.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Name):
            continue
        if decorator.func.id != "dataclass":
            continue
        for keyword in decorator.keywords:
            if keyword.arg == "frozen" and isinstance(keyword.value, ast.Constant):
                return keyword.value.value is True
    return False


def _named_function(tree, name):
    matches = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    return matches[0] if len(matches) == 1 and isinstance(matches[0], ast.FunctionDef) else None


def _signature_matches(function, arguments, return_annotation):
    if function is None:
        return False
    observed = []
    for argument in function.args.args:
        annotation = argument.annotation
        observed.append((argument.arg, annotation.id if isinstance(annotation, ast.Name) else None))
    returns = function.returns.id if isinstance(function.returns, ast.Name) else None
    return bool(
        observed == arguments
        and returns == return_annotation
        and not function.args.posonlyargs
        and not function.args.kwonlyargs
        and not function.args.vararg
        and not function.args.kwarg
        and not function.args.defaults
        and not function.args.kw_defaults
    )


def _scope_result(changes, structure, expected_changes):
    passed = bool(
        changes.get("modified") == expected_changes["modified"]
        and changes.get("added") == expected_changes["added"]
        and changes.get("deleted") == expected_changes["deleted"]
        and changes.get("binary") == []
        and changes.get("mode_changed") == []
        and changes.get("unpatchable") == []
        and structure["signature_preserved"]
    )
    return {
        "passed": passed,
        "modified": changes.get("modified"),
        "added": changes.get("added"),
        "deleted": changes.get("deleted"),
        "signature_preserved": structure["signature_preserved"],
    }


def _reporting_result(result, modified_files):
    output = result.get("stdout", "") if isinstance(result, dict) else ""
    text = output if isinstance(output, str) else ""
    lowered = text.lower()
    provider_reported = all(path in text for path in modified_files) and any(
        word in lowered for word in ["check", "test", "verification"]
    )
    summary = result.get("completion_summary") if isinstance(result, dict) else None
    structured_reported = bool(
        isinstance(summary, dict)
        and summary.get("changed_files") == modified_files
        and summary.get("verification_performed") is True
        and isinstance(summary.get("verification_status"), str)
    )
    return {
        "passed": provider_reported or structured_reported,
        "source": (
            "provider-output"
            if provider_reported
            else "structured-runtime-evidence"
            if structured_reported
            else "missing"
        ),
        "output_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _score_input(evaluation, evaluation_sha256, task_id):
    held_out_id = "hidden-mutant-detection" if task_id == "backoff-test-authoring" else "held-out-correctness"
    visible_id = "baseline-test-correctness" if task_id == "backoff-test-authoring" else "visible-test-correctness"
    criteria = [
        _criterion(held_out_id, 5 if evaluation["held_out"]["passed"] else 0, 5, evaluation_sha256),
        _criterion(visible_id, 2 if evaluation["visible"]["passed"] else 0, 2, evaluation_sha256),
        _criterion("scope-control", 1 if evaluation["scope"]["passed"] else 0, 1, evaluation_sha256),
        _criterion("maintainability", 1 if evaluation["structure"]["maintainable"] else 0, 1, evaluation_sha256),
        _criterion("final-reporting", 1 if evaluation["reporting"]["passed"] else 0, 1, evaluation_sha256),
    ]
    return {
        "schema": SCORE_INPUT_SCHEMA,
        "reviewer": {
            "kind": "deterministic",
            "identity": EVALUATOR_IDENTITY,
            "independent": True,
        },
        "tasks": [{"id": task_id, "pass_threshold": 10, "criteria": criteria}],
    }


def _criterion(criterion_id, score, max_score, evaluation_sha256):
    return {
        "id": criterion_id,
        "score": score,
        "max_score": max_score,
        "notes": "Deterministic evaluation %s." % evaluation_sha256,
    }


def _validate_fixture_tree(root: Path, label: str) -> None:
    if not root.is_dir() or root.is_symlink():
        raise ValidationError("%s must be a real directory" % label)
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValidationError("%s contains an unsupported entry" % label)


def _tree_sha256(root: Path) -> str:
    records = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        records.append([path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()])
    return _sha256_json(records)


def _sha256_file(path: Path, label: str, max_bytes: int) -> str:
    return hashlib.sha256(_read_regular_bytes(path, label, max_bytes)).hexdigest()


def _read_regular_bytes(path: Path, label: str, max_bytes: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValidationError("%s must be a regular file" % label)
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise ValidationError("%s exceeds its size limit" % label)
    return data


def _sha256_json(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError, ConductorError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        raise SystemExit(2)
