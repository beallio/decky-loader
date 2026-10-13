from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import os
import platform
import sys
import sysconfig
from pathlib import Path
from typing import Any, Callable, Coroutine, Sequence, cast


class WorkerError(RuntimeError):
    """A worker invocation or plugin startup failure."""


def _runtime_description() -> dict[str, Any]:
    from decky_loader.helpers import get_loader_version

    return {
        "python": platform.python_version(),
        "abi": sysconfig.get_config_var("SOABI") or sys.implementation.cache_tag,
        "cache_tag": sys.implementation.cache_tag,
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "decky_version": get_loader_version(),
    }


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one Decky plugin in an isolated worker")
    parser.add_argument("--describe", action="store_true", help="print runtime information and exit")
    parser.add_argument("--plugin-root", help="directory containing installed plugin directories")
    parser.add_argument("--plugin-directory", help="plugin directory, inside --plugin-root")
    parser.add_argument("--socket", help="Unix socket path used for plugin IPC")
    parser.add_argument("--homebrew", help="isolated Decky homebrew directory")
    parser.add_argument("--user", help="account that owns the isolated Decky tree")
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
        )
        if not value
    ]
    if missing:
        parser.error("missing required arguments: " + ", ".join(missing))


def _resolve_plugin_directory(plugin_root: str, plugin_directory: str) -> tuple[Path, Path]:
    try:
        root = Path(plugin_root).expanduser().resolve(strict=True)
    except OSError as error:
        raise WorkerError(f"plugin root is not accessible: {plugin_root}: {error}") from error
    if not root.is_dir():
        raise WorkerError(f"plugin root is not a directory: {root}")

    requested = Path(plugin_directory).expanduser()
    candidate = requested if requested.is_absolute() else root / requested
    try:
        directory = candidate.resolve(strict=True)
    except OSError as error:
        raise WorkerError(f"plugin directory is not accessible: {plugin_directory}: {error}") from error
    try:
        directory.relative_to(root)
    except ValueError as error:
        raise WorkerError(f"plugin directory escapes plugin root: {plugin_directory}") from error
    if directory == root or directory.parent != root or not directory.is_dir():
        raise WorkerError(f"plugin directory must be a direct directory inside plugin root: {plugin_directory}")
    return root, directory


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WorkerError(f"could not read plugin metadata {path}: {error}") from error


def _plugin_metadata(plugin_directory: Path) -> tuple[str, list[str], str, str | None, str, int]:
    metadata_path = plugin_directory / "plugin.json"
    metadata = _read_json(metadata_path)
    if not isinstance(metadata, dict):
        raise WorkerError(f"plugin metadata must be a JSON object: {metadata_path}")
    metadata = cast(dict[str, Any], metadata)

    try:
        name = metadata["name"]
        author = metadata["author"]
        flags = metadata["flags"]
        api_version = metadata.get("api_version", 0)
    except KeyError as error:
        raise WorkerError(f"plugin metadata is missing required field {error.args[0]!r}: {metadata_path}") from error

    if not isinstance(name, str) or not name:
        raise WorkerError(f"plugin metadata field 'name' must be a non-empty string: {metadata_path}")
    if not isinstance(author, str):
        raise WorkerError(f"plugin metadata field 'author' must be a string: {metadata_path}")
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in cast(list[Any], flags)):
        raise WorkerError(f"plugin metadata field 'flags' must be a list of strings: {metadata_path}")
    if type(api_version) is not int or api_version < 0:
        raise WorkerError(f"plugin metadata field 'api_version' must be a non-negative integer: {metadata_path}")

    version: str | None = None
    package_path = plugin_directory / "package.json"
    if package_path.is_file():
        package = _read_json(package_path)
        if not isinstance(package, dict):
            raise WorkerError(f"package metadata must be a JSON object: {package_path}")
        package = cast(dict[str, Any], package)
        try:
            version = package["version"]
        except KeyError as error:
            raise WorkerError(f"package metadata is missing required field 'version': {package_path}") from error
        if not isinstance(version, str):
            raise WorkerError(f"package metadata field 'version' must be a string: {package_path}")

    entrypoint = plugin_directory / "main.py"
    if not entrypoint.is_file():
        raise WorkerError(f"plugin has no backend entrypoint and is passive: {entrypoint}")

    return name, cast(list[str], flags), str(entrypoint), version, author, api_version


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


