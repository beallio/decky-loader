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
import io
import json
import math
import os
import posixpath
import pwd
import re
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, cast

from decky_loader.plugin import launcher, worker

PROTOCOL = 1
RUN_ROOT = Path("/run/decky-loader-runtime")
IDLE_EXIT_SECONDS = 60.0
_STOP_TIMEOUT = 5.0
_MAX_JSON_BYTES = 1 << 20
_MAX_REQUEST_BYTES = 1 << 16
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_MAX_ARCHIVE_BYTES = 512 << 20
SUDO = "/usr/bin/sudo"
TRUE = "/usr/bin/true"
# Wait after a wrong password, so a process of the user cannot guess quickly.
_PASSWORD_RETRY_SECONDS = 5.0
RELEASE_TAG = re.compile(r"v\d+\.\d+\.\d+-rust\.\d+(-rc\.\d+)?")
SHA256 = re.compile(r"[0-9a-f]{64}")


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


def _release_key(tag: Any) -> tuple[int, int, int, int, float] | None:
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)-rust\.(\d+)(?:-rc\.(\d+))?", tag) if isinstance(tag, str) else None
    if match is None:
        return None
    major, minor, patch, rust, rc = match.groups()
    # A final release sorts after all of its release candidates.
    return int(major), int(minor), int(patch), int(rust), float(rc) if rc is not None else float("inf")


def _newer_release(candidate: str, current: Any) -> bool:
    """True when `candidate` is a newer release than `current`.

    The root runtime only moves forward, so a process that pretends to be an
    older loader cannot make root install an older release. A `dev` runtime
    (not a release) is always replaced.
    """
    new, old = _release_key(candidate), _release_key(current)
    return new is not None and (old is None or new > old)


def _only_root_can_change(path: Path) -> bool:
    """True when the real path and every folder above it belong to root and are not group or other writable."""
    try:
        resolved = Path(os.path.realpath(path))
        for part in (resolved, *resolved.parents):
            info = os.lstat(part)
            if info.st_uid != 0 or info.st_mode & 0o022:
                return False
    except OSError:
        return False
    return True


