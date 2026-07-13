import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.install_bundle import build_skill_manifest
from tools.package_plugin import validate_plugin_integrity


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = "codex-conductor-marketplace"
PLUGIN_RELATIVE = Path("plugins") / "codex-conductor"


def package_marketplace(root: Path) -> Path:
    dist = root / "dist"
    result = subprocess.run(
        [sys.executable, "-B", str(PROJECT_ROOT / "tools" / "package_runtime.py"), str(dist)],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return dist / "codex-conductor-marketplace.zip"


def extract_marketplace(archive: Path, root: Path) -> Path:
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(root)
    return root / ARCHIVE_ROOT


class PluginPackageTests(unittest.TestCase):
    def test_plugin_archive_binds_manifest_skill_and_embedded_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_path = package_marketplace(root)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertIsNone(archive.testzip())
                names = set(archive.namelist())
                self.assertIn(
                    "%s/.agents/plugins/marketplace.json" % ARCHIVE_ROOT,
                    names,
                )
                self.assertIn(
                    "%s/plugins/codex-conductor/.codex-plugin/plugin.json" % ARCHIVE_ROOT,
                    names,
                )
                self.assertIn(
                    "%s/plugins/codex-conductor/skills/codex-conductor/SKILL.md" % ARCHIVE_ROOT,
                    names,
                )
                self.assertIn(
                    "%s/plugins/codex-conductor/scripts/conductor-runtime.pyz" % ARCHIVE_ROOT,
                    names,
                )
                self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))

            marketplace_root = extract_marketplace(archive_path, root / "extracted")
            marketplace = json.loads(
                (marketplace_root / ".agents" / "plugins" / "marketplace.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(marketplace["name"], "codex-conductor-local")
            self.assertEqual(marketplace["plugins"][0]["name"], "codex-conductor")
            self.assertEqual(
                marketplace["plugins"][0]["source"],
                {"source": "local", "path": "./plugins/codex-conductor"},
            )
            self.assertEqual(
                marketplace["plugins"][0]["policy"],
                {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            )

            plugin_root = marketplace_root / PLUGIN_RELATIVE
            plugin_manifest = json.loads(
                (plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
            )
            integrity = json.loads((plugin_root / "plugin-integrity.json").read_text(encoding="utf-8"))
            self.assertEqual(validate_plugin_integrity(plugin_root), integrity)
            self.assertEqual(plugin_manifest["name"], "codex-conductor")
            self.assertEqual(plugin_manifest["version"], integrity["version"])
            self.assertEqual(plugin_manifest["skills"], "./skills/")
            self.assertNotIn("mcpServers", plugin_manifest)
            self.assertNotIn("apps", plugin_manifest)

            manifest_bytes = (plugin_root / ".codex-plugin" / "plugin.json").read_bytes()
            self.assertEqual(
                integrity["plugin_manifest"]["sha256"],
                hashlib.sha256(manifest_bytes).hexdigest(),
            )
            runtime = plugin_root / "scripts" / "conductor-runtime.pyz"
            self.assertEqual(integrity["runtime"]["sha256"], hashlib.sha256(runtime.read_bytes()).hexdigest())
            skill = build_skill_manifest(plugin_root / "skills" / "codex-conductor")
            self.assertEqual(integrity["skill"]["tree_sha256"], skill["tree_sha256"])
            self.assertEqual(integrity["skill"]["files"], skill["files"])

            invoked = subprocess.run(
                [sys.executable, "-B", str(runtime), "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            self.assertEqual(invoked.returncode, 0, invoked.stderr)
            self.assertEqual(invoked.stdout.strip(), "conductor-runtime %s" % plugin_manifest["version"])

            runtime.write_bytes(runtime.read_bytes() + b"tampered")
            with self.assertRaisesRegex(ValueError, "runtime does not match"):
                validate_plugin_integrity(plugin_root)

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is required for plugin ingestion coverage")
    def test_plugin_marketplace_installs_in_isolated_codex_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marketplace_root = extract_marketplace(package_marketplace(root), root / "extracted")
            home = root / "home"
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            environment = dict(os.environ)
            environment.update({"HOME": str(home), "CODEX_HOME": str(codex_home)})
            codex = shutil.which("codex")

            added = subprocess.run(
                [codex, "plugin", "marketplace", "add", str(marketplace_root), "--json"],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            self.assertEqual(added.returncode, 0, added.stderr)
            self.assertEqual(json.loads(added.stdout)["marketplaceName"], "codex-conductor-local")

            installed = subprocess.run(
                [codex, "plugin", "add", "codex-conductor@codex-conductor-local", "--json"],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            installed_record = json.loads(installed.stdout)
            self.assertEqual(installed_record["name"], "codex-conductor")
            installed_path = Path(installed_record["installedPath"])
            self.assertTrue(installed_path.is_relative_to(codex_home.resolve()))

            listed = subprocess.run(
                [codex, "plugin", "list", "--json"],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            plugins = json.loads(listed.stdout)["installed"]
            self.assertEqual(len(plugins), 1)
            self.assertEqual(plugins[0]["pluginId"], "codex-conductor@codex-conductor-local")
            self.assertTrue(plugins[0]["enabled"])

            runtime = installed_path / "scripts" / "conductor-runtime.pyz"
            invoked = subprocess.run(
                [sys.executable, "-B", str(runtime), "--version"],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            self.assertEqual(invoked.returncode, 0, invoked.stderr)
            self.assertEqual(invoked.stdout.strip(), "conductor-runtime %s" % installed_record["version"])


if __name__ == "__main__":
    unittest.main()
