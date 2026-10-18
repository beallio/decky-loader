"""Security checks of the root plugin host that run without root."""

import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from decky_loader import helpers  # noqa: E402
from decky_loader.plugin import root_host  # noqa: E402


def write_plugin(directory: Path, flags: list[str]) -> None:
    (directory / "bin").mkdir(parents=True)
    (directory / "plugin.json").write_text(json.dumps({"name": "Tools", "author": "a", "flags": flags, "api_version": 1}))
    (directory / "package.json").write_text(json.dumps({"version": "1.0.0"}))
    (directory / "main.py").write_text("class Plugin:\n    pass\n")
    (directory / "bin" / "backend").write_text("#!/bin/sh\n")
    (directory / "bin" / "backend").chmod(0o755)
    os.symlink("backend", directory / "bin" / "current")
    for path in [directory, *directory.rglob("*")]:
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() or path.name == "backend" else 0o644)


class TreeHashTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.plugin = Path(self.temporary.name) / "Tools"
        write_plugin(self.plugin, ["root"])
        self.owner = os.getuid()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def hash(self) -> str:
        directory_fd = os.open(self.plugin, os.O_RDONLY | os.O_DIRECTORY)
        try:
            return root_host.plugin_tree_sha256(directory_fd, owner_uid=self.owner)
        finally:
            os.close(directory_fd)

    def test_hash_is_stable_and_changes_with_content_mode_and_links(self) -> None:
        original = self.hash()
        self.assertEqual(original, self.hash())
        changes = [
            lambda: (self.plugin / "main.py").write_text("class Plugin:\n    x = 1\n"),
            lambda: (self.plugin / "bin" / "backend").chmod(0o700),
            lambda: (os.unlink(self.plugin / "bin" / "current"), os.symlink("../main.py", self.plugin / "bin" / "current")),
            lambda: (self.plugin / "extra").write_text(""),
        ]
        seen = {original}
        for change in changes:
            change()
            current = self.hash()
            self.assertNotIn(current, seen)
            seen.add(current)

    def test_refuses_what_another_user_could_change(self) -> None:
        cases = [
            lambda: (self.plugin / "main.py").chmod(0o664),
            lambda: (self.plugin / "bin").chmod(0o777),
            lambda: self.plugin.chmod(0o775),
            lambda: os.symlink("/usr/bin/env", self.plugin / "escape"),
            lambda: os.symlink("../../outside", self.plugin / "bin" / "escape"),
            lambda: os.mkfifo(self.plugin / "pipe"),
        ]
        for index, change in enumerate(cases):
            with self.subTest(case=index):
                self.tearDown()
                self.setUp()
                change()
                with self.assertRaises(root_host.RootHostError):
                    self.hash()

    def test_refuses_files_owned_by_another_user(self) -> None:
        self.owner = self.owner + 1
        with self.assertRaises(root_host.RootHostError):
            self.hash()


class FakeConfig:
    def __init__(self, uid: int, state: dict[str, Any], homebrew: Path) -> None:
        self.uid = uid
        self.gid = os.getgid()
        self.user = "deck"
        self.homebrew = homebrew
        self.state = state
        self.mount_root = homebrew / "mnt"
        self.socket_directory = homebrew / "sockets"

    def approved(self, directory: str) -> dict[str, Any]:
        entry = self.state.get(directory)
        if entry is None:
            raise root_host.RootHostError(f"{directory} is not an approved root plugin")
        return entry

    def approved_directories(self) -> dict[str, Any]:
        return self.state


class ProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.homebrew = Path(self.temporary.name)
        write_plugin(self.homebrew / "plugins" / "Tools", ["root"])
        write_plugin(self.homebrew / "plugins" / "Plain", [])
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(self.homebrew / "host.sock"))
        self.listener.listen()

    def tearDown(self) -> None:
        self.listener.close()
        self.temporary.cleanup()

    def host(self, uid: int, state: dict[str, Any]) -> root_host.Host:
        config: Any = FakeConfig(uid, state, self.homebrew)
        return root_host.Host(config, self.listener)

    def connect(self, host: root_host.Host) -> socket.socket:
        client = socket.socket(socket.AF_UNIX)
        client.connect(str(self.homebrew / "host.sock"))
        host.accept()
        return client

    def test_other_users_cannot_connect(self) -> None:
        host = self.host(os.getuid() + 1, {})
        client = self.connect(host)
        self.assertIsNone(host.connection)
        self.assertEqual(client.recv(1), b"")
        client.close()

    def test_only_one_backend_connection_at_a_time(self) -> None:
        host = self.host(os.getuid(), {})
        first = self.connect(host)
        second = self.connect(host)
        self.assertEqual(second.recv(1), b"")
        self.assertIsNotNone(host.connection)
        host.drop_connection()
        first.close()
        second.close()

    def test_requests_before_hello_are_refused(self) -> None:
        host = self.host(os.getuid(), {})
        response = host.handle_line(json.dumps({"op": "start", "id": "a", "directory": "Tools"}).encode())
        self.assertFalse(response["ok"])
        self.assertIn("hello", response["error"])

    def greeted(self, state: dict[str, Any]) -> root_host.Host:
        host = self.host(os.getuid(), state)
        response = host.handle_line(json.dumps(
            {"op": "hello", "protocol": 1, "auth_token": "t", "server_host": "127.0.0.1", "server_port": 1337}
        ).encode())
        self.assertTrue(response["ok"], response)
        return host

    def test_hello_updates_the_csrf_token_used_by_helpers(self) -> None:
        original_token = helpers.csrf_token
        try:
            self.greeted({})
            self.assertEqual(helpers.get_csrf_token(), "t")
        finally:
            helpers.csrf_token = original_token

    def start(self, host: root_host.Host, directory: str) -> dict[str, Any]:
        return host.handle_line(json.dumps({"op": "start", "id": "a", "directory": directory}).encode())

    def test_start_refuses_unapproved_unmarked_and_changed_plugins(self) -> None:
        cases = {
            "not approved": ({}, "Tools", "not an approved"),
            "path in name": ({}, "../Tools", "plain folder name"),
            "not marked root": ({"Plain": {"name": "Tools", "tree_sha256": "x"}}, "Plain", "not marked"),
            "other plugin": ({"Tools": {"name": "Other", "tree_sha256": "x"}}, "Tools", "not the plugin"),
        }
        for label, (state, directory, message) in cases.items():
            with self.subTest(label):
                response = self.start(self.greeted(state), directory)
                self.assertFalse(response["ok"])
                self.assertIn(message, response["error"])

    def test_start_refuses_a_folder_that_root_does_not_own(self) -> None:
        # The real host checks ownership by root. Files made by this test user fail it.
        response = self.start(self.greeted({"Tools": {"name": "Tools", "tree_sha256": "x"}}), "Tools")
        self.assertFalse(response["ok"])
        self.assertIn("not owned by root", response["error"])


if __name__ == "__main__":
    unittest.main()
