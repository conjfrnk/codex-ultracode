import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from conductor_runtime.agent_packets import packetize_agent_items
from conductor_runtime.claude_saved_workflows import compile_claude_saved_workflow
from conductor_runtime.legacy_cli import main as cli_main
from conductor_runtime.errors import ValidationError
from conductor_runtime.packet_items import read_packet_items_json_file
from conductor_runtime.runner import WorkflowRunner, _agent_output_relative, _agent_packet_label
from conductor_runtime.saved_workflows import (
    apply_saved_workflow_args,
    load_saved_workflow,
    parse_saved_workflow_args,
)
from conductor_runtime.schemas import get_schema
from conductor_runtime.security import RuntimePolicy
from conductor_runtime.workflow import validate_workflow


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


class ResultFixtureRunner(WorkflowRunner):
    def _codex_exec(self, step):
        prepared = self._prepare_codex_dependency_context(step)
        if prepared is not step:
            self.prepared_dependency_context = prepared["_agent_dependency_context"]
        output = step.get("fixture_output", step["prompt"])
        path = self.run.write_artifact(
            step.get("capture", "%s.md" % step["id"]),
            output,
        )
        self.run.mark_step(
            step["id"],
            "completed",
            "fixture codex output",
            kind=step["kind"],
            metrics={
                "fixture_output_bytes": path.stat().st_size,
            },
        )

    def _agent_map(self, step):
        items = self._agent_items(step)
        packets = packetize_agent_items(items, step.get("max_packets"))
        outputs = step["fixture_outputs"]
        if len(outputs) != len(packets):
            raise AssertionError("fixture output count must match packet count")
        capture_dir = step.get("capture_dir", step["id"])
        for index, (packet, output) in enumerate(zip(packets, outputs), start=1):
            relative = _agent_output_relative(
                capture_dir,
                _agent_packet_label(
                    packet.items,
                    step.get("item_semantics", "workspace_path"),
                ),
                index,
            )
            self.run.write_artifact(relative, output)
        self.run.mark_step(
            step["id"],
            "completed",
            "fixture map output",
            kind=step["kind"],
        )


def result_workflow(source, *, filter_falsey=False, output_limit_bytes=None):
    collector = {
        "id": "result",
        "kind": "collect_results",
        "source_step": source["id"],
        "output": "result.json",
        "depends_on": [source["id"]],
    }
    if filter_falsey:
        collector["filter_falsey"] = True
    if output_limit_bytes is not None:
        collector["output_limit_bytes"] = output_limit_bytes
    return {
        "schema": "conductor.workflow.v1",
        "name": "result-fixture",
        "mode": "read_only",
        "result_artifact": "result.json",
        "steps": [source, collector],
    }


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
        self.assertEqual(workflow["result_artifact"], "claude-workflow/result.json")
        self.assertEqual(len(workflow["steps"]), 3)

        discovery, audits, result = workflow["steps"]
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
        self.assertEqual(result["kind"], "collect_results")
        self.assertEqual(result["source_step"], "audits")
        self.assertEqual(result["depends_on"], ["audits"])
        self.assertEqual(result["output"], workflow["result_artifact"])
        self.assertTrue(result["filter_falsey"])

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

    def test_compiles_static_parallel_map_with_phases_and_schema_constants(self):
        source = """\
export const meta = {
  name: 'parallel-review',
  phases: [{ title: 'Code Review', detail: 'Independent review' }],
}
const TOPICS = [
  { key: 'correctness', prompt: 'Review correctness.' },
  { key: 'security', prompt: 'Review security.' },
]
const RESULT_SCHEMA = {
  type: 'object',
  properties: { finding: { type: 'string' } },
  required: ['finding'],
}
phase('Code Review')
const reviews = (await parallel(
  TOPICS.map((topic) => () => agent(
    `${topic.prompt}\nReturn one finding.`,
    { label: `review:${topic.key}`, phase: 'Code Review', schema: RESULT_SCHEMA },
  )),
)).filter(Boolean)
return reviews
"""
        meta, workflow = self.compile(source)
        validate_workflow(workflow)

        self.assertEqual(meta["phases"][0]["title"], "Code Review")
        reviews, result = workflow["steps"]
        self.assertEqual(reviews["kind"], "agent_map")
        self.assertEqual(reviews["item_semantics"], "opaque")
        self.assertEqual(reviews["prompt_template"], "{item}")
        self.assertEqual(
            reviews["items"],
            [
                "Review correctness.\nReturn one finding.",
                "Review security.\nReturn one finding.",
            ],
        )
        self.assertEqual(reviews["phase"], "Code-Review")
        self.assertFalse(reviews["output_schema"]["additionalProperties"])
        self.assertEqual(reviews["max_workers"], 16)
        self.assertTrue(result["filter_falsey"])

    def test_parallel_args_render_as_opaque_items(self):
        source = """\
export const meta = { name: 'parallel-topics' }
const reviews = await parallel(args.topics.map(topic => () => agent(`Review ${topic}.`)))
return reviews
"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "review.js"
            path.write_text(source, encoding="utf-8")
            saved = load_saved_workflow(path, workspace=workspace)
            rendered = apply_saved_workflow_args(
                saved,
                {"topics": ["auth/session behavior", "release readiness"]},
            )

        step = rendered.workflow["steps"][0]
        self.assertEqual(step["items"], ["auth/session behavior", "release readiness"])
        self.assertEqual(step["item_semantics"], "opaque")
        self.assertEqual(step["prompt_template"], "Review {item}.")

        prior = """\