def _root_system_pythonpaths() -> list[str]:
    """System Python module folders for root plugins.

    The user service asks python3 as the user, which includes the user's own
    module folders. Root must not import from folders that the user can change,
    so this asks an isolated python3 (no user site, no environment) and keeps
    only folders that only root can change.
    """
    try:
        result = subprocess.run(
            ["/usr/bin/python3", "-I", "-c", "import sys; print('\\n'.join(p for p in sys.path if p))"],
            capture_output=True, text=True, env={}, cwd="/", timeout=30, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [path for path in result.stdout.splitlines() if path and _only_root_can_change(Path(path))]


def _write_root_file(path: Path, value: Any) -> None:
    """Replace a root-owned JSON file in a folder that only root can change."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _download(url: str, limit: int) -> bytes:
    # Imported here: only installs download, and certifi is optional outside the frozen runtime.
    import ssl

    try:
        import certifi

        where: Callable[[], str] = getattr(certifi, "where")
        context = ssl.create_default_context(cafile=where())
    except ImportError:
        context = ssl.create_default_context()
    request = urllib.request.Request(url, headers={"User-Agent": "Decky root plugin host"})
    with urllib.request.urlopen(request, timeout=120, context=context) as response:
        data = cast(bytes, response.read(limit + 1))
    if len(data) > limit:
        raise RootHostError(f"download is larger than {limit} bytes: {url}")
    return data


def _check_password(config: Config, password: object) -> None:
    """Raise unless `password` is the user's sudo password, as sudo itself checks it.

    sudo runs as the user, with the user's groups, so its policy, PAM stack and
    lockout apply unchanged. `-k` ignores cached sudo credentials and does not
    update them, so a check never leaves the user with passwordless sudo.
    """
    if not isinstance(password, str) or not password or "\n" in password or "\x00" in password:
        raise RootHostError("installing a plugin that needs root needs your sudo password")

    def sudo(arguments: list[str], password_input: bytes | None) -> int:
        try:
            return subprocess.run(
                [SUDO, *arguments, "--", TRUE],
                input=password_input,
                stdin=subprocess.DEVNULL if password_input is None else None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                user=config.uid,
                group=config.gid,
                extra_groups=os.getgrouplist(config.user, config.gid),
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                cwd="/",
                timeout=60,
            ).returncode
        except subprocess.TimeoutExpired as error:
            raise RootHostError("sudo did not answer the password check") from error

    if sudo(["-n", "-k"], None) == 0:
        raise RootHostError(
            f"{config.user} can use sudo without a password, so a password cannot protect plugins that need root; "
            "remove that NOPASSWD sudo rule first"
        )
    if sudo(["-S", "-k", "-p", ""], password.encode() + b"\n") != 0:
        raise RootHostError("wrong sudo password")


def _write_new_file(directory_fd: int, name: str, data: bytes) -> None:
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o755, dir_fd=directory_fd)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        # Same mode as the user installer gives plugin files.
        os.fchmod(handle.fileno(), 0o755)


def _open_subdirectory(root_fd: int, parts: tuple[str, ...]) -> int:
    """Open (and create) a folder below root_fd, one level at a time, without links."""
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o755, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _extract_plugin_archive(data: bytes, directory: str, stage_fd: int) -> None:
    """Unpack the store ZIP's `directory/` member tree into stage_fd.

    Follows the user installer's rules: one top folder, no unsafe paths, no
    links, no duplicates.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        seen: set[str] = set()
        for info in archive.infolist():
            name = info.filename
            trimmed = name[:-1] if name.endswith("/") else name
            parts = tuple(trimmed.split("/"))
            if (not trimmed or name.startswith("/") or "\\" in name or "\0" in name
                    or any(part in ("", ".", "..") for part in parts) or ":" in parts[0]):
                raise RootHostError(f"unsafe path in plugin archive: {name}")
            if parts[0] != directory:
                raise RootHostError(f"plugin archive entry is outside {directory}: {name}")
            relative = parts[1:]
            if not relative:
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                raise RootHostError(f"plugin archive contains a link: {name}")
            if trimmed in seen:
                raise RootHostError(f"plugin archive contains a duplicate entry: {name}")
            seen.add(trimmed)
            if info.is_dir():
                os.close(_open_subdirectory(stage_fd, relative))
                continue
            parent = _open_subdirectory(stage_fd, relative[:-1])
            try:
                _write_new_file(parent, relative[-1], archive.read(info))
            finally:
                os.close(parent)


def _download_remote_binaries(stage_fd: int) -> None:
    """Fetch `remote_binary` files that package.json lists, checked by their SHA-256."""
    package = _read_json_at(stage_fd, "package.json")
    binaries = cast(dict[str, Any], package).get("remote_binary") if isinstance(package, dict) else None
    if not binaries:
        return
    if not isinstance(binaries, list):
        raise RootHostError("package.json remote_binary must be a list")
    bin_fd = _open_subdirectory(stage_fd, ("bin",))
    try:
        for item in cast(list[Any], binaries):
            entry = cast(dict[str, Any], item) if isinstance(item, dict) else {}
            name, url, expected = entry.get("name"), entry.get("url"), entry.get("sha256hash")
            if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
                raise RootHostError(f"invalid remote binary name: {name!r}")
            if not isinstance(url, str) or urllib.parse.urlsplit(url).scheme != "https":
                raise RootHostError(f"remote binary {name} needs an https URL")
            if not isinstance(expected, str) or not SHA256.fullmatch(expected):
                raise RootHostError(f"remote binary {name} needs a SHA-256")
            data = _download(url, _MAX_ARCHIVE_BYTES)
            if hashlib.sha256(data).hexdigest() != expected:
                raise RootHostError(f"remote binary {name} does not match its SHA-256")
            _write_new_file(bin_fd, name, data)
    finally:
        os.close(bin_fd)


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
        self.path = path
        self.state_directory = path.parent
        self.mount_root = self.state_directory / "mnt"
        self.socket_directory = RUN_ROOT / str(uid) / "root-plugins"

    def current(self) -> dict[str, Any]:
        """The config as it is on disk now; the installer changes it during updates."""
        data = _read_root_file(self.path)
        return cast(dict[str, Any], data) if isinstance(data, dict) else {}

    def record(self, directory: str, entry: dict[str, Any] | None) -> None:
        """Add, replace or (with None) remove one approved plugin in state.json."""
        plugins = dict(self.approved_directories())
        if entry is None:
            plugins.pop(directory, None)
        else:
            plugins[directory] = entry
        _write_root_file(self.state_directory / "state.json", {"plugins": plugins})

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
        self.children: dict[str, launcher._Child] = {}
        self.directories: dict[str, tuple[str, int]] = {}
        self.stopping = False
        self.children_changed = False
        self.wake_read, self.wake_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.update: subprocess.Popen[bytes] | None = None
        self.update_version = ""
        self.failed_updates: set[str] = set()
        self.root_pythonpaths: list[str] | None = None
        self.password_retry_at = 0.0

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
            return self.list_plugins()
        if operation == "start":
            return self.start(request)
        if operation == "install":
            return self.install(request)
        if operation == "uninstall":
            return self.uninstall(request)
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
            launcher._poll_children({identifier: child})
            if child.wait_status is None:
                return {"ok": True, "op": "status", "id": identifier, "running": True}
            return {"ok": True, "op": "status", "id": identifier, "running": False, "wait_status": child.wait_status}
        if operation == "stop":
            self.stop({identifier: child})
            if child.wait_status is None:
                raise RootHostError(f"root plugin {identifier} did not exit")
            return {"ok": True, "op": "stopped", "id": identifier, "wait_status": child.wait_status}
        if operation == "forget":
            launcher._poll_children({identifier: child})
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
        from decky_loader import helpers

        os.environ.update({"DECKY_AUTH_TOKEN": token, "SERVER_HOST": host, "SERVER_PORT": str(port)})
        helpers.csrf_token = token
        self.greeted = True
        updating = self.update_runtime(request.get("loader_version"))
        try:
            from decky_loader.helpers import get_loader_version

            version = get_loader_version()
        except Exception:
            version = None
        return {"ok": True, "op": "hello", "protocol": PROTOCOL, "version": version, "pid": os.getpid(), "updating": updating}

    # Runtime updates

    def update_runtime(self, loader_version: Any) -> bool:
        """Install the root runtime that matches the loader, if they differ.

        The installer downloads the release from GitHub and checks it against
        its checksum file. It changes only files on disk; this process keeps
        running and switches at its next start (see `should_restart`).
        """
        if self.update is not None:
            return True
        if not isinstance(loader_version, str) or not RELEASE_TAG.fullmatch(loader_version):
            return False
        current = self.config.current().get("release_version")
        if loader_version in self.failed_updates or not _newer_release(loader_version, current):
            return False
        installer = Path(sys.executable).parent / "install.py"
        if not getattr(sys, "frozen", False) or not installer.is_file():
            return False
        self.update_version = loader_version
        self.update = subprocess.Popen(
            ["/usr/bin/python3", "-B", str(installer), "update-root-host", "--user", self.config.user,
             "--homebrew", str(self.config.homebrew), "--tag", loader_version],
            # A minimal environment: nothing from this process reaches the root installer.
            stdin=subprocess.DEVNULL, cwd="/", close_fds=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
        print(f"DeckyRootHost: installing the root runtime for {loader_version}", file=sys.stderr, flush=True)
        return True

    def poll_update(self) -> None:
        if self.update is None or self.update.poll() is None:
            return
        if self.update.returncode:
            self.failed_updates.add(self.update_version)
            print(f"DeckyRootHost: root runtime update to {self.update_version} failed", file=sys.stderr, flush=True)
        self.update = None

    def system_pythonpaths(self) -> list[str]:
        if self.root_pythonpaths is None:
            self.root_pythonpaths = _root_system_pythonpaths()
        return self.root_pythonpaths

    def should_restart(self) -> bool:
        """True when an updated runtime is installed and nothing would be interrupted."""
        if self.connection is not None or self.children or self.update is not None:
            return False
        if not getattr(sys, "frozen", False):
            return False
        runtime = self.config.current().get("runtime")
        return isinstance(runtime, str) and runtime != Path(sys.executable).parent.name

    # Plugin files

    def _plugin_root_fd(self) -> int:
        """Open <homebrew>/plugins without following links; it must belong to the user.

        This stops a redirected path from making root write into a root-owned
        `plugins` folder elsewhere on the system.
        """
        descriptor = os.open("/", _DIRECTORY_FLAGS)
        try:
            for part in (*self.config.homebrew.parts[1:], "plugins"):
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            if os.fstat(descriptor).st_uid != self.config.uid:
                raise RootHostError("the plugins folder must belong to the Decky user")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def install(self, request: dict[str, Any]) -> dict[str, Any]:
        directory = _directory_name(request.get("directory"))
        name, sha256, artifact = request.get("name"), request.get("sha256"), request.get("artifact")
        if not isinstance(name, str) or not name:
            raise RootHostError("install needs the plugin name")
        if not isinstance(sha256, str) or not SHA256.fullmatch(sha256):
            raise RootHostError("install needs the plugin's SHA-256")
        if not isinstance(artifact, str) or urllib.parse.urlsplit(artifact).scheme != "https":
            raise RootHostError("install needs an https:// download URL")
        if any(running == directory for running, _ in self.directories.values()):
            raise RootHostError(f"{directory} is running; stop it first")
        # The password is the only approval: any download with its SHA-256 may be installed.
        wait = self.password_retry_at - time.monotonic()
        if wait > 0:
            raise RootHostError(f"wait {math.ceil(wait)} seconds after a wrong sudo password")
        try:
            _check_password(self.config, request.get("password"))
        except RootHostError:
            self.password_retry_at = time.monotonic() + _PASSWORD_RETRY_SECONDS
            raise
        archive = _download(artifact, _MAX_ARCHIVE_BYTES)
        if hashlib.sha256(archive).hexdigest() != sha256:
            raise RootHostError("the downloaded plugin does not match its SHA-256")

        plugins_fd = self._plugin_root_fd()
        stage = f".decky-root-stage-{uuid.uuid4().hex}"
        try:
            try:
                os.mkdir(stage, 0o700, dir_fd=plugins_fd)
                stage_fd = os.open(stage, _DIRECTORY_FLAGS, dir_fd=plugins_fd)
                try:
                    info = os.fstat(stage_fd)
                    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
                        raise RootHostError("the staging folder was replaced")
                    _extract_plugin_archive(archive, directory, stage_fd)
                    metadata = plugin_metadata(stage_fd)
                    if metadata["name"] != name:
                        raise RootHostError(f"the archive contains {metadata['name']}, not {name}")
                    if "root" not in metadata["flags"]:
                        raise RootHostError(f"{name} does not need root; install it as a normal plugin")
                    _download_remote_binaries(stage_fd)
                    os.fchmod(stage_fd, 0o755)
                    tree = plugin_tree_sha256(stage_fd)
                finally:
                    os.close(stage_fd)
                backup = self._replace(plugins_fd, stage, directory)
            except BaseException:
                shutil.rmtree(stage, dir_fd=plugins_fd, ignore_errors=True)
                raise
            entry = {"name": name, "version": metadata["version"], "tree_sha256": tree}
            try:
                self.config.record(directory, entry)
            except BaseException:
                shutil.rmtree(directory, dir_fd=plugins_fd)
                if backup:
                    os.rename(backup, directory, src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd)
                raise
            if backup:
                shutil.rmtree(backup, dir_fd=plugins_fd, ignore_errors=True)
        finally:
            os.close(plugins_fd)
        return {"ok": True, "op": "installed", "directory": directory, **entry}

    @staticmethod
    def _replace(plugins_fd: int, stage: str, directory: str) -> str:
        """Install a staged directory and return its backup for later commit."""
        backup = ""
        try:
            os.stat(directory, dir_fd=plugins_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            backup = f".decky-root-backup-{uuid.uuid4().hex}"
            os.rename(directory, backup, src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd)
        try:
            os.rename(stage, directory, src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd)
        except BaseException:
            if backup:
                os.rename(backup, directory, src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd)
            raise
        return backup

    def uninstall(self, request: dict[str, Any]) -> dict[str, Any]:
        directory = _directory_name(request.get("directory"))
        if any(running == directory for running, _ in self.directories.values()):
            raise RootHostError(f"{directory} is running; stop it first")
        plugins_fd = self._plugin_root_fd()
        try:
            try:
                info = os.stat(directory, dir_fd=plugins_fd, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None:
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
                    raise RootHostError(f"{directory} is not a root plugin folder")
                backup = f".decky-root-backup-{uuid.uuid4().hex}"
                os.rename(directory, backup, src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd)
                shutil.rmtree(backup, dir_fd=plugins_fd)
        finally:
            os.close(plugins_fd)
        self.config.record(directory, None)
        return {"ok": True, "op": "uninstalled", "directory": directory}

    def list_plugins(self) -> dict[str, Any]:
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
        root_fd = self._plugin_root_fd()
        try:
            directory_fd = os.open(directory, _DIRECTORY_FLAGS, dir_fd=root_fd)
        finally:
            os.close(root_fd)
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
                "approved_launch": {
                    "descriptor": {"pid": os.getpid(), "fd": directory_fd, "device": info.st_dev, "inode": info.st_ino},
                    "metadata": metadata,
                },
                "system_pythonpaths": self.system_pythonpaths(),
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
        self.children[identifier] = launcher._Child(identifier, pid, gate)
        self.directories[identifier] = (directory, directory_fd)
        return {"ok": True, "op": "forked", "id": identifier, "pid": pid, "socket": str(socket_path)}

    def stop(self, children: dict[str, launcher._Child]) -> None:
        running = {key: child for key, child in children.items() if child.wait_status is None}
        for child in running.values():
            launcher._signal_group(child.pid, signal.SIGTERM)
        for sig in (None, signal.SIGKILL):
            if sig is not None:
                for child in running.values():
                    if child.wait_status is None:
                        launcher._signal_group(child.pid, sig)
            deadline = time.monotonic() + _STOP_TIMEOUT
            while time.monotonic() < deadline:
                launcher._poll_children(running)
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
                    launcher._poll_children(self.children)
                self.poll_update()
                busy = self.connection is not None or bool(self.children) or self.update is not None
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
                readable, _, _ = select.select(watched, [], [], 5.0 if self.update is not None else timeout)
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
                    if self.should_restart():
                        # Leave the waiting connection to the next start, which
                        # systemd runs from the updated runtime.
                        print("DeckyRootHost: restarting into the updated root runtime", file=sys.stderr, flush=True)
                        break
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


def _tree_hash_command(directory: str | None, descriptor: int | None) -> int:
    directory_fd = os.dup(descriptor) if descriptor is not None else os.open(cast(str, directory), _DIRECTORY_FLAGS)
    try:
        metadata = plugin_metadata(directory_fd)
        if "root" not in metadata["flags"]:
            raise RootHostError(f"{directory or 'the plugin folder'} is not marked as a root plugin")
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
    source = tree_hash.add_mutually_exclusive_group(required=True)
    source.add_argument("--directory")
    source.add_argument("--fd", type=int, help="an open folder descriptor passed by the installer")
    args = parser.parse_args(argv)
    try:
        return _tree_hash_command(args.directory, args.fd)
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
        worker.prepare_runtime()
        launcher._assert_fork_safe()
        return Host(config, listener).run()
    except (RootHostError, OSError, ValueError, KeyError) as error:
        print(f"DeckyRootHost: {error}", file=sys.stderr, flush=True)
        return 1
