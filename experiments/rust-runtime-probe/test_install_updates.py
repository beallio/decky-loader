import os
import tempfile
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


INSTALLER_PATH = Path(__file__).with_name("install.py")


class RustBundleUpdateTests(unittest.TestCase):
    def test_running_installer_keeps_the_bundle_file_set_unchanged(self):
        temporary_root = Path(os.environ.get("TMPDIR", "/var/tmp/omp-1000"))
        with tempfile.TemporaryDirectory(dir=temporary_root) as temporary:
            bundle = Path(temporary)
            for name in ("install.py", "process_observer.py"):
                shutil.copy2(INSTALLER_PATH.with_name(name), bundle / name)
            before = {path.relative_to(bundle) for path in bundle.rglob("*")}
            environment = dict(os.environ)
            environment.pop("PYTHONDONTWRITEBYTECODE", None)
            subprocess.run(
                [sys.executable, str(bundle / "install.py"), "--help"],
                env=environment, capture_output=True, text=True, check=True,
            )
            self.assertEqual({path.relative_to(bundle) for path in bundle.rglob("*")}, before)




if __name__ == "__main__":
    unittest.main()
