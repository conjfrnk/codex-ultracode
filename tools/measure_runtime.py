#!/usr/bin/env python3
"""Measure the default Conductor runtime without launching a model provider."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import tokenize
import zipfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARCHIVE = PROJECT_ROOT / "dist" / "conductor-runtime.pyz"
DEFAULT_ARTIFACTS = (
    "skill.zip",
    "conductor-runtime.pyz",
    "conductor-extras.pyz",
    "release-manifest.json",
    "codex-conductor-marketplace.zip",
    "codex-conductor-bundle.zip",
)
METRIC_SENTINEL = "CONDUCTOR_IMPORT_METRIC="


def _python_files(root: Path) -> List[Path]:
    if root.is_file():
        return [root] if root.suffix == ".py" else []
    return sorted(
        path
        for path in root.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )


def _source_metrics(root: Path) -> Dict:
    records = []
    physical_total = 0
    code_total = 0
    byte_total = 0
    for path in _python_files(root):
        data = path.read_bytes()
        physical = len(data.splitlines())
        code_lines = _code_line_count(path)
        record = {
            "path": path.relative_to(PROJECT_ROOT).as_posix(),
            "physical_lines": physical,
            "code_lines": code_lines,
            "size_bytes": len(data),
        }
        records.append(record)
        physical_total += physical
        code_total += code_lines
        byte_total += len(data)
    return {
        "root": root.relative_to(PROJECT_ROOT).as_posix(),
        "files": len(records),
        "physical_lines": physical_total,
        "code_lines": code_total,
        "size_bytes": byte_total,
        "largest_files": sorted(
            records,
            key=lambda item: (item["physical_lines"], item["path"]),
            reverse=True,
        )[:20],
    }


def _code_line_count(path: Path) -> int:
    lines = set()
    try:
        with path.open("rb") as source:
            tokens = tokenize.tokenize(source.readline)
            for token in tokens:
                if token.type in {
                    tokenize.ENCODING,
                    tokenize.ENDMARKER,
                    tokenize.INDENT,
                    tokenize.DEDENT,
                    tokenize.NEWLINE,
                    tokenize.NL,
                    tokenize.COMMENT,
                }:
                    continue
                start, end = token.start[0], token.end[0]
                lines.update(range(start, end + 1))
    except (OSError, SyntaxError, tokenize.TokenError):
        return 0
    return len(lines)


def _module_name(package_root: Path, path: Path) -> str:
    relative = path.relative_to(package_root.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _import_graph(package_root: Path) -> Tuple[Dict[str, Set[str]], Dict[str, Path]]:
    modules = {_module_name(package_root, path): path for path in _python_files(package_root)}
    graph = {module: set() for module in modules}
    for module, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Import):
                targets.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    relative = "." * node.level + (node.module or "")
                    try:
                        base = importlib.util.resolve_name(relative, package)
                    except (ImportError, ValueError):
                        continue
                else:
                    base = node.module or ""
                targets.append(base)
                if node.module is None:
                    targets.extend(f"{base}.{alias.name}" for alias in node.names)
            for target in targets:
                resolved = _nearest_module(target, modules)
                if resolved is not None and resolved != module:
                    graph[module].add(resolved)
    return graph, modules


def _nearest_module(target: str, modules: Dict[str, Path]) -> Optional[str]:
    candidate = target
    while candidate:
        if candidate in modules:
            return candidate
        candidate = candidate.rpartition(".")[0]
    return None


def _reachable(graph: Dict[str, Set[str]], roots: Iterable[str]) -> Set[str]:
    seen = set()
    queue = deque(root for root in roots if root in graph)
    while queue:
        module = queue.popleft()
        if module in seen:
            continue
        seen.add(module)
        queue.extend(graph[module] - seen)
    return seen


def _strongly_connected_components(graph: Dict[str, Set[str]]) -> List[List[str]]:
    index = 0
    indices: Dict[str, int] = {}
    lowlinks: Dict[str, int] = {}
    stack: List[str] = []
    on_stack = set()
    components = []

    def visit(module: str) -> None:
        nonlocal index
        indices[module] = index
        lowlinks[module] = index
        index += 1
        stack.append(module)
        on_stack.add(module)
        for dependency in graph[module]:
            if dependency not in indices:
                visit(dependency)
                lowlinks[module] = min(lowlinks[module], lowlinks[dependency])
            elif dependency in on_stack:
                lowlinks[module] = min(lowlinks[module], indices[dependency])
        if lowlinks[module] != indices[module]:
            return
        component = []
        while stack:
            dependency = stack.pop()
            on_stack.remove(dependency)
            component.append(dependency)
            if dependency == module:
                break
        components.append(sorted(component))

    for module in sorted(graph):
        if module not in indices:
            visit(module)
    return components


def _graph_metrics(package_root: Path, entry_modules: Iterable[str]) -> Dict:
    graph, modules = _import_graph(package_root)
    reachable = _reachable(graph, entry_modules)
    components = _strongly_connected_components(graph)
    cycles = sorted((item for item in components if len(item) > 1), key=lambda item: (-len(item), item))
    reverse = defaultdict(set)
    for source, targets in graph.items():
        for target in targets:
            reverse[target].add(source)
    return {
        "modules": len(modules),
        "internal_import_edges": sum(len(targets) for targets in graph.values()),
        "entry_modules": list(entry_modules),
        "entry_reachable_modules": len(reachable),
        "entry_reachable_source_bytes": sum(modules[module].stat().st_size for module in reachable),
        "strongly_connected_components": len(components),
        "import_cycles": cycles,
        "highest_fan_in": [
            {"module": module, "importers": len(importers)}
            for module, importers in sorted(
                reverse.items(), key=lambda item: (-len(item[1]), item[0])
            )[:15]
        ],
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_metrics(path: Path) -> Dict:
    result = {
        "path": path.relative_to(PROJECT_ROOT).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }
    with zipfile.ZipFile(path) as archive:
        files = [item for item in archive.infolist() if not item.is_dir()]
        runtime_files = [item for item in files if item.filename.startswith("conductor_runtime/")]
        result.update(
            {
                "entries": len(files),
                "runtime_entries": len(runtime_files),
                "uncompressed_bytes": sum(item.file_size for item in files),
                "compressed_payload_bytes": sum(item.compress_size for item in files),
                "runtime_uncompressed_bytes": sum(item.file_size for item in runtime_files),
                "runtime_compressed_payload_bytes": sum(item.compress_size for item in runtime_files),
            }
        )
    return result


def _artifact_metrics(dist: Path) -> List[Dict]:
    records = []
    for name in DEFAULT_ARTIFACTS:
        path = dist / name
        if not path.is_file():
            continue
        records.append(
            {
                "path": path.relative_to(PROJECT_ROOT).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return records


def _timing_summary(samples: List[float]) -> Dict:
    ordered = sorted(samples)
    p95_index = max(0, min(len(ordered) - 1, (95 * len(ordered) + 99) // 100 - 1))
    return {
        "runs": len(samples),
        "minimum_ms": round(ordered[0] * 1000, 3),
        "median_ms": round(statistics.median(ordered) * 1000, 3),
        "p95_ms": round(ordered[p95_index] * 1000, 3),
        "maximum_ms": round(ordered[-1] * 1000, 3),
    }


def _run_timed(command: List[str], env: Dict[str, str], cwd: Path) -> Tuple[float, subprocess.CompletedProcess]:
    started = time.perf_counter()
    completed = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
    elapsed = time.perf_counter() - started
    return elapsed, completed


def _startup_metrics(package: str, runs: int) -> Dict:
    command = [sys.executable, "-m", package, "--version"]
    return _command_startup_metrics(command, runs)


def _command_startup_metrics(command: List[str], runs: int) -> Dict:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    samples = []
    for _ in range(runs):
        elapsed, completed = _run_timed(command, env, PROJECT_ROOT)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr or completed.stdout or "version command failed")
        samples.append(elapsed)
    return {"command": command, **_timing_summary(samples)}


def _dynamic_import_metrics(package: str) -> Dict:
    script = """
