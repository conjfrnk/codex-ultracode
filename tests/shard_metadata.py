"""Exhaustive repository test-module ownership for focused shard execution."""

from collections import Counter
from pathlib import Path


TEST_SHARD_ORDER = (
    "fast_core",
    "installer_package",
    "extras_runtime",
    "background_routine",
    "team",
    "benchmark",
    "ui_server",
)

TEST_MODULE_SHARDS = {
    "fast_core": (
        "test_core_adversarial",
        "test_core_cli",
        "test_core_policy_security",
        "test_core_runtime",
        "test_core_stage_lifecycle",
    ),
    "installer_package": (
        "test_bundle_installer",
        "test_plugin_package",
        "test_python_support",
        "test_release_checksums",
        "test_test_sharding",
        "test_verification_tools",
    ),
    "extras_runtime": (
        "test_claude_saved_workflows",
        "test_extras_cli_namespaces",
        "test_workspace_merge",
    ),
    "background_routine": (
        "test_agent_lifecycle_hooks",
        "test_desktop_notification",
        "test_dynamic_goal_interval",
        "test_routine_cron",
    ),
    "team": (
        "test_agent_memory",
        "test_agent_native_tool_hooks",
        "test_agent_profiles",
        "test_agent_team",
        "test_agent_tool_policy",
        "test_dependency_context",
        "test_skill_mcp_auth_broker",
        "test_skill_mcp_stdio",
        "test_structured_packet_items",
    ),
    "benchmark": (
        "test_auto_topology",
        "test_codex_native_usage",
        "test_readonly_parity",
    ),
    "ui_server": (
        "test_system_doctor",
    ),
}

RUNTIME_SHARD_BY_TEST_SHARD = {
    "fast_core": None,
    "installer_package": "installer_package",
    "extras_runtime": "extras_runtime",
    "background_routine": "background_routine",
    "team": "team",
    "benchmark": "benchmark",
    "ui_server": "ui_server",
}

FULL_DISCOVERY_AGGREGATOR = "test_runtime"


def validate_test_module_shards(test_dir=None):
    """Fail closed when a discovered test module has ambiguous or missing ownership."""
    root = Path(test_dir) if test_dir is not None else Path(__file__).resolve().parent
    discovered = {path.stem for path in root.glob("test_*.py")}
    assigned = [module for shard in TEST_SHARD_ORDER for module in TEST_MODULE_SHARDS[shard]]
    counts = Counter(assigned)
    duplicates = sorted(module for module, count in counts.items() if count != 1)
    missing = sorted(discovered - set(assigned) - {FULL_DISCOVERY_AGGREGATOR})
    unknown = sorted(set(assigned) - discovered)
    unexpected_shards = sorted(set(TEST_MODULE_SHARDS) - set(TEST_SHARD_ORDER))
    runtime_mapping_shards = sorted(set(RUNTIME_SHARD_BY_TEST_SHARD) ^ set(TEST_SHARD_ORDER))
    if duplicates or missing or unknown or unexpected_shards or runtime_mapping_shards:
        detail = (
            "duplicates=%r missing=%r unknown=%r unexpected_shards=%r "
            "runtime_mapping_shards=%r"
        ) % (
            duplicates,
            missing,
            unknown,
            unexpected_shards,
            runtime_mapping_shards,
        )
        raise RuntimeError("test shard metadata is invalid: " + detail)
    return {shard: len(TEST_MODULE_SHARDS[shard]) for shard in TEST_SHARD_ORDER}


validate_test_module_shards()
