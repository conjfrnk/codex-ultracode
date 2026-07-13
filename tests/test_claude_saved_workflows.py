import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from conductor_runtime.claude_saved_workflows import compile_claude_saved_workflow
from conductor_runtime.cli import main as cli_main
from conductor_runtime.errors import ValidationError
from conductor_runtime.packet_items import read_packet_items_json_file
from conductor_runtime.runner import WorkflowRunner
from conductor_runtime.saved_workflows import apply_saved_workflow_args, load_saved_workflow
from conductor_runtime.security import RuntimePolicy


OFFICIAL_EXAMPLE = """\
export const meta = {
  name: 'audit-routes',
  description: 'Audit every route handler for missing auth checks',
}

const found = await agent('List every .ts file under src/routes/.', {
  schema: { type: 'object', required: ['files'], properties: { files: { type: 'array', items: { type: 'string' } } } },
})

const audits = await pipeline(found.files, file =>
  agent(`Audit ${file} for missing authentication checks.`, { label: file }),
)

return audits.filter(Boolean)
"""


class ClaudeSavedWorkflowCompilerTests(unittest.TestCase):
    def compile(self, source):
        return compile_claude_saved_workflow(source, Path("audit.js"))

    def assert_rejected(self, source, detail):
        with self.assertRaisesRegex(ValidationError, detail):
            self.compile(source)

    def test_compiles_documented_agent_pipeline_shape(self):
        meta, workflow = self.compile(OFFICIAL_EXAMPLE)

        self.assertEqual(meta["name"], "audit-routes")
        self.assertEqual(workflow["schema"], "conductor.workflow.v1")
        self.assertEqual(workflow["mode"], "read_only")
        self.assertEqual(workflow["max_workers"], 16)
        self.assertEqual(workflow["max_items"], 999)
        self.assertEqual(len(workflow["steps"]), 2)

        discovery, audits = workflow["steps"]
        self.assertEqual(discovery["kind"], "codex_exec")
        self.assertEqual(discovery["sandbox"], "read-only")
        self.assertEqual(discovery["capture"], "claude-workflow/found.json")
        self.assertEqual(discovery["output_schema"]["type"], "object")
        self.assertFalse(discovery["output_schema"]["additionalProperties"])
        self.assertEqual(audits["kind"], "agent_map")
        self.assertEqual(audits["items_artifact"], discovery["capture"])
        self.assertEqual(audits["items_pointer"], "/files")
        self.assertEqual(audits["prompt_template"], "Audit {item} for missing authentication checks.")
        self.assertEqual(audits["depends_on"], ["found"])
        self.assertEqual(audits["max_workers"], 16)
        self.assertTrue(audits["preserve_duplicate_items"])

    def test_loader_compiles_dynamic_script_and_applies_structured_args(self):
        source = """\
export const meta = { name: 'review-files', description: 'Review selected files' }
const reviews = await pipeline(args.files, file => agent(`Review ${file}.`))
return reviews
"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "review.js"
            path.write_text(source, encoding="utf-8")
            saved = load_saved_workflow(path, workspace=workspace)
            rendered = apply_saved_workflow_args(saved, {"files": ["a.py", "b.py"]})

        step = rendered.workflow["steps"][0]
        self.assertEqual(step["items"], ["a.py", "b.py"])
        self.assertEqual(step["prompt_template"], "Review {item}.")

    def test_compiles_static_pipeline_and_escapes_literal_format_braces(self):
        source = """\
export const meta = { name: 'review-static' }
const reviews = await pipeline(['a.py', 'b.py'], async file =>
  await agent(`Review ${file}; return JSON like {\"ok\": true}.`),
)
return reviews.filter(Boolean)
"""
        _, workflow = self.compile(source)

        step = workflow["steps"][0]
        self.assertEqual(step["items"], ["a.py", "b.py"])
        self.assertEqual(
            step["prompt_template"],
            'Review {item}; return JSON like {{"ok": true}}.',
        )

    def test_agent_prompt_accepts_args_reference_and_template(self):
        direct = """\
export const meta = { name: 'research' }
const report = await agent(args.question)
return report
"""
        _, workflow = self.compile(direct)
        self.assertEqual(workflow["steps"][0]["prompt"], "{{args.question}}")

        templated = """\
