"""Behavioral regressions for approved plugin loading and import paths."""
import argparse
import asyncio
import importlib
import importlib.machinery
import importlib.util
import json
import logging
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

WORKER_PATH = Path(__file__).parents[1] / "backend" / "decky_loader" / "plugin" / "worker.py"
SANDBOX_PATH = Path(__file__).parents[1] / "backend" / "decky_loader" / "plugin" / "sandboxed_plugin.py"
WORKER_SPEC = importlib.util.spec_from_file_location("decky_worker_imports_test", WORKER_PATH)
assert WORKER_SPEC is not None and WORKER_SPEC.loader is not None
worker = importlib.util.module_from_spec(WORKER_SPEC)
sys.modules[WORKER_SPEC.name] = worker
WORKER_SPEC.loader.exec_module(worker)


def _approved_launch(directory_fd: int, *, flags: list[str], name: str = "approved-plugin") -> dict:
    status = os.fstat(directory_fd)
    return {
        "descriptor": {
            "pid": os.getpid(),
            "fd": directory_fd,
            "device": status.st_dev,
            "inode": status.st_ino,
        },
        "metadata": {
            "name": name,
            "author": "approved author",
            "flags": flags,
            "api_version": 7,
            "version": "1.2.3",
        },
    }


def _fake_sdk_modules(homebrew: Path) -> dict[str, types.ModuleType]:
    root = types.ModuleType("decky_loader")
    root.__path__ = []
    localplatform = types.ModuleType("decky_loader.localplatform")
    localplatform.__path__ = []
    plugin = types.ModuleType("decky_loader.plugin")
    plugin.__path__ = []
    root.localplatform = localplatform
    root.plugin = plugin

    helpers = types.ModuleType("decky_loader.helpers")
    helpers.get_system_pythonpaths = lambda: []
    helpers.get_loader_version = lambda: "test-version"
    helpers.get_home_path = lambda: str(homebrew / "user-home")
    helpers.get_homebrew_path = lambda: str(homebrew)
    root.helpers = helpers
    localplatform_impl = types.ModuleType("decky_loader.localplatform.localplatform")
    localplatform_impl.get_log_level = lambda: logging.CRITICAL
    localplatform_impl.setgid = lambda _user: None
    localplatform_impl.setuid = lambda _user: None
    localplatform_impl.get_username = lambda: "selected-user"
    localplatform_impl.get_home_path = lambda _user: str(homebrew / "user-home")
    localplatform_impl.ON_LINUX = False
    localplatform.localplatform = localplatform_impl
    localsocket = types.ModuleType("decky_loader.localplatform.localsocket")

    class LocalSocket:
        socket_addr = ""

        async def setup_server(self, _on_new_message):
            return None

    localsocket.LocalSocket = LocalSocket
    localplatform.localsocket = localsocket
    enums = types.ModuleType("decky_loader.enums")

    class UserType:
        EFFECTIVE_USER = 0
        HOST_USER = 1

    enums.UserType = UserType
    root.enums = enums
    settings = types.ModuleType("decky_loader.settings")
    root.settings = settings
    messages = types.ModuleType("decky_loader.plugin.messages")

    class SocketMessageType:
        EVENT = "event"
        RESPONSE = "response"

    messages.SocketMessageType = SocketMessageType
    messages.SocketResponseDict = dict
    imports = types.ModuleType("decky_loader.plugin.imports")
    imports.__path__ = []
    decky = types.ModuleType("decky_loader.plugin.imports.decky")

    async def emit(_event, *_args):
        return None

    decky.emit = emit
    imports.decky = decky
    plugin.messages = messages
    plugin.imports = imports
    sandboxed = types.ModuleType("decky_loader.plugin.sandboxed_plugin")
    plugin.sandboxed_plugin = sandboxed
    setproctitle = types.ModuleType("setproctitle")
    setproctitle.setproctitle = lambda _title: None
    setproctitle.setthreadtitle = lambda _title: None
    return {
        "decky_loader": root,
        "decky_loader.helpers": helpers,
        "decky_loader.localplatform": localplatform,
        "decky_loader.localplatform.localplatform": localplatform_impl,
        "decky_loader.localplatform.localsocket": localsocket,
        "decky_loader.enums": enums,
        "decky_loader.settings": settings,
        "decky_loader.plugin": plugin,
        "decky_loader.plugin.messages": messages,
        "decky_loader.plugin.imports": imports,
        "decky_loader.plugin.imports.decky": decky,
        "decky_loader.plugin.sandboxed_plugin": sandboxed,
        "setproctitle": setproctitle,
    }


