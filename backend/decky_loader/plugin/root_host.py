"""Root plugin host: runs only the plugins marked ``root`` for one user.

See docs/root-plugin-host.md. systemd starts this process as root through a
socket unit whose socket only the selected user can open. The user's Rust
backend sends JSON-line requests. It gives plugin folder names, never paths or
code. Before each start the host checks, through an open folder descriptor,
that the folder belongs entirely to root and matches the tree hash that root
recorded when the plugin was approved. The plugin then sees that checked folder
read-only at a root-owned path in its own mount namespace.

The process stays single-threaded so that it can fork plugin workers with the
shared launcher code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import pwd
import select
import signal
import socket
import stat
import struct
import sys
import time
from pathlib import Path
from typing import Any, cast

from decky_loader.plugin import launcher, worker

PROTOCOL = 1
STATE_ROOT = Path("/var/lib/decky-loader-runtime/root-host")
RUN_ROOT = Path("/run/decky-loader-runtime")
IDLE_EXIT_SECONDS = 60.0
_STOP_TIMEOUT = 5.0
_MAX_JSON_BYTES = 1 << 20
_MAX_REQUEST_BYTES = 1 << 16
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class RootHostError(Exception):
    pass


def _check_entry(info: os.stat_result, owner_uid: int, relative: str) -> None:
    item = relative or "the plugin folder"
    if info.st_uid != owner_uid:
        raise RootHostError(f"{item} is not owned by root")
    if not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022:
        raise RootHostError(f"{item} can be changed by other users")


def _walk(directory_fd: int, prefix: str, owner_uid: int, records: list[bytes]) -> None:
    with os.scandir(directory_fd) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        relative = posixpath.join(prefix, name)
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _check_entry(info, owner_uid, relative)
        mode = f"{stat.S_IMODE(info.st_mode):o}"
        if stat.S_ISDIR(info.st_mode):
            records.append(f"{relative}\0d\0{mode}\0\n".encode("utf-8", "surrogateescape"))
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                child = os.fstat(child_fd)
                if (child.st_dev, child.st_ino) != (info.st_dev, info.st_ino):
                    raise RootHostError(f"{relative} changed while it was checked")
                _walk(child_fd, relative, owner_uid, records)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(info.st_mode):
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory_fd)
            try:
                opened = os.fstat(file_fd)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise RootHostError(f"{relative} changed while it was checked")
                digest = hashlib.sha256()
                while chunk := os.read(file_fd, 1 << 20):
                    digest.update(chunk)
            finally:
                os.close(file_fd)
            records.append(f"{relative}\0f\0{mode}\0{digest.hexdigest()}\n".encode("utf-8", "surrogateescape"))
        elif stat.S_ISLNK(info.st_mode):
            target = os.readlink(name, dir_fd=directory_fd)
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative), target))
            if posixpath.isabs(target) or resolved == ".." or resolved.startswith("../"):
                raise RootHostError(f"{relative} is a link that leaves the plugin folder")
            records.append(f"{relative}\0l\0\0{target}\n".encode("utf-8", "surrogateescape"))
        else:
            raise RootHostError(f"{relative} is not a file, folder or link")


def plugin_tree_sha256(directory_fd: int, *, owner_uid: int = 0) -> str:
    """Hash a plugin folder after checking that only `owner_uid` can change it.

    Raises RootHostError for any entry that another user owns or can write, any
    special file, and any link that points outside the folder.
    """
    _check_entry(os.fstat(directory_fd), owner_uid, "")
    records: list[bytes] = []
    _walk(directory_fd, "", owner_uid, records)
    digest = hashlib.sha256()
    for record in sorted(records):
        digest.update(record)
    return digest.hexdigest()


def _read_json_at(directory_fd: int, name: str) -> Any:
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    with os.fdopen(file_fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise RootHostError(f"{name} is not a regular file")
        return json.loads(handle.read(_MAX_JSON_BYTES))


def plugin_metadata(directory_fd: int) -> dict[str, Any]:
    """The launch metadata, read the same way as the Rust backend reads it."""
    plugin = _read_json_at(directory_fd, "plugin.json")
    if not isinstance(plugin, dict):
        raise RootHostError("plugin.json is missing or is not an object")
    plugin = cast(dict[str, Any], plugin)
    name, author, flags = plugin.get("name"), plugin.get("author"), plugin.get("flags")
    api_version = plugin.get("api_version", 0)
    if not isinstance(name, str) or not name:
        raise RootHostError("plugin.json has no valid name")
    if not isinstance(author, str):
        raise RootHostError("plugin.json has no valid author")
    if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in cast(list[Any], flags)):
        raise RootHostError("plugin.json has no valid flags list")
    if type(api_version) is not int or api_version < 0:
        raise RootHostError("plugin.json has no valid API version")
    package = _read_json_at(directory_fd, "package.json")
    version = cast(dict[str, Any], package).get("version") if isinstance(package, dict) else None
    if version is not None and not isinstance(version, str):
        raise RootHostError("package.json has no valid version")
    return {
        "name": name,
        "author": author,
        "flags": list(cast(list[str], flags)),
        "api_version": api_version,
        "version": version,
    }


def _directory_name(value: Any) -> str:
    if not isinstance(value, str) or value in ("", ".", "..") or "/" in value or "\0" in value:
        raise RootHostError("directory must be a plain folder name")
    return value


def _read_root_file(path: Path) -> Any:
    """Read JSON from a root-owned file that no other user can read or change."""
    file_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(file_fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RootHostError(f"{path} must be a root-owned file that only root can change")
        return json.loads(handle.read(_MAX_JSON_BYTES))


def _ensure_root_directory(path: Path, mode: int) -> None:
    try:
        os.mkdir(path, mode)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RootHostError(f"{path} must be a root-owned folder that only root can change")
    os.chmod(path, mode)


class Config:
    def __init__(self, path: Path) -> None:
        data = _read_root_file(path)
        if not isinstance(data, dict):
            raise RootHostError("config must be a JSON object")
        data = cast(dict[str, Any], data)
        if data.get("protocol") != PROTOCOL:
            raise RootHostError(f"config protocol must be {PROTOCOL}")
        user, uid, gid, homebrew = data.get("user"), data.get("uid"), data.get("gid"), data.get("homebrew")
        if not isinstance(user, str) or type(uid) is not int or type(gid) is not int or uid <= 0:
            raise RootHostError("config has no valid non-root user")
        if pwd.getpwnam(user).pw_uid != uid:
            raise RootHostError("config user and uid do not match")
        if not isinstance(homebrew, str) or not os.path.isabs(homebrew):
            raise RootHostError("config homebrew must be an absolute path")
        self.user: str = user
        self.uid: int = uid
        self.gid: int = gid
        self.homebrew = Path(homebrew)
        self.state_directory = path.parent
        self.mount_root = self.state_directory / "mnt"
        self.socket_directory = RUN_ROOT / str(uid) / "root-plugins"

    def approved(self, directory: str) -> dict[str, Any]:
        state = _read_root_file(self.state_directory / "state.json")
        plugins = cast(dict[str, Any], state).get("plugins") if isinstance(state, dict) else None
        entry = cast(dict[str, Any], plugins).get(directory) if isinstance(plugins, dict) else None
        if not isinstance(entry, dict):
            raise RootHostError(f"{directory} is not an approved root plugin")
        return cast(dict[str, Any], entry)

    def approved_directories(self) -> dict[str, Any]:
        try:
            state = _read_root_file(self.state_directory / "state.json")
        except FileNotFoundError:
            return {}
        plugins = cast(dict[str, Any], state).get("plugins") if isinstance(state, dict) else None
        return cast(dict[str, Any], plugins) if isinstance(plugins, dict) else {}


class Host:
    def __init__(self, config: Config, listener: socket.socket) -> None:
        self.config = config
        self.listener = listener
        self.connection: socket.socket | None = None
        self.buffer = b""
        self.greeted = False
        self.children: dict[str, launcher._Child] = {}  # pyright: ignore[reportPrivateUsage]
        self.directories: dict[str, tuple[str, int]] = {}
        self.stopping = False
        self.children_changed = False
        self.wake_read, self.wake_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)

    # Connection handling

    def accept(self) -> None:
        connection, _ = self.listener.accept()
        uid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[1]
        if self.connection is not None or uid != self.config.uid:
            connection.close()
            return
        connection.setblocking(False)
        self.connection = connection
        self.buffer = b""
        self.greeted = False

    def drop_connection(self) -> None:
        # Root plugins belong to the backend that started them.
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        self.stop_all()

    def read_connection(self) -> None:
        assert self.connection is not None
        try:
            data = self.connection.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            self.drop_connection()
            return
        self.buffer += data
        while self.connected() and b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            self.reply(self.handle_line(line))
        if len(self.buffer) > _MAX_REQUEST_BYTES:
            self.drop_connection()

    def reply(self, response: dict[str, Any]) -> None:
        if self.connection is None:
            return
        data = (json.dumps(response, separators=(",", ":")) + "\n").encode()
        self.connection.setblocking(True)
        try:
            self.connection.sendall(data)
        except OSError:
            self.drop_connection()
            return
        if self.connected():
            self.connection.setblocking(False)

    def connected(self) -> bool:
        # reply() and drop_connection() can close the connection during a call.
        return self.connection is not None

    def handle_line(self, line: bytes) -> dict[str, Any]:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise RootHostError("request must be a JSON object")
            return self.handle(cast(dict[str, Any], request))
        except (RootHostError, OSError, ValueError, KeyError, TypeError) as error:
            return {"ok": False, "error": str(error)}

    # Requests

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = request.get("op")
        if operation == "hello":
            return self.hello(request)
        if not self.greeted:
            raise RootHostError("the first request must be hello")
        if operation == "list":
            return self.list()
        if operation == "start":
            return self.start(request)
        identifier = request.get("id")
        if not isinstance(identifier, str) or identifier not in self.children:
            raise RootHostError(f"unknown root plugin id: {identifier!r}")
        child = self.children[identifier]
        if operation == "release":
            if child.gate_fd is None or child.wait_status is not None:
                raise RootHostError(f"root plugin {identifier} cannot be released")
            os.write(child.gate_fd, b"\x01")
            os.close(child.gate_fd)
            child.gate_fd = None
            return {"ok": True, "op": "released", "id": identifier}
        if operation == "status":
            launcher._poll_children({identifier: child})  # pyright: ignore[reportPrivateUsage]
            if child.wait_status is None:
                return {"ok": True, "op": "status", "id": identifier, "running": True}
            return {"ok": True, "op": "status", "id": identifier, "running": False, "wait_status": child.wait_status}
        if operation == "stop":
            self.stop({identifier: child})
            if child.wait_status is None:
                raise RootHostError(f"root plugin {identifier} did not exit")
            return {"ok": True, "op": "stopped", "id": identifier, "wait_status": child.wait_status}
        if operation == "forget":
            launcher._poll_children({identifier: child})  # pyright: ignore[reportPrivateUsage]
            if child.wait_status is None:
                raise RootHostError(f"root plugin {identifier} is still running")
            self.forget(identifier)
            return {"ok": True, "op": "forgotten", "id": identifier}
        raise RootHostError(f"unknown request: {operation!r}")

    def hello(self, request: dict[str, Any]) -> dict[str, Any]:
        token, host, port = request.get("auth_token"), request.get("server_host"), request.get("server_port")
        if not isinstance(token, str) or not isinstance(host, str) or type(port) is not int:
            raise RootHostError("hello needs auth_token, server_host and server_port")
        if self.greeted:
            raise RootHostError("hello was already sent")
        os.environ.update({"DECKY_AUTH_TOKEN": token, "SERVER_HOST": host, "SERVER_PORT": str(port)})
        self.greeted = True
        try:
            from decky_loader.helpers import get_loader_version

            version = get_loader_version()
        except Exception:
            version = None
        return {"ok": True, "op": "hello", "protocol": PROTOCOL, "version": version, "pid": os.getpid()}

    def list(self) -> dict[str, Any]:
        running = {directory for directory, _ in self.directories.values()}
        plugins = [
            {
                "directory": directory,
                "name": cast(dict[str, Any], entry).get("name"),
                "version": cast(dict[str, Any], entry).get("version"),
                "running": directory in running,
            }
            for directory, entry in sorted(self.config.approved_directories().items())
            if isinstance(entry, dict)
        ]
        return {"ok": True, "op": "list", "plugins": plugins}

    def start(self, request: dict[str, Any]) -> dict[str, Any]:
        identifier = request.get("id")
        if not isinstance(identifier, str) or not identifier or "/" in identifier or len(identifier) > 64:
            raise RootHostError("id must be a short plain name")
        if identifier in self.children:
            raise RootHostError(f"root plugin id is already active: {identifier}")
        directory = _directory_name(request.get("directory"))
        if any(running == directory for running, _ in self.directories.values()):
            raise RootHostError(f"{directory} is already running")
        approved = self.config.approved(directory)
        plugin_root = self.config.homebrew / "plugins"
        directory_fd = self._open_plugin(plugin_root, directory)
        try:
            metadata = plugin_metadata(directory_fd)
            if "root" not in metadata["flags"]:
                raise RootHostError(f"{directory} is not marked as a root plugin")
            if metadata["name"] != approved.get("name"):
                raise RootHostError(f"{directory} is not the plugin that was approved")
            if plugin_tree_sha256(directory_fd) != approved.get("tree_sha256"):
                raise RootHostError(f"{directory} changed after it was approved; approve it again")
            _ensure_root_directory(self.config.mount_root, 0o755)
            target = self.config.mount_root / directory
            _ensure_root_directory(target, 0o755)
            _ensure_root_directory(self.config.socket_directory, 0o711)
            socket_path = self.config.socket_directory / f"{identifier}.sock"
            if socket_path.exists() or socket_path.is_symlink():
                socket_path.unlink()
            info = os.fstat(directory_fd)
            arguments: dict[str, Any] = {
                "plugin_root": str(self.config.mount_root),
                "plugin_directory": directory,
                "socket": str(socket_path),
                "homebrew": str(self.config.homebrew),
                "user": self.config.user,
                "bootstrap": "lean",
                "approved_launch": {
                    "descriptor": {"pid": os.getpid(), "fd": directory_fd, "device": info.st_dev, "inode": info.st_ino},
                    "metadata": metadata,
                },
                "system_pythonpaths": launcher._shared_system_pythonpaths(self.config.user),  # pyright: ignore[reportPrivateUsage]
                "socket_owner": [self.config.uid, self.config.gid],
                "private_plugin_mount": {
                    "source_fd": directory_fd,
                    "source_path": str(plugin_root / directory),
                    "target": str(target),
                },
            }
            inherited = [self.listener.fileno(), self.wake_read, self.wake_write]
            if self.connection is not None:
                inherited.append(self.connection.fileno())
            inherited += [child.gate_fd for child in self.children.values() if child.gate_fd is not None]
            inherited += [fd for _, fd in self.directories.values()]
            pid, gate = launcher.fork_worker(arguments, tuple(inherited))
        except BaseException:
            os.close(directory_fd)
            raise
        self.children[identifier] = launcher._Child(identifier, pid, gate)  # pyright: ignore[reportPrivateUsage]
        self.directories[identifier] = (directory, directory_fd)
        return {"ok": True, "op": "forked", "id": identifier, "pid": pid, "socket": str(socket_path)}

    @staticmethod
    def _open_plugin(plugin_root: Path, directory: str) -> int:
        root_fd = os.open(plugin_root, _DIRECTORY_FLAGS)
        try:
            return os.open(directory, _DIRECTORY_FLAGS, dir_fd=root_fd)
        finally:
            os.close(root_fd)

    def stop(self, children: dict[str, launcher._Child]) -> None:  # pyright: ignore[reportPrivateUsage]
        running = {key: child for key, child in children.items() if child.wait_status is None}
        for child in running.values():
            launcher._signal_group(child.pid, signal.SIGTERM)  # pyright: ignore[reportPrivateUsage]
        for sig in (None, signal.SIGKILL):
            if sig is not None:
                for child in running.values():
                    if child.wait_status is None:
                        launcher._signal_group(child.pid, sig)  # pyright: ignore[reportPrivateUsage]
            deadline = time.monotonic() + _STOP_TIMEOUT
            while time.monotonic() < deadline:
                launcher._poll_children(running)  # pyright: ignore[reportPrivateUsage]
                if all(child.wait_status is not None for child in running.values()):
                    return
                time.sleep(0.05)

    def forget(self, identifier: str) -> None:
        child = self.children.pop(identifier)
        if child.gate_fd is not None:
            os.close(child.gate_fd)
        _, directory_fd = self.directories.pop(identifier)
        os.close(directory_fd)
        socket_path = self.config.socket_directory / f"{identifier}.sock"
        if socket_path.is_socket():
            socket_path.unlink(missing_ok=True)

    def stop_all(self) -> None:
        self.stop(dict(self.children))
        for identifier in list(self.children):
            self.forget(identifier)

    # Main loop

    def request_stop(self, _signum: int, _frame: Any) -> None:
        self.stopping = True

    def request_poll(self, _signum: int, _frame: Any) -> None:
        self.children_changed = True

    def run(self) -> int:
        signal.set_wakeup_fd(self.wake_write)
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGCHLD, self.request_poll)
        idle_since = time.monotonic()
        try:
            while not self.stopping:
                if self.children_changed:
                    self.children_changed = False
                    launcher._poll_children(self.children)  # pyright: ignore[reportPrivateUsage]
                busy = self.connection is not None or bool(self.children)
                if busy:
                    idle_since = time.monotonic()
                    timeout = None
                else:
                    timeout = idle_since + IDLE_EXIT_SECONDS - time.monotonic()
                    if timeout <= 0:
                        break
                watched: list[Any] = [self.listener, self.wake_read]
                if self.connection is not None:
                    watched.append(self.connection)
                readable, _, _ = select.select(watched, [], [], timeout)
                if self.wake_read in readable:
                    while True:
                        try:
                            if not os.read(self.wake_read, 4096):
                                break
                        except BlockingIOError:
                            break
                if self.stopping:
                    break
                if self.listener in readable:
                    self.accept()
                if self.connection is not None and self.connection in readable:
                    self.read_connection()
            return 0
        finally:
            signal.set_wakeup_fd(-1)
            self.stop_all()


def _activation_socket() -> socket.socket:
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise RootHostError("the root plugin host must be started by its systemd socket unit")
    for name in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        os.environ.pop(name, None)
    listener = socket.socket(fileno=3)
    if listener.family != socket.AF_UNIX:
        raise RootHostError("the activation socket is not a Unix socket")
    os.set_inheritable(listener.fileno(), False)
    listener.setblocking(False)
    return listener


def _tree_hash_command(directory: str) -> int:
    directory_fd = os.open(directory, _DIRECTORY_FLAGS)
    try:
        metadata = plugin_metadata(directory_fd)
        if "root" not in metadata["flags"]:
            raise RootHostError(f"{directory} is not marked as a root plugin")
        print(json.dumps({
            "tree_sha256": plugin_tree_sha256(directory_fd),
            "name": metadata["name"],
            "version": metadata["version"],
        }))
    finally:
        os.close(directory_fd)
    return 0


def admin_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Root plugin host administration")
    commands = parser.add_subparsers(dest="command", required=True)
    tree_hash = commands.add_parser("tree-hash", help="check and hash a root-owned plugin folder")
    tree_hash.add_argument("--directory", required=True)
    args = parser.parse_args(argv)
    try:
        return _tree_hash_command(args.directory)
    except (RootHostError, OSError, ValueError) as error:
        print(f"DeckyRootHost: {error}", file=sys.stderr)
        return 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the Decky root plugin host")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise RootHostError("the root plugin host must run as root")
        config = Config(Path(args.config))
        listener = _activation_socket()
        os.environ.update({
            "PLUGIN_PATH": str(config.mount_root),
            "UNPRIVILEGED_PATH": str(config.homebrew),
            "PRIVILEGED_PATH": str(config.state_directory),
            "UNPRIVILEGED_USER": config.user,
            "CHOWN_PLUGIN_PATH": "0",
        })
        worker.prepare_runtime("lean")
        launcher._assert_fork_safe()  # pyright: ignore[reportPrivateUsage]
        return Host(config, listener).run()
    except (RootHostError, OSError, ValueError, KeyError) as error:
        print(f"DeckyRootHost: {error}", file=sys.stderr, flush=True)
        return 1