export const meta = { name: 'parallel-prior' }
const FILES_SCHEMA = {
  type: 'object', required: ['files'],
  properties: { files: { type: 'array', items: { type: 'string' } } },
}
const found = await agent('List files', { schema: FILES_SCHEMA })
const reviews = await parallel(found.files.map(file => () => agent(`Review ${file}.`)))
return reviews
"""
        _, prior_workflow = self.compile(prior)
        prior_step = prior_workflow["steps"][1]
        self.assertEqual(prior_step["items_artifact"], "claude-workflow/found.json")
        self.assertEqual(prior_step["items_pointer"], "/files")
        self.assertNotIn("item_semantics", prior_step)

    def test_guarded_arg_aliases_compile_structured_parallel_map(self):
        source = """\
export const meta = { name: 'service-review' }
const system = args && args.system
const services = args && args.services
const reviews = await parallel(services.map(service => () => agent(
  'Review ' + service.name + ' in ' + system + ': ' + service.responsibilities,
  { label: 'review:' + service.name, effort: 'max' },
)))
return reviews.filter(Boolean)
"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "service-review.js"
            path.write_text(source, encoding="utf-8")
            saved = load_saved_workflow(path, workspace=workspace)
            rendered = apply_saved_workflow_args(
                saved,
                {
                    "system": "billing",
                    "services": [
                        {
                            "name": "ledger",
                            "responsibilities": "balance settlement",
                        },
                        {
                            "name": "invoice",
                            "responsibilities": "issue invoices",
                        },
                    ],
                },
            )

        step = rendered.workflow["steps"][0]
        self.assertEqual(step["item_semantics"], "json")
        self.assertEqual(
            step["prompt_template"],
            "Review {item.name} in billing: {item.responsibilities}",
        )
        self.assertEqual(step["items"][0]["name"], "ledger")
        self.assertEqual(step["effort"], "ultra")
        self.assertTrue(rendered.workflow["steps"][1]["filter_falsey"])

    def test_structured_pipeline_accepts_prior_object_array_schema(self):
        source = """\
export const meta = { name: 'discover-services' }
const SERVICES_SCHEMA = {
  type: 'object',
  required: ['services'],
  properties: {
    services: {
      type: 'array',
      items: { type: 'object', properties: { name: { type: 'string' } } },
    },
  },
}
const found = await agent('Find services', { schema: SERVICES_SCHEMA })
const reviews = await pipeline(found.services, service => agent(`Review ${service.name}`))
return reviews
"""
        _, workflow = self.compile(source)
        validate_workflow(workflow)

        step = workflow["steps"][1]
        self.assertEqual(step["item_semantics"], "json")
        self.assertEqual(step["items_artifact"], "claude-workflow/found.json")
        self.assertEqual(step["items_pointer"], "/services")
        self.assertEqual(step["prompt_template"], "Review {item.name}")

    def test_guarded_aliases_and_json_args_fail_closed(self):
        invalid_aliases = [
            "const services = args && input.services",
            "const services = args && args.services.extra",
        ]
        for declaration in invalid_aliases:
            source = """\
export const meta = { name: 'invalid-alias' }
%s
const report = await agent('Review')
return report
""" % declaration
            with self.subTest(declaration=declaration):
                self.assert_rejected(source, "exact `args && args.NAME` syntax")

        with self.assertRaisesRegex(ValidationError, "duplicate JSON key name"):
            parse_saved_workflow_args(
                ['services=[{"name":"ledger","name":"invoice"}]']
            )

    def test_parallel_phase_and_static_subset_rejections_are_explicit(self):
        cases = [
            (
                """\
export const meta = { name: 'phase-conflict', phases: [{ title: 'Review' }, { title: 'Verify' }] }
const ITEMS = ['one']
phase('Review')
const results = await parallel(ITEMS.map(item => () => agent(`${item}`, { phase: 'Verify' })))
return results
""",
                "phase marker.+conflicts",
            ),
            (
                """\
export const meta = { name: 'unknown-phase', phases: [{ title: 'Review' }] }
const report = await agent('Report', { phase: 'Verify' })
return report
""",
                "not declared",
            ),
            (
                """\
export const meta = { name: 'phase-collision', phases: [{ title: 'Code Review' }, { title: 'Code-Review' }] }
const report = await agent('Report')
return report
""",
                "collapse to the same",
            ),
            (
                """\
export const meta = { name: 'unsafe-constant' }
const VALUE = process.env
const report = await agent('Report')
return report
""",
                "static constants must contain only",
            ),
            (
                """\
export const meta = { name: 'filtered-intermediate' }
const ITEMS = ['one']
const reviews = (await parallel(ITEMS.map(item => () => agent(`${item}`)))).filter(Boolean)
const report = await agent('Report')
return report
""",
                r"filter\(Boolean\).+cannot feed",
            ),
            (
                """\
export const meta = { name: 'unsupported-agent-type' }
const ITEMS = ['one']
const reviews = await parallel(ITEMS.map(item => () => agent(`${item}`, { agentType: 'worker' })))
return reviews
""",
                "unsupported field.+agentType",
            ),
        ]
        for source, detail in cases:
            with self.subTest(detail=detail):
                self.assert_rejected(source, detail)

    def test_opaque_agent_map_hashes_text_without_workspace_resolution(self):
        step = {
            "id": "reviews",
            "kind": "agent_map",
            "items": ["Review auth/session behavior.\nReturn JSON."],
            "item_semantics": "opaque",
            "prompt_template": "{item}",
        }
        workflow = {
            "schema": "conductor.workflow.v1",
            "name": "opaque-items",
            "mode": "read_only",
            "steps": [step],
        }
        validate_workflow(workflow)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = WorkflowRunner(
                workflow,
                root,
                root / "runs",
                RuntimePolicy(),
                dry_run=True,
                run_id="opaque",
            )
            with patch.object(
                runner,
                "_workspace_item_fingerprint",
                side_effect=AssertionError("opaque text reached the filesystem"),
            ):
                source = runner._agent_packet_source_fingerprint(step, step["items"])

        self.assertEqual(source["state"], "opaque")
        self.assertEqual(source["size"], len(step["items"][0].encode("utf-8")))
        self.assertEqual(len(source["sha256"]), 64)
        self.assertNotIn(step["items"][0], json.dumps(source))

    def test_opaque_agent_map_rejects_line_files_and_accepts_json_artifacts(self):
        workflow = {
            "schema": "conductor.workflow.v1",
            "name": "bad-opaque",
            "mode": "read_only",
            "steps": [
                {
                    "id": "reviews",
                    "kind": "agent_map",
                    "items_file": "topics.txt",
                    "item_semantics": "opaque",
                    "prompt_template": "Review {item}",
                }
            ],
        }
        with self.assertRaisesRegex(ValidationError, "do not support line-oriented items_file"):
            validate_workflow(workflow)

        workflow["steps"][0].pop("items_file")
        workflow["steps"][0]["items_artifact"] = "topics.json"
        workflow["steps"][0]["items_pointer"] = ""
        validate_workflow(workflow)

        schema = get_schema("workflow")
        agent_map = next(
            item
            for item in schema["properties"]["steps"]["items"]["oneOf"]
            if item["properties"]["kind"]["const"] == "agent_map"
        )
        self.assertEqual(
            agent_map["properties"]["item_semantics"]["enum"],
            ["workspace_path", "opaque", "json"],
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

    def test_compiles_text_concatenation_effort_and_discovery_metadata(self):
        source = """\
export const meta = {
  name: 'targeted-review',
  description: 'Review ' + 'one target',
  whenToUse: 'Use for ' + 'a focused review',
}
const PREFIX = 'Review '
const report = await agent(PREFIX + args.target + '.', {
  label: 'review:' + args.target,
  effort: 'max',
})
return report
"""
        meta, workflow = self.compile(source)
        validate_workflow(workflow)

        self.assertEqual(meta["description"], "Review one target")
        self.assertEqual(meta["whenToUse"], "Use for a focused review")
        step = workflow["steps"][0]
        self.assertEqual(step["prompt"], "Review {{args.target}}.")
        self.assertEqual(step["effort"], "ultra")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "targeted-review.js"
            path.write_text(source, encoding="utf-8")
            saved = load_saved_workflow(path, workspace=root)

        self.assertEqual(saved.when_to_use, "Use for a focused review")

        secret_metadata = source.replace(
            "Use for ' + 'a focused review",
            "api_key=super-secret-value",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "secret-metadata.js"
            path.write_text(secret_metadata, encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "whenToUse must not contain secret-like"):
                load_saved_workflow(path, workspace=root)

    def test_compiles_pipeline_and_static_parallel_text_concatenation(self):
        pipeline_source = """\
export const meta = { name: 'pipeline-concat' }
const LEVEL = 'xhigh'
const reviews = await pipeline(args.files, file => agent(
  'Review ' + file + ' for ' + args.focus + '.',
  { label: 'review:' + file, effort: LEVEL },
))
return reviews
"""
        _, pipeline_workflow = self.compile(pipeline_source)
        pipeline_step = pipeline_workflow["steps"][0]
        self.assertEqual(
            pipeline_step["prompt_template"],
            "Review {item} for {{args.focus}}.",
        )
        self.assertEqual(pipeline_step["effort"], "xhigh")

        parallel_source = """\
export const meta = { name: 'parallel-concat' }
const TOPICS = [
  { key: 'security', prompt: 'Review security' },
  { key: 'correctness', prompt: 'Review correctness' },
]
const reviews = await parallel(TOPICS.map(topic => () => agent(
  topic.prompt + '. Key: ' + topic.key,
  { label: 'review:' + topic.key, effort: 'high' },
)))
return reviews
"""
        _, parallel_workflow = self.compile(parallel_source)
        parallel_step = parallel_workflow["steps"][0]
        self.assertEqual(
            parallel_step["items"],
            [
                "Review security. Key: security",
                "Review correctness. Key: correctness",
            ],
        )
        self.assertEqual(parallel_step["effort"], "high")

    def test_text_concatenation_and_effort_remain_bounded(self):
        arithmetic = """\
export const meta = { name: 'arithmetic' }
const VALUE = 1 + 2
const report = await agent('Report')
return report
"""
        self.assert_rejected(arithmetic, "static constants must contain only")

        prompt_arithmetic = """\
export const meta = { name: 'prompt-arithmetic' }
const report = await agent(1 + 2)
return report
"""
        self.assert_rejected(prompt_arithmetic, "prompt text may reference only")

        dynamic_effort = """\
export const meta = { name: 'dynamic-effort' }
const report = await agent('Report', { effort: args.effort })
return report
"""
        self.assert_rejected(dynamic_effort, "references unknown static constant args")

        invalid_effort = """\
export const meta = { name: 'invalid-effort' }
const report = await agent('Report', { effort: 'extreme' })
return report
"""
        self.assert_rejected(invalid_effort, "effort must be low, medium, high, xhigh, max, or ultra")

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

    def test_pipeline_consumes_filtered_prior_map_text_results(self):
        source = """\
export const meta = { name: 'challenge-audits' }
const audits = await pipeline(['a.py', 'b.py'], file => agent(`Audit ${file}`))
const challenges = await pipeline(audits.filter(Boolean), audit => agent(`Challenge ${audit}`))
return challenges
"""
        _, workflow = self.compile(source)
        validate_workflow(workflow)

        self.assertEqual(workflow["max_items"], 500)
        self.assertEqual(len(workflow["steps"]), 4)
        audits, handoff, challenges, result = workflow["steps"]
        self.assertEqual(audits["kind"], "agent_map")
        self.assertEqual(handoff["kind"], "collect_results")
        self.assertTrue(handoff["intermediate"])
        self.assertTrue(handoff["filter_falsey"])
        self.assertEqual(handoff["source_step"], "audits")
        self.assertEqual(handoff["output_limit_bytes"], 1024 * 1024)
        self.assertEqual(challenges["items_artifact"], handoff["output"])
        self.assertEqual(challenges["items_pointer"], "")
        self.assertEqual(challenges["item_semantics"], "opaque")
        self.assertEqual(challenges["depends_on"], [handoff["id"]])
        self.assertEqual(result["source_step"], "challenges")

    def test_parallel_consumes_assignment_filtered_structured_map_results(self):
        source = """\
export const meta = { name: 'challenge-findings' }
const findings = (await pipeline(['a.py', 'b.py'], file => agent(`Audit ${file}`, {
  schema: {
    type: 'object',
    required: ['summary', 'severity'],
    properties: { summary: { type: 'string' }, severity: { type: 'string' } },
  },
}))).filter(Boolean)
const challenges = await parallel(findings.map(finding => () =>
  agent(`Challenge ${finding.severity}: ${finding.summary}`)
))
return challenges
"""
        _, workflow = self.compile(source)
        validate_workflow(workflow)

        findings, handoff, challenges, _ = workflow["steps"]
        self.assertEqual(findings["output_schema"]["type"], "object")
        self.assertTrue(handoff["intermediate"])
        self.assertTrue(handoff["filter_falsey"])
        self.assertEqual(challenges["item_semantics"], "json")
        self.assertEqual(
            challenges["prompt_template"],
            "Challenge {item.severity}: {item.summary}",
        )
        self.assertEqual(challenges["items_artifact"], handoff["output"])

    def test_direct_agent_synthesizes_filtered_prior_map_as_bounded_context(self):
        source = """\
export const meta = { name: 'synthesize-findings' }
const findings = (await pipeline(['a.py', 'b.py'], file => agent(`Audit ${file}`, {
  schema: {
    type: 'object',
    required: ['summary'],
    properties: { summary: { type: 'string' } },
  },
}))).filter(Boolean)
const summary = await agent(`Rank ${findings}; join ${findings.filter(Boolean).join('\\n---\\n')}; preserve ${JSON.stringify(findings)}.`)
return summary
"""
        _, workflow = self.compile(source)
        validate_workflow(workflow)

        findings, handoff, summary, result = workflow["steps"]
        self.assertEqual(findings["kind"], "agent_map")
        self.assertTrue(handoff["intermediate"])
        self.assertTrue(handoff["filter_falsey"])
        self.assertEqual(handoff["output_limit_bytes"], 1024 * 1024)
        self.assertEqual(summary["kind"], "codex_exec")
        self.assertEqual(summary["context_from"], [handoff["id"]])
        self.assertEqual(summary["depends_on"], [handoff["id"]])
        self.assertEqual(summary["prompt"].count("completed dependency evidence"), 3)
        self.assertEqual(result["source_step"], "summary")

    def test_direct_map_context_rejects_forward_dotted_and_non_text_references(self):
        forward = """\
export const meta = { name: 'forward-synthesis' }
const summary = await agent(`Synthesize ${findings}`)
const findings = await pipeline(['a.py'], file => agent(`Audit ${file}`))
return summary
"""
        self.assert_rejected(forward, "must reference a prior map")

        dotted = """\
export const meta = { name: 'dotted-synthesis' }
const findings = await pipeline(['a.py'], file => agent(`Audit ${file}`))
const summary = await agent(`Count ${findings.length}`)
return summary
"""
        self.assert_rejected(dotted, "prior map result")

        non_text = """\
export const meta = { name: 'non-text-synthesis' }
const findings = await pipeline(['a.py'], file => agent(`Audit ${file}`))
const summary = await agent(findings)
return summary
"""
        self.assert_rejected(non_text, "prior map result")

        dynamic_join = """\
export const meta = { name: 'dynamic-join-synthesis' }
const findings = await pipeline(['a.py'], file => agent(`Audit ${file}`))
const summary = await agent(`Join ${findings.join(args.separator)}`)
return summary
"""
        self.assert_rejected(dynamic_join, "one static string")

        arbitrary_transform = """\
export const meta = { name: 'transform-synthesis' }
const findings = await pipeline(['a.py'], file => agent(`Audit ${file}`))
const summary = await agent(`Slice ${findings.slice(0)}`)
return summary
"""
        self.assert_rejected(arbitrary_transform, "exact map filter/join/JSON.stringify")

    def test_direct_agent_keeps_filtered_and_unfiltered_map_views_distinct(self):
        source = """\
export const meta = { name: 'dual-view-synthesis' }
const findings = await pipeline(['a.py', 'b.py'], file => agent(`Audit ${file}`))
const summary = await agent(`Filtered ${findings.filter(Boolean)}; complete ${JSON.stringify(findings)}.`)
return summary
"""
        _, workflow = self.compile(source)
        validate_workflow(workflow)

        findings, filtered, complete, summary, _ = workflow["steps"]
        self.assertEqual(findings["kind"], "agent_map")
        self.assertTrue(filtered["filter_falsey"])
        self.assertNotIn("filter_falsey", complete)
        self.assertNotEqual(filtered["output"], complete["output"])
        self.assertEqual(
            summary["context_from"],
            [filtered["id"], complete["id"]],
        )
        self.assertEqual(set(summary["context_from"]), set(summary["depends_on"]))

    def test_direct_agent_map_context_enforces_source_ceiling(self):
        maps = "\n".join(
            "const map%02d = await pipeline(['item'], item => agent(`Review ${item}`))"
            % index
            for index in range(33)
        )
        references = " ".join("${map%02d}" % index for index in range(33))
        source = """\
export const meta = { name: 'too-many-contexts' }
%s
const summary = await agent(`%s`)
return summary
""" % (maps, references)
        self.assert_rejected(source, "more than 32 map results")

    def test_map_handoff_rejects_unprovable_shapes_and_non_map_filtering(self):
        missing_schema = """\
export const meta = { name: 'missing-map-schema' }
const findings = await pipeline(args.files, file => agent(`Audit ${file}`))
const reviews = await pipeline(findings, finding => agent(`Review ${finding.summary}`))
return reviews
"""
        self.assert_rejected(missing_schema, "must declare an object output schema")

        scalar_schema = """\
export const meta = { name: 'scalar-map-schema' }
const findings = await pipeline(args.files, file => agent(`Audit ${file}`, {
  schema: { type: 'number' },
}))
const reviews = await pipeline(findings, finding => agent(`Review ${finding}`))
return reviews
"""
        self.assert_rejected(scalar_schema, "root type string or object")

        direct_filter = """\
export const meta = { name: 'direct-filter' }
const found = await agent('Find files', {
  schema: { type: 'object', required: ['files'], properties: { files: { type: 'array', items: { type: 'string' } } } },
})
const reviews = await pipeline(found.files.filter(Boolean), file => agent(`Review ${file}`))
return reviews
"""
        self.assert_rejected(direct_filter, "may filter only a prior map result")

        forward_map = """\
export const meta = { name: 'forward-map' }
const reviews = await pipeline(findings, finding => agent(`Review ${finding}`))
const findings = await pipeline(args.files, file => agent(`Audit ${file}`))
return reviews
"""
        self.assert_rejected(forward_map, "must reference a prior map")

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

    def test_result_contract_validates_cross_step_invariants(self):
        source = {
            "id": "report",
            "kind": "codex_exec",
            "prompt": "Report",
            "capture": "report.md",
        }
        workflow = result_workflow(source)
        validate_workflow(workflow)

        cases = []
        missing_contract = json.loads(json.dumps(workflow))
        del missing_contract["result_artifact"]
        cases.append((missing_contract, "requires a workflow result_artifact"))

        missing_dependency = json.loads(json.dumps(workflow))
        missing_dependency["steps"][1]["depends_on"] = []
        cases.append((missing_dependency, "must be a direct dependency"))

        direct_filter = json.loads(json.dumps(workflow))
        direct_filter["steps"][1]["filter_falsey"] = True
        cases.append((direct_filter, "requires an agent_map source"))

        overwrite = json.loads(json.dumps(workflow))
        overwrite["result_artifact"] = "report.md"
        overwrite["steps"][1]["output"] = "report.md"
        cases.append((overwrite, "must not overwrite"))

        for candidate, detail in cases:
            with self.subTest(detail=detail):
                with self.assertRaisesRegex(ValidationError, detail):
                    validate_workflow(candidate)

    def test_intermediate_result_contract_is_narrow_and_dependency_bound(self):
        source = """\
export const meta = { name: 'intermediate-contract' }
const audits = await pipeline(['a.py', 'b.py'], file => agent(`Audit ${file}`))
const reviews = await pipeline(audits.filter(Boolean), audit => agent(`Review ${audit}`))
return reviews
"""
        _, workflow = self.compile(source)
        validate_workflow(workflow)
        handoff = workflow["steps"][1]
        consumer = workflow["steps"][2]

        cases = []
        terminal = json.loads(json.dumps(workflow))
        terminal["steps"][1]["intermediate"] = False
        cases.append((terminal, "exactly one terminal collect_results"))

        orphaned = json.loads(json.dumps(workflow))
        orphaned["steps"][1]["output"] = "claude-workflow/handoffs/orphan.json"
        cases.append((orphaned, "must feed a later agent_map"))

        missing_dependency = json.loads(json.dumps(workflow))
        missing_dependency["steps"][2]["depends_on"] = []
        cases.append((missing_dependency, "must directly depend on intermediate collector"))

        nested_pointer = json.loads(json.dumps(workflow))
        nested_pointer["steps"][2]["items_pointer"] = "/items"
        cases.append((nested_pointer, "at the JSON root"))

        path_semantics = json.loads(json.dumps(workflow))
        path_semantics["steps"][2].pop("item_semantics")
        cases.append((path_semantics, "must use opaque or json semantics"))

        result_collision = json.loads(json.dumps(workflow))
        result_collision["steps"][1]["output"] = result_collision["result_artifact"]
        result_collision["steps"][2]["items_artifact"] = result_collision["result_artifact"]
        cases.append((result_collision, "must not equal workflow result_artifact"))

        for candidate, detail in cases:
            with self.subTest(detail=detail):
                with self.assertRaisesRegex(ValidationError, detail):
                    validate_workflow(candidate)

        self.assertEqual(consumer["items_artifact"], handoff["output"])

    def test_workflow_schema_exports_result_contract(self):
        schema = get_schema("workflow")
        self.assertIn("result_artifact", schema["properties"])
        step_schemas = schema["properties"]["steps"]["items"]["oneOf"]
        collector = next(
            item
            for item in step_schemas
            if item["properties"]["kind"]["const"] == "collect_results"
        )
        self.assertEqual(collector["required"], ["id", "kind", "source_step", "output"])
        self.assertEqual(collector["properties"]["filter_falsey"]["type"], "boolean")
        self.assertEqual(collector["properties"]["intermediate"]["type"], "boolean")

    def test_collects_canonical_structured_direct_result(self):
        source = {
            "id": "report",
            "kind": "codex_exec",
            "prompt": "unused",
            "capture": "report.json",
            "output_schema": {},
            "fixture_output": '{"z":2,"a":1}',
        }
        workflow = result_workflow(source)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = ResultFixtureRunner(
                workflow,
                root,
                root / "runs",
                RuntimePolicy(),
                run_id="structured-result",
            ).execute()

            self.assertEqual(
                (run.artifacts_dir / "result.json").read_text(encoding="utf-8"),
                '{"a":1,"z":2}\n',
            )
            state = run.read_state()["steps"]["result"]
            self.assertEqual(state["result_item_count"], 1)
            self.assertEqual(state["result_source_count"], 1)
            self.assertEqual(len(state["result_source_sha256"]), 64)
            self.assertEqual(len(state["result_output_sha256"]), 64)

    def test_collects_map_results_in_order_with_javascript_falsey_filter(self):
        source = {
            "id": "reviews",
            "kind": "agent_map",
            "items": ["a", "b", "a", "d", "e", "f"],
            "preserve_duplicate_items": True,
            "prompt_template": "Review {item}",
            "capture_dir": "reviews",
            "output_schema": {},
            "fixture_outputs": ['"first"', '""', '"second"', "{}", "[]", "false"],
        }
        workflow = result_workflow(source, filter_falsey=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = ResultFixtureRunner(
                workflow,
                root,
                root / "runs",
                RuntimePolicy(),
                run_id="map-result",
            ).execute()

            self.assertEqual(
                (run.artifacts_dir / "result.json").read_text(encoding="utf-8"),
                '["first","second",{},[]]\n',
            )
            state = run.read_state()["steps"]["result"]
            self.assertEqual(state["result_source_count"], 6)
            self.assertEqual(state["result_item_count"], 4)
            self.assertTrue(state["result_filter_falsey"])

    def test_executes_filtered_map_to_map_handoff_and_detects_resume_drift(self):
        source = """\
export const meta = { name: 'runtime-handoff' }
const audits = await pipeline(['a.py', 'b.py', 'c.py'], file => agent(`Audit ${file}`))
const reviews = await pipeline(audits.filter(Boolean), audit => agent(`Review ${audit}`))
return reviews
"""
        _, workflow = self.compile(source)
        workflow["steps"][0]["fixture_outputs"] = ["alpha", "", "beta"]
        workflow["steps"][2]["fixture_outputs"] = ["review-alpha", "review-beta"]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = ResultFixtureRunner(
                workflow,
                root,
                root / "runs",
                RuntimePolicy(allow_agent=True, allow_parallel=True),
                run_id="runtime-handoff",
            ).execute()

            handoff = workflow["steps"][1]
            self.assertEqual(
                (run.artifacts_dir / handoff["output"]).read_text(encoding="utf-8"),
                '["alpha","beta"]\n',
            )
            self.assertEqual(
                (run.artifacts_dir / workflow["result_artifact"]).read_text(encoding="utf-8"),
                '["review-alpha","review-beta"]\n',
            )
            state = run.read_state()["steps"][handoff["id"]]
            self.assertEqual(state["result_source_count"], 3)
            self.assertEqual(state["result_item_count"], 2)
            self.assertTrue(state["result_filter_falsey"])

            (run.artifacts_dir / handoff["output"]).write_text(
                '["changed"]\n',
                encoding="utf-8",
            )
            resumed = ResultFixtureRunner(
                workflow,
                root,
                root / "runs",
                RuntimePolicy(allow_agent=True, allow_parallel=True),
                resume_dir=run.run_dir,
            )
            with self.assertRaisesRegex(ValidationError, "output changed after completion"):
                resumed.execute()

    def test_map_consumer_revalidates_handoff_immediately_before_launch(self):
        source = """\
export const meta = { name: 'runtime-handoff-race' }
const audits = await pipeline(['a.py'], file => agent(`Audit ${file}`))
const reviews = await pipeline(audits.filter(Boolean), audit => agent(`Review ${audit}`))
return reviews
"""
        _, workflow = self.compile(source)
        workflow["steps"][0]["fixture_outputs"] = ["alpha"]
        workflow["steps"][2]["fixture_outputs"] = ["review-alpha"]

        class HandoffTamperRunner(ResultFixtureRunner):
            def _agent_map(self, step):
                if step["id"] == "reviews":
                    handoff = self.workflow["steps"][1]
                    self.run.write_artifact(handoff["output"], '["changed"]\n')
                super()._agent_map(step)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = HandoffTamperRunner(
                workflow,
                root,
                root / "runs",
                RuntimePolicy(allow_agent=True, allow_parallel=True),
                run_id="runtime-handoff-race",
            )
            with self.assertRaisesRegex(ValidationError, "output changed after completion"):
                runner.execute()

    def test_executes_map_to_synthesis_context_and_revalidates_before_launch(self):
        source = """\
export const meta = { name: 'runtime-synthesis' }
const findings = (await pipeline(['a.py', 'b.py', 'c.py'], file => agent(`Audit ${file}`))).filter(Boolean)
const summary = await agent(`Rank these findings: ${findings}`)
return summary
"""
        _, workflow = self.compile(source)
        workflow["steps"][0]["fixture_outputs"] = ["alpha", "", "beta"]
        workflow["steps"][2]["fixture_output"] = "ranked-summary"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = ResultFixtureRunner(
                workflow,
                root,
                root / "runs",
                RuntimePolicy(allow_agent=True, allow_parallel=True),
                run_id="runtime-synthesis",
            )
            run = runner.execute()
            self.assertIn('["alpha","beta"]', runner.prepared_dependency_context)
            self.assertIn("step=claude-handoff-001", runner.prepared_dependency_context)
            self.assertEqual(
                (run.artifacts_dir / workflow["result_artifact"]).read_text(
                    encoding="utf-8"
                ),
                '"ranked-summary"\n',
            )

        class SynthesisTamperRunner(ResultFixtureRunner):
            def _codex_exec(self, step):
                if step["id"] == "summary":
                    handoff = self.workflow["steps"][1]
                    self.run.write_artifact(handoff["output"], '["changed"]\n')
                super()._codex_exec(step)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = SynthesisTamperRunner(
                workflow,
                root,
                root / "runs",
                RuntimePolicy(allow_agent=True, allow_parallel=True),
                run_id="runtime-synthesis-tamper",
            )
            with self.assertRaisesRegex(ValidationError, "output changed after completion"):
                runner.execute()

    def test_collect_results_rejects_non_strict_and_oversized_outputs(self):
        invalid_values = [
            '{"a":1,"a":2}',
            "NaN",
            "1e999",
        ]
        for index, invalid in enumerate(invalid_values):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = {
                    "id": "report",
                    "kind": "codex_exec",
                    "prompt": "unused",
                    "output_schema": {},
                    "fixture_output": invalid,
                }
                runner = ResultFixtureRunner(
                    result_workflow(source),
                    root,
                    root / "runs",
                    RuntimePolicy(),
                    run_id="invalid-%d" % index,
                )
                with self.assertRaisesRegex(ValidationError, "strict JSON|finite JSON"):
                    runner.execute()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = {
                "id": "report",
                "kind": "codex_exec",
                "prompt": "unused",
                "fixture_output": "abcd",
            }
            runner = ResultFixtureRunner(
                result_workflow(source, output_limit_bytes=6),
                root,
                root / "runs",
                RuntimePolicy(),
                run_id="oversized",
            )
            with self.assertRaisesRegex(ValidationError, "exceeds output_limit_bytes 6"):
                runner.execute()

    def test_collect_results_rejects_symlinked_source_output(self):
        class SymlinkSourceRunner(ResultFixtureRunner):
            def _codex_exec(self, step):
                super()._codex_exec(step)
                path = self.run.resolve_artifact_path(step.get("capture", "%s.md" % step["id"]))
                outside = self.workspace / "outside.txt"
                outside.write_text("outside", encoding="utf-8")
                path.unlink()
                path.symlink_to(outside)

        source = {
            "id": "report",
            "kind": "codex_exec",
            "prompt": "unused",
            "fixture_output": "report",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = SymlinkSourceRunner(
                result_workflow(source),
                root,
                root / "runs",
                RuntimePolicy(),
                run_id="symlinked-source",
            )
            try:
                with self.assertRaisesRegex(ValidationError, "must not be a symlink"):
                    runner.execute()
            except OSError:
                self.skipTest("symlinks are not supported on this filesystem")

    def test_collect_results_rejects_skipped_source_step(self):
        source = {
            "id": "report",
            "kind": "codex_exec",
            "prompt": "unused",
            "capture": "report.md",
        }
        workflow = result_workflow(source)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = ResultFixtureRunner(
                workflow,
                root,
                root / "runs",
                RuntimePolicy(),
                run_id="skipped-source",
            )
            runner.run.write_artifact("report.md", "stale")
            runner.run.mark_step("report", "skipped", "fixture skip", kind="codex_exec")
            with self.assertRaisesRegex(ValidationError, "source step report must be completed"):
                runner.execute()

    def test_collect_results_resume_rejects_source_and_result_drift(self):
        for target in ("source", "result"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = {
                    "id": "report",
                    "kind": "codex_exec",
                    "prompt": "unused",
                    "capture": "report.md",
                    "fixture_output": "original",
                }
                workflow = result_workflow(source)
                run = ResultFixtureRunner(
                    workflow,
                    root,
                    root / "runs",
                    RuntimePolicy(),
                    run_id="drift-%s" % target,
                ).execute()
                relative = "report.md" if target == "source" else "result.json"
                (run.artifacts_dir / relative).write_text("changed\n", encoding="utf-8")

                resumed = WorkflowRunner(
                    workflow,
                    root,
                    root / "runs",
                    RuntimePolicy(),
                    resume_dir=run.run_dir,
                )
                detail = "source outputs changed" if target == "source" else "output changed"
                with self.assertRaisesRegex(ValidationError, detail):
                    resumed.execute()

    def test_cli_prints_saved_workflow_result_only_when_requested(self):
        source = """\
export const meta = { name: 'report' }
const report = await agent('CLI result')
return report
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow_dir = root / ".claude" / "workflows"
            workflow_dir.mkdir(parents=True)
            (workflow_dir / "report.js").write_text(source, encoding="utf-8")
            stdout = StringIO()
            with patch("conductor_runtime.legacy_cli.WorkflowRunner", ResultFixtureRunner):
                with redirect_stdout(stdout):
                    code = cli_main(
                        [
                            "run-saved-workflow",
                            "report",
                            "--workspace",
                            str(root),
                            "--runs-dir",
                            str(root / "runs"),
                            "--run-id",
                            "printed-result",
                            "--print-result",
                        ]
                    )
            self.assertEqual(code, 0)
            self.assertIn("Result artifact:", stdout.getvalue())
            self.assertIn("Result:\nCLI result\n", stdout.getvalue())

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
            self.assertIn("claude-result [collect_results", stdout.getvalue())

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
            self.assertIn("Result artifact (planned):", stdout.getvalue())
            persisted = json.loads(
                (root / "runs" / "dynamic-dry-run" / "workflow.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["steps"][1]["items_pointer"], "/files")
            self.assertEqual(persisted["steps"][2]["kind"], "collect_results")

            stderr = StringIO()
            with redirect_stderr(stderr):
                code = cli_main(
                    [
                        "run-saved-workflow",
                        "audit-routes",
                        "--workspace",
                        str(root),
                        "--dry-run",
                        "--print-result",
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("--print-result requires real workflow execution", stderr.getvalue())

    def test_cli_keep_going_reports_complete_nonexecuting_corpus(self):
        valid = """\
export const meta = { name: 'valid', whenToUse: 'Focused audits' }
const report = await agent('Review ' + args.target, { effort: 'high' })
return report
"""
        invalid = """\
export const meta = { name: 'invalid' }
if (args.enabled) { await agent('Review') }
const report = await agent('Fallback')
return report
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow_dir = root / ".claude" / "workflows"
            workflow_dir.mkdir(parents=True)
            (workflow_dir / "valid.js").write_text(valid, encoding="utf-8")
            (workflow_dir / "invalid.js").write_text(invalid, encoding="utf-8")
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = cli_main(
                    [
                        "validate-saved-workflows",
                        "--workspace",
                        str(root),
                        "--keep-going",
                    ]
                )

        self.assertEqual(code, 2)
        self.assertIn("OK:", stdout.getvalue())
        self.assertIn("valid", stdout.getvalue())
        self.assertIn("INVALID:", stdout.getvalue())
        self.assertIn("invalid.js", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
