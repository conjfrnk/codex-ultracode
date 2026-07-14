import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from conductor_extras.runtime.codex_native_usage import (
    MAX_NATIVE_USAGE_SESSIONS,
    NativeUsageUnavailable,
    empty_native_usage,
    reconcile_codex_native_usage,
    validate_native_usage,
)
from conductor_runtime.errors import ValidationError


PARENT = "00000000-0000-4000-8000-000000000001"
CHILD = "00000000-0000-4000-8000-000000000002"
GRANDCHILD = "00000000-0000-4000-8000-000000000003"
PRIVATE_SENTINEL = "native-rollout-private-sentinel"


class CodexNativeUsageTests(unittest.TestCase):
    def test_reconciles_parent_and_child_without_retaining_private_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            home, database = self._state(root)
            parent_path = self._rollout(home, PARENT, [(100, 40, 10), (220, 100, 20)])
            child_path = self._rollout(home, CHILD, [(80, 30, 8), (120, 70, 12)])
            self._thread(database, PARENT, parent_path, workspace, 240)
            self._thread(database, CHILD, child_path, workspace, 132)
            self._edge(database, PARENT, CHILD)

            result = reconcile_codex_native_usage(PARENT, workspace, codex_home=home)
            self.assertEqual(
                result,
                {
                    "status": "complete",
                    "session_count": 2,
                    "child_count": 1,
                    "input_tokens": 340,
                    "cached_input_tokens": 170,
                    "output_tokens": 32,
                    "total_tokens": 372,
                    "rollout_tokens": 202,
                },
            )
            validate_native_usage(result)
            self.assertNotIn(PRIVATE_SENTINEL, json.dumps(result, sort_keys=True))

    def test_no_child_is_a_complete_attributed_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            home, database = self._state(root)
            path = self._rollout(home, PARENT, [(50, 10, 5)])
            self._thread(database, PARENT, path, workspace, 55)
            result = reconcile_codex_native_usage(PARENT, workspace, codex_home=home)
            self.assertEqual(result["session_count"], 1)
            self.assertEqual(result["child_count"], 0)
            self.assertEqual(result["rollout_tokens"], 45)

    def test_rejects_index_mismatch_depth_escape_and_rollout_escape(self):
        scenarios = ("token-mismatch", "grandchild", "outside")
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workspace = root / "workspace"
                workspace.mkdir()
                home, database = self._state(root)
                parent_path = self._rollout(home, PARENT, [(50, 10, 5)])
                child_path = self._rollout(home, CHILD, [(40, 20, 4)])
                if scenario == "outside":
                    child_path = root / ("outside-%s.jsonl" % CHILD)
                    self._write_rollout(child_path, [(40, 20, 4)])
                self._thread(database, PARENT, parent_path, workspace, 55)
                self._thread(
                    database,
                    CHILD,
                    child_path,
                    workspace,
                    99 if scenario == "token-mismatch" else 44,
                )
                self._edge(database, PARENT, CHILD)
                if scenario == "grandchild":
                    grandchild_path = self._rollout(home, GRANDCHILD, [(20, 10, 2)])
                    self._thread(database, GRANDCHILD, grandchild_path, workspace, 22)
                    self._edge(database, CHILD, GRANDCHILD)
                with self.assertRaises(NativeUsageUnavailable):
                    reconcile_codex_native_usage(PARENT, workspace, codex_home=home)

    def test_rejects_symlinked_rollout_and_wrong_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            other = root / "other"
            workspace.mkdir()
            other.mkdir()
            home, database = self._state(root)
            real = root / ("real-%s.jsonl" % PARENT)
            self._write_rollout(real, [(30, 5, 3)])
            linked = home / "sessions" / ("rollout-%s.jsonl" % PARENT)
            try:
                linked.symlink_to(real)
            except OSError:
                self.skipTest("symlinks are not supported")
            self._thread(database, PARENT, linked, workspace, 33)
            with self.assertRaises(NativeUsageUnavailable):
                reconcile_codex_native_usage(PARENT, workspace, codex_home=home)

            linked.unlink()
            path = self._rollout(home, PARENT, [(30, 5, 3)])
            connection = sqlite3.connect(database)
            connection.execute("UPDATE threads SET rollout_path = ?, cwd = ? WHERE id = ?", (str(path), str(other), PARENT))
            connection.commit()
            connection.close()
            with self.assertRaises(NativeUsageUnavailable):
                reconcile_codex_native_usage(PARENT, workspace, codex_home=home)

    def test_rejects_malformed_latest_usage_and_excess_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            home, database = self._state(root)
            path = home / "sessions" / ("rollout-%s.jsonl" % PARENT)
            path.write_text(
                '{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":1,"input_tokens":2,"cached_input_tokens":0,"output_tokens":0,"total_tokens":2}}}}\n',
                encoding="utf-8",
            )
            self._thread(database, PARENT, path, workspace, 2)
            with self.assertRaises(NativeUsageUnavailable):
                reconcile_codex_native_usage(PARENT, workspace, codex_home=home)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            home, database = self._state(root)
            path = self._rollout(home, PARENT, [(10, 0, 1)])
            self._thread(database, PARENT, path, workspace, 11)
            connection = sqlite3.connect(database)
            for index in range(MAX_NATIVE_USAGE_SESSIONS):
                child = "00000000-0000-4000-8%03d-%012d" % (index, index + 10)
                connection.execute(
                    "INSERT INTO thread_spawn_edges(parent_thread_id, child_thread_id, status) VALUES (?, ?, 'open')",
                    (PARENT, child),
                )
            connection.commit()
            connection.close()
            with self.assertRaises(NativeUsageUnavailable):
                reconcile_codex_native_usage(PARENT, workspace, codex_home=home)

    def test_contract_rejects_inconsistent_values(self):
        validate_native_usage(empty_native_usage("not-requested"))
        validate_native_usage(empty_native_usage("unavailable"))
        with self.assertRaises(ValidationError):
            empty_native_usage("complete")
        invalid = empty_native_usage("unavailable")
        invalid["total_tokens"] = 1
        with self.assertRaises(ValidationError):
            validate_native_usage(invalid)

    @staticmethod
    def _state(root):
        home = root / "codex-home"
        (home / "sessions").mkdir(parents=True)
        database = home / "state_5.sqlite"
        connection = sqlite3.connect(database)
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                cwd TEXT NOT NULL,
                tokens_used INTEGER NOT NULL
            );
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT NOT NULL,
                child_thread_id TEXT NOT NULL PRIMARY KEY,
                status TEXT NOT NULL
            );
            """
        )
        connection.close()
        return home, database

    def _rollout(self, home, session_id, usages):
        path = home / "sessions" / ("rollout-%s.jsonl" % session_id)
        self._write_rollout(path, usages)
        return path

    @staticmethod
    def _write_rollout(path, usages):
        rows = [
            {"type": "response_item", "payload": {"type": "message", "content": PRIVATE_SENTINEL}}
        ]
        for input_tokens, cached_input_tokens, output_tokens in usages:
            rows.append(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": input_tokens,
                                "cached_input_tokens": cached_input_tokens,
                                "output_tokens": output_tokens,
                                "reasoning_output_tokens": 0,
                                "total_tokens": input_tokens + output_tokens,
                            }
                        },
                    },
                }
            )
            rows.append({"type": "response_item", "payload": {"content": PRIVATE_SENTINEL}})
        path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")

    @staticmethod
    def _thread(database, session_id, rollout_path, workspace, tokens_used):
        connection = sqlite3.connect(database)
        connection.execute(
            "INSERT INTO threads(id, rollout_path, cwd, tokens_used) VALUES (?, ?, ?, ?)",
            (session_id, str(rollout_path), str(workspace.resolve()), tokens_used),
        )
        connection.commit()
        connection.close()

    @staticmethod
    def _edge(database, parent, child):
        connection = sqlite3.connect(database)
        connection.execute(
            "INSERT INTO thread_spawn_edges(parent_thread_id, child_thread_id, status) VALUES (?, ?, 'open')",
            (parent, child),
        )
        connection.commit()
        connection.close()


if __name__ == "__main__":
    unittest.main()