import json
import runpy
import sys
sys.argv = ["conductor-runtime", "--version"]
try:
    runpy.run_module(%r + ".__main__", run_name="__main__")
except SystemExit as exc:
    if exc.code not in (None, 0):
        raise
runtime = sorted(name for name in sys.modules if name == %r or name.startswith(%r + "."))
print(%r + json.dumps({"runtime_modules": runtime, "all_modules": len(sys.modules)}), file=sys.stderr)
""" % (package, package, package, METRIC_SENTINEL)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or "import probe failed")
    metric_line = next(
        (line for line in completed.stderr.splitlines() if line.startswith(METRIC_SENTINEL)),
        None,
    )
    if metric_line is None:
        raise RuntimeError("import probe did not emit metrics")
    payload = json.loads(metric_line[len(METRIC_SENTINEL) :])
    return {
        "runtime_modules": len(payload["runtime_modules"]),
        "all_modules": payload["all_modules"],
        "module_names": payload["runtime_modules"],
    }


def _simple_plan_metrics(command_prefix: List[str], runs: int, contract: str) -> Dict:
    samples = []
    receipts = []
    with tempfile.TemporaryDirectory(prefix="conductor-measure-") as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        workspace.mkdir()
        for index in range(runs):
            state = root / ("state-%02d" % index)
            receipt = state / "simple-plan.json"
            env = os.environ.copy()
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["CODEX_CONDUCTOR_HOME"] = str(state)
            command = command_prefix + [
                "auto",
                "--task",
                "Fix one obvious typo in README.md.",
                "--strategy",
                "direct",
                "--plan-only",
                "--allow-agent",
                "--workspace",
                str(workspace),
                "--receipt",
                str(receipt),
            ]
            elapsed, completed = _run_timed(command, env, PROJECT_ROOT)
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr or completed.stdout or "simple plan failed")
            samples.append(elapsed)
            receipts.append(receipt.stat().st_size if receipt.is_file() else 0)
    return {
        "contract": contract,
        "provider_launches": 0,
        "receipt_size_bytes": {
            "minimum": min(receipts),
            "maximum": max(receipts),
        },
        **_timing_summary(samples),
    }


def build_report(args) -> Dict:
    package_root = (PROJECT_ROOT / args.package_path).resolve()
    tests_root = (PROJECT_ROOT / args.tests_path).resolve()
    archive = (PROJECT_ROOT / args.archive).resolve()
    if not package_root.is_dir() or not tests_root.is_dir() or not archive.is_file():
        raise ValueError("package, tests, and archive paths must exist")
    package_name = package_root.name
    optional_root = PROJECT_ROOT / "conductor_extras"
    extras_archive = PROJECT_ROOT / "dist" / "conductor-extras.pyz"
    report = {
        "schema": "conductor.runtime_measurement.v1",
        "python": sys.version.split()[0],
        "production": _source_metrics(package_root),
        "tests": _source_metrics(tests_root),
        "core_tests": _source_metrics(tests_root / "test_core_runtime.py"),
        "imports": _graph_metrics(
            package_root,
            (f"{package_name}.__main__", f"{package_name}.cli"),
        ),
        "dynamic_startup_imports": _dynamic_import_metrics(package_name),
        "startup": _startup_metrics(package_name, args.runs),
        "simple_task_overhead": _simple_plan_metrics(
            [sys.executable, "-m", package_name],
            args.runs,
            "source-direct-plan-only-no-provider-v2",
        ),
        "packaged_startup": _command_startup_metrics(
            [sys.executable, str(archive), "--version"],
            args.runs,
        ),
        "packaged_simple_task_overhead": _simple_plan_metrics(
            [sys.executable, str(archive)],
            args.runs,
            "packaged-direct-plan-only-no-provider-v2",
        ),
        "archive": _archive_metrics(archive),
        "artifacts": _artifact_metrics(PROJECT_ROOT / "dist"),
    }
    if optional_root.is_dir():
        report["optional_production"] = _source_metrics(optional_root)
        report["optional_imports"] = _graph_metrics(
            optional_root,
            ("conductor_extras.__main__", "conductor_extras.cli"),
        )
    if extras_archive.is_file():
        report["optional_archive"] = _archive_metrics(extras_archive)
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-path", default="conductor_runtime")
    parser.add_argument("--tests-path", default="tests")
    parser.add_argument("--archive", default=str(DEFAULT_ARCHIVE.relative_to(PROJECT_ROOT)))
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.runs <= 100:
        parser.error("--runs must be between 1 and 100")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        report = build_report(args)
    except (OSError, RuntimeError, ValueError, SyntaxError, zipfile.BadZipFile) as exc:
        print("Measurement failed: %s" % exc, file=sys.stderr)
        return 1
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
