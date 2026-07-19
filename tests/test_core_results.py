import contextlib
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tracemalloc
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from conductor_runtime.core import process as process_module
from conductor_runtime.core import results as results_module
from conductor_runtime.cli import _print_run_detail, main
from conductor_runtime.core.policy import RuntimePolicy, shell_approval
from conductor_runtime.core.process import discard_process_captures, run_process
from conductor_runtime.core.results import RunResultStore
from conductor_runtime.core.runner import WorkflowRunner
from conductor_runtime.core.safe import canonical_json_bytes, sha256_bytes
from conductor_runtime.core.state import RunState
from conductor_runtime.errors import ValidationError


class CoreResultCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="conductor-results-")
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.home = self.root / "state"
        self.environment = patch.dict(
            os.environ,
            {"CODEX_CONDUCTOR_HOME": str(self.home)},
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def _run_shell(self, command, *, output_limit=96, timeout_seconds=10):
        workflow = {
            "schema": "conductor.core.workflow.v1",
            "name": "result-capture",
            "steps": [
                {
                    "id": "inspect",
                    "kind": "shell",
                    "command": command,
                    "capture": "preview.txt",
                    "capture_mode": "stdout",
                    "output_limit_bytes": output_limit,
                    "timeout_seconds": timeout_seconds,
                    "writes": False,
                    "destructive": False,
                    "network": False,
                }
            ],
        }
        policy = RuntimePolicy(approvals={shell_approval(command)})
        return WorkflowRunner(workflow, self.workspace, None, policy).execute()

    def _rewrite_state(self, run, mutate):
        path = run.run_dir / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        mutate(state)
        state.pop("state_sha256", None)
        state["state_sha256"] = sha256_bytes(canonical_json_bytes(state))
        path.write_bytes(canonical_json_bytes(state))

    def _rewrite_result(self, run, record, mutate, *, canonical=True):
        path = run.run_dir / "results" / record["container_relative"]
        header, payload = results_module._decode_container(path.read_bytes())
        mutate(header)
        if canonical:
            header["record_sha256"] = results_module._record_hash(header)
            path.write_bytes(results_module._encode_container(header, payload))
            return
        rendered = json.dumps(header, indent=2, sort_keys=False).encode("utf-8")
        path.write_bytes(
            results_module.CONTAINER_MAGIC
            + ("%08x\n" % len(rendered)).encode("ascii")
            + rendered
            + payload
        )

    def test_process_capture_preserves_full_stream_behind_bounded_preview(self):
        capture_root = self.root / "captures"
        result = run_process(
            [sys.executable, "-c", "import sys;sys.stdout.write('a'*4096);sys.stderr.write('b'*3072)"],
            cwd=self.workspace,
            output_limit_bytes=64,
            capture_directory=capture_root,
            capture_limit_bytes=8192,
        )
        try:
            self.assertEqual(len(result.stdout.encode()), 64)
            self.assertEqual(len(result.stderr.encode()), 64)
            self.assertTrue(result.stdout_truncated)
            self.assertTrue(result.stderr_truncated)
            self.assertEqual(result.stdout_capture.total_bytes, 4096)
            self.assertEqual(result.stderr_capture.total_bytes, 3072)
            self.assertFalse(result.stdout_capture.truncated)
            self.assertFalse(result.stderr_capture.truncated)
            self.assertEqual(result.stdout_capture.handle.read(), b"a" * 4096)
            self.assertEqual(result.stderr_capture.handle.read(), b"b" * 3072)
        finally:
            discard_process_captures(result)
        self.assertEqual(list(capture_root.iterdir()), [])

    def test_capture_limit_is_explicit_and_does_not_stop_pipe_drainage(self):
        result = run_process(
            [sys.executable, "-c", "print('z'*4096, end='')"],
            cwd=self.workspace,
            output_limit_bytes=32,
            capture_directory=self.root / "captures",
            capture_limit_bytes=128,
        )
        try:
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout_capture.total_bytes, 4096)
            self.assertEqual(result.stdout_capture.stored_bytes, 128)
            self.assertTrue(result.stdout_capture.truncated)
        finally:
            discard_process_captures(result)

    def test_capture_limit_record_exposes_why_the_pipe_is_incomplete(self):
        workflow = {
            "schema": "conductor.core.workflow.v1",
            "name": "capture-limit-semantics",
            "steps": [
                {
                    "id": "inspect",
                    "kind": "write_artifact",
                    "output": "unused.txt",
                    "content": "unused",
                }
            ],
        }
        run = RunState.create(workflow, self.workspace, RuntimePolicy())
        run.transition_step("inspect", "running")
        result = run_process(
            [sys.executable, "-c", "print('z'*4096, end='')"],
            cwd=self.workspace,
            output_limit_bytes=32,
            capture_directory=run.run_dir / ".result-spool",
            capture_limit_bytes=128,
        )
        values = RunResultStore(run).preserve_process_overflow(
            result,
            source_id="inspect",
            step_id="inspect",
            attempt=1,
            preview_limit_bytes=32,
        )
        record = RunResultStore(run).get(values["stdout_result_id"])
        stored = RunResultStore(run).list_records()[0]
        self.assertFalse(record["pipe_complete"])
        self.assertTrue(stored["capture_state"]["capture_limit_reached"])
        self.assertTrue(stored["capture_state"]["reader_eof"])
        self.assertTrue(stored["capture_state"]["reader_finished"])
        self.assertFalse(stored["capture_state"]["redaction_limit_reached"])

    def test_invalid_result_metadata_releases_anonymous_capture(self):
        workflow = {
            "schema": "conductor.core.workflow.v1",
            "name": "invalid-result-metadata",
            "steps": [
                {
                    "id": "inspect",
                    "kind": "write_artifact",
                    "output": "unused.txt",
                    "content": "unused",
                }
            ],
        }
        run = RunState.create(workflow, self.workspace, RuntimePolicy())
        result = run_process(
            [sys.executable, "-c", "print('overflow'*512)"],
            cwd=self.workspace,
            output_limit_bytes=32,
            capture_directory=run.run_dir / ".result-spool",
        )
        self.assertGreater(process_module._capture_budget_used, 0)
        with self.assertRaisesRegex(ValidationError, "source id"):
            RunResultStore(run).preserve_process_overflow(
                result,
                source_id="bad/source",
                step_id="inspect",
                attempt=1,
                preview_limit_bytes=32,
            )
        self.assertEqual(process_module._capture_budget_used, 0)
        self.assertTrue(result.stdout_capture.handle.closed)

    def test_capture_is_lazy_and_optional_storage_faults_do_not_change_success(self):
        unused_root = self.root / "unused-capture"
        small = run_process(
            [sys.executable, "-c", "print('small')"],
            cwd=self.workspace,
            output_limit_bytes=1024,
            capture_directory=unused_root,
        )
        self.assertEqual(small.returncode, 0)
        self.assertIsNone(small.stdout_capture)
        self.assertIsNone(small.stdout_capture_unavailable)
        self.assertFalse(unused_root.exists())

        with patch.object(process_module.tempfile, "TemporaryFile", side_effect=OSError("full")):
            overflow = run_process(
                [sys.executable, "-c", "print('x'*4096)"],
                cwd=self.workspace,
                output_limit_bytes=32,
                capture_directory=self.root / "faulted-capture",
            )
        self.assertEqual(overflow.returncode, 0)
        self.assertTrue(overflow.stdout_truncated)
        self.assertIsNone(overflow.stdout_capture)
        self.assertEqual(overflow.stdout_capture_unavailable, "capture_storage_unavailable")

        with patch.object(process_module.os, "fsync", side_effect=OSError("full")):
            finalization_fault = run_process(
                [sys.executable, "-c", "print('f'*4096)"],
                cwd=self.workspace,
                output_limit_bytes=32,
                capture_directory=self.root / "finalization-fault",
            )
        self.assertEqual(finalization_fault.returncode, 0)
        self.assertEqual(
            finalization_fault.stdout_capture_unavailable,
            "capture_storage_unavailable",
        )
        self.assertEqual(process_module._capture_budget_used, 0)

        with patch.object(process_module, "MAX_TRANSIENT_CAPTURE_BYTES", 0):
            exhausted = run_process(
                [sys.executable, "-c", "print('y'*4096)"],
                cwd=self.workspace,
                output_limit_bytes=32,
                capture_directory=self.root / "budgeted-capture",
            )
        self.assertEqual(exhausted.returncode, 0)
        self.assertEqual(exhausted.stdout_capture_unavailable, "transient_budget_exhausted")

        limited = process_module._CaptureBuffer(self.root / "limited-capture", 128)
        with patch.object(
            process_module.os,
            "write",
            side_effect=OSError(errno.EFBIG, "file-size limit"),
        ):
            limited.append(b"overflow", overflow_prefix=b"")
        self.assertIsNone(limited.finish())
        self.assertEqual(limited.unavailable_reason, "capture_storage_unavailable")
        self.assertEqual(process_module._capture_budget_used, 0)

    def test_invalid_capture_target_is_rejected_before_process_launch(self):
        invalid_target = self.root / "capture-is-a-file"
        invalid_target.write_text("not a directory", encoding="utf-8")
        with patch.object(process_module.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValidationError, "capture directory"):
                run_process(
                    [sys.executable, "-c", "import time;time.sleep(30)"],
                    cwd=self.workspace,
                    capture_directory=invalid_target,
                )
        popen.assert_not_called()

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink verification is unavailable")
    def test_capture_integrity_faults_remain_fatal(self):
        target = self.root / "real-capture-root"
        target.mkdir()
        symlink = self.root / "late-capture-link"
        symlink.symlink_to(target, target_is_directory=True)
        raced = process_module._CaptureBuffer(symlink, 128)
        raced.append(b"overflow", overflow_prefix=b"")
        with self.assertRaisesRegex(ValidationError, "capture integrity"):
            raced.finish()

        changed = process_module._CaptureBuffer(self.root / "changed-capture", 128)
        changed.append(b"overflow", overflow_prefix=b"")
        changed.stored_bytes += 1
        with self.assertRaisesRegex(ValidationError, "capture size changed"):
            changed.finish((True, False, True))

        with patch.object(
            process_module.tempfile,
            "TemporaryFile",
            side_effect=OSError(errno.ELOOP, "path race"),
        ):
            with self.assertRaisesRegex(ValidationError, "capture integrity"):
                run_process(
                    [sys.executable, "-c", "print('overflow'*256)"],
                    cwd=self.workspace,
                    output_limit_bytes=32,
                    capture_directory=self.root / "raced-capture",
                )

        broken = process_module._CaptureBuffer(self.root / "broken-capture", 128)
        with patch.object(
            process_module.os,
            "write",
            side_effect=OSError(errno.EBADF, "broken descriptor"),
        ):
            broken.append(b"overflow", overflow_prefix=b"")
        with self.assertRaisesRegex(ValidationError, "capture integrity"):
            broken.finish()
        self.assertEqual(process_module._capture_budget_used, 0)

    def test_invalid_input_and_thread_start_failure_do_not_leak_a_child(self):
        with patch.object(process_module.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValidationError, "valid UTF-8"):
                run_process(
                    [sys.executable, "-c", "import time;time.sleep(30)"],
                    cwd=self.workspace,
                    input_text="\ud800",
                )
        popen.assert_not_called()

        launched = []
        real_popen = process_module.subprocess.Popen

        def tracked_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            launched.append(process)
            return process

        with patch.object(process_module.subprocess, "Popen", side_effect=tracked_popen):
            with patch.object(process_module.threading.Thread, "start", side_effect=RuntimeError("exhausted")):
                with self.assertRaisesRegex(ValidationError, "I/O worker failed"):
                    run_process(
                        [sys.executable, "-c", "import time;time.sleep(30)"],
                        cwd=self.workspace,
                        capture_directory=self.root / "thread-failure-capture",
                    )
        self.assertEqual(len(launched), 1)
        self.assertIsNotNone(launched[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(launched[0].pid, 0)
        self.assertEqual(process_module._capture_budget_used, 0)

    def test_shell_capture_storage_failure_is_explicit_but_nonfatal(self):
        command = [sys.executable, "-c", "print('optional'*256)"]
        with patch.object(process_module.tempfile, "TemporaryFile", side_effect=OSError("full")):
            run = self._run_shell(command, output_limit=32)
        self.assertEqual(run.state["status"], "completed")
        metrics = run.state["steps"]["inspect"]["metrics"]
        self.assertEqual(
            metrics["stdout_result_unavailable"],
            "capture_storage_unavailable",
        )
        self.assertNotIn("stdout_result_id", metrics)
        RunState.inspect(run.run_dir)

    def test_durable_result_storage_failure_is_explicit_but_nonfatal(self):
        command = [sys.executable, "-c", "print('durable'*256)"]
        with patch.object(results_module, "write_new_bytes", side_effect=OSError("disk full")):
            run = self._run_shell(command, output_limit=32)
        self.assertEqual(run.state["status"], "completed")
        metrics = run.state["steps"]["inspect"]["metrics"]
        self.assertEqual(
            metrics["stdout_result_unavailable"],
            "result_storage_unavailable",
        )
        self.assertNotIn("stdout_result_id", metrics)
        self.assertEqual(RunResultStore(run).list_records(), [])
        RunState.inspect(run.run_dir)

    def test_custom_metrics_named_like_results_remain_compatible(self):
        workflow = {
            "schema": "conductor.core.workflow.v1",
            "name": "custom-result-like-metrics",
            "steps": [
                {
                    "id": "record",
                    "kind": "write_artifact",
                    "output": "record.txt",
                    "content": "recorded",
                }
            ],
        }
        run = RunState.create(workflow, self.workspace, RuntimePolicy())
        run.transition_step("record", "running")
        run.transition_step(
            "record",
            "completed",
            metrics={
                "custom_result_id": "legacy-value",
                "custom_result_unavailable": "legacy-reason",
            },
        )
        RunState.inspect(run.run_dir)

    def test_parallel_capture_limits_fit_the_transient_budget(self):
        mebibyte = 1024 * 1024
        expected = {1: 32, 2: 32, 4: 32, 8: 16, 16: 8, 32: 4}
        for workers, expected_mib in expected.items():
            with self.subTest(workers=workers):
                limit = process_module.capture_limit_for_parallelism(workers)
                self.assertEqual(limit, expected_mib * mebibyte)
                self.assertLessEqual(
                    2 * workers * limit,
                    process_module.MAX_TRANSIENT_CAPTURE_BYTES,
                )

    def test_shell_overflow_is_hash_bound_redacted_and_retrievable(self):
        secret = "ghp_" + "q" * 36
        payload = "first\n" + ("x" * 512) + "\nlate-marker\n" + secret + "\n"
        command = [sys.executable, "-c", "print(%r, end='')" % payload]
        run = self._run_shell(command)

        self.assertEqual(run.state["status"], "completed")
        metrics = run.state["steps"]["inspect"]["metrics"]
        result_id = metrics["stdout_result_id"]
        self.assertEqual(len(run.read_artifact("preview.txt")), 96)

        store = RunResultStore(run)
        records = store.list_records()
        self.assertEqual([record["result_id"] for record in records], [result_id])
        self.assertEqual(records[0]["source"]["step_id"], "inspect")
        self.assertEqual(records[0]["source"]["stream"], "stdout")
        self.assertTrue(records[0]["pipe_complete"])
        self.assertEqual(
            records[0]["capture_state"],
            {
                "capture_limit_reached": False,
                "reader_eof": True,
                "reader_error": False,
                "reader_finished": True,
                "redaction_limit_reached": False,
            },
        )
        self.assertEqual(records[0]["producer_status"], "completed")
        self.assertEqual(records[0]["text_fidelity"], "utf8")

        excerpt = store.get(result_id, start_line=3, max_lines=2, max_bytes=4096)
        self.assertIn("late-marker", excerpt["text"])
        self.assertIn("<redacted-github-token>", excerpt["text"])
        self.assertNotIn(secret, excerpt["text"])
        self.assertEqual(excerpt["citation"], "%s:L3-L4" % result_id)

        matches = store.search("late-marker", result_ids=[result_id], max_matches=5)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["line"], 3)
        self.assertEqual(matches[0]["citation"], "%s:L3" % result_id)
        outline = store.outline(result_id, chunk_lines=2, max_chunks=10)
        self.assertEqual(outline[0]["start_line"], 1)
        self.assertEqual(outline[1]["start_line"], 3)

        result_tree = b"".join(
            path.read_bytes() for path in sorted((run.run_dir / "results").rglob("*")) if path.is_file()
        )
        self.assertNotIn(secret.encode(), result_tree)
        self.assertNotIn(str(self.workspace).encode(), result_tree)

    def test_result_line_operations_match_python_splitlines_semantics(self):
        edge_cases = [
            "",
            "plain",
            "\n",
            "\n\n",
            "plain\n",
            "plain\n\n",
            "\r\n",
            "left\r\nright",
            "left\v\fright\x1c\x1d\x1e\x85\u2028\u2029tail",
            "\x1f",
        ]
        for value in edge_cases:
            with self.subTest(value=repr(value)):
                self.assertEqual(list(results_module._iter_text_lines(value)), value.splitlines())

        payload = (
            "alpha\r\n"
            "\r"
            "beta\v"
            "(blank)\n"
            "replacement\n"
            "gamma\f"
            "\x1c"
            "delta\x1d"
            "epsilon\x1e"
            "zeta\x85"
            "eta\u2028"
            "theta\u2029"
            "omega\n\n"
        )
        command = [sys.executable, "-c", "import sys;sys.stdout.write(%r)" % payload]
        run = self._run_shell(command, output_limit=1)
        store = RunResultStore(run)
        record = store.list_records()[0]
        result_id = record["result_id"]
        expected_lines = payload.splitlines()

        self.assertEqual(record["line_count"], len(expected_lines))
        excerpt = store.get(result_id, max_lines=len(expected_lines), max_bytes=4096)
        self.assertEqual(excerpt["text"], "\n".join(expected_lines))
        self.assertEqual(excerpt["total_lines"], len(expected_lines))
        self.assertEqual(excerpt["end_line"], len(expected_lines))
        self.assertFalse(excerpt["truncated"])

        delta_line = expected_lines.index("delta") + 1
        self.assertEqual(
            store.search("delta", result_ids=[result_id]),
            [
                {
                    "result_id": result_id,
                    "line": delta_line,
                    "text": "delta",
                    "citation": "%s:L%d" % (result_id, delta_line),
                }
            ],
        )
        outline = store.outline(result_id, chunk_lines=3, max_chunks=20)
        self.assertEqual(
            [(chunk["start_line"], chunk["end_line"], chunk["label"]) for chunk in outline],
            [
                (
                    start + 1,
                    min(start + 3, len(expected_lines)),
                    next(
                        (
                            line.strip()
                            for line in expected_lines[start : start + 3]
                            if line.strip()
                        ),
                        "(blank)",
                    ),
                )
                for start in range(0, len(expected_lines), 3)
            ],
        )

    def test_dense_short_line_results_have_bounded_line_processing_memory(self):
        dense_bytes = 1024 * 1024
        command = [
            sys.executable,
            "-c",
            "import os;os.write(1, b'line\\n' * %d)" % (dense_bytes // 5),
        ]
        workflow = {
            "schema": "conductor.core.workflow.v1",
            "name": "dense-result-memory",
            "steps": [
                {
                    "id": "inspect",
                    "kind": "write_artifact",
                    "output": "unused.txt",
                    "content": "unused",
                }
            ],
        }
        run = RunState.create(workflow, self.workspace, RuntimePolicy())
        process = run_process(
            command,
            cwd=self.workspace,
            output_limit_bytes=32,
            capture_directory=run.run_dir / ".result-spool",
        )
        store = RunResultStore(run)

        def measured(call):
            tracemalloc.start()
            try:
                value = call()
                _current, peak = tracemalloc.get_traced_memory()
                return value, peak
            finally:
                tracemalloc.stop()

        values, store_peak = measured(
            lambda: store.preserve_process_overflow(
                process,
                source_id="inspect",
                step_id="inspect",
                attempt=1,
                preview_limit_bytes=32,
            )
        )
        result_id = values["stdout_result_id"]
        operations = {
            "store": store_peak,
            "verify": measured(store.verify)[1],
            "get": measured(lambda: store.get(result_id, max_lines=1))[1],
            "search": measured(
                lambda: store.search("absent", result_ids=[result_id])
            )[1],
            "outline": measured(
                lambda: store.outline(result_id, chunk_lines=1, max_chunks=1)
            )[1],
        }
        memory_limit = dense_bytes * 8
        self.assertEqual(store.list_records()[0]["line_count"], dense_bytes // 5)
        for operation, peak in operations.items():
            with self.subTest(operation=operation):
                self.assertLess(peak, memory_limit)

    def test_failed_shell_still_leaves_recoverable_bounded_diagnostics(self):
        command = [
            sys.executable,
            "-c",
            "import sys;print('p'*256);print('failure-tail');sys.exit(7)",
        ]
        run = self._run_shell(command, output_limit=32)
        self.assertEqual(run.state["status"], "failed")
        records = RunResultStore(run).list_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source"]["step_id"], "inspect")
        self.assertEqual(records[0]["producer_status"], "failed")
        self.assertEqual(records[0]["returncode"], 7)
        self.assertTrue(records[0]["pipe_complete"])
        self.assertEqual(
            run.state["steps"]["inspect"]["metrics"]["stdout_result_id"],
            records[0]["result_id"],
        )
        matches = RunResultStore(run).search("failure-tail", max_matches=5)
        self.assertEqual(len(matches), 1)
        (run.run_dir / "results" / records[0]["container_relative"]).unlink()
        with self.assertRaisesRegex(ValidationError, "missing result"):
            RunState.inspect(run.run_dir)

    def test_retry_start_drops_stale_attempt_result_references(self):
        run = self._run_shell(
            [
                sys.executable,
                "-c",
                "import sys;print('retry'*128);sys.exit(3)",
            ],
            output_limit=32,
        )
        first_record = run.state["steps"]["inspect"]
        self.assertEqual(first_record["attempt"], 1)
        self.assertIn("stdout_result_id", first_record["metrics"])

        run.transition_step("inspect", "running")
        self.assertEqual(run.state["steps"]["inspect"]["attempt"], 2)
        self.assertNotIn("metrics", run.state["steps"]["inspect"])
        RunState.inspect(run.run_dir)

        workflow = {
            "schema": "conductor.core.workflow.v1",
            "name": "packet-retry-result-references",
            "steps": [
                {
                    "id": "map",
                    "kind": "write_artifact",
                    "output": "unused.txt",
                    "content": "unused",
                }
            ],
        }
        packet_run = RunState.create(workflow, self.workspace, RuntimePolicy())
        packet_run.transition_step("map", "running")
        packet_process = run_process(
            [sys.executable, "-c", "print('packet'*128)"],
            cwd=self.workspace,
            output_limit_bytes=32,
            capture_directory=packet_run.run_dir / ".result-spool",
        )
        packet_metrics = RunResultStore(packet_run).preserve_process_overflow(
            packet_process,
            source_id="map-p0001",
            step_id="map",
            attempt=1,
            preview_limit_bytes=32,
        )
        packet_run.update_step(
            "map",
            packets={
                "0001": {
                    "status": "failed",
                    "error_class": "CodexInvocationError",
                    "resume_session_id": "resume-me",
                    "result_metrics": packet_metrics,
                }
            },
        )
        packet_run.transition_step("map", "failed", metrics=packet_metrics)
        RunState.inspect(packet_run.run_dir)
        packet_run.transition_step("map", "running")
        retried_packet = packet_run.state["steps"]["map"]["packets"]["0001"]
        self.assertNotIn("result_metrics", retried_packet)
        self.assertEqual(retried_packet["resume_session_id"], "resume-me")
        RunState.inspect(packet_run.run_dir)

    def test_timeout_and_invalid_utf8_do_not_overstate_result_semantics(self):
        timeout_command = [
            sys.executable,
            "-c",
            "import time;print('t'*256, flush=True);time.sleep(30)",
        ]
        timed_out = self._run_shell(timeout_command, output_limit=32, timeout_seconds=1)
        self.assertEqual(timed_out.state["status"], "failed")
        timeout_record = RunResultStore(timed_out).list_records()[0]
        self.assertEqual(timeout_record["producer_status"], "timed_out")
        self.assertTrue(timeout_record["timed_out"])

        binary_command = [
            sys.executable,
            "-c",
            "import os;os.write(1, b'a'*128 + b'\\xfflate-bytes\\n')",
        ]
        binary = self._run_shell(binary_command, output_limit=32)
        binary_record = RunResultStore(binary).list_records()[0]
        self.assertEqual(binary_record["text_fidelity"], "replacement-decoded")
        self.assertTrue(binary_record["pipe_complete"])
        self.assertEqual(len(RunResultStore(binary).search("late-bytes")), 1)

    def test_run_quota_fails_capture_explicitly_without_partial_container(self):
        command = [sys.executable, "-c", "print('q'*1024)"]
        with patch.object(results_module, "MAX_RUN_RESULT_BYTES", 1):
            run = self._run_shell(command, output_limit=32)
            self.assertEqual(run.state["status"], "completed")
            metrics = run.state["steps"]["inspect"]["metrics"]
            self.assertEqual(metrics["stdout_result_unavailable"], "run_quota_exceeded")
            self.assertNotIn("stdout_result_id", metrics)
            self.assertEqual(RunResultStore(run).list_records(), [])
            self.assertEqual(
                [path.name for path in (run.run_dir / "results").iterdir()],
                [".lock"],
            )
            RunState.inspect(run.run_dir)

    def test_result_tampering_and_identifier_injection_are_rejected(self):
        command = [sys.executable, "-c", "print('x'*256);print('tail')"]
        run = self._run_shell(command, output_limit=32)
        store = RunResultStore(run)
        record = store.list_records()[0]
        with self.assertRaisesRegex(ValidationError, "result id"):
            store.get("../../state.json")

        content = run.run_dir / "results" / record["container_relative"]
        content.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "container|size|hash"):
            store.get(record["result_id"])

    def test_result_header_semantics_are_derived_and_canonical(self):
        command = [sys.executable, "-c", "print('x'*256);print('tail')"]
        cases = {
            "line_count": (
                lambda header: header.__setitem__("line_count", header["line_count"] + 1),
                True,
                "line count",
            ),
            "identity": (
                lambda header: header.__setitem__(
                    "preview_limit_bytes", header["preview_limit_bytes"] + 1
                ),
                True,
                "identifier binding",
            ),
            "canonical_json": (lambda _header: None, False, "canonical JSON"),
        }
        for name, (mutate, canonical, expected) in cases.items():
            with self.subTest(name=name):
                run = self._run_shell(command, output_limit=32)
                store = RunResultStore(run)
                record = store.list_records()[0]
                self._rewrite_result(run, record, mutate, canonical=canonical)
                with self.assertRaisesRegex(ValidationError, expected):
                    store.get(record["result_id"])

    def test_state_result_references_bind_step_stream_and_attempt(self):
        command = [
            sys.executable,
            "-c",
            "import sys;print('o'*256);print('e'*256, file=sys.stderr)",
        ]
        workflow = {
            "schema": "conductor.core.workflow.v1",
            "name": "result-reference-binding",
            "steps": [
                {
                    "id": step_id,
                    "kind": "shell",
                    "command": command,
                    "output_limit_bytes": 32,
                    "writes": False,
                    "destructive": False,
                    "network": False,
                }
                for step_id in ("first", "second")
            ],
        }
        policy = RuntimePolicy(approvals={shell_approval(command)})

        def replace_step(state):
            state["steps"]["first"]["metrics"]["stdout_result_id"] = state["steps"][
                "second"
            ]["metrics"]["stdout_result_id"]

        def replace_stream(state):
            state["steps"]["first"]["metrics"]["stdout_result_id"] = state["steps"][
                "first"
            ]["metrics"]["stderr_result_id"]

        def replace_attempt(state):
            state["steps"]["first"]["attempt"] += 1

        for name, mutate in {
            "step": replace_step,
            "stream": replace_stream,
            "attempt": replace_attempt,
        }.items():
            with self.subTest(name=name):
                run = WorkflowRunner(workflow, self.workspace, None, policy).execute()
                self.assertEqual(run.state["status"], "completed")
                self._rewrite_state(run, mutate)
                with self.assertRaisesRegex(ValidationError, "reference binding"):
                    RunState.inspect(run.run_dir)

    def test_map_result_references_are_bound_to_packet_identity(self):
        workflow = {
            "schema": "conductor.core.workflow.v1",
            "name": "packet-result-reference-binding",
            "steps": [
                {
                    "id": "map",
                    "kind": "write_artifact",
                    "output": "unused.txt",
                    "content": "unused",
                }
            ],
        }
        run = RunState.create(workflow, self.workspace, RuntimePolicy())
        run.transition_step("map", "running")
        packets = {}
        for packet_id, marker in (("0001", "first"), ("0002", "second")):
            process = run_process(
                [sys.executable, "-c", "print(%r*128)" % marker],
                cwd=self.workspace,
                output_limit_bytes=32,
                capture_directory=run.run_dir / ".result-spool",
            )
            metrics = RunResultStore(run).preserve_process_overflow(
                process,
                source_id="map-p%s" % packet_id,
                step_id="map",
                attempt=1,
                preview_limit_bytes=32,
            )
            packets[packet_id] = {
                "status": "failed",
                "error_class": "PacketFailure",
                "result_metrics": metrics,
            }
        run.update_step("map", packets=packets)
        run.transition_step("map", "failed")
        RunState.inspect(run.run_dir)

        def swap_packet_results(state):
            first = state["steps"]["map"]["packets"]["0001"]["result_metrics"]
            second = state["steps"]["map"]["packets"]["0002"]["result_metrics"]
            first["stdout_result_id"], second["stdout_result_id"] = (
                second["stdout_result_id"],
                first["stdout_result_id"],
            )

        self._rewrite_state(run, swap_packet_results)
        with self.assertRaisesRegex(ValidationError, "reference binding"):
            RunState.inspect(run.run_dir)

    def test_result_binding_rejects_cross_run_copy_and_hardlinks(self):
        command = [sys.executable, "-c", "print('x'*256);print('bound-tail')"]
        first = self._run_shell(command, output_limit=32)
        first_store = RunResultStore(first)
        record = first_store.list_records()[0]
        container = first.run_dir / "results" / record["container_relative"]

        interrupted_temporary = container.with_name(".%s.interrupted" % container.name)
        os.link(container, interrupted_temporary)
        self.assertEqual(len(first_store.list_records()), 1)
        self.assertTrue(interrupted_temporary.exists())

        recovery_process = run_process(
            [sys.executable, "-c", "print('recovery'*64)"],
            cwd=self.workspace,
            output_limit_bytes=32,
            capture_directory=first.run_dir / ".result-spool",
        )
        first_store.preserve_process_overflow(
            recovery_process,
            source_id="inspect-recovery",
            step_id="inspect",
            attempt=1,
            preview_limit_bytes=32,
        )
        self.assertFalse(interrupted_temporary.exists())

        hardlink = self.root / "hardlinked-result"
        os.link(container, hardlink)
        with self.assertRaisesRegex(ValidationError, "container"):
            first_store.get(record["result_id"])
        hardlink.unlink()

        second = self._run_shell([sys.executable, "-c", "print('small')"], output_limit=1024)
        second_root = second.run_dir / "results"
        second_root.mkdir()
        (second_root / ".lock").write_bytes(b"")
        shutil.copy2(container, second_root / container.name)
        with self.assertRaisesRegex(ValidationError, "binding"):
            RunResultStore(second).list_records()
        with self.assertRaisesRegex(ValidationError, "binding"):
            RunState.inspect(second.run_dir)

    def test_empty_crash_residue_and_read_only_result_inspection_are_supported(self):
        legacy = self._run_shell(
            [sys.executable, "-c", "print('small')"],
            output_limit=1024,
        )
        empty_root = legacy.run_dir / "results"
        empty_root.mkdir()
        RunState.inspect(legacy.run_dir)
        self.assertEqual(list(empty_root.iterdir()), [])

        captured = self._run_shell(
            [sys.executable, "-c", "print('read-only'*128)"],
            output_limit=32,
        )
        store = RunResultStore(captured)
        record = store.list_records()[0]
        container = captured.run_dir / "results" / record["container_relative"]
        interrupted = container.with_name(".%s.interrupted" % container.name)
        os.link(container, interrupted)
        lock_path = captured.run_dir / "results" / ".lock"
        result_root = captured.run_dir / "results"
        try:
            container.chmod(0o400)
            interrupted.chmod(0o400)
            lock_path.chmod(0o400)
            result_root.chmod(0o500)
            inspected = RunState.inspect(captured.run_dir)
            self.assertEqual(
                RunResultStore(inspected).list_records()[0]["result_id"],
                record["result_id"],
            )
            self.assertTrue(interrupted.exists())
        finally:
            result_root.chmod(0o700)
            lock_path.chmod(0o600)
            container.chmod(0o600)
            interrupted.chmod(0o600)
            interrupted.unlink()

    def test_result_lock_replacement_cannot_create_overlapping_processes(self):
        run = self._run_shell(
            [sys.executable, "-c", "print('locked'*128)"],
            output_limit=32,
        )
        first_store = RunResultStore(run)
        lock_path = run.run_dir / "results" / ".lock"
        displaced = run.run_dir / "displaced-result-lock"
        script = """
import sys
from pathlib import Path
from conductor_runtime.core.results import RunResultStore
store = RunResultStore.inspect(Path(sys.argv[1]))
with store._locked(create=True):
    print('entered', flush=True)
"""
        child = None
        with first_store._locked(create=True):
            lock_path.rename(displaced)
            lock_path.write_bytes(b"")
            child = subprocess.Popen(
                [sys.executable, "-c", script, str(run.run_dir)],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertIsNone(child.poll())
            with self.assertRaises(subprocess.TimeoutExpired):
                child.communicate(timeout=0.2)
        stdout, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), "entered")
        lock_path.unlink()
        displaced.rename(lock_path)

    def test_parallel_result_writers_are_serialized_and_no_clobber(self):
        workflow = {
            "schema": "conductor.core.workflow.v1",
            "name": "parallel-result-writes",
            "steps": [
                {"id": "first", "kind": "write_artifact", "output": "first.txt", "content": "one"},
                {"id": "second", "kind": "write_artifact", "output": "second.txt", "content": "two"},
            ],
        }
        run = RunState.create(workflow, self.workspace, RuntimePolicy())
        captures = [
            run_process(
                [sys.executable, "-c", "print(%r*512)" % marker],
                cwd=self.workspace,
                output_limit_bytes=32,
                capture_directory=run.run_dir / ".result-spool",
            )
            for marker in ("a", "b")
        ]
        store = RunResultStore(run)

        def preserve(index):
            step_id = ("first", "second")[index]
            return store.preserve_process_overflow(
                captures[index],
                source_id=step_id,
                step_id=step_id,
                attempt=1,
                preview_limit_bytes=32,
            )

        active_redactions = 0
        maximum_redactions = 0
        redaction_lock = threading.Lock()
        original_redact = results_module.redact_text

        def observed_redact(value):
            nonlocal active_redactions, maximum_redactions
            with redaction_lock:
                active_redactions += 1
                maximum_redactions = max(maximum_redactions, active_redactions)
            try:
                return original_redact(value)
            finally:
                with redaction_lock:
                    active_redactions -= 1

        with patch.object(results_module, "redact_text", side_effect=observed_redact):
            with ThreadPoolExecutor(max_workers=2) as executor:
                values = list(executor.map(preserve, range(2)))
        self.assertEqual(len({value["stdout_result_id"] for value in values}), 2)
        self.assertEqual(len(store.list_records()), 2)
        self.assertEqual(maximum_redactions, 1)

    def test_results_cli_returns_citations_without_host_paths(self):
        command = [sys.executable, "-c", "print('x'*256);print('needle-at-end')"]
        run = self._run_shell(command, output_limit=32)
        result_id = RunResultStore(run).list_records()[0]["result_id"]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            returncode = main(
                [
                    "results",
                    "search",
                    str(run.run_dir),
                    "--query",
                    "needle-at-end",
                    "--json",
                ]
            )
        self.assertEqual(returncode, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload[0]["citation"], "%s:L2" % result_id)
        self.assertNotIn(str(self.workspace), stdout.getvalue())

    def test_results_cli_bounds_controls_filters_and_failure_handoff(self):
        command = [
            sys.executable,
            "-c",
            "import os;os.write(1, b'x'*128 + b'\\x1b]0;unsafe\\x07needle\\n')",
        ]
        run = self._run_shell(command, output_limit=32)
        record = RunResultStore(run).list_records()[0]
        result_id = record["result_id"]

        listed = io.StringIO()
        with contextlib.redirect_stdout(listed):
            self.assertEqual(
                main(["results", "list", str(run.run_dir), "--step", "inspect", "--json"]),
                0,
            )
        self.assertEqual(json.loads(listed.getvalue())[0]["result_id"], result_id)

        rendered = io.StringIO()
        with contextlib.redirect_stdout(rendered):
            self.assertEqual(
                main(["results", "search", str(run.run_dir), "--query", "needle"]),
                0,
            )
        self.assertNotIn("\x1b", rendered.getvalue())
        self.assertNotIn("\x07", rendered.getvalue())
        self.assertIn("\\x1b", rendered.getvalue())
        self.assertIn("\\x07", rendered.getvalue())

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(
                main(
                    [
                        "results",
                        "get",
                        str(run.run_dir),
                        result_id,
                        "--start-line",
                        "999",
                    ]
                ),
                2,
            )
        self.assertIn("start line exceeds", stderr.getvalue())

        failure = self._run_shell(
            [sys.executable, "-c", "import sys;print('f'*256);sys.exit(4)"],
            output_limit=32,
        )
        handoff = io.StringIO()
        with contextlib.redirect_stdout(handoff):
            _print_run_detail(failure)
        failure_id = RunResultStore(failure).list_records()[0]["result_id"]
        self.assertIn(failure_id, handoff.getvalue())
        self.assertIn("conductor-runtime results get", handoff.getvalue())


if __name__ == "__main__":
    unittest.main()
