"""The fork's runtime Python packages must equal upstream's."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/runtime"))

import python_parity  # noqa: E402

ASYNC_TIMEOUT = """
[[package]]
name = "async-timeout"
version = "5.0.1"
groups = ["main"]
markers = 'python_version == "3.10"'
"""

POETRY = """
[[package]]
name = "aiohttp"
version = "3.14.3"
groups = ["main"]

[[package]]
name = "typing_extensions"
version = "4.16.0"
groups = ["main", "dev"]

[[package]]
name = "pyinstaller"
version = "6.22.3"
groups = ["dev"]
""" + ASYNC_TIMEOUT


def uv_lock(aiohttp="3.14.3", project_extra=""):
    return f"""
version = 1

[[package]]
name = "decky-loader"
version = "3.3.0"
source = {{ editable = "." }}
dependencies = [{{ name = "aiohttp"{project_extra} }}]

[package.dev-dependencies]
dev = [{{ name = "pyinstaller" }}]

[[package]]
name = "aiohttp"
version = "{aiohttp}"
dependencies = [
    {{ name = "async-timeout", marker = "python_full_version < '3.11'" }},
    {{ name = "typing-extensions", marker = "python_full_version < '3.13'" }},
]

[package.optional-dependencies]
speedups = [{{ name = "brotli" }}]

[[package]]
name = "async-timeout"
version = "5.0.1"

[[package]]
name = "typing-extensions"
version = "4.16.0"

[[package]]
name = "pyinstaller"
version = "6.30.0"

[[package]]
name = "brotli"
version = "1.1.0"
"""


class ParityTest(unittest.TestCase):
    def test_same_runtime_packages_on_the_target_pass(self):
        # Dev-only packages may differ; async-timeout is Python 3.10 only in both locks.
        self.assertEqual(python_parity.differences(POETRY, uv_lock(), {}), [])
        self.assertEqual(python_parity.differences(POETRY.replace(ASYNC_TIMEOUT, ""), uv_lock(), {}), [])

    def test_version_and_package_differences_are_reported(self):
        self.assertEqual(python_parity.differences(POETRY, uv_lock(aiohttp="3.15.0"), {}),
                         ["aiohttp: upstream 3.14.3, fork 3.15.0"])
        self.assertEqual(python_parity.differences(POETRY, uv_lock(project_extra=', extra = ["speedups"]'), {}),
                         ["brotli: upstream absent, fork 1.1.0"])

    def test_markers_are_evaluated_for_the_shipped_target(self):
        windows_only = uv_lock().replace("python_full_version < '3.13'", "sys_platform == 'win32'")
        self.assertEqual(python_parity.differences(POETRY, windows_only, {}),
                         ["typing-extensions: upstream 4.16.0, fork absent"])
        upstream_on_311 = POETRY.replace('python_version == "3.10"', 'python_version < "3.12"')
        self.assertEqual(python_parity.differences(upstream_on_311, uv_lock(), {}),
                         ["async-timeout: upstream 5.0.1, fork absent"])

    def test_unknown_marker_syntax_fails_closed(self):
        for marker in ("platform_release == '6.1'", "python_version ~= '3.11'", "sys_platform < 'linux'",
                       "(sys_platform == 'linux'", "sys_platform == 'linux' xor os_name == 'posix'"):
            with self.subTest(marker), self.assertRaises(ValueError):
                python_parity.marker_applies(marker)

    def test_a_package_locked_in_two_versions_never_matches(self):
        split = uv_lock().replace(
            '{ name = "typing-extensions", marker',
            '{ name = "typing-extensions", version = "4.16.0", marker').replace(
            "dependencies = [{ name = \"aiohttp\" }]",
            "dependencies = [{ name = \"aiohttp\" }, { name = \"typing-extensions\", version = \"4.0.0\" }]",
        ) + '\n[[package]]\nname = "typing-extensions"\nversion = "4.0.0"\n'
        self.assertEqual(python_parity.differences(POETRY, split, {}),
                         ["typing-extensions: upstream 4.16.0, fork 4.0.0 | 4.16.0"])

    def test_allowed_divergence_must_be_real(self):
        self.assertEqual(python_parity.differences(POETRY, uv_lock(aiohttp="3.15.0"), {"aiohttp": "security fix"}), [])
        self.assertEqual(python_parity.differences(POETRY, uv_lock(), {"aiohttp": "security fix"}),
                         ["aiohttp: listed in python_divergence but equal to upstream (3.14.3); remove the entry"])


if __name__ == "__main__":
    unittest.main()
