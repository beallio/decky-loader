"""Release builds never reuse compiler output."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/runtime"))

import build  # noqa: E402


class CleanReleaseBuildTest(unittest.TestCase):
    def test_a_release_refuses_a_reused_cargo_target(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(build, "capture") as capture:
            with self.assertRaisesRegex(ValueError, "clean target directory"):
                build.build(Path(temporary) / "out", "v3.3.0-rust.1-rc.8", None, Path(temporary) / "target")
            capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