def _ensure_user_directory(path: Path, uid: int, gid: int, mode: int) -> None:
    try:
        if path.is_symlink():
            raise WorkerError(f"refusing to use a symlink for plugin storage: {path}")
        if path.exists() and not path.is_dir():
            raise WorkerError(f"expected a directory but found another file: {path}")
        path.mkdir(parents=True, exist_ok=True)
        status = path.stat()
        if os.geteuid() == 0:
            if status.st_uid != uid or status.st_gid != gid:
                os.chown(path, uid, gid)
            os.chmod(path, mode)
        else:
            if status.st_uid != uid:
                raise WorkerError(f"directory is not owned by selected user {uid}: {path}")
            os.chmod(path, mode)
        if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            raise WorkerError(f"selected user cannot read, write, and traverse directory: {path}")
    except OSError as error:
        raise WorkerError(f"could not prepare directory for selected user: {path}: {error}") from error


def _prepare_plugin_storage(homebrew: Path, directory_name: str, user: Any) -> None:
    _ensure_user_directory(homebrew, user.pw_uid, user.pw_gid, 0o755)
    for root_name in ("settings", "data", "logs"):
        storage_root = homebrew / root_name
        _ensure_user_directory(storage_root, user.pw_uid, user.pw_gid, 0o755)
        _ensure_user_directory(storage_root / directory_name, user.pw_uid, user.pw_gid, 0o700)


def _configure_environment(plugin_root: Path, homebrew: Path, user: str) -> None:
    os.environ.update(
        {
            "UNPRIVILEGED_PATH": str(homebrew),
            "PRIVILEGED_PATH": str(plugin_root.parent),
            "PLUGIN_PATH": str(plugin_root),
            "UNPRIVILEGED_USER": user,
            "CHOWN_PLUGIN_PATH": "0",
        }
    )


def _append_system_pythonpaths(get_system_pythonpaths: Any) -> None:
    for python_path in get_system_pythonpaths():
        if python_path and python_path not in sys.path:
            sys.path.append(python_path)


def _launch(args: argparse.Namespace) -> None:
    root, plugin_directory = _resolve_plugin_directory(args.plugin_root, args.plugin_directory)
    name, flags, entrypoint, version, author, api_version = _plugin_metadata(plugin_directory)
    account = _selected_user(args.user)
    if "root" in flags and os.geteuid() != 0:
        raise WorkerError(f"plugin {name!r} requires root privileges, but the worker is not running as root")

    try:
        homebrew = Path(args.homebrew).expanduser().resolve()
        homebrew.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise WorkerError(f"could not prepare isolated homebrew directory {args.homebrew}: {error}") from error
    _configure_environment(root, homebrew, args.user)
    _prepare_plugin_storage(homebrew, plugin_directory.name, account)

    socket_path = Path(args.socket).expanduser().absolute()
    if not socket_path.parent.is_dir():
        raise WorkerError(f"IPC socket parent directory does not exist: {socket_path.parent}")
    if socket_path.exists() or socket_path.is_symlink():
        raise WorkerError(f"refusing to replace an existing IPC socket path: {socket_path}")

    logging.basicConfig(
        level=logging.INFO,
        format="[%(name)s][%(levelname)s]: %(message)s",
        stream=sys.stderr,
    )

    # Importing main loads the legacy module graph (including its SDK aliases and
    # PyInstaller extraction permissions) but does not construct or run services.
    importlib.import_module("decky_loader.main")
    from decky_loader.helpers import get_system_pythonpaths
    from decky_loader.localplatform.localsocket import LocalSocket
    from decky_loader.plugin.sandboxed_plugin import SandboxedPlugin

    class TrackedLocalSocket(LocalSocket):
        server_ready = False
        startup_error: Exception | None = None

        async def setup_server(self, on_new_message: Callable[[str], Coroutine[Any, Any, Any]]) -> None:
            try:
                await super().setup_server(on_new_message)
            except Exception as error:
                self.startup_error = error
                asyncio.get_running_loop().stop()
                raise
            else:
                self.server_ready = True

    _append_system_pythonpaths(get_system_pythonpaths)

    tracked_socket = TrackedLocalSocket()
    tracked_socket.socket_addr = str(socket_path)
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = _argument_parser()
    args = parser.parse_args(argv)
    if args.describe:
        print(json.dumps(_runtime_description(), ensure_ascii=False, separators=(",", ":")))
        return 0

    _required_arguments(parser, args)
    try:
        _launch(args)
    except Exception as error:
        print(f"DeckyPluginWorker: {error}", file=sys.stderr)
        return 1
    return 0
