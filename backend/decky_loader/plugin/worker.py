from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import (
    BYTECODE_SUFFIXES,
    EXTENSION_SUFFIXES,
    SOURCE_SUFFIXES,
    ExtensionFileLoader,
    FileFinder,
    ModuleSpec,
    SourceFileLoader,
    SourcelessFileLoader,
)
import json
import logging
import os
import platform
import stat
import sys
import sysconfig
import zipimport
from pathlib import Path
from types import CodeType, ModuleType
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Mapping, Sequence, cast

if TYPE_CHECKING:
    from _typeshed import ReadableBuffer
    from typing import Protocol

    class _FileFinderConstructor(Protocol):
        # CPython calls loader factories; typeshed restricts these to classes.
        def __call__(
            self, path: str,
            *loader_details: tuple[Callable[[str, str], Loader], Sequence[str]],
        ) -> FileFinder: ...


class WorkerError(RuntimeError):
    """A worker invocation or plugin startup failure."""


_SDK_ALIAS_ROOTS = frozenset(
    ("browser", "enums", "helpers", "injector", "loader", "localplatform",
     "main", "plugin", "settings", "updater", "utilities", "wsrouter")
)
_prepared_bootstraps: set[str] = set()


class _AnchoredSourceFileLoader(SourceFileLoader):
    def __init__(self, bridge: _PluginImportBridge, fullname: str, path: str) -> None:
        self._bridge = bridge
        super().__init__(fullname, bridge.public_path(path))

    def get_data(self, path: str) -> bytes:
        return super().get_data(self._bridge.anchored_path(path))

    def path_stats(self, path: str) -> Mapping[str, float | int]:
        return super().path_stats(self._bridge.anchored_path(path))

    def set_data(self, path: str, data: ReadableBuffer, *, _mode: int = 0o666) -> None:
        super().set_data(self._bridge.anchored_path(path), data, _mode=_mode)


class _AnchoredBytecodeLoader(SourcelessFileLoader):
    def __init__(self, bridge: _PluginImportBridge, fullname: str, path: str) -> None:
        self._bridge = bridge
        super().__init__(fullname, bridge.public_path(path))

    def get_data(self, path: str) -> bytes:
        return super().get_data(self._bridge.anchored_path(path))


class _PublicOriginExtensionLoader(Loader):
    def __init__(self, loader: ExtensionFileLoader, public_origin: str) -> None:
        self._loader = loader
        self._anchored_origin = loader.path
        self._public_origin = public_origin

    def create_module(self, spec: ModuleSpec) -> ModuleType | None:
        spec.origin = self._anchored_origin
        try:
            return self._loader.create_module(spec)
        finally:
            spec.origin = self._public_origin

    def exec_module(self, module: ModuleType) -> None:
        spec = module.__spec__
        if spec is None:
            raise ImportError(f"native module {module.__name__!r} has no import specification")
        spec.origin = self._anchored_origin
        try:
            self._loader.exec_module(module)
        finally:
            spec.origin = self._public_origin
            module.__file__ = self._public_origin

    def get_filename(self, fullname: str) -> str:
        return self._public_origin

    def __getattr__(self, name: str) -> Any:
        return getattr(self._loader, name)


class _PublicOriginZipLoader(Loader):
    def __init__(
        self, bridge: _PluginImportBridge, loader: zipimport.zipimporter
    ) -> None:
        self._bridge = bridge
        self._loader = loader

    def create_module(self, spec: ModuleSpec) -> ModuleType | None:
        return self._loader.create_module(spec)

    def _public_code(self, code: CodeType) -> CodeType:
        try:
            filename = self._bridge.public_path(code.co_filename)
        except ImportError:
            filename = code.co_filename
        return code.replace(
            co_filename=filename,
            co_consts=tuple(
                self._public_code(value) if isinstance(value, CodeType) else value
                for value in code.co_consts
            ),
        )

    def get_code(self, fullname: str) -> CodeType:
        return self._public_code(self._loader.get_code(fullname))

    def exec_module(self, module: ModuleType) -> None:
        exec(self.get_code(module.__name__), module.__dict__)

    def get_filename(self, fullname: str) -> str:
        return self._bridge.public_path(self._loader.get_filename(fullname))

    def get_data(self, path: str) -> bytes:
        return self._loader.get_data(self._bridge.anchored_path(path))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._loader, name)


