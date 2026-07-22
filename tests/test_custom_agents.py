import json
import os
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path

from tools.check_codex_agents import (
    AgentCheckError,
    extract_advertised_agent_roles,
    probe_codex_agent_discovery,
    validate_agent_profiles,
    validate_codex_discovery_request,
    validate_installed_profiles,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODEX = shutil.which("codex")
EXPECTED_AGENT_NAMES = {
    "conductor-docs-researcher",
    "conductor-explorer",
    "conductor-reviewer",
    "conductor-verifier",
    "conductor-worker",
}


def write_profile(
    root: Path,
    name: str,
    *,
    filename: str | None = None,
    description: str = "A bounded test agent.",
    sandbox_mode: str = "read-only",
    instructions: str = "Perform only the assigned test task.",
) -> Path:
    path = root / (filename or (name + ".toml"))
    path.write_text(
        "\n".join(
            [
                "name = %s" % json.dumps(name),
                "description = %s" % json.dumps(description),
                "sandbox_mode = %s" % json.dumps(sandbox_mode),
                "developer_instructions = %s" % json.dumps(instructions),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def discovery_request(roles: dict[str, str]):
    role_text = "\n".join("%s: {\n%s\n}" % item for item in roles.items())
    return {
        "model": "probe-model",
        "stream": True,
        "tools": [
            {
                "type": "namespace",
                "name": "multi_agent_v1",
                "tools": [
                    {
                        "type": "function",
                        "name": "spawn_agent",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "agent_type": {
                                    "type": "string",
                                    "description": "Optional type.\nAvailable roles:\n" + role_text,
                                }
                            },
                        },
                    }
                ],
            }
        ],
    }


class CustomAgentProfileTests(unittest.TestCase):
    def test_repository_quality_defaults_are_aggressive_but_bounded(self):
        config = tomllib.loads(
            (PROJECT_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(config["model"], "gpt-5.6-sol")
        self.assertEqual(config["model_reasoning_effort"], "ultra")
        self.assertEqual(config["service_tier"], "priority")
        self.assertEqual(config["agents"], {"max_threads": 16, "max_depth": 2})

    def test_repository_profiles_are_valid_and_complete(self):
        profiles = validate_agent_profiles(PROJECT_ROOT / "codex-agents")
        self.assertEqual({profile.name for profile in profiles}, EXPECTED_AGENT_NAMES)
        self.assertEqual(
            {profile.name: profile.sandbox_mode for profile in profiles},
            {
                "conductor-docs-researcher": "read-only",
                "conductor-explorer": "read-only",
                "conductor-reviewer": "read-only",
                "conductor-verifier": "read-only",
                "conductor-worker": "workspace-write",
            },
        )

    def test_profile_requires_every_owned_string_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "incomplete.toml").write_text(
                'name = "incomplete"\ndescription = "Missing instructions."\nsandbox_mode = "read-only"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AgentCheckError, "developer_instructions"):
                validate_agent_profiles(root)

    def test_profile_name_must_match_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_profile(root, "actual-name", filename="different.toml")
            with self.assertRaisesRegex(AgentCheckError, "filename must match"):
                validate_agent_profiles(root)

    def test_profile_names_must_be_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_profile(root, "shared", filename="first.toml")
            write_profile(root, "shared", filename="second.toml")
            with self.assertRaisesRegex(AgentCheckError, "duplicated: shared"):
                validate_agent_profiles(root)

    def test_profiles_reject_unbounded_sandbox_and_unexpected_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_profile(root, "unsafe", sandbox_mode="danger-full-access")
            with self.assertRaisesRegex(AgentCheckError, "sandbox_mode must be one of"):
                validate_agent_profiles(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_profile(root, "safe")
            (root / "notes.md").write_text("not an agent profile\n", encoding="utf-8")
            with self.assertRaisesRegex(AgentCheckError, "unexpected entries: notes.md"):
                validate_agent_profiles(root)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink validation requires symlink support")
    def test_profiles_reject_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = write_profile(root, "target")
            linked = root / "linked.toml"
            linked.symlink_to(target)
            with self.assertRaisesRegex(AgentCheckError, "regular non-symlink"):
                validate_agent_profiles(root)

    def test_installed_profiles_must_match_source_but_may_include_unrelated_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            installed = root / "codex-home" / "agents"
            source.mkdir()
            installed.mkdir(parents=True)
            profile = write_profile(source, "managed")
            (installed / profile.name).write_bytes(profile.read_bytes())
            write_profile(installed, "personal")

            profiles = validate_agent_profiles(source)
            self.assertEqual(validate_installed_profiles(profiles, root / "codex-home"), installed)

            (installed / profile.name).write_text(profile.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
            with self.assertRaisesRegex(AgentCheckError, "differs from source: managed"):
                validate_installed_profiles(profiles, root / "codex-home")


class CodexDiscoveryProtocolTests(unittest.TestCase):
    def test_extracts_namespaced_spawn_agent_role_blocks(self):
        roles = extract_advertised_agent_roles(
            discovery_request(
                {
                    "conductor-explorer": "Read-only explorer.",
                    "default": "Default agent.",
                }
            )
        )
        self.assertEqual(
            roles,
            {
                "conductor-explorer": "Read-only explorer.",
                "default": "Default agent.",
            },
        )

    def test_discovery_request_rejects_missing_spawn_tool(self):
        request = discovery_request({"default": "Default agent."})
        request["tools"][0]["tools"] = []
        with self.assertRaisesRegex(AgentCheckError, "exactly one spawn_agent"):
            extract_advertised_agent_roles(request)

    def test_discovery_request_rejects_missing_or_wrong_custom_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_profile(root, "conductor-explorer", description="Repository explorer.")
            profiles = validate_agent_profiles(root)

            with self.assertRaisesRegex(AgentCheckError, "did not advertise"):
                validate_codex_discovery_request(
                    discovery_request({"default": "Default agent."}),
                    profiles,
                )
            with self.assertRaisesRegex(AgentCheckError, "wrong description"):
                validate_codex_discovery_request(
                    discovery_request({"conductor-explorer": "Different role."}),
                    profiles,
                )

    @unittest.skipIf(CODEX is None, "Codex CLI is required for real discovery coverage")
    def test_real_codex_discovers_profiles_through_isolated_loopback_provider(self):
        result = probe_codex_agent_discovery(CODEX, PROJECT_ROOT / "codex-agents", timeout_seconds=20)
        self.assertRegex(result.codex_version, r"^codex-cli \d+\.\d+\.\d+$")
        self.assertEqual(result.request_path, "/v1/responses")
        self.assertEqual(result.request_count, 1)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(EXPECTED_AGENT_NAMES.issubset(result.advertised_agent_types))


if __name__ == "__main__":
    unittest.main()