export const meta = { name: 'research' }
const report = await agent(`Research: ${args.question}`)
return report
"""
        _, workflow = self.compile(templated)
        self.assertEqual(workflow["steps"][0]["prompt"], "Research: {{args.question}}")

    def test_multiple_pipelines_share_total_agent_budget(self):
        source = """\
export const meta = { name: 'two-pass' }
const found = await agent('List files', {
  schema: { type: 'object', required: ['files'], properties: { files: { type: 'array', items: { type: 'string' } } } },
})
const first = await pipeline(found.files, file => agent(`First ${file}`))
const second = await pipeline(found.files, file => agent(`Second ${file}`))
return second
"""
        _, workflow = self.compile(source)

        self.assertEqual(workflow["max_items"], 499)
        self.assertEqual(workflow["steps"][1]["max_items"], 499)
        self.assertEqual(workflow["steps"][2]["max_items"], 499)
        self.assertEqual(workflow["steps"][2]["depends_on"], ["first", "found"])

    def test_pipeline_requires_prior_array_of_strings_schema(self):
        missing_required = """\
export const meta = { name: 'bad-schema' }
const found = await agent('List files', {
  schema: { type: 'object', properties: { files: { type: 'array', items: { type: 'string' } } } },
})
const reviews = await pipeline(found.files, file => agent(`Review ${file}`))
return reviews
"""
        self.assert_rejected(missing_required, "array-of-strings schema property")

        wrong_items = missing_required.replace("type: 'string'", "type: 'number'").replace(
            "properties: { files", "required: ['files'], properties: { files"
        )
        self.assert_rejected(wrong_items, "array-of-strings schema property")

    def test_pipeline_must_reference_prior_agent(self):
        source = """\
export const meta = { name: 'forward-reference' }
const reviews = await pipeline(found.files, file => agent(`Review ${file}`))
const found = await agent('List files', {
  schema: { type: 'object', required: ['files'], properties: { files: { type: 'array', items: { type: 'string' } } } },
})
return reviews
"""
        self.assert_rejected(source, "must reference a prior agent")

    def test_filter_boolean_is_limited_to_pipeline_results(self):
        source = """\
export const meta = { name: 'bad-filter' }
const report = await agent('Report')
return report.filter(Boolean)
"""
        self.assert_rejected(source, r"filter\(Boolean\).+only on a pipeline")

    def test_rejects_unsupported_agent_options_and_meta_fields(self):
        options = """\
export const meta = { name: 'tools' }
const report = await agent('Report', { tools: ['Bash'] })
return report
"""
        self.assert_rejected(options, "unsupported field.+tools")

        meta = """\
export const meta = { name: 'mode', dangerous: true }
const report = await agent('Report')
return report
"""
        self.assert_rejected(meta, "meta contains unsupported field.+dangerous")

        label = """\
export const meta = { name: 'label' }
const reviews = await pipeline(args.files, file => agent(`Review ${file}`, { label: process.env }))
return reviews
"""
        self.assert_rejected(label, "agent label may reference only")

    def test_rejects_arbitrary_javascript_without_executing_it(self):
        prefix = "export const meta = { name: 'unsafe' }\n"
        suffix = "\nconst report = await agent('Report')\nreturn report\n"
        cases = {
            "import fs from 'node:fs'": "supports only",
            "while (true) { agent('x') }": "supports only",
            "process.exit(1)": "supports only",
            "const x = await fetch('https://example.com')": "only agent.+pipeline",
            "const x = await agent(require('fs'))": "expected",
        }
        for body, detail in cases.items():
            with self.subTest(body=body):
                self.assert_rejected(prefix + body + suffix, detail)

    def test_rejects_expression_template_interpolation(self):
        source = """\
export const meta = { name: 'expression' }
const reviews = await pipeline(args.files, file => agent(`Review ${file.toUpperCase()}`))
return reviews
"""
        self.assert_rejected(source, "template interpolations may contain only dotted identifiers")

    def test_rejects_pipeline_prompt_without_callback_interpolation(self):
        source = """\