class _AnchoredFileFinder:
    def __init__(self, bridge: _PluginImportBridge, path: str) -> None:
        self._bridge = bridge
        try:
            self._finder = zipimport.zipimporter(path)
        except zipimport.ZipImportError:
            self._finder = cast("_FileFinderConstructor", FileFinder)(
                path,
                (ExtensionFileLoader, EXTENSION_SUFFIXES),
                (self._source_loader, SOURCE_SUFFIXES),
                (self._bytecode_loader, BYTECODE_SUFFIXES),
            )

    def _source_loader(self, fullname: str, path: str) -> Loader:
        return _AnchoredSourceFileLoader(self._bridge, fullname, path)

    def _bytecode_loader(self, fullname: str, path: str) -> Loader:
        return _AnchoredBytecodeLoader(self._bridge, fullname, path)

    def find_spec(
        self,
        fullname: str,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        spec = self._finder.find_spec(fullname, target=target)
        if spec is None:
            return None
        cached = spec.cached
        if spec.origin is not None:
            public_origin = self._bridge.public_path(spec.origin)
            if isinstance(spec.loader, ExtensionFileLoader):
                spec.loader = _PublicOriginExtensionLoader(spec.loader, public_origin)
            elif isinstance(spec.loader, zipimport.zipimporter):
                spec.loader = _PublicOriginZipLoader(self._bridge, spec.loader)
            spec.origin = public_origin
        if cached is not None:
            spec.cached = self._bridge.public_path(cached)
        if spec.submodule_search_locations is not None:
            spec.submodule_search_locations = [
                self._bridge.public_path(location)
                for location in spec.submodule_search_locations
            ]
        return spec

    def invalidate_caches(self) -> None:
        self._finder.invalidate_caches()


class _PluginImportBridge:
    def __init__(self, directory_fd: int, public_directory: Path) -> None:
        self._public_directory = Path(os.path.abspath(public_directory))
        self._anchored_directory = f"/proc/self/fd/{directory_fd}"
        self._installed = False

    def _relative_public_path(self, path: str) -> Path:
        candidate = Path(os.path.abspath(path))
        try:
            return candidate.relative_to(self._public_directory)
        except ValueError as error:
            raise OSError(f"path is outside the approved plugin directory: {path}") from error

    def anchored_path(self, public_path: str) -> str:
        relative = self._relative_public_path(public_path)
        return os.path.join(self._anchored_directory, *relative.parts)

    def public_path(self, anchored_path: str) -> str:
        candidate = Path(os.path.abspath(anchored_path))
        try:
            relative = candidate.relative_to(Path(self._anchored_directory))
        except ValueError as error:
            raise ImportError(f"path is outside the approved plugin descriptor: {anchored_path}") from error
        return str(self._public_directory.joinpath(relative))

    def entrypoint_loader(self, public_path: str) -> Loader:
        relative = self._relative_public_path(public_path)
        if relative.parts != ("main.py",):
            raise WorkerError(f"loader-managed entrypoint is outside the approved plugin directory: {public_path}")
        return _AnchoredSourceFileLoader(self, "_", self.anchored_path(public_path))

    def __call__(self, path_entry: object) -> _AnchoredFileFinder:
        if not isinstance(path_entry, str):
            raise ImportError("plugin import path must be a string")
        candidate = Path(os.path.abspath(path_entry))
        try:
            relative = candidate.relative_to(self._public_directory)
        except ValueError as error:
            raise ImportError(f"path is outside the approved plugin directory: {path_entry}") from error
        anchored_path = os.path.join(self._anchored_directory, *relative.parts)
        return _AnchoredFileFinder(self, anchored_path)

    def _clear_importer_cache(self) -> None:
        for path_entry in tuple(sys.path_importer_cache):
            try:
                Path(os.path.abspath(path_entry)).relative_to(self._public_directory)
            except ValueError:
                continue
            sys.path_importer_cache.pop(path_entry, None)

    def install(self) -> None:
        self._clear_importer_cache()
        sys.path_hooks.insert(0, self)
        self._installed = True

    def close(self) -> None:
        if self._installed:
            sys.path_hooks[:] = [path_hook for path_hook in sys.path_hooks if path_hook is not self]
            self._installed = False
        self._clear_importer_cache()


class _SdkAliasLoader(Loader):
    def __init__(self, module: ModuleType) -> None:
        self.module = module
        self.original_spec = module.__spec__

    def create_module(self, spec: ModuleSpec) -> ModuleType:
        return self.module

    def exec_module(self, module: ModuleType) -> None:
        # importlib assigns the alias spec; keep the real SDK's reload identity.
        module.__spec__ = self.original_spec


class _SdkAliasFinder(MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if fullname.partition(".")[0] not in _SDK_ALIAS_ROOTS:
            return None
        module = importlib.import_module(f"decky_loader.{fullname}")
        spec = ModuleSpec(fullname, _SdkAliasLoader(module), is_package=hasattr(module, "__path__"))
        if module.__spec__ is not None:
            spec.submodule_search_locations = module.__spec__.submodule_search_locations
        return spec


def _json_encoder_mode() -> str:
    default = "rust" if getattr(sys, "frozen", False) else "python"
    selected = os.environ.get("DECKY_JSON_ENCODER", default)
    if selected not in ("python", "rust"):
        raise WorkerError(
            f"invalid DECKY_JSON_ENCODER value {selected!r}; expected 'python' or 'rust'"
        )
    return selected


def _native_json_module(required: bool) -> ModuleType | None:
    try:
        native = importlib.import_module("_decky_native_json")
    except Exception as error:
        if required:
            raise WorkerError(f"could not load requested native JSON encoder: {error}") from error
        return None
    if not callable(getattr(native, "dumps", None)):
        if required:
            raise WorkerError("requested native JSON encoder does not export callable dumps")
        return None
    if not isinstance(getattr(native, "__version__", None), str):
        if required:
            raise WorkerError("requested native JSON encoder does not export a string __version__")
        return None
    return native

def _json_encoder_description() -> dict[str, Any]:
    selected = _json_encoder_mode()
    default = "rust" if getattr(sys, "frozen", False) else "python"
    required = selected == "rust" or bool(getattr(sys, "frozen", False))
    native = _native_json_module(required=required)
    native_info: dict[str, Any] | None = None
    if native is not None:
        module_path = getattr(native, "__file__", None)
        if not isinstance(module_path, str):
            if required:
                raise WorkerError("requested native JSON encoder has no module file")
        else:
            try:
                digest = hashlib.sha256(Path(module_path).read_bytes()).hexdigest()
            except OSError as error:
                if required:
                    raise WorkerError(f"could not hash requested native JSON encoder: {error}") from error
            else:
                native_info = {
                    "module": "_decky_native_json",
                    "version": getattr(native, "__version__", None),
                    "sha256": digest,
                }
    return {
        "selected": selected,
        "default": default,
        "native": native_info,
        "system_unwind_library": "libgcc_s.so.1",
    }


def prepare_runtime(bootstrap: str) -> None:
    if bootstrap in _prepared_bootstraps:
        return
    selected = _json_encoder_mode()
    if bootstrap == "full":
        importlib.import_module("decky_loader.main")
    elif bootstrap == "lean":
        from decky_loader.localplatform.localplatform import chmod

        if hasattr(sys, "_MEIPASS"):
            chmod(cast(str, getattr(sys, "_MEIPASS")), 755)
        if not any(isinstance(finder, _SdkAliasFinder) for finder in sys.meta_path):
            sys.meta_path.insert(0, _SdkAliasFinder())
    else:
        raise WorkerError(f"unknown worker bootstrap: {bootstrap}")
    importlib.import_module("decky_loader.localplatform.localsocket")
    from decky_loader.plugin import sandboxed_plugin
    if selected == "rust":
        native = _native_json_module(required=True)
        if native is None:
            raise WorkerError("requested native JSON encoder is unavailable")
        sandboxed_plugin.dumps = native.dumps
    else:
        sandboxed_plugin.dumps = json.dumps
    auth_token = os.environ.get("DECKY_AUTH_TOKEN")
    if auth_token is not None:
        from decky_loader import helpers

        helpers.csrf_token = auth_token
    _prepared_bootstraps.add(bootstrap)


def _normalize_code(code: CodeType) -> CodeType:
    return code.replace(
        co_filename="",
        co_consts=tuple(_normalize_code(value) if isinstance(value, CodeType) else value
                        for value in code.co_consts),
    )


def _helper_source_description() -> dict[str, Any]:
    from decky_loader import helpers

    source = Path(helpers.__file__).read_bytes()
    loader = helpers.__loader__
    code = loader.get_code(helpers.__name__)
    compiled = compile(source, helpers.__file__, "exec", dont_inherit=True, optimize=0)
    if not isinstance(code, CodeType) or _normalize_code(code) != _normalize_code(compiled):
        raise WorkerError("packaged helper source differs from its loaded Python code")
    return {
        "sha256": hashlib.sha256(source).hexdigest(),
        "code_matches_source": True,
    }


def _runtime_description() -> dict[str, Any]:
    from decky_loader.helpers import get_loader_version

    encoder_description = _json_encoder_description()
    return {
        "python": platform.python_version(),
        "abi": sysconfig.get_config_var("SOABI") or sys.implementation.cache_tag,
        "cache_tag": sys.implementation.cache_tag,
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "decky_version": get_loader_version(),
        "sdk_sources": {"decky_loader.helpers": _helper_source_description()},
        "json_encoder": encoder_description,
    }


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one Decky plugin in an isolated worker")
    parser.add_argument("--describe", action="store_true", help="print runtime information and exit")
    parser.add_argument("--plugin-root", help="directory containing installed plugin directories")
    parser.add_argument("--plugin-directory", help="plugin directory, inside --plugin-root")
    parser.add_argument("--socket", help="Unix socket path used for plugin IPC")
    parser.add_argument("--homebrew", help="isolated Decky homebrew directory")
    parser.add_argument("--user", help="account that owns the isolated Decky tree")
    parser.add_argument("--approved-launch", type=json.loads, help="authoritative launch metadata and directory reference")
    parser.add_argument("--bootstrap", choices=("full", "lean"), default="lean", help="SDK startup path")
    return parser


def _required_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    missing = [
        flag
        for flag, value in (
            ("--plugin-root", args.plugin_root),
            ("--plugin-directory", args.plugin_directory),
            ("--socket", args.socket),
            ("--homebrew", args.homebrew),
            ("--user", args.user),
            ("--approved-launch", args.approved_launch),
        )
        if not value
    ]
    if missing:
        parser.error("missing required arguments: " + ", ".join(missing))


def _acquire_approved_launch(value: Any) -> tuple[int, dict[str, Any]]:
    launch = cast(dict[str, Any], value)
    if not isinstance(value, dict) or set(launch) != {"descriptor", "metadata"}:
        raise WorkerError("--approved-launch must contain exactly descriptor and metadata objects")
    descriptor_value = launch["descriptor"]
    metadata_value = launch["metadata"]
    descriptor = cast(dict[str, Any], descriptor_value)
    metadata = cast(dict[str, Any], metadata_value)
    if not isinstance(descriptor_value, dict) or set(descriptor) != {"pid", "fd", "device", "inode"}:
        raise WorkerError("--approved-launch descriptor has an invalid shape")
    if not isinstance(metadata_value, dict) or set(metadata) != {
        "name", "author", "flags", "api_version", "version"
    }:
        raise WorkerError("--approved-launch metadata has an invalid shape")

    def unsigned_integer(item: Any, field: str, maximum: int) -> int:
        if type(item) is not int or item < 0 or item > maximum:
            raise WorkerError(f"--approved-launch {field} must be an unsigned integer")
        return item

    pid = unsigned_integer(descriptor["pid"], "descriptor.pid", 2**32 - 1)
    if pid == 0:
        raise WorkerError("--approved-launch descriptor.pid must be positive")
    remote_fd = unsigned_integer(descriptor["fd"], "descriptor.fd", 2**31 - 1)
    device = unsigned_integer(descriptor["device"], "descriptor.device", 2**64 - 1)
    inode = unsigned_integer(descriptor["inode"], "descriptor.inode", 2**64 - 1)

    name = metadata["name"]
    author = metadata["author"]
    flags = metadata["flags"]
    api_version = metadata["api_version"]
    version = metadata["version"]
    if not isinstance(name, str) or not name:
        raise WorkerError("--approved-launch metadata.name must be a non-empty string")
    if not isinstance(author, str):
        raise WorkerError("--approved-launch metadata.author must be a string")
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in cast(list[object], flags)):
        raise WorkerError("--approved-launch metadata.flags must be a list of strings")
    if type(api_version) is not int or api_version < 0:
        raise WorkerError("--approved-launch metadata.api_version must be a non-negative integer")
    if version is not None and not isinstance(version, str):
        raise WorkerError("--approved-launch metadata.version must be a string or null")

    approved_metadata: dict[str, Any] = {
        "name": name,
        "author": author,
        "flags": list(cast(list[str], flags)),
        "api_version": api_version,
        "version": version,
    }
    reference = f"/proc/{pid}/fd/{remote_fd}"
    try:
        directory_fd = os.open(reference, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as error:
        raise WorkerError(f"could not acquire approved plugin directory descriptor {reference}: {error}") from error
    try:
        status = os.fstat(directory_fd)
    except OSError as error:
        os.close(directory_fd)
        raise WorkerError(f"could not inspect approved plugin directory descriptor {reference}: {error}") from error
    if status.st_dev != device or status.st_ino != inode:
        os.close(directory_fd)
        raise WorkerError(
            "approved plugin directory descriptor identity does not match the Rust launch reference"
        )

    return directory_fd, approved_metadata


def _public_plugin_paths(plugin_root: str, plugin_directory: str) -> tuple[Path, Path]:
    root = Path(os.path.abspath(os.path.expanduser(plugin_root)))
    requested = Path(os.path.expanduser(plugin_directory))
    directory = requested if requested.is_absolute() else root / requested
    directory = Path(os.path.abspath(directory))
    try:
        relative = directory.relative_to(root)
    except ValueError as error:
        raise WorkerError(f"plugin directory escapes plugin root: {plugin_directory}") from error
    if len(relative.parts) != 1 or relative.parts[0] in ("", ".", ".."):
        raise WorkerError(
            f"plugin directory must be a direct directory inside plugin root: {plugin_directory}"
        )
    return root, directory


def _validate_approved_entrypoint(directory_fd: int, public_path: Path) -> None:
    try:
        entrypoint_fd = os.open(
            "main.py",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
    except OSError as error:
        raise WorkerError(f"plugin has no readable backend entrypoint: {public_path}: {error}") from error
    try:
        if not stat.S_ISREG(os.fstat(entrypoint_fd).st_mode):
            raise WorkerError(f"plugin backend entrypoint is not a regular file: {public_path}")
    finally:
        os.close(entrypoint_fd)


def _selected_user(user: str) -> Any:
    try:
        import pwd
    except ImportError as error:
        raise WorkerError("the worker requires a POSIX account database") from error

    try:
        account = pwd.getpwnam(user)
    except KeyError as error:
        raise WorkerError(f"selected user does not exist: {user}") from error

    if not hasattr(os, "geteuid") or not hasattr(os, "getegid"):
        raise WorkerError("the worker requires POSIX user and group support")
    effective_uid = os.geteuid()
    if effective_uid != 0 and effective_uid != account.pw_uid:
        raise WorkerError(
            f"worker must run as root or as selected user {user!r}; effective uid is {effective_uid}"
        )
    if effective_uid != 0 and os.getegid() != account.pw_gid:
        raise WorkerError(
            f"worker must run with selected user {user!r}'s primary group id {account.pw_gid}"
        )
    return account


def _absolute_storage_path(path: Path) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _open_directory_at(parent_fd: int, name: str, path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        directory_fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        try:
            status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            status = None
        if status is not None:
            if stat.S_ISLNK(status.st_mode):
                raise WorkerError(
                    f"refusing to use a symlink for plugin storage: {path}"
                ) from error
            if not stat.S_ISDIR(status.st_mode):
                raise WorkerError(
                    f"expected a directory but found another file: {path}"
                ) from error
        raise

    try:
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            raise WorkerError(f"expected a directory but found another file: {path}")
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _open_or_create_directory_at(parent_fd: int, name: str, path: Path) -> int:
    try:
        return _open_directory_at(parent_fd, name, path)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o777, dir_fd=parent_fd)
        except FileExistsError:
            pass
        return _open_directory_at(parent_fd, name, path)


def _open_or_create_storage_path(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptors = [os.open(os.sep, flags)]
    try:
        for component in path.parts[1:]:
            if component == "..":
                if len(descriptors) > 1:
                    os.close(descriptors.pop())
                continue
            if component in ("", "."):
                continue
            descriptors.append(
                _open_or_create_directory_at(descriptors[-1], component, path)
            )
        return descriptors.pop()
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _ensure_user_directory_fd(
    directory_fd: int, path: Path, uid: int, gid: int, mode: int
) -> None:
    status = os.fstat(directory_fd)
    if not stat.S_ISDIR(status.st_mode):
        raise WorkerError(f"expected a directory but found another file: {path}")

    if os.geteuid() == 0:
        if status.st_uid != uid or status.st_gid != gid:
            os.fchown(directory_fd, uid, gid)
        os.fchmod(directory_fd, mode)
    else:
        if status.st_uid != uid:
            raise WorkerError(f"directory is not owned by selected user {uid}: {path}")
        os.fchmod(directory_fd, mode)

    if not os.access(".", os.R_OK | os.W_OK | os.X_OK, dir_fd=directory_fd):
        raise WorkerError(f"selected user cannot read, write, and traverse directory: {path}")


def _prepare_plugin_storage(homebrew: Path, directory_name: str, user: Any) -> Path:
    if directory_name in ("", ".", "..") or os.sep in directory_name:
        raise WorkerError(f"invalid plugin storage directory name: {directory_name!r}")

    homebrew = _absolute_storage_path(homebrew)
    homebrew_fd: int | None = None
    try:
        homebrew_fd = _open_or_create_storage_path(homebrew)
        _ensure_user_directory_fd(homebrew_fd, homebrew, user.pw_uid, user.pw_gid, 0o755)
        for root_name in ("settings", "data", "logs"):
            storage_root = homebrew / root_name
            storage_fd = _open_or_create_directory_at(homebrew_fd, root_name, storage_root)
            try:
                _ensure_user_directory_fd(storage_fd, storage_root, user.pw_uid, user.pw_gid, 0o755)
                plugin_path = storage_root / directory_name
                plugin_fd = _open_or_create_directory_at(storage_fd, directory_name, plugin_path)
                try:
                    _ensure_user_directory_fd(plugin_fd, plugin_path, user.pw_uid, user.pw_gid, 0o700)
                finally:
                    os.close(plugin_fd)
            finally:
                os.close(storage_fd)
    except WorkerError:
        raise
    except OSError as error:
        raise WorkerError(
            f"could not prepare directory for selected user: {homebrew}: {error}"
        ) from error
    finally:
        if homebrew_fd is not None:
            os.close(homebrew_fd)
    return homebrew


def _configure_environment(plugin_root: Path, homebrew: Path, user: str) -> None:
    os.environ.update(
        {
            "UNPRIVILEGED_PATH": str(homebrew),
            "PRIVILEGED_PATH": str(plugin_root.parent),
            "PLUGIN_PATH": str(plugin_root),
            "UNPRIVILEGED_USER": user,
            "CHOWN_PLUGIN_PATH": os.environ.get("CHOWN_PLUGIN_PATH", "0"),
        }
    )


def _append_system_pythonpaths(get_system_pythonpaths: Any) -> None:
    for python_path in get_system_pythonpaths():
        if python_path and python_path not in sys.path:
            sys.path.append(python_path)


def launch(args: argparse.Namespace) -> None:
    directory_fd, metadata = _acquire_approved_launch(getattr(args, "approved_launch", None))
    process_owned = False
    try:
        root, plugin_directory = _public_plugin_paths(args.plugin_root, args.plugin_directory)
        name = cast(str, metadata["name"])
        flags = cast(list[str], metadata["flags"])
        version = cast(str | None, metadata["version"])
        author = cast(str, metadata["author"])
        api_version = cast(int, metadata["api_version"])
        entrypoint = str(plugin_directory / "main.py")
        _validate_approved_entrypoint(directory_fd, Path(entrypoint))

        account = _selected_user(args.user)
        if "root" in flags and os.geteuid() != 0:
            raise WorkerError(f"plugin {name!r} requires root privileges, but the worker is not running as root")

        try:
            homebrew = _prepare_plugin_storage(Path(args.homebrew), plugin_directory.name, account)
        except OSError as error:
            raise WorkerError(
                f"could not prepare isolated homebrew directory {args.homebrew}: {error}"
            ) from error
        _configure_environment(root, homebrew, args.user)

        socket_path = Path(args.socket).expanduser().absolute()
        if not socket_path.parent.is_dir():
            raise WorkerError(f"IPC socket parent directory does not exist: {socket_path.parent}")
        if socket_path.exists() or socket_path.is_symlink():
            raise WorkerError(f"refusing to replace an existing IPC socket path: {socket_path}")

        from decky_loader.localplatform.localplatform import get_log_level

        logging.basicConfig(
            level=get_log_level(),
            format="[%(name)s][%(levelname)s]: %(message)s",
            stream=sys.stderr,
        )

        prepare_runtime(args.bootstrap)
        from decky_loader.helpers import get_system_pythonpaths
        from decky_loader.localplatform.localsocket import LocalSocket
        from decky_loader.plugin.sandboxed_plugin import SandboxedPlugin

        # A root plugin's socket is given to the loader's user, so that only the
        # user's backend can connect. Only the root host sets this.
        socket_owner = cast("list[int] | None", getattr(args, "socket_owner", None))

        class TrackedLocalSocket(LocalSocket):
            server_ready = False
            startup_error: Exception | None = None

            async def setup_server(
                self, on_new_message: Callable[[str], Coroutine[Any, Any, Any]]
            ) -> None:
                try:
                    await super().setup_server(on_new_message)
                    if socket_owner is not None:
                        os.chown(self.socket_addr, socket_owner[0], socket_owner[1], follow_symlinks=False)
                        os.chmod(self.socket_addr, 0o600)
                except Exception as error:
                    self.startup_error = error
                    asyncio.get_running_loop().stop()
                    raise
                else:
                    self.server_ready = True

        system_pythonpaths = getattr(args, "system_pythonpaths", None)
        if system_pythonpaths is None:
            _append_system_pythonpaths(get_system_pythonpaths)
        else:
            _append_system_pythonpaths(lambda: system_pythonpaths)

        tracked_socket = TrackedLocalSocket()
        tracked_socket.socket_addr = str(socket_path)
        bridge = _PluginImportBridge(directory_fd, plugin_directory)
        bridge.install()
        # sys.path_hooks retains the bridge. Keep its descriptor process-owned
        # through failed imports and interpreter exit callbacks, not just the loop.
        process_owned = True
        plugin = SandboxedPlugin(
            name,
            False,
            flags,
            entrypoint,
            plugin_directory.name,
            str(root),
            version,
            author,
            api_version,
            entrypoint_loader=bridge.entrypoint_loader,
        )
        try:
            plugin.initialize(tracked_socket)
        except SystemExit as error:
            if not tracked_socket.server_ready:
                raise WorkerError(
                    f"plugin {name!r} exited during startup before its IPC listener became ready"
                ) from error
            raise WorkerError(f"plugin {name!r} exited with status {error.code}") from error

        if tracked_socket.startup_error is not None:
            raise WorkerError(f"could not create plugin IPC listener: {tracked_socket.startup_error}")
        if not tracked_socket.server_ready:
            raise WorkerError(f"plugin {name!r} exited before its IPC listener became ready")
    finally:
        if not process_owned:
            os.close(directory_fd)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _argument_parser()
    args = parser.parse_args(argv)
    if args.describe:
        try:
            print(json.dumps(_runtime_description(), ensure_ascii=False, separators=(",", ":")))
        except Exception as error:
            print(f"DeckyPluginWorker: {error}", file=sys.stderr)
            return 1
        return 0

    _required_arguments(parser, args)
    try:
        launch(args)
    except Exception as error:
        print(f"DeckyPluginWorker: {error}", file=sys.stderr)
        return 1
    return 0
