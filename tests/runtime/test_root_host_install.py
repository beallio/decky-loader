"""Root plugin host install rules that run without root or network."""

import hashlib
import io
import json
import os
import socket
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
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
        root_host._extract_plugin_archive(archive(PLUGIN), "Tools", self.stage_fd)
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
                    root_host._extract_plugin_archive(archive(members, links), "Tools", self.stage_fd)


class FakeConfig:
    user = "deck"
    uid = 1000
    gid = 1000

    def __init__(self) -> None:
        self.homebrew = Path("/nonexistent")


class FailingRecordConfig(FakeConfig):
    uid = os.getuid()

    def record(self, directory: str, entry: dict[str, Any] | None) -> None:
        raise RuntimeError("approval write failed")


class InstallRequestTest(unittest.TestCase):
    def host(self) -> root_host.Host:
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        config: Any = FakeConfig()
        host = root_host.Host(config, left)
        host.greeted = True
        return host

    def test_install_restores_the_old_plugin_when_approval_record_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plugins = Path(temporary) / "plugins"
            old_plugin = plugins / "Tools"
            old_plugin.mkdir(parents=True)
            (old_plugin / "sentinel").write_text("old plugin")
            plugins_fd = os.open(plugins, os.O_RDONLY | os.O_DIRECTORY)
            config: Any = FailingRecordConfig()
            config.homebrew = Path(temporary)
            real_fstat = os.fstat

            def root_owned(descriptor: int) -> Any:
                # The host requires a root-owned staging folder; keep the real identity fields.
                info = real_fstat(descriptor)
                return SimpleNamespace(st_uid=0, st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino)
            left, right = socket.socketpair()
            self.addCleanup(left.close)
            self.addCleanup(right.close)
            host = root_host.Host(config, left)
            host.greeted = True
            payload = b"plugin archive"
            request = {
                "directory": "Tools",
                "name": "Tools",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "artifact": "https://cdn.tzatzikiweeb.moe/Tools.zip",
            }

            with mock.patch.object(host, "_plugin_root_fd", return_value=plugins_fd), \
                    mock.patch.object(root_host, "_check_password"), \
                    mock.patch.object(root_host, "_download", return_value=payload), \
                    mock.patch.object(root_host.os, "fstat", side_effect=root_owned), \
                    mock.patch.object(root_host, "_extract_plugin_archive"), \
                    mock.patch.object(root_host, "plugin_metadata", return_value={
                        "name": "Tools", "version": "1.0.0", "flags": ["root"],
                    }), \
                    mock.patch.object(root_host, "_download_remote_binaries"), \
                    mock.patch.object(root_host, "plugin_tree_sha256", return_value="tree"):
                with self.assertRaisesRegex(RuntimeError, "approval write failed"):
                    host.install(request)

            self.assertEqual((old_plugin / "sentinel").read_text(), "old plugin")
            self.assertEqual(sorted(path.name for path in plugins.iterdir()), ["Tools"])

    def test_install_refuses_requests_without_https_download_hash_or_folder(self) -> None:
        good = {"op": "install", "directory": "Tools", "name": "Tools", "sha256": "a" * 64,
                "artifact": "https://example.com/x.zip", "password": "secret"}
        cases = {
            "plain http": {**good, "artifact": "http://example.com/x.zip"},
            "local file": {**good, "artifact": "file:///home/deck/x.zip"},
            "no hash": {**good, "sha256": ""},
            "path in folder name": {**good, "directory": "../Tools"},
        }
        with mock.patch.object(root_host, "_check_password") as check, \
                mock.patch.object(root_host, "_download") as download:
            for label, request in cases.items():
                with self.subTest(label):
                    response = self.host().handle_line(json.dumps(request).encode())
                    self.assertFalse(response["ok"], response)
            check.assert_not_called()
            download.assert_not_called()

    @staticmethod
    def sudo(password: str, passwordless: bool = False) -> tuple[Any, list[tuple[list[str], dict[str, Any]]]]:
        """A fake sudo that knows `password`, and the list of its calls."""
        calls: list[tuple[list[str], dict[str, Any]]] = []

        def run(command: list[str], **options: Any) -> Any:
            calls.append((command, options))
            if "-n" in command:
                return SimpleNamespace(returncode=0 if passwordless else 1)
            return SimpleNamespace(returncode=0 if options["input"] == password.encode() + b"\n" else 1)
        return run, calls

    def test_install_downloads_any_https_plugin_only_with_the_sudo_password(self) -> None:
        request = {"op": "install", "directory": "Tools", "name": "Tools", "sha256": "a" * 64,
                   "artifact": "https://github.com/author/tools/releases/download/v1/Tools.zip"}
        cases = {
            "no password": ({}, "needs your sudo password", False),
            "empty password": ({"password": ""}, "needs your sudo password", False),
            "two lines": ({"password": "secret\nsecret"}, "needs your sudo password", False),
            "wrong password": ({"password": "guess"}, "wrong sudo password", False),
            "right password": ({"password": "secret"}, "fetched plugin", False),
            "sudo without password": ({"password": "secret"}, "NOPASSWD", True),
        }
        for label, (extra, expected, passwordless) in cases.items():
            with self.subTest(label):
                run, calls = self.sudo("secret", passwordless)
                with mock.patch.object(root_host.subprocess, "run", side_effect=run), \
                        mock.patch.object(root_host.os, "getgrouplist", return_value=[1000, 998]), \
                        mock.patch.object(root_host, "_download",
                                          side_effect=root_host.RootHostError("fetched plugin")) as download:
                    response = self.host().handle_line(json.dumps({**request, **extra}).encode())
                self.assertFalse(response["ok"], response)
                self.assertIn(expected, response["error"])
                self.assertEqual(download.called, label == "right password")
                for command, options in calls:
                    # sudo runs as the user with the user's groups and never reuses cached credentials.
                    self.assertEqual((command[0], command[-2:]), (root_host.SUDO, ["--", root_host.TRUE]))
                    self.assertIn("-k", command)
                    self.assertEqual((options["user"], options["group"], options["extra_groups"]), (1000, 1000, [1000, 998]))

    def test_wrong_password_blocks_the_next_attempt_for_a_while(self) -> None:
        host = self.host()
        request = {"op": "install", "directory": "Tools", "name": "Tools", "sha256": "a" * 64,
                   "artifact": "https://example.com/Tools.zip", "password": "guess"}
        run, calls = self.sudo("secret")
        with mock.patch.object(root_host.subprocess, "run", side_effect=run), \
                mock.patch.object(root_host.os, "getgrouplist", return_value=[1000]), \
                mock.patch.object(root_host.time, "monotonic", return_value=100.0):
            self.assertIn("wrong sudo password", host.handle_line(json.dumps(request).encode())["error"])
            attempts = len(calls)
            response = host.handle_line(json.dumps({**request, "password": "secret"}).encode())
        self.assertIn("wait 5 seconds", response["error"])
        self.assertEqual(len(calls), attempts, "a blocked attempt must not reach sudo")
        with mock.patch.object(root_host.subprocess, "run", side_effect=run), \
                mock.patch.object(root_host.os, "getgrouplist", return_value=[1000]), \
                mock.patch.object(root_host.time, "monotonic", return_value=105.5), \
                mock.patch.object(root_host, "_download", side_effect=root_host.RootHostError("fetched plugin")):
            response = host.handle_line(json.dumps({**request, "password": "secret"}).encode())
        self.assertIn("fetched plugin", response["error"])

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
            self.assertTrue(root_host._newer_release(version, current), version)
        for version in not_newer:
            self.assertFalse(root_host._newer_release(version, current), version)
        # A development runtime is always replaced by a release.
        self.assertTrue(root_host._newer_release("v3.2.10-rust.8-rc.1", "dev"))


if __name__ == "__main__":
    unittest.main()