class _TestLoop:
    def add_signal_handler(self, *_args):
        return None

    def run_until_complete(self, coroutine):
        return asyncio.run(coroutine)

    def create_task(self, coroutine):
        return asyncio.run(coroutine)

    def run_forever(self):
        return None

    def close(self):
        return None

def _load_sandboxed_plugin() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "decky_loader.plugin.sandboxed_plugin", SANDBOX_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    sys.modules["decky_loader.plugin"].sandboxed_plugin = module
    return module



class WorkerPinnedImportTests(unittest.TestCase):
    def test_linked_entrypoint_uses_approved_code_with_ordinary_file_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            public_directory = Path(temporary) / "plugin"
            public_directory.mkdir()
            (public_directory / "backend.py").write_text("VALUE = 'approved linked code'\n")
            (public_directory / "main.py").symlink_to("backend.py")
            directory_fd = os.open(public_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                public_directory.rename(Path(temporary) / "approved")
                public_directory.mkdir()
                public_main = public_directory / "main.py"
                public_main.write_text("VALUE = 'replacement code'\n")
                worker._validate_approved_entrypoint(directory_fd, public_main)
                bridge = worker._PluginImportBridge(directory_fd, public_directory)
                spec = importlib.util.spec_from_file_location(
                    "_", str(public_main), loader=bridge.entrypoint_loader(str(public_main))
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertEqual(module.VALUE, "approved linked code")
                self.assertEqual(module.__file__, str(public_main))
            finally:
                os.close(directory_fd)

    def test_replacement_zip_cannot_bypass_approved_module_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            public_directory = Path(temporary) / "plugin"
            modules = public_directory / "py_modules"
            modules.mkdir(parents=True)
            name = f"pinned_zip_boundary_{uuid.uuid4().hex}"
            (modules / f"{name}.py").write_text("VALUE = 'approved'\n")
            directory_fd = os.open(public_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                public_directory.rename(Path(temporary) / "approved")
                public_directory.mkdir()
                with zipfile.ZipFile(modules, "w") as archive:
                    archive.writestr(f"{name}.py", "VALUE = 'replacement zip'\n")
                bridge = worker._PluginImportBridge(directory_fd, public_directory)
                with mock.patch.object(sys, "path", list(sys.path)), \
                        mock.patch.object(sys, "path_hooks", list(sys.path_hooks)), \
                        mock.patch.dict(sys.path_importer_cache), \
                        mock.patch.dict(sys.modules):
                    bridge.install()
                    try:
                        sys.path.insert(0, str(modules))
                        loaded = importlib.import_module(name)
                        self.assertEqual(loaded.VALUE, "approved")
                        self.assertEqual(loaded.__file__, str(modules / f"{name}.py"))
                    finally:
                        bridge.close()
            finally:
                os.close(directory_fd)

    def test_approved_zip_package_keeps_public_origins_and_negative_lookups(self):
        with tempfile.TemporaryDirectory() as temporary:
            public_directory = Path(temporary) / "plugin"
            modules = public_directory / "py_modules"
            modules.mkdir(parents=True)
            suffix = uuid.uuid4().hex
            package_name = f"pinned_zip_package_{suffix}"
            missing_name = f"replacement_zip_only_{suffix}"
            archive_path = modules / f"packages_{suffix}.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(f"{package_name}/__init__.py", "VALUE = 'approved package'\n")
                archive.writestr(f"{package_name}/child.py", "VALUE = 'approved child'\n")
            directory_fd = os.open(public_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                public_directory.rename(Path(temporary) / "approved")
                modules.mkdir(parents=True)
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(f"{package_name}/__init__.py", "VALUE = 'replacement package'\n")
                    archive.writestr(f"{package_name}/child.py", "VALUE = 'replacement child'\n")
                    archive.writestr(f"{missing_name}.py", "VALUE = 'replacement only'\n")
                bridge = worker._PluginImportBridge(directory_fd, public_directory)
                with mock.patch.object(sys, "path", list(sys.path)), \
                        mock.patch.object(sys, "path_hooks", list(sys.path_hooks)), \
                        mock.patch.dict(sys.path_importer_cache), \
                        mock.patch.dict(sys.modules):
                    bridge.install()
                    try:
                        sys.path.insert(0, str(archive_path))
                        package = importlib.import_module(package_name)
                        child = importlib.import_module(f"{package_name}.child")
                        self.assertEqual((package.VALUE, child.VALUE), ("approved package", "approved child"))
                        expected_package = str(archive_path / package_name)
                        self.assertEqual(package.__file__, expected_package + "/__init__.py")
                        self.assertEqual(package.__path__, [expected_package])
                        self.assertEqual(child.__file__, expected_package + "/child.py")
                        self.assertEqual(child.__spec__.origin, child.__file__)
                        with self.assertRaises(ModuleNotFoundError):
                            importlib.import_module(missing_name)
                    finally:
                        bridge.close()
            finally:
                os.close(directory_fd)

    def test_launch_uses_approved_object_code_and_flags_after_public_replacement(self):
        # launch() owns its binding until process exit, as it does in a worker.
        test_name = f"{type(self).__name__}.{self._testMethodName}"
        if os.environ.get("DECKY_IMPORT_TEST_PROCESS") != test_name:
            child = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), test_name],
                env=dict(os.environ, DECKY_IMPORT_TEST_PROCESS=test_name),
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            return
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            plugin_root = base / "plugins"
            plugin_root.mkdir()
            public_directory = plugin_root / "example-plugin"
            public_directory.mkdir()
            original_modules = public_directory / "py_modules"
            original_modules.mkdir()

            suffix = uuid.uuid4().hex
            probe_name = f"approved_probe_{suffix}"
            package_name = f"approved_package_{suffix}"
            rogue_name = f"rogue_only_{suffix}"
            (public_directory / "plugin.json").write_text(
                json.dumps({"name": "old metadata", "flags": []}), encoding="utf-8"
            )
            (public_directory / "package.json").write_text(
                json.dumps({"version": "old package version"}), encoding="utf-8"
            )
            (public_directory / "main.py").write_text(
                f"import {probe_name} as probe\n"
                f"import {package_name} as package\n"
                f"from {package_name} import child\n"
                "from pathlib import Path\n"
                "RESULT = (probe.VALUE, package.VALUE, child.VALUE)\n"
                "RESOURCE_PATH = str(Path(__file__).parent / 'assets')\n"
                "SPEC_ORIGIN = __spec__.origin\n"
                "class Plugin:\n"
                "    RESULT = RESULT\n"
                "    RESOURCE_PATH = RESOURCE_PATH\n"
                "    MAIN_FILE = __file__\n"
                "    SPEC_ORIGIN = SPEC_ORIGIN\n",
                encoding="utf-8",
            )
            (original_modules / f"{probe_name}.py").write_text(
                "VALUE = 'approved module'\n", encoding="utf-8"
            )
            package_directory = original_modules / package_name
            package_directory.mkdir()
            (package_directory / "__init__.py").write_text(
                "VALUE = 'approved package'\nfrom . import child\n", encoding="utf-8"
            )
            (package_directory / "child.py").write_text(
                "VALUE = 'approved package child'\n", encoding="utf-8"
            )

            directory_fd = os.open(public_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                public_cache_key = str(original_modules)
                sys.path_importer_cache[public_cache_key] = importlib.machinery.FileFinder(
                    public_cache_key,
                    (importlib.machinery.SourceFileLoader, importlib.machinery.SOURCE_SUFFIXES),
                )
                approved_object = base / "approved-object"
                public_directory.rename(approved_object)
                public_directory.mkdir()
                replacement_modules = public_directory / "py_modules"
                replacement_modules.mkdir()
                (public_directory / "plugin.json").write_text(
                    json.dumps({
                        "name": "replacement metadata",
                        "author": "replacement author",
                        "flags": ["root"],
                        "api_version": 99,
                    }),
                    encoding="utf-8",
                )
                (public_directory / "package.json").write_text(
                    json.dumps({"version": "replacement version"}), encoding="utf-8"
                )
                (public_directory / "main.py").write_text("RESULT = 'replacement main'\n", encoding="utf-8")
                (replacement_modules / f"{probe_name}.py").write_text(
                    "VALUE = 'replacement module'\n", encoding="utf-8"
                )
                replacement_package = replacement_modules / package_name
                replacement_package.mkdir()
                (replacement_package / "__init__.py").write_text(
                    "VALUE = 'replacement package'\nfrom . import child\n", encoding="utf-8"
                )
                (replacement_package / "child.py").write_text(
                    "VALUE = 'replacement child'\n", encoding="utf-8"
                )
                (replacement_modules / f"{rogue_name}.py").write_text(
                    "VALUE = 'replacement-only module'\n", encoding="utf-8"
                )


                args = argparse.Namespace(
                    plugin_root=str(plugin_root),
                    plugin_directory=str(public_directory),
                    socket=str(base / "plugin.sock"),
                    homebrew=str(base / "homebrew"),
                    user="selected-user",
                    bootstrap="lean",
                    approved_launch=_approved_launch(directory_fd, flags=[]),
                )
                instances = []
                modules = _fake_sdk_modules(base / "homebrew")
                with mock.patch.object(sys, "path", list(sys.path)), \
                        mock.patch.object(sys, "path_hooks", list(sys.path_hooks)), \
                        mock.patch.dict(sys.path_importer_cache), \
                        mock.patch.dict(sys.modules, modules), \
                        mock.patch.dict(os.environ, {}, clear=False), \
                        mock.patch.object(worker, "_selected_user", return_value=SimpleNamespace()), \
                        mock.patch.object(worker, "_prepare_plugin_storage", return_value=base / "homebrew"), \
                        mock.patch.object(worker, "prepare_runtime"), \
                        mock.patch.object(worker.os, "geteuid", return_value=1000):
                    sandbox_module = _load_sandboxed_plugin()
                    loop = _TestLoop()
                    sandbox_module.new_event_loop = lambda: loop
                    sandbox_module.set_event_loop = lambda _loop: None
                    sandbox_module.get_event_loop = lambda: loop

                    class ObservedSandboxedPlugin(sandbox_module.SandboxedPlugin):
                        def initialize(self, socket):
                            super().initialize(socket)
                            self.probe = sys.modules[probe_name]
                            self.package = sys.modules[package_name]
                            self.child = sys.modules[f"{package_name}.child"]
                            try:
                                importlib.import_module(rogue_name)
                            except ModuleNotFoundError as error:
                                self.rogue_was_missing = error.name == rogue_name
                            else:
                                self.rogue_was_missing = False
                            self.public_py_modules = str(
                                Path(self.plugin_path) / self.plugin_directory / "py_modules"
                            )
                            self.plugin_path_value = os.environ["PLUGIN_PATH"]
                            self.plugin_dir_value = os.environ["DECKY_PLUGIN_DIR"]
                            instances.append(self)

                    sandbox_module.SandboxedPlugin = ObservedSandboxedPlugin
                    worker.launch(args)
                self.assertEqual(len(instances), 1)
                plugin = instances[0]
                self.assertEqual((plugin.name, plugin.passive, plugin.flags), ("approved-plugin", False, []))
                self.assertEqual(plugin.file, str(public_directory / "main.py"))
                self.assertEqual((
                    plugin.plugin_directory, plugin.plugin_path, plugin.version,
                    plugin.author, plugin.api_version,
                ), ("example-plugin", str(plugin_root), "1.2.3", "approved author", 7))
                self.assertEqual(plugin.Plugin.RESULT, (
                    "approved module", "approved package", "approved package child"
                ))
                self.assertTrue(plugin.rogue_was_missing)
                self.assertEqual(plugin.Plugin.MAIN_FILE, str(public_directory / "main.py"))
                self.assertEqual(plugin.Plugin.SPEC_ORIGIN, str(public_directory / "main.py"))
                self.assertEqual(plugin.Plugin.RESOURCE_PATH, str(public_directory / "assets"))
                self.assertEqual(plugin.probe.__file__, str(original_modules / f"{probe_name}.py"))
                self.assertEqual(plugin.probe.__spec__.origin, str(original_modules / f"{probe_name}.py"))
                self.assertEqual(plugin.package.__file__, str(original_modules / package_name / "__init__.py"))
                self.assertEqual(plugin.package.__path__, [str(original_modules / package_name)])
                self.assertEqual(plugin.child.__file__, str(original_modules / package_name / "child.py"))
                self.assertTrue(plugin.public_py_modules.startswith(str(plugin_root)))
                self.assertEqual(plugin.plugin_path_value, str(plugin_root))
                self.assertEqual(plugin.plugin_dir_value, str(public_directory))
            finally:
                os.close(directory_fd)

    def test_cached_and_sourceless_bytecode_are_read_from_approved_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            plugin_root = base / "plugins"
            plugin_root.mkdir()
            public_directory = plugin_root / "example-plugin"
            modules = public_directory / "py_modules"
            modules.mkdir(parents=True)
            suffix = uuid.uuid4().hex
            cached_name = f"pinned_cached_{suffix}"
            sourceless_name = f"pinned_sourceless_{suffix}"

            cached_source = modules / f"{cached_name}.py"
            cached_source.write_text("VALUE = 'approved bytecode'\n", encoding="utf-8")
            py_compile.compile(
                str(cached_source),
                invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
            )
            cached_source.write_text("VALUE = 'approved source'\n", encoding="utf-8")

            sourceless_source = modules / f"{sourceless_name}.py"
            sourceless_source.write_text("VALUE = 'approved sourceless bytecode'\n", encoding="utf-8")
            sourceless_bytecode = modules / f"{sourceless_name}.pyc"
            py_compile.compile(
                str(sourceless_source),
                cfile=str(sourceless_bytecode),
                dfile=str(sourceless_source),
                invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
            )
            sourceless_source.unlink()

            directory_fd = os.open(public_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                approved_object = base / "approved-object"
                public_directory.rename(approved_object)
                public_directory.mkdir()
                replacement_modules = public_directory / "py_modules"
                replacement_modules.mkdir()
                (replacement_modules / f"{cached_name}.py").write_text(
                    "VALUE = 'replacement source'\n", encoding="utf-8"
                )
                (replacement_modules / f"{sourceless_name}.py").write_text(
                    "VALUE = 'replacement sourceless source'\n", encoding="utf-8"
                )

                bridge = worker._PluginImportBridge(directory_fd, public_directory)
                with mock.patch.object(sys, "path", list(sys.path)), \
                        mock.patch.object(sys, "path_hooks", list(sys.path_hooks)), \
                        mock.patch.dict(sys.path_importer_cache), \
                        mock.patch.dict(sys.modules):
                    bridge.install()
                    try:
                        sys.path.insert(0, str(public_directory / "py_modules"))
                        cached_module = importlib.import_module(cached_name)
                        sourceless_module = importlib.import_module(sourceless_name)
                        self.assertEqual(cached_module.VALUE, "approved bytecode")
                        self.assertEqual(sourceless_module.VALUE, "approved sourceless bytecode")
                        self.assertEqual(cached_module.__file__, str(public_directory / "py_modules" / f"{cached_name}.py"))
                        self.assertEqual(cached_module.__cached__, str(public_directory / "py_modules" / "__pycache__" / f"{cached_name}.{sys.implementation.cache_tag}.pyc"))
                        self.assertEqual(sourceless_module.__file__, str(public_directory / "py_modules" / f"{sourceless_name}.pyc"))
                    finally:
                        bridge.close()
            finally:
                os.close(directory_fd)

    def test_descriptor_identity_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "plugin"
            directory.mkdir()
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                approved_launch = _approved_launch(directory_fd, flags=[])
                approved_launch["descriptor"]["inode"] += 1
                with self.assertRaisesRegex(worker.WorkerError, "identity does not match"):
                    worker._acquire_approved_launch(approved_launch)
            finally:
                os.close(directory_fd)

    def test_native_extension_load_uses_anchored_origin_then_restores_public_origin(self):
        native_spec = importlib.util.find_spec("math")
        extension_origin = native_spec.origin if native_spec is not None else None
        if (
            not extension_origin
            or not os.path.isfile(extension_origin)
            or not any(extension_origin.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES)
        ):
            self.skipTest("the host math module has no loadable native extension file")

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            plugin_root = base / "plugins"
            plugin_root.mkdir()
            public_directory = plugin_root / "example-plugin"
            native_package_name = f"pinned_native_{uuid.uuid4().hex}"
            package_directory = public_directory / "py_modules" / native_package_name
            package_directory.mkdir(parents=True)
            (package_directory / "__init__.py").write_text(
                "APPROVED_PACKAGE = True\n", encoding="utf-8"
            )
            extension_name = "math" + next(
                suffix for suffix in importlib.machinery.EXTENSION_SUFFIXES
                if extension_origin.endswith(suffix)
            )
            shutil.copy2(extension_origin, package_directory / extension_name)

            directory_fd = os.open(public_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                approved_object = base / "approved-object"
                public_directory.rename(approved_object)
                public_directory.mkdir()
                replacement_package = public_directory / "py_modules" / native_package_name
                replacement_package.mkdir(parents=True)
                (replacement_package / "__init__.py").write_text(
                    "APPROVED_PACKAGE = False\n", encoding="utf-8"
                )
                (replacement_package / "math.py").write_text(
                    "VALUE = 'replacement source module'\n", encoding="utf-8"
                )

                bridge = worker._PluginImportBridge(directory_fd, public_directory)
                with mock.patch.object(sys, "path", list(sys.path)), \
                        mock.patch.object(sys, "path_hooks", list(sys.path_hooks)), \
                        mock.patch.dict(sys.path_importer_cache), \
                        mock.patch.dict(sys.modules):
                    bridge.install()
                    try:
                        sys.path.insert(0, str(public_directory / "py_modules"))
                        package = importlib.import_module(native_package_name)
                        native_module = importlib.import_module(f"{native_package_name}.math")
                        expected_origin = str(public_directory / "py_modules" / native_package_name / extension_name)
                        self.assertTrue(package.APPROVED_PACKAGE)
                        self.assertEqual(package.__path__, [str(public_directory / "py_modules" / native_package_name)])
                        self.assertEqual(native_module.sqrt(81), 9)
                        self.assertEqual(native_module.__file__, expected_origin)
                        self.assertEqual(native_module.__spec__.origin, expected_origin)
                    finally:
                        bridge.close()
            finally:
                os.close(directory_fd)


if __name__ == "__main__":
    unittest.main()
