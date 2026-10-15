"""Descriptor-anchored storage preparation regressions for the Python worker."""
import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


WORKER_PATH = Path(__file__).parents[1] / "backend" / "decky_loader" / "plugin" / "worker.py"
WORKER_SPEC = importlib.util.spec_from_file_location("decky_worker_storage_test", WORKER_PATH)
assert WORKER_SPEC is not None and WORKER_SPEC.loader is not None
worker = importlib.util.module_from_spec(WORKER_SPEC)
sys.modules[WORKER_SPEC.name] = worker
WORKER_SPEC.loader.exec_module(worker)


class PluginStorageTests(unittest.TestCase):
    def selected_user(self):
        return SimpleNamespace(pw_uid=os.geteuid(), pw_gid=os.getegid())

    def test_storage_ownership_and_modes_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            homebrew = Path(temporary) / "homebrew"
            homebrew.mkdir()

            worker._prepare_plugin_storage(homebrew, "example-plugin", self.selected_user())
            for root_name in ("settings", "data", "logs"):
                shared = homebrew / root_name
                plugin = shared / "example-plugin"
                self.assertEqual((shared.stat().st_uid, shared.stat().st_gid, stat.S_IMODE(shared.stat().st_mode)),
                                 (os.geteuid(), os.getegid(), 0o755))
                self.assertEqual((plugin.stat().st_uid, plugin.stat().st_gid, stat.S_IMODE(plugin.stat().st_mode)),
                                 (os.geteuid(), os.getegid(), 0o700))

    def test_replacing_storage_name_after_open_cannot_modify_outside_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            homebrew = base / "homebrew"
            outside = base / "outside"
            homebrew.mkdir()
            outside.mkdir(mode=0o711)
            outside.chmod(0o711)
            before = outside.stat()
            original_fchmod = worker.os.fchmod
            original_chmod = worker.os.chmod
            permission_calls = 0
            replaced = False

            def replace_settings_name():
                nonlocal permission_calls, replaced
                permission_calls += 1
                if permission_calls == 2:
                    (homebrew / "settings").rename(base / "detached-settings")
                    (homebrew / "settings").symlink_to(outside, target_is_directory=True)
                    replaced = True

            def fchmod(directory_fd, mode):
                replace_settings_name()
                return original_fchmod(directory_fd, mode)

            def chmod(path, mode, *args, **kwargs):
                replace_settings_name()
                return original_chmod(path, mode, *args, **kwargs)

            with mock.patch.object(worker.os, "fchmod", side_effect=fchmod), \
                    mock.patch.object(worker.os, "chmod", side_effect=chmod):
                worker._prepare_plugin_storage(homebrew, "example-plugin", self.selected_user())

            after = outside.stat()
            self.assertTrue(replaced)
            self.assertEqual((after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)),
                             (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)))
            self.assertFalse((outside / "example-plugin").exists())

    def test_storage_rejects_symlink_ancestor_without_touching_external_inode(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            outside = base / "outside"
            homebrew_target = outside / "homebrew"
            homebrew_target.mkdir(parents=True)
            homebrew_target.chmod(0o711)
            before = homebrew_target.stat()
            configured_parent = base / "configured"
            configured_parent.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(worker.WorkerError):
                worker._prepare_plugin_storage(
                    configured_parent / "homebrew", "example-plugin", self.selected_user()
                )

            after = homebrew_target.stat()
            self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))
            self.assertEqual((after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)),
                             (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)))
            self.assertFalse(any((homebrew_target / name).exists() for name in ("settings", "data", "logs")))


if __name__ == "__main__":
    unittest.main()
