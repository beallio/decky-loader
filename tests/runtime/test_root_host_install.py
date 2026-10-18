"""Root plugin host install rules that run without root or network."""

import io
import json
import os
import socket
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from decky_loader.plugin import root_host  # noqa: E402


def archive(members: dict[str, bytes | None], links: tuple[str, ...] = ()) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        for name, data in members.items():
            if data is None:
                output.writestr(zipfile.ZipInfo(name), b"")
            else:
                output.writestr(name, data)
        for name in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o120777 << 16
            output.writestr(info, "../../etc")
    return buffer.getvalue()


PLUGIN = {
    "Tools/": None,
    "Tools/plugin.json": json.dumps({"name": "Tools", "author": "a", "flags": ["root"]}).encode(),
    "Tools/main.py": b"class Plugin:\n    pass\n",
    "Tools/bin/backend": b"\x7fELF",
}


class ExtractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.stage = Path(self.temporary.name)
        self.stage_fd = os.open(self.stage, os.O_RDONLY | os.O_DIRECTORY)

    def tearDown(self) -> None:
        os.close(self.stage_fd)
        self.temporary.cleanup()

    def test_unpacks_the_plugin_folder_with_installer_modes(self) -> None:
        root_host._extract_plugin_archive(archive(PLUGIN), "Tools", self.stage_fd)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((self.stage / "main.py").read_bytes(), PLUGIN["Tools/main.py"])
        self.assertEqual((self.stage / "bin" / "backend").stat().st_mode & 0o777, 0o755)

    def test_refuses_unsafe_archives(self) -> None:
        cases: dict[str, tuple[dict[str, bytes | None], tuple[str, ...]]] = {
            "parent path": ({**PLUGIN, "Tools/../escape": b"x"}, ()),
            "absolute path": ({**PLUGIN, "/Tools/escape": b"x"}, ()),
            "other top folder": ({**PLUGIN, "Other/main.py": b"x"}, ()),
            "link": (PLUGIN, ("Tools/bin/link",)),
            "duplicate": ({**PLUGIN, "Tools//main.py": b"x"}, ()),
        }
        for label, (members, links) in cases.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                with self.assertRaises(root_host.RootHostError):
                    root_host._extract_plugin_archive(archive(members, links), "Tools", self.stage_fd)  # pyright: ignore[reportPrivateUsage]


class FakeConfig:
    store_hosts = ("cdn.tzatzikiweeb.moe",)

    def __init__(self) -> None:
        self.homebrew = Path("/nonexistent")

    def approved_directories(self) -> dict[str, Any]:
        return {}


class InstallRequestTest(unittest.TestCase):
    def host(self) -> root_host.Host:
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        config: Any = FakeConfig()
        host = root_host.Host(config, left)
        host.greeted = True
        return host

    def test_install_accepts_only_store_downloads_with_a_hash(self) -> None:
        good = {"op": "install", "directory": "Tools", "name": "Tools", "sha256": "a" * 64,
                "artifact": "https://cdn.tzatzikiweeb.moe/file/steam-deck-homebrew/versions/x.zip"}
        cases = {
            "other host": {**good, "artifact": "https://example.com/x.zip"},
            "plain http": {**good, "artifact": "http://cdn.tzatzikiweeb.moe/x.zip"},
            "local file": {**good, "artifact": "file:///home/deck/x.zip"},
            "no hash": {**good, "sha256": ""},
            "path in folder name": {**good, "directory": "../Tools"},
        }
        for label, request in cases.items():
            with self.subTest(label):
                response = self.host().handle_line(json.dumps(request).encode())
                self.assertFalse(response["ok"], response)

    def test_install_from_the_set_store_needs_its_exact_listing(self) -> None:
        store = "https://store.example/plugins.json"
        artifact = "https://github.com/author/tools/releases/download/v1/Tools.zip"
        listing = [
            {"name": "Other", "versions": [{"name": "1", "hash": "a" * 64, "artifact": artifact}]},
            {"name": "Tools", "versions": [
                {"name": "2", "hash": "b" * 64, "artifact": artifact.replace("v1", "v2")},
                {"name": "1", "hash": "a" * 64, "artifact": artifact},
            ]},
        ]
        good = {"op": "install", "directory": "Tools", "name": "Tools", "sha256": "a" * 64,
                "artifact": artifact, "store_url": store}

        def download(url: str, limit: int) -> bytes:
            if url == store:
                return json.dumps(listing).encode()
            raise root_host.RootHostError(f"fetched plugin {url}")

        cases = {
            "listed": (good, "fetched plugin"),
            "other hash": ({**good, "sha256": "c" * 64}, "only be installed"),
            "other plugin's entry": ({**good, "name": "Tools2"}, "only be installed"),
            "other download": ({**good, "artifact": artifact + "?x"}, "only be installed"),
            "no store": ({k: v for k, v in good.items() if k != "store_url"}, "only be installed"),
            "plain http store": ({**good, "store_url": "http://store.example/plugins.json"}, "only be installed"),
        }
        with mock.patch.object(root_host, "_download", side_effect=download):
            for label, (request, expected) in cases.items():
                with self.subTest(label):
                    response = self.host().handle_line(json.dumps(request).encode())
                    self.assertFalse(response["ok"], response)
                    self.assertIn(expected, response["error"])

    def test_runtime_updates_only_follow_release_tags(self) -> None:
        host = self.host()
        for version in ("dev", "v3.2.10-rust.8-dev.root", "v3.2.10; rm -rf /", None):
            with self.subTest(version=version):
                self.assertFalse(host.update_runtime(version))
                self.assertIsNone(host.update)

    def test_root_runtime_only_moves_to_newer_releases(self) -> None:
        current = "v3.2.10-rust.8-rc.9"
        newer = ["v3.2.10-rust.8", "v3.2.10-rust.8-rc.10", "v3.2.10-rust.9-rc.1", "v3.3.0-rust.1-rc.1"]
        not_newer = ["v3.2.10-rust.8-rc.9", "v3.2.10-rust.8-rc.8", "v3.2.10-rust.7", "v3.2.9-rust.9"]
        for version in newer:
            self.assertTrue(root_host._newer_release(version, current), version)  # pyright: ignore[reportPrivateUsage]
        for version in not_newer:
            self.assertFalse(root_host._newer_release(version, current), version)  # pyright: ignore[reportPrivateUsage]
        # A development runtime is always replaced by a release.
        self.assertTrue(root_host._newer_release("v3.2.10-rust.8-rc.1", "dev"))  # pyright: ignore[reportPrivateUsage]


if __name__ == "__main__":
    unittest.main()
