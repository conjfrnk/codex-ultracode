import tomllib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from conductor_runtime import MINIMUM_PYTHON, __version__, require_supported_python
from tools.install_bundle import InstallError, require_supported_python as require_installer_python


class PythonSupportTests(unittest.TestCase):
    def test_project_metadata_matches_runtime_support_and_version(self):
        project_root = Path(__file__).resolve().parents[1]
        with (project_root / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)["project"]

        self.assertEqual(project["requires-python"], ">=%d.%d" % MINIMUM_PYTHON)
        self.assertEqual(project["version"], __version__)

    def test_declared_minimum_is_accepted_by_runtime_and_installer(self):
        version = (*MINIMUM_PYTHON, 0)

        require_supported_python(version)
        require_installer_python(version)

    def test_older_interpreter_is_rejected_by_runtime_and_installer(self):
        version = (MINIMUM_PYTHON[0], MINIMUM_PYTHON[1] - 1, 9)

        with self.assertRaisesRegex(RuntimeError, r"requires Python 3\.12 or newer; found Python 3\.11\.9"):
            require_supported_python(version)
        with self.assertRaisesRegex(InstallError, r"requires Python 3\.12 or newer; found Python 3\.11\.9"):
            require_installer_python(version)

    def test_packaged_entrypoints_reject_an_actual_unsupported_interpreter(self):
        candidate = Path(os.environ.get("CONDUCTOR_UNSUPPORTED_PYTHON", "/usr/bin/python3"))
        if not candidate.is_file():
            self.skipTest("no unsupported system Python is available")
        probe = subprocess.run(
            [str(candidate), "-c", "import json,sys; print(json.dumps(list(sys.version_info[:3])))"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        if probe.returncode != 0:
            self.skipTest("candidate Python cannot launch")
        observed = tuple(json.loads(probe.stdout))
        if observed[:2] >= MINIMUM_PYTHON:
            self.skipTest("candidate Python is supported")

        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp).resolve()
            for script in ("package_runtime.py", "package_extras.py"):
                packaged = subprocess.run(
                    [sys.executable, "-B", str(project_root / "tools" / script), str(dist)],
                    cwd=project_root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(packaged.returncode, 0, packaged.stderr)
            runtime = Path(tmp).resolve() / "embedded-conductor-runtime.pyz"
            with zipfile.ZipFile(dist / "codex-conductor-bundle.zip") as archive:
                runtime.write_bytes(archive.read("conductor-runtime.pyz"))
            for artifact in (runtime, dist / "conductor-extras.pyz"):
                rejected = subprocess.run(
                    [str(candidate), str(artifact), "--version"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(rejected.returncode, 2, rejected.stderr)
                self.assertIn("requires Python 3.12 or newer", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
