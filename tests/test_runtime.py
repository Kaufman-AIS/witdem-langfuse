import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from witdem_langfuse.runtime import backfill_executable


class RuntimeTest(unittest.TestCase):
    def test_environment_runtime_without_path_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / ("duckle.exe" if os.name == "nt" else "duckle")
            binary.touch()
            binary.chmod(0o755)
            with (
                patch.dict(os.environ, {}, clear=True),
                patch(
                    "witdem_langfuse.runtime.sys.executable",
                    str(Path(directory) / "python"),
                ),
                patch("witdem_langfuse.runtime.shutil.which", return_value=None),
            ):
                self.assertEqual(backfill_executable(), str(binary))

    def test_override_and_path_fallback(self):
        with patch.dict(os.environ, {"DUCKLE_EXECUTABLE": "/custom/runtime"}):
            self.assertEqual(backfill_executable(), "/custom/runtime")
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("witdem_langfuse.runtime.sys.executable", "/missing/python"),
            patch("witdem_langfuse.runtime.shutil.which", return_value="/bin/duckle"),
        ):
            self.assertEqual(backfill_executable(), "/bin/duckle")

    def test_missing_runtime_explains_install(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("witdem_langfuse.runtime.sys.executable", "/missing/python"),
            patch("witdem_langfuse.runtime.shutil.which", return_value=None),
            self.assertRaisesRegex(RuntimeError, r"\[etl\]"),
        ):
            backfill_executable()
