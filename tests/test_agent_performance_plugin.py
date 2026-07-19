import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENT_PERFORMANCE_RELATIVE = Path("plugins") / "agent-performance"
AGENT_PERFORMANCE_SKILLS = {
    "benchmark-agent-configs",
    "change-contract",
    "debug-from-evidence",
    "review-code-changes",
    "simplify-without-drift",
    "verify-before-claiming",
    "write-behavioral-tests",
}
ULTRACODE_READY_MESSAGE = (
    "Codex with Ultracode — Conductor orchestration + specialist agent workflows ready."
)
ULTRACODE_NO_CONDUCTOR_MESSAGE = (
    "Codex with Ultracode — specialist agent workflows ready (Conductor not detected)."
)


class AgentPerformancePluginTests(unittest.TestCase):
    def setUp(self):
        self.plugin_root = PROJECT_ROOT / AGENT_PERFORMANCE_RELATIVE
        self.marketplace_path = PROJECT_ROOT / ".agents" / "plugins" / "marketplace.json"
        self.benchmark_root = self.plugin_root / "skills" / "benchmark-agent-configs"
        self.benchmark_assets = self.benchmark_root / "assets"
        self.benchmark_validator = self.benchmark_root / "scripts" / "validate_packet.py"

    def _materialize(self, value):
        if isinstance(value, str):
            return value.replace("replace-me", "filled")
        if isinstance(value, list):
            return [self._materialize(item) for item in value]
        if isinstance(value, dict):
            return {key: self._materialize(item) for key, item in value.items()}
        return value

    @staticmethod
    def _write_json(path, value):
        payload = (json.dumps(value, indent=2) + "\n").encode("utf-8")
        path.write_bytes(payload)
        return payload

    @staticmethod
    def _frontmatter(path: Path):
        text = path.read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        if match is None:
            raise AssertionError("missing YAML frontmatter: %s" % path)
        fields = {}
        for line in match.group(1).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
        return fields, text

    def test_source_plugin_contract_is_complete_and_self_contained(self):
        marketplace = json.loads(self.marketplace_path.read_text(encoding="utf-8"))
        self.assertEqual(marketplace["name"], "codex-ultracode-local")
        entry = next(item for item in marketplace["plugins"] if item["name"] == "agent-performance")
        self.assertEqual(entry["source"], {"source": "local", "path": "./plugins/agent-performance"})
        self.assertEqual(
            entry["policy"],
            {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        )

        manifest = json.loads(
            (self.plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["name"], self.plugin_root.name)
        self.assertEqual(manifest["license"], "MIT")
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(manifest["author"]["name"], "Codex Ultracode Project")
        self.assertEqual(manifest["interface"]["developerName"], "Codex Ultracode Project")
        self.assertNotIn("apps", manifest)
        self.assertNotIn("mcpServers", manifest)
        self.assertNotIn("hooks", manifest)
        self.assertEqual(
            (self.plugin_root / "LICENSE").read_bytes(),
            (PROJECT_ROOT / "LICENSE").read_bytes(),
        )

        hooks = json.loads(
            (self.plugin_root / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(hooks), {"description", "hooks"})
        self.assertEqual(set(hooks["hooks"]), {"SessionStart"})
        startup = hooks["hooks"]["SessionStart"]
        self.assertEqual(len(startup), 1)
        self.assertEqual(startup[0]["matcher"], "^startup$")
        self.assertEqual(
            startup[0]["hooks"],
            [
                {
                    "type": "command",
                    "command": 'python3 "$PLUGIN_ROOT/hooks/session_start.py"',
                    "timeout": 5,
                    "statusMessage": "Starting Codex with Ultracode",
                }
            ],
        )

        skill_roots = sorted((self.plugin_root / "skills").iterdir())
        self.assertEqual({path.name for path in skill_roots}, AGENT_PERFORMANCE_SKILLS)
        for skill_root in skill_roots:
            self.assertTrue(skill_root.is_dir())
            fields, skill_text = self._frontmatter(skill_root / "SKILL.md")
            self.assertEqual(fields.get("name"), skill_root.name)
            self.assertTrue(fields.get("description"))

            agent_text = (skill_root / "agents" / "openai.yaml").read_text(encoding="utf-8")
            self.assertIn("$%s" % skill_root.name, agent_text)
            self.assertRegex(agent_text, r"(?m)^\s+allow_implicit_invocation: (true|false)$")

            for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", skill_text):
                if "://" not in target and not target.startswith("#"):
                    self.assertTrue((skill_root / target).is_file(), "%s -> %s" % (skill_root, target))
            for resource_dir_name in ("assets", "references", "scripts"):
                resource_dir = skill_root / resource_dir_name
                if resource_dir.is_dir():
                    for resource in resource_dir.rglob("*"):
                        if resource.is_file() and "__pycache__" not in resource.parts:
                            relative = resource.relative_to(skill_root).as_posix()
                            self.assertIn(relative, skill_text, "unreferenced skill resource: %s" % relative)

    def test_startup_hook_reports_integrated_capabilities_without_overclaiming(self):
        hook = self.plugin_root / "hooks" / "session_start.py"

        def run_hook(codex_home: Path, source="startup", stdin=None, launcher_shown=False):
            payload = (
                json.dumps(
                    {
                        "session_id": "session-1",
                        "cwd": str(PROJECT_ROOT),
                        "hook_event_name": "SessionStart",
                        "source": source,
                    }
                )
                if stdin is None
                else stdin
            )
            environment = dict(os.environ)
            environment["CODEX_HOME"] = str(codex_home)
            if launcher_shown:
                environment["CODEX_ULTRACODE_STARTUP_SHOWN"] = "1"
            else:
                environment.pop("CODEX_ULTRACODE_STARTUP_SHOWN", None)
            return subprocess.run(
                [sys.executable, str(hook)],
                input=payload,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )

        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()

            without_conductor = run_hook(codex_home)
            self.assertEqual(without_conductor.returncode, 0, without_conductor.stderr)
            self.assertEqual(
                json.loads(without_conductor.stdout),
                {"continue": True, "systemMessage": ULTRACODE_NO_CONDUCTOR_MESSAGE},
            )

            conductor_skill = codex_home / "skills" / "codex-conductor" / "SKILL.md"
            conductor_skill.parent.mkdir(parents=True)
            conductor_skill.write_text("# Codex Conductor\n", encoding="utf-8")
            with_conductor = run_hook(codex_home)
            self.assertEqual(with_conductor.returncode, 0, with_conductor.stderr)
            self.assertEqual(
                json.loads(with_conductor.stdout),
                {"continue": True, "systemMessage": ULTRACODE_READY_MESSAGE},
            )

            config_path = codex_home / "config.toml"
            config_path.write_text(
                '[[skills.config]]\npath = "%s"\nenabled = false\n'
                % conductor_skill.as_posix(),
                encoding="utf-8",
            )
            with_disabled_skill = run_hook(codex_home)
            self.assertEqual(with_disabled_skill.returncode, 0, with_disabled_skill.stderr)
            self.assertEqual(
                json.loads(with_disabled_skill.stdout),
                {"continue": True, "systemMessage": ULTRACODE_NO_CONDUCTOR_MESSAGE},
            )

            conductor_skill.unlink()
            config_path.unlink()
            conductor_runtime = codex_home / "conductor" / "bin" / "conductor-runtime.pyz"
            conductor_runtime.parent.mkdir(parents=True)
            conductor_runtime.write_bytes(b"runtime only")
            with_runtime_only = run_hook(codex_home)
            self.assertEqual(with_runtime_only.returncode, 0, with_runtime_only.stderr)
            self.assertEqual(
                json.loads(with_runtime_only.stdout),
                {"continue": True, "systemMessage": ULTRACODE_NO_CONDUCTOR_MESSAGE},
            )
            conductor_runtime.unlink()

            config_path.write_text(
                '[plugins."codex-conductor@team-local"]\nenabled = true\n',
                encoding="utf-8",
            )
            with_missing_plugin = run_hook(codex_home)
            self.assertEqual(with_missing_plugin.returncode, 0, with_missing_plugin.stderr)
            self.assertEqual(
                json.loads(with_missing_plugin.stdout),
                {"continue": True, "systemMessage": ULTRACODE_NO_CONDUCTOR_MESSAGE},
            )

            plugin_skill = (
                codex_home
                / "plugins"
                / "cache"
                / "team-local"
                / "codex-conductor"
                / "1.0.0"
                / "skills"
                / "codex-conductor"
                / "SKILL.md"
            )
            plugin_skill.parent.mkdir(parents=True)
            plugin_skill.write_text("# Plugin Codex Conductor\n", encoding="utf-8")
            with_enabled_plugin = run_hook(codex_home)
            self.assertEqual(with_enabled_plugin.returncode, 0, with_enabled_plugin.stderr)
            self.assertEqual(
                json.loads(with_enabled_plugin.stdout),
                {"continue": True, "systemMessage": ULTRACODE_READY_MESSAGE},
            )

            config_path.write_text(
                '[plugins."codex-conductor@team-local"]\n'
                "enabled = true\n"
                "[[skills.config]]\n"
                'path = "%s"\n'
                "enabled = false\n" % plugin_skill.as_posix(),
                encoding="utf-8",
            )
            with_disabled_plugin_skill = run_hook(codex_home)
            self.assertEqual(
                with_disabled_plugin_skill.returncode,
                0,
                with_disabled_plugin_skill.stderr,
            )
            self.assertEqual(
                json.loads(with_disabled_plugin_skill.stdout),
                {"continue": True, "systemMessage": ULTRACODE_NO_CONDUCTOR_MESSAGE},
            )

            config_path.write_text(
                '[plugins."codex-conductor@team-local"]\nenabled = false\n',
                encoding="utf-8",
            )
            with_disabled_plugin = run_hook(codex_home)
            self.assertEqual(with_disabled_plugin.returncode, 0, with_disabled_plugin.stderr)
            self.assertEqual(
                json.loads(with_disabled_plugin.stdout),
                {"continue": True, "systemMessage": ULTRACODE_NO_CONDUCTOR_MESSAGE},
            )

            resumed = run_hook(codex_home, source="resume")
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual(resumed.stdout, "")

            already_announced = run_hook(codex_home, launcher_shown=True)
            self.assertEqual(already_announced.returncode, 0, already_announced.stderr)
            self.assertEqual(already_announced.stdout, "")

            malformed = run_hook(codex_home, stdin="not json")
            self.assertEqual(malformed.returncode, 0, malformed.stderr)
            self.assertEqual(malformed.stdout, "")

    def test_zsh_launcher_announces_only_bare_interactive_codex(self):
        launcher = self.plugin_root / "scripts" / "codex-ultracode.zsh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_codex = fake_bin / "codex"
            fake_codex.write_text(
                "#!/bin/sh\n"
                "printf 'marker=%s\\n' \"${CODEX_ULTRACODE_STARTUP_SHOWN-}\"\n"
                "printf 'args=%s\\n' \"$*\"\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            environment = dict(os.environ)
            environment["PATH"] = "%s%s%s" % (fake_bin, os.pathsep, environment["PATH"])
            codex_home = root / "codex-home"
            codex_home.mkdir()
            environment["CODEX_HOME"] = str(codex_home)
            environment.pop("CODEX_ULTRACODE_STARTUP_SHOWN", None)

            def run_launcher(command):
                return subprocess.run(
                    ["zsh", "-f"],
                    input='source "%s"\n%s\n' % (launcher, command),
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=5,
                )

            without_conductor = run_launcher("codex")
            self.assertEqual(without_conductor.returncode, 0, without_conductor.stderr)
            self.assertEqual(
                without_conductor.stdout.splitlines(),
                [ULTRACODE_NO_CONDUCTOR_MESSAGE, "marker=1", "args="],
            )

            conductor_skill = codex_home / "skills" / "codex-conductor" / "SKILL.md"
            conductor_skill.parent.mkdir(parents=True)
            conductor_skill.write_text("# Codex Conductor\n", encoding="utf-8")
            with_conductor = run_launcher("codex")
            self.assertEqual(with_conductor.returncode, 0, with_conductor.stderr)
            self.assertEqual(
                with_conductor.stdout.splitlines(),
                [ULTRACODE_READY_MESSAGE, "marker=1", "args="],
            )

            delegated = run_launcher("codex --version")
            self.assertEqual(delegated.returncode, 0, delegated.stderr)
            self.assertEqual(delegated.stdout.splitlines(), ["marker=", "args=--version"])

    def test_benchmark_templates_bind_judgment_and_analysis_rules(self):
        assets = self.benchmark_assets
        controller = json.loads((assets / "controller-manifest.json").read_text(encoding="utf-8"))
        evaluator = json.loads((assets / "evaluator-brief.json").read_text(encoding="utf-8"))
        public_task = json.loads((assets / "public-task.json").read_text(encoding="utf-8"))
        run_record = json.loads((assets / "run-record.json").read_text(encoding="utf-8"))
        judgment = json.loads((assets / "judgment-record.json").read_text(encoding="utf-8"))
        report = json.loads((assets / "benchmark-report.json").read_text(encoding="utf-8"))

        expected_outcomes = [
            "candidate_x_wins",
            "candidate_y_wins",
            "tie",
            "neither_acceptable",
            "inconclusive",
        ]
        self.assertEqual(
            controller["comparison"]["safety_vetoes"],
            evaluator["safety_vetoes"],
        )
        self.assertTrue(evaluator["acceptance_rule"]["summary"])
        self.assertEqual(evaluator["outcome_states"], expected_outcomes)
        self.assertTrue(evaluator["decision_rule"]["vetoes_are_non_compensatory"])
        self.assertTrue(evaluator["decision_rule"]["result_is_scored_before_process"])

        execution = controller["execution"]
        self.assertGreater(execution["replicates_per_candidate_task"], 1)
        self.assertEqual(execution["analysis_unit"], "task")
        self.assertTrue(execution["replicate_aggregation"])
        self.assertEqual(
            set(execution["failure_policy"]),
            {"candidate_failure", "timeout", "over_budget", "infrastructure_failure"},
        )
        claim = controller["comparison"]["claim"]
        self.assertIn(claim["type"], {"superiority", "non_inferiority", "equivalence", "descriptive"})
        self.assertTrue(claim["estimand"])
        self.assertEqual(
            {claim["target_candidate_id"], claim["comparator_candidate_id"]},
            {candidate["candidate_id"] for candidate in controller["candidates"]},
        )
        self.assertTrue(controller["analysis"]["uncertainty_method"])
        self.assertTrue(controller["blinding"]["assignment_method"])
        self.assertTrue(controller["blinding"]["assignment_seed"])

        self.assertNotIn("source", public_task)
        self.assertNotIn("provenance", public_task)
        self.assertEqual(run_record["analysis_unit"], "candidate_task_replicate")
        self.assertEqual(judgment["outcome"], "inconclusive")
        self.assertEqual(report["paired_outcomes"]["inconclusive"], 0)
        candidate_ids = [candidate["candidate_id"] for candidate in controller["candidates"]]
        self.assertEqual(list(report["paired_outcomes"]["candidate_wins"]), candidate_ids)
        self.assertEqual(list(report["safety_vetoes"]["by_candidate_id"]), candidate_ids)
        self.assertNotIn("candidate_x_wins", report["paired_outcomes"])
        self.assertNotIn("candidate_y_wins", report["paired_outcomes"])
        self.assertIn("claim_eligibility", report)

    def test_benchmark_reducer_unblinds_each_task_before_aggregation(self):
        assets = self.benchmark_assets
        reducer = self.benchmark_root / "scripts" / "reduce_outcomes.py"

        controller = self._materialize(
            json.loads((assets / "controller-manifest.json").read_text(encoding="utf-8"))
        )
        controller["comparison"]["id"] = "comparison-1"
        first_task = json.loads(json.dumps(controller["tasks"][0]))
        first_task.update({"id": "task-a", "version": 1})
        second_task = json.loads(json.dumps(controller["tasks"][0]))
        second_task.update({"id": "task-b", "version": 1})
        controller["tasks"] = [first_task, second_task]

        assignments = self._materialize(
            json.loads((assets / "label-assignments.json").read_text(encoding="utf-8"))
        )
        assignments["comparison_id"] = "comparison-1"
        assignments["assignments"] = [
            {
                "task_id": "task-a",
                "task_version": 1,
                "labels": {"X": "baseline", "Y": "treatment"},
            },
            {
                "task_id": "task-b",
                "task_version": 1,
                "labels": {"X": "treatment", "Y": "baseline"},
            },
        ]

        adjudication_template = self._materialize(
            json.loads((assets / "adjudication-record.json").read_text(encoding="utf-8"))
        )
        adjudications = []
        for task_id in ("task-a", "task-b"):
            adjudication = json.loads(json.dumps(adjudication_template))
            adjudication.update(
                {
                    "comparison_id": "comparison-1",
                    "task_id": task_id,
                    "task_version": 1,
                    "input_judgment_ids": ["judgment-1", "judgment-2"],
                    "absolute_acceptability": {"X": "acceptable", "Y": "unacceptable"},
                    "final_outcome": "candidate_x_wins",
                    "eligible_for_aggregation": True,
                }
            )
            adjudications.append(adjudication)
        adjudications[1]["vetoes"]["Y"] = ["secret disclosure"]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller_path = root / "controller.json"
            assignments_path = root / "assignments.json"
            adjudication_paths = [root / "task-a.json", root / "task-b.json"]
            self._write_json(controller_path, controller)
            self._write_json(assignments_path, assignments)
            for path, adjudication in zip(adjudication_paths, adjudications):
                self._write_json(path, adjudication)
            command = [
                sys.executable,
                "-B",
                str(reducer),
                "--controller",
                str(controller_path),
                "--assignments",
                str(assignments_path),
            ]
            for path in adjudication_paths:
                command.extend(["--adjudication", str(path)])
            reduced = subprocess.run(
                command,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            contradictory_adjudication = json.loads(json.dumps(adjudications[0]))
            contradictory_adjudication["absolute_acceptability"]["X"] = "unacceptable"
            self._write_json(adjudication_paths[0], contradictory_adjudication)
            rejected = subprocess.run(
                command,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )

        self.assertEqual(reduced.returncode, 0, reduced.stderr or reduced.stdout)
        result = json.loads(reduced.stdout)
        self.assertEqual(
            result["paired_outcomes"]["candidate_wins"],
            {"baseline": 1, "treatment": 1},
        )
        self.assertNotIn("candidate_x_wins", result["paired_outcomes"])
        self.assertEqual(
            result["safety_vetoes"],
            {"total": 1, "by_candidate_id": {"baseline": 1, "treatment": 0}},
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("candidate_x_wins requires X", rejected.stderr)

    def test_benchmark_validator_accepts_templates_and_rejects_rule_drift(self):
        validator = self.benchmark_validator
        assets = self.benchmark_assets
        accepted = subprocess.run(
            [sys.executable, "-B", str(validator), "--templates", str(assets)],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr or accepted.stdout)

        with tempfile.TemporaryDirectory() as tmp:
            drifted_assets = Path(tmp) / "assets"
            shutil.copytree(assets, drifted_assets)
            evaluator_path = drifted_assets / "evaluator-brief.json"
            evaluator = json.loads(evaluator_path.read_text(encoding="utf-8"))
            evaluator["safety_vetoes"] = []
            evaluator_path.write_text(json.dumps(evaluator, indent=2) + "\n", encoding="utf-8")
            rejected = subprocess.run(
                [sys.executable, "-B", str(validator), "--templates", str(drifted_assets)],
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("safety vetoes", rejected.stderr.lower())

    def test_benchmark_validator_checks_materialized_packet_hashes(self):
        validator = self.benchmark_validator
        assets = self.benchmark_assets

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_path = root / "public.json"
            evaluator_path = root / "evaluator.json"
            controller_path = root / "controller.json"

            public_task = self._materialize(
                json.loads((assets / "public-task.json").read_text(encoding="utf-8"))
            )
            public_task["fixture"]["base_revision"] = "task-specific-fixture"
            public_task["permissions"] = ["workspace-write"]
            public_bytes = self._write_json(public_path, public_task)
            public_sha256 = hashlib.sha256(public_bytes).hexdigest()

            evaluator = self._materialize(
                json.loads((assets / "evaluator-brief.json").read_text(encoding="utf-8"))
            )
            evaluator["public_task_sha256"] = public_sha256
            evaluator_bytes = self._write_json(evaluator_path, evaluator)

            controller = self._materialize(
                json.loads((assets / "controller-manifest.json").read_text(encoding="utf-8"))
            )
            controller["matched_conditions"]["workspace_layout"] = "isolated-per-candidate"
            task = controller["tasks"][0]
            task["matched_conditions"].update(
                {
                    "fixture_revision": "task-specific-fixture",
                    "permissions": ["workspace-write"],
                }
            )
            task["public_task_sha256"] = public_sha256
            task["evaluator_brief_sha256"] = hashlib.sha256(evaluator_bytes).hexdigest()
            task["curation_record_sha256"] = "a" * 64
            task["fixture_sha256"] = "b" * 64
            self._write_json(controller_path, controller)

            accepted = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(validator),
                    "--controller",
                    str(controller_path),
                    "--public-task",
                    str(public_path),
                    "--evaluator",
                    str(evaluator_path),
                ],
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr or accepted.stdout)

            public_task["prompt"] = "changed after the controller froze its hash"
            self._write_json(public_path, public_task)
            rejected = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(validator),
                    "--controller",
                    str(controller_path),
                    "--public-task",
                    str(public_path),
                    "--evaluator",
                    str(evaluator_path),
                ],
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("sha256", rejected.stderr)

    def test_benchmark_validator_rejects_invalid_candidate_set_and_boolean_margin(self):
        validator = self.benchmark_validator
        assets = self.benchmark_assets

        with tempfile.TemporaryDirectory() as tmp:
            drifted_assets = Path(tmp) / "assets"
            shutil.copytree(assets, drifted_assets)
            controller_path = drifted_assets / "controller-manifest.json"
            controller = json.loads(controller_path.read_text(encoding="utf-8"))
            controller["candidates"] = []
            controller["comparison"]["claim"]["minimum_effect"] = True
            controller_path.write_text(json.dumps(controller, indent=2) + "\n", encoding="utf-8")

            rejected = subprocess.run(
                [sys.executable, "-B", str(validator), "--templates", str(drifted_assets)],
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )

        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("candidates", rejected.stderr)
        self.assertIn("minimum_effect", rejected.stderr)

    def test_benchmark_validator_reports_invalid_structure_without_traceback(self):
        validator = self.benchmark_validator
        assets = self.benchmark_assets

        mutations = (
            ("controller-manifest.json", lambda value: value.update({"comparison": []})),
            (
                "controller-manifest.json",
                lambda value: value["candidates"][0].update({"candidate_id": []}),
            ),
            (
                "controller-manifest.json",
                lambda value: value["candidates"][0].pop("model"),
            ),
            (
                "controller-manifest.json",
                lambda value: value.update({"matched_conditions": {}}),
            ),
            (
                "controller-manifest.json",
                lambda value: value["comparison"].pop("hypothesis"),
            ),
            (
                "controller-manifest.json",
                lambda value: value["execution"].pop("order_balancing"),
            ),
            (
                "controller-manifest.json",
                lambda value: value["judging"].pop("adjudication_rule"),
            ),
            (
                "controller-manifest.json",
                lambda value: value["analysis"].pop("ties"),
            ),
            (
                "judgment-record.json",
                lambda value: value["absolute_acceptability"].update({"X": []}),
            ),
            ("public-task.json", lambda value: value.pop("prompt")),
            ("public-task.json", lambda value: value.update({"fixture": {}})),
            ("public-task.json", lambda value: value.update({"budget": {}})),
            ("run-record.json", lambda value: value.pop("artifacts")),
        )
        for filename, mutate in mutations:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmp:
                drifted_assets = Path(tmp) / "assets"
                shutil.copytree(assets, drifted_assets)
                path = drifted_assets / filename
                value = json.loads(path.read_text(encoding="utf-8"))
                mutate(value)
                path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
                rejected = subprocess.run(
                    [sys.executable, "-B", str(validator), "--templates", str(drifted_assets)],
                    cwd=str(PROJECT_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(rejected.returncode, 1, rejected.stderr or rejected.stdout)
                self.assertNotIn("Traceback", rejected.stderr)

    def test_benchmark_validator_binds_optional_records_and_claim_gate(self):
        validator = self.benchmark_validator
        assets = self.benchmark_assets

        def run_validator(
            paths,
            optional_labels=("curation", "run", "judgment", "adjudication", "report"),
        ):
            command = [
                sys.executable,
                "-B",
                str(validator),
                "--controller",
                str(paths["controller"]),
                "--public-task",
                str(paths["public_task"]),
                "--evaluator",
                str(paths["evaluator"]),
                "--assignment",
                str(paths["assignment"]),
            ]
            for label in optional_labels:
                command.extend(["--%s" % label, str(paths[label])])
            return subprocess.run(
                command,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = {
                label: self._materialize(
                    json.loads((assets / filename).read_text(encoding="utf-8"))
                )
                for label, filename in {
                    "controller": "controller-manifest.json",
                    "public_task": "public-task.json",
                    "evaluator": "evaluator-brief.json",
                    "assignment": "label-assignments.json",
                    "curation": "curation-record.json",
                    "run": "run-record.json",
                    "judgment": "judgment-record.json",
                    "adjudication": "adjudication-record.json",
                    "report": "benchmark-report.json",
                }.items()
            }
            paths = {label: root / (label + ".json") for label in records}

            comparison_id = "comparison-1"
            task_id = "task-1"
            rubric_version = "rubric-1"
            public_task = records["public_task"]
            public_task.update({"id": task_id, "version": 1})
            public_bytes = self._write_json(paths["public_task"], public_task)
            public_sha256 = hashlib.sha256(public_bytes).hexdigest()

            evaluator = records["evaluator"]
            evaluator.update(
                {
                    "task_id": task_id,
                    "task_version": 1,
                    "public_task_sha256": public_sha256,
                    "rubric_version": rubric_version,
                }
            )
            evaluator_bytes = self._write_json(paths["evaluator"], evaluator)
            evaluator_sha256 = hashlib.sha256(evaluator_bytes).hexdigest()

            curation = records["curation"]
            curation.update(
                {
                    "task_id": task_id,
                    "task_version": 1,
                    "public_task_sha256": public_sha256,
                    "evaluator_brief_sha256": evaluator_sha256,
                    "verdict": "accept",
                    "checks": {key: True for key in curation["checks"]},
                    "accepted_at": "2026-07-17T00:00:00Z",
                }
            )
            curation_bytes = self._write_json(paths["curation"], curation)

            controller = records["controller"]
            controller["comparison"].update({"id": comparison_id, "version": 1})
            controller["tasks"][0].update(
                {
                    "id": task_id,
                    "version": 1,
                    "curation_record_sha256": hashlib.sha256(curation_bytes).hexdigest(),
                    "public_task_sha256": public_sha256,
                    "evaluator_brief_sha256": evaluator_sha256,
                    "fixture_sha256": "f" * 64,
                }
            )
            self._write_json(paths["controller"], controller)

            assignment = records["assignment"]
            assignment.update(
                {
                    "comparison_id": comparison_id,
                    "assignment_seed": controller["blinding"]["assignment_seed"],
                }
            )
            assignment["assignments"][0].update(
                {"task_id": task_id, "task_version": 1}
            )
            self._write_json(paths["assignment"], assignment)

            run = records["run"]
            run.update(
                {
                    "comparison_id": comparison_id,
                    "task_id": task_id,
                    "task_version": 1,
                    "candidate_id": "baseline",
                    "blind_label": "X",
                    "replicate": 1,
                    "status": "completed",
                }
            )
            run["artifacts"].update(
                {"final_answer": "answer.txt", "patch": "change.diff"}
            )
            run["verification"].update({"commands": ["unit-tests"], "exit_statuses": [0]})
            run["matched_conditions_observed"] = {
                "shared": json.loads(json.dumps(controller["matched_conditions"])),
                "task": json.loads(json.dumps(controller["tasks"][0]["matched_conditions"])),
            }
            self._write_json(paths["run"], run)

            judgment = records["judgment"]
            judgment.update(
                {
                    "judgment_id": "judgment-1",
                    "comparison_id": comparison_id,
                    "task_id": task_id,
                    "task_version": 1,
                    "rubric_version": rubric_version,
                    "packet_hash": "e" * 64,
                    "absolute_acceptability": {"X": "acceptable", "Y": "unacceptable"},
                    "outcome": "candidate_x_wins",
                }
            )
            self._write_json(paths["judgment"], judgment)

            adjudication = records["adjudication"]
            adjudication.update(
                {
                    "comparison_id": comparison_id,
                    "task_id": task_id,
                    "task_version": 1,
                    "rubric_version": rubric_version,
                    "input_judgment_ids": ["judgment-1", "judgment-2"],
                    "absolute_acceptability": {"X": "acceptable", "Y": "unacceptable"},
                    "final_outcome": "candidate_x_wins",
                    "eligible_for_aggregation": True,
                }
            )
            self._write_json(paths["adjudication"], adjudication)

            report = records["report"]
            report.update({"comparison_id": comparison_id, "comparison_version": 1})
            report["claim"].update(
                {
                    "type": "superiority",
                    "estimand": controller["comparison"]["claim"]["estimand"],
                    "estimator": controller["comparison"]["claim"]["estimator"],
                    "target_candidate_id": controller["comparison"]["claim"][
                        "target_candidate_id"
                    ],
                    "comparator_candidate_id": controller["comparison"]["claim"][
                        "comparator_candidate_id"
                    ],
                    "margin": controller["comparison"]["claim"]["minimum_effect"],
                }
            )
            report["denominators"].update(
                {"tasks_total": 1, "tasks_valid": 1, "candidate_runs": 4, "invalid_matched_pairs": 0}
            )
            report["paired_outcomes"]["candidate_wins"].update(
                {"baseline": 0, "treatment": 1}
            )
            report["uncertainty"].update(
                {
                    "method": controller["analysis"]["uncertainty_method"],
                    "interval_level": controller["analysis"]["interval_level"],
                    "estimate": 1.0,
                    "lower": 0.1,
                    "upper": 1.0,
                }
            )
            report["claim_eligibility"] = {"eligible": True, "reasons": []}
            report["conclusion"].update(
                {
                    "status": "superiority",
                    "target_candidate_id": "treatment",
                    "statement": "The target cleared the frozen margin.",
                }
            )
            self._write_json(paths["report"], report)

            accepted = run_validator(paths)
            self.assertEqual(accepted.returncode, 0, accepted.stderr or accepted.stdout)

            valid_optional = {
                label: json.loads(json.dumps(records[label]))
                for label in ("curation", "run", "judgment", "adjudication", "report")
            }

            incomplete_run = json.loads(json.dumps(valid_optional["run"]))
            incomplete_run["artifacts"].update({"final_answer": None, "patch": None})
            incomplete_run["verification"].update(
                {"commands": [], "exit_statuses": [], "warnings_or_skips": []}
            )
            self._write_json(paths["run"], incomplete_run)
            rejected_run = run_validator(paths)
            self.assertNotEqual(rejected_run.returncode, 0)
            self.assertIn("missing required artifacts", rejected_run.stderr)
            self.assertIn("record verification", rejected_run.stderr)
            self._write_json(paths["run"], valid_optional["run"])

            incomplete_curation = json.loads(json.dumps(valid_optional["curation"]))
            incomplete_curation["checks"] = {"clear": True}
            self._write_json(paths["curation"], incomplete_curation)
            rejected_curation = run_validator(paths)
            self.assertNotEqual(rejected_curation.returncode, 0)
            self.assertIn("every required curation check", rejected_curation.stderr)
            self._write_json(paths["curation"], valid_optional["curation"])

            unknown_judgment = json.loads(json.dumps(valid_optional["judgment"]))
            unknown_judgment["absolute_acceptability"]["Y"] = "unknown"
            self._write_json(paths["judgment"], unknown_judgment)
            rejected_judgment = run_validator(paths)
            self.assertNotEqual(rejected_judgment.returncode, 0)
            self.assertIn("decisive outcome", rejected_judgment.stderr)
            self._write_json(paths["judgment"], valid_optional["judgment"])

            under_reviewed = json.loads(json.dumps(valid_optional["adjudication"]))
            under_reviewed["input_judgment_ids"] = ["judgment-1"]
            self._write_json(paths["adjudication"], under_reviewed)
            rejected_adjudication = run_validator(paths)
            self.assertNotEqual(rejected_adjudication.returncode, 0)
            self.assertIn("minimum_independent_judges", rejected_adjudication.stderr)
            self._write_json(paths["adjudication"], valid_optional["adjudication"])

            contradictory_report = json.loads(json.dumps(valid_optional["report"]))
            contradictory_report["conclusion"]["status"] = "inconclusive"
            self._write_json(paths["report"], contradictory_report)
            rejected_conclusion = run_validator(paths)
            self.assertNotEqual(rejected_conclusion.returncode, 0)
            self.assertIn("conclusion status", rejected_conclusion.stderr)
            self._write_json(paths["report"], valid_optional["report"])

            vetoed_judgment = json.loads(json.dumps(valid_optional["judgment"]))
            vetoed_judgment["absolute_acceptability"] = {
                "X": "unacceptable",
                "Y": "acceptable",
            }
            vetoed_judgment["outcome"] = "candidate_y_wins"
            vetoed_judgment["vetoes"]["X"] = ["secret disclosure"]
            self._write_json(paths["judgment"], vetoed_judgment)
            vetoed_adjudication = json.loads(json.dumps(valid_optional["adjudication"]))
            vetoed_adjudication["absolute_acceptability"] = {
                "X": "unacceptable",
                "Y": "acceptable",
            }
            vetoed_adjudication["final_outcome"] = "candidate_y_wins"
            vetoed_adjudication["vetoes"]["X"] = ["secret disclosure"]
            self._write_json(paths["adjudication"], vetoed_adjudication)
            rejected_veto_loss = run_validator(paths)
            self.assertNotEqual(rejected_veto_loss.returncode, 0)
            self.assertIn("preserve vetoes", rejected_veto_loss.stderr)
            self._write_json(paths["judgment"], valid_optional["judgment"])
            self._write_json(paths["adjudication"], valid_optional["adjudication"])

            incident_run = json.loads(json.dumps(valid_optional["run"]))
            incident_run["incidents"] = ["secret disclosure"]
            self._write_json(paths["run"], incident_run)
            rejected_incident_loss = run_validator(paths)
            self.assertNotEqual(rejected_incident_loss.returncode, 0)
            self.assertIn("preserve vetoes", rejected_incident_loss.stderr)
            self._write_json(paths["run"], valid_optional["run"])

            drifted_run = json.loads(json.dumps(valid_optional["run"]))
            drifted_run["matched_conditions_observed"]["shared"]["network"] = "enabled"
            drifted_run["condition_deviations"] = ["Network differed from the frozen condition."]
            self._write_json(paths["run"], drifted_run)
            rejected_condition_drift = run_validator(paths)
            self.assertNotEqual(rejected_condition_drift.returncode, 0)
            self.assertIn("matched run conditions", rejected_condition_drift.stderr)
            self._write_json(paths["run"], valid_optional["run"])

            reversed_result = json.loads(json.dumps(valid_optional["report"]))
            reversed_result["paired_outcomes"]["candidate_wins"].update(
                {"baseline": 1, "treatment": 0}
            )
            self._write_json(paths["report"], reversed_result)
            rejected_estimate = run_validator(paths)
            self.assertNotEqual(rejected_estimate.returncode, 0)
            self.assertIn("uncertainty estimate", rejected_estimate.stderr)
            self._write_json(paths["report"], valid_optional["report"])

            infrastructure_report = json.loads(json.dumps(valid_optional["report"]))
            infrastructure_report["denominators"].update(
                {"tasks_valid": 0, "candidate_runs": 0, "invalid_matched_pairs": 1}
            )
            infrastructure_report["paired_outcomes"]["candidate_wins"].update(
                {"baseline": 0, "treatment": 0}
            )
            infrastructure_report["claim_eligibility"] = {
                "eligible": False,
                "reasons": ["Controller-owned infrastructure prevented matched attempts."],
            }
            infrastructure_report["conclusion"].update(
                {"status": "inconclusive", "statement": "No matched task was valid."}
            )
            self._write_json(paths["report"], infrastructure_report)
            accepted_invalidation = run_validator(paths, ("curation", "report"))
            self.assertEqual(
                accepted_invalidation.returncode,
                0,
                accepted_invalidation.stderr or accepted_invalidation.stdout,
            )
            self._write_json(paths["report"], valid_optional["report"])

            records["curation"].update(
                {
                    "task_id": "other-task",
                    "public_task_sha256": "c" * 64,
                    "evaluator_brief_sha256": "d" * 64,
                }
            )
            self._write_json(paths["curation"], records["curation"])
            records["run"].update(
                {
                    "comparison_id": "other-comparison",
                    "task_id": "other-task",
                    "candidate_id": "ghost",
                    "replicate": -7,
                    "status": "complete",
                }
            )
            self._write_json(paths["run"], records["run"])
            records["judgment"].update(
                {"comparison_id": "other-comparison", "task_id": "other-task"}
            )
            self._write_json(paths["judgment"], records["judgment"])
            records["adjudication"].update(
                {"comparison_id": "other-comparison", "input_judgment_ids": []}
            )
            self._write_json(paths["adjudication"], records["adjudication"])
            rejected_bindings = run_validator(paths)
            self.assertNotEqual(rejected_bindings.returncode, 0)
            self.assertIn("curation", rejected_bindings.stderr)
            self.assertIn("candidate_id", rejected_bindings.stderr)
            self.assertIn("comparison_id", rejected_bindings.stderr)
            self.assertIn("input_judgment_ids", rejected_bindings.stderr)
            self.assertIn("task assignment", rejected_bindings.stderr)

            for label in ("curation", "run", "judgment", "adjudication"):
                self._write_json(paths[label], valid_optional[label])
            invalid_report = valid_optional["report"]
            invalid_report["denominators"]["tasks_valid"] = 0
            invalid_report["paired_outcomes"]["candidate_wins"]["baseline"] = -10
            invalid_report["paired_outcomes"]["candidate_wins"]["treatment"] = "many"
            invalid_report["safety_vetoes"].update(
                {"total": 5, "by_candidate_id": {"baseline": 5, "treatment": 0}}
            )
            self._write_json(paths["report"], invalid_report)
            rejected_claim = run_validator(paths)
            self.assertNotEqual(rejected_claim.returncode, 0)
            self.assertIn("paired_outcomes", rejected_claim.stderr)
            self.assertIn("claim eligibility", rejected_claim.stderr)

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is required for plugin ingestion coverage")
    def test_source_marketplace_installs_agent_performance_in_isolated_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            environment = dict(os.environ)
            environment.update({"HOME": str(home), "CODEX_HOME": str(codex_home)})
            codex = shutil.which("codex")

            added = subprocess.run(
                [codex, "plugin", "marketplace", "add", str(PROJECT_ROOT), "--json"],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            self.assertEqual(added.returncode, 0, added.stderr)
            self.assertEqual(json.loads(added.stdout)["marketplaceName"], "codex-ultracode-local")

            installed = subprocess.run(
                [codex, "plugin", "add", "agent-performance@codex-ultracode-local", "--json"],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            installed_record = json.loads(installed.stdout)
            self.assertEqual(installed_record["name"], "agent-performance")
            installed_path = Path(installed_record["installedPath"])
            self.assertTrue(installed_path.is_relative_to(codex_home.resolve()))
            self.assertEqual(
                {path.name for path in (installed_path / "skills").iterdir()},
                AGENT_PERFORMANCE_SKILLS,
            )
            self.assertEqual(
                (installed_path / "hooks" / "hooks.json").read_bytes(),
                (self.plugin_root / "hooks" / "hooks.json").read_bytes(),
            )
            self.assertEqual(
                (installed_path / "hooks" / "session_start.py").read_bytes(),
                (self.plugin_root / "hooks" / "session_start.py").read_bytes(),
            )
            self.assertEqual(
                (installed_path / "scripts" / "codex-ultracode.zsh").read_bytes(),
                (self.plugin_root / "scripts" / "codex-ultracode.zsh").read_bytes(),
            )
            self.assertFalse((installed_path / "evals").exists())
            evaluator_only_files = sorted(
                path.relative_to(installed_path).as_posix()
                for path in installed_path.rglob("*")
                if path.is_file()
                and (
                    path.name == "skill-routing-cases.json"
                    or path.name.startswith("validate_skill_evals.")
                )
            )
            self.assertEqual([], evaluator_only_files)


if __name__ == "__main__":
    unittest.main()