export const meta = { name: 'constant-prompt' }
const reviews = await pipeline(args.files, file => agent(`Review a file`))
return reviews
"""
        self.assert_rejected(source, "must interpolate its file callback parameter")

    def test_keeps_existing_declarative_saved_workflow_format(self):
        source = """\
export const meta = {"name": "declarative", "description": "Existing format"};
export const workflow = {
  "schema": "conductor.workflow.v1",
  "name": "declarative",
  "mode": "read_only",
  "steps": [{"id": "note", "kind": "write_artifact", "output": "note.txt", "content": "ok"}]
};
"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "declarative.js"
            path.write_text(source, encoding="utf-8")
            saved = load_saved_workflow(path, workspace=workspace)

        self.assertEqual(saved.workflow["steps"][0]["kind"], "write_artifact")

    def test_json_pointer_handoff_reads_strict_bounded_string_arrays(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "items.json"
            path.write_text('{"result":{"files":["a.py","b.py","a.py"]}}', encoding="utf-8")
            items = read_packet_items_json_file(path, "items", 10, "/result/files")
            self.assertEqual(items, ["a.py", "b.py"])
            repeated = read_packet_items_json_file(
                path,
                "items",
                10,
                "/result/files",
                preserve_duplicates=True,
            )
            self.assertEqual(repeated, ["a.py", "b.py", "a.py"])

            path.write_text('{"files":["a.py"],"files":["b.py"]}', encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "duplicate JSON key"):
                read_packet_items_json_file(path, "items", 10, "/files")

            path.write_text('{"files":[1]}', encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "must resolve to a string array"):
                read_packet_items_json_file(path, "items", 10, "/files")

    def test_runner_materializes_and_passes_inline_output_schema(self):
        schema = {
            "type": "object",
            "required": ["files"],
            "properties": {"files": {"type": "array", "items": {"type": "string"}}},
            "additionalProperties": False,
        }
        workflow = {
            "schema": "conductor.workflow.v1",
            "name": "schema-command",
            "mode": "read_only",
            "steps": [
                {
                    "id": "discover",
                    "kind": "codex_exec",
                    "prompt": "List files",
                    "output_schema": schema,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = WorkflowRunner(
                workflow,
                root,
                root / "runs",
                RuntimePolicy(allow_agent=True),
                dry_run=True,
                run_id="schema",
            )
            prepared = runner._prepare_codex_output_schema(workflow["steps"][0])
            command = runner._codex_command(prepared, runner.run.artifacts_dir / "discover.json")
            schema_path = Path(command[command.index("--output-schema") + 1])

            self.assertTrue(schema_path.is_file())
            self.assertEqual(json.loads(schema_path.read_text(encoding="utf-8")), schema)

    def test_cli_validates_inspects_and_dry_runs_documented_script(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow_dir = root / ".claude" / "workflows"
            workflow_dir.mkdir(parents=True)
            (workflow_dir / "audit-routes.js").write_text(OFFICIAL_EXAMPLE, encoding="utf-8")

            stdout = StringIO()
            with redirect_stdout(stdout):
                code = cli_main(["validate-saved-workflows", "--workspace", str(root)])
            self.assertEqual(code, 0)
            self.assertIn("audit-routes", stdout.getvalue())

            stdout = StringIO()
            with redirect_stdout(stdout):
                code = cli_main(["inspect-saved-workflow", "audit-routes", "--workspace", str(root)])
            self.assertEqual(code, 0)
            self.assertIn("found [codex_exec", stdout.getvalue())
            self.assertIn("audits [agent_map", stdout.getvalue())

            stdout = StringIO()
            with redirect_stdout(stdout):
                code = cli_main(
                    [
                        "run-saved-workflow",
                        "audit-routes",
                        "--workspace",
                        str(root),
                        "--runs-dir",
                        str(root / "runs"),
                        "--run-id",
                        "dynamic-dry-run",
                        "--dry-run",
                        "--allow-agent",
                        "--allow-parallel",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("Status: planned", stdout.getvalue())
            persisted = json.loads(
                (root / "runs" / "dynamic-dry-run" / "workflow.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["steps"][1]["items_pointer"], "/files")


if __name__ == "__main__":
    unittest.main()
