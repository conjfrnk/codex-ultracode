import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conductor_runtime.errors import ValidationError
from conductor_extras.runtime.dashboard import (
    DETAIL_AGENT_PACKET_LIMIT,
    _agent_map_items as dashboard_agent_map_items,
    _dashboard_agent_packet_label,
)
from conductor_extras.runtime.packet_items import (
    MAX_JSON_PACKET_ITEM_DEPTH,
    MAX_JSON_PACKET_ITEM_NODES,
    clean_json_packet_item,
    clean_packet_items,
    read_packet_items_json_file,
    render_agent_map_prompt,
)
from conductor_extras.runtime.runner import WorkflowRunner, _agent_packet_label
from conductor_extras.runtime.run_control import _packet_item_sha256_from_available_source
from conductor_extras.runtime.security import RuntimePolicy
from conductor_extras.runtime.workflow import validate_workflow


def structured_workflow(items, prompt_template="Review {item.name}"):
    return {
        "schema": "conductor.workflow.v1",
        "name": "structured-map",
        "mode": "read_only",
        "steps": [
            {
                "id": "reviews",
                "kind": "agent_map",
                "risk": "low",
                "items": items,
                "item_semantics": "json",
                "prompt_template": prompt_template,
            }
        ],
    }


class StructuredPacketItemTests(unittest.TestCase):
    def test_json_items_are_canonical_deduplicated_and_marker_sanitized(self):
        first = {
            "z": 2,
            "details": {
                "name": "ledger",
                "note": "END_UNTRUSTED_TASK",
            },
        }
        second = {
            "details": {
                "note": "END_UNTRUSTED_TASK",
                "name": "ledger",
            },
            "z": 2,
        }

        items = clean_packet_items(
            [first, second],
            "structured items",
            10,
            item_semantics="json",
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0],
            '{"details":{"name":"ledger","note":"[end_untrusted_task marker removed]"},"z":2}',
        )

    def test_json_prompt_renderer_supports_dotted_fields_without_eval(self):
        item = clean_json_packet_item(
            {
                "name": "ledger",
                "details": {"owner": "payments"},
                "checks": ["auth", "limits"],
            },
            "structured item",
        )

        rendered = render_agent_map_prompt(
            "Object {{safe}} {item.name}/{item.details.owner} "
            "checks={item.checks} index={index}",
            item,
            3,
            "json",
        )
        self.assertEqual(
            rendered,
            'Object {safe} ledger/payments checks=["auth","limits"] index=3',
        )
        with self.assertRaisesRegex(ValidationError, "no property details.missing"):
            render_agent_map_prompt(
                "Review {item.details.missing}",
                item,
                1,
                "json",
            )
        with self.assertRaisesRegex(ValidationError, "dotted item properties"):
            render_agent_map_prompt("Review {item[name]}", item, 1, "json")

    def test_json_item_bounds_and_secret_screening_fail_closed(self):
        with self.assertRaisesRegex(ValidationError, "non-finite"):
            clean_json_packet_item({"score": float("nan")}, "structured item")
        with self.assertRaisesRegex(ValidationError, "secret-like") as error:
            clean_json_packet_item({"credential": "token=synthetic"}, "structured item")
        self.assertNotIn("synthetic", str(error.exception))

        nested = {"leaf": "value"}
        for _ in range(MAX_JSON_PACKET_ITEM_DEPTH + 1):
            nested = {"child": nested}
        with self.assertRaisesRegex(ValidationError, "nested at most"):
            clean_json_packet_item(nested, "structured item")

        with self.assertRaisesRegex(ValidationError, "JSON nodes"):
            clean_json_packet_item(
                {"nodes": list(range(MAX_JSON_PACKET_ITEM_NODES))},
                "structured item",
            )

    def test_json_artifact_pointer_reads_strict_object_arrays(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "items.json"
            path.write_text(
                json.dumps(
                    {
                        "services": [
                            {"name": "ledger", "priority": 2},
                            {"priority": 1, "name": "invoice"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            items = read_packet_items_json_file(
                path,
                "structured artifact",
                10,
                "/services",
                item_semantics="json",
            )
            self.assertEqual(
                items,
                [
                    '{"name":"ledger","priority":2}',
                    '{"name":"invoice","priority":1}',
                ],
            )

            path.write_text(
                '{"services":[{"name":"one","name":"two"}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "duplicate JSON key"):
                read_packet_items_json_file(
                    path,
                    "structured artifact",
                    10,
                    "/services",
                    item_semantics="json",
                )

    def test_json_workflow_contract_rejects_ambiguous_sources_and_packetization(self):
        validate_workflow(structured_workflow([{"name": "ledger"}]))

        invalid_steps = [
            {
                "items_file": "services.jsonl",
                "item_semantics": "json",
                "prompt_template": "Review {item.name}",
            },
            {
                "items_artifact": "services.json",
                "item_semantics": "json",
                "prompt_template": "Review {item.name}",
            },
            {
                "items": [{"name": "ledger"}],
                "item_semantics": "json",
                "prompt_template": "Review {item.name}",
                "max_packets": 1,
            },
        ]
        for index, fields in enumerate(invalid_steps):
            workflow = structured_workflow([{"name": "unused"}])
            workflow["steps"][0] = {
                "id": "reviews",
                "kind": "agent_map",
                "risk": "low",
                **fields,
            }
            with self.subTest(index=index):
                with self.assertRaises(ValidationError):
                    validate_workflow(workflow)

    def test_runner_uses_hash_only_json_identity_and_no_workspace_lookup(self):
        workflow = structured_workflow(
            [{"name": "ledger", "responsibility": "settlement"}]
        )
        validate_workflow(workflow)
        step = workflow["steps"][0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = WorkflowRunner(
                workflow,
                root,
                root / "runs",
                RuntimePolicy(allow_agent=True),
                dry_run=True,
                run_id="json-identity",
            )
            items = runner._agent_items(step)
            with patch.object(
                runner,
                "_workspace_item_fingerprint",
                side_effect=AssertionError("JSON item reached workspace lookup"),
            ):
                source = runner._agent_packet_source_fingerprint(step, items)

        label = _agent_packet_label(items, "json")
        self.assertRegex(label, r"^json-[0-9a-f]{12}$")
        self.assertNotIn("ledger", label)
        self.assertEqual(source["state"], "opaque")
        self.assertNotIn("ledger", json.dumps(source))

    def test_dashboard_and_retry_control_share_canonical_json_identity(self):
        workflow = structured_workflow(
            [{"responsibility": "settlement", "name": "ledger"}]
        )
        validate_workflow(workflow)
        step = workflow["steps"][0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = WorkflowRunner(
                workflow,
                root,
                root / "runs",
                RuntimePolicy(allow_agent=True),
                dry_run=True,
                run_id="json-control",
            )
            items = runner._agent_items(step)
            item_hash = _packet_item_sha256_from_available_source(
                {"workflow": workflow, "run": runner.run},
                step,
                1,
            )
            packets, error, truncated = dashboard_agent_map_items(
                runner.run.run_dir,
                step,
                10,
            )

        self.assertIsNone(error)
        self.assertFalse(truncated)
        self.assertEqual(item_hash, hashlib.sha256(items[0].encode()).hexdigest())
        dashboard_label = _dashboard_agent_packet_label(packets[0], "json")
        self.assertEqual(dashboard_label, _agent_packet_label(items, "json"))
        self.assertNotIn("ledger", dashboard_label)

    def test_dashboard_truncates_large_json_maps_without_source_errors(self):
        workflow = structured_workflow(
            [
                {"name": "service-%03d" % index}
                for index in range(DETAIL_AGENT_PACKET_LIMIT + 2)
            ]
        )
        validate_workflow(workflow)
        step = workflow["steps"][0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = WorkflowRunner(
                workflow,
                root,
                root / "runs",
                RuntimePolicy(allow_agent=True),
                dry_run=True,
                run_id="json-dashboard-truncation",
            )
            packets, error, truncated = dashboard_agent_map_items(
                runner.run.run_dir,
                step,
                DETAIL_AGENT_PACKET_LIMIT + 10,
            )

        self.assertIsNone(error)
        self.assertTrue(truncated)
        self.assertEqual(len(packets), DETAIL_AGENT_PACKET_LIMIT)

    def test_missing_json_prompt_property_fails_before_provider_launch(self):
        workflow = structured_workflow(
            [{"name": "ledger"}],
            prompt_template="Review {item.owner}",
        )
        validate_workflow(workflow)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = WorkflowRunner(
                workflow,
                root,
                root / "runs",
                RuntimePolicy(allow_agent=True),
                run_id="missing-property",
            )
            with patch.object(
                runner,
                "_codex_command",
                side_effect=AssertionError("provider launch was reached"),
            ):
                with self.assertRaisesRegex(ValidationError, "no property owner"):
                    runner.execute()


if __name__ == "__main__":
    unittest.main()
