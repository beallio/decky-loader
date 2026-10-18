#!/usr/bin/env python3
"""Transactional private Rust-runtime installer and updater activation helper."""
from __future__ import annotations

import argparse
import errno
import base64
import ctypes
import fcntl
import hashlib
import ipaddress
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn
import pwd
import re
import shutil
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# Importing installer helpers must not add unlisted files to a checksummed bundle.
sys.dont_write_bytecode = True

from process_observer import Process, cgroup_processes, read_process


KIND = 'decky-loader-runtime'
# The upstream SDK tag that the bundle builder pins. Bundles built on another
# upstream tag are accepted when their own records agree (see _validate_bundle),
# so an installed loader can update to a release on a newer upstream base.
BASELINE_TAG = 'v3.2.10'
SDK_TAG = re.compile(r'v\d+\.\d+\.\d+(-pre\d+)?')
SERVICE_STOP_DIRECTORY = Path('/run/decky-loader-runtime-stops')
LEGACY_MIGRATION_ROOT = Path('/var/lib/decky-loader-runtime/legacy-migrations')
# On SteamOS /var is a small partition (about 230 MB) and each runtime is about
# 100 MB, so the root runtime lives on the large /home partition. /home belongs
# to root, so the user cannot change anything below this folder.
ROOT_HOST_STATE = Path('/home/.decky-loader-runtime/root-host')
ROOT_HOST_RUN = Path('/run/decky-loader-runtime')
ROOT_HOST_UNIT_DIRECTORY = Path('/etc/systemd/system')
ROOT_HOST_PROTOCOL = 1
ROOT_HOST_STORE_HOSTS = ('plugins.deckbrew.xyz', 'cdn.tzatzikiweeb.moe')
SYSTEMCTL_CANDIDATES = (Path('/usr/bin/systemctl'), Path('/bin/systemctl'))
DEFAULT_LISTEN = '127.0.0.1:1337'
DEFAULT_CEF_URL = 'http://127.0.0.1:8080'
DEFAULT_GITHUB_API = 'https://api.github.com'
DEFAULT_RELEASE_REPOSITORY = 'beallio/decky-loader'
DEFAULT_TESTING_DOWNLOAD_URL = 'https://nightly.link'
RUST_EXECUTABLE = 'decky-loader-runtime'
WORKER_EXECUTABLE = 'DeckyPluginWorker'
MAX_HTTP_HEADERS = 65536
MAX_WEBSOCKET_FRAME = 1024 * 1024
HEALTH_RPC_IDS = (-1, -2, -3)


class InstallerError(RuntimeError):
    pass


def _fail(message: str) -> NoReturn:
    raise InstallerError(message)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _write_json(path: Path, value: object, mode: int = 0o600) -> None:
    data = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode('utf-8')
    _write_atomic(path, data, mode)


def _write_json_exclusive(path: Path, value: object, mode: int = 0o600) -> bool:
    data = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode('utf-8')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags, mode)
    except FileExistsError:
        return False
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, 'wb') as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(path.parent)
        return True
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open('r', encoding='utf-8') as stream:
            value = json.load(stream, object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InstallerError(f'cannot read valid JSON from {path}: {error}') from error
    if not isinstance(value, dict):
        _fail(f'expected a JSON object in {path}')
    return value


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def _lstat_regular_or_absent(path: Path) -> os.stat_result | None:
    try:
        result = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(result.st_mode):
        _fail(f'refusing non-regular managed file: {path}')
    return result


def _ensure_directory(path: Path, mode: int, *, managed: bool = False) -> None:
    try:
        result = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=mode)
        os.chmod(path, mode)
        _fsync_directory(path.parent)
        result = path.lstat()
    if not stat.S_ISDIR(result.st_mode):
        _fail(f'refusing non-directory or symlink: {path}')
    if managed and (result.st_uid != os.geteuid() or result.st_mode & 0o022):
        _fail(f'managed installer directory must be owned by uid {os.geteuid()} and not group/world writable: {path}')


def _require_root() -> None:
    if os.name != 'posix' or os.geteuid() != 0:
        _fail('legacy system-service migration and restore require root on Linux')


def _current_user() -> pwd.struct_passwd:
    try:
        return pwd.getpwuid(os.geteuid())
    except KeyError as error:
        raise InstallerError(f'cannot resolve effective uid {os.geteuid()} to a Unix account') from error


def _require_user(user: pwd.struct_passwd) -> None:
    if os.name != 'posix' or os.geteuid() == 0:
        _fail('ordinary installation and service management must run as the non-root service account')
    if user.pw_uid == 0 or user.pw_uid != os.geteuid():
        _fail('ordinary installation and service management must run as the account named by --user')


def _user_runtime_directory(user: pwd.struct_passwd) -> Path:
    runtime = Path(f'/run/user/{user.pw_uid}')
    try:
        info = runtime.lstat()
    except OSError as error:
        raise InstallerError(f'user runtime directory is unavailable: {error}') from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != user.pw_uid or info.st_mode & 0o077:
        _fail(f'user runtime directory must be a private directory owned by {user.pw_name}: {runtime}')
    return runtime


def _manager_environment(user: pwd.struct_passwd) -> dict[str, str]:
    runtime = _user_runtime_directory(user)
    environment = os.environ.copy()
    environment.pop('XDG_CONFIG_HOME', None)
    environment.update({
        'HOME': user.pw_dir,
        'USER': user.pw_name,
        'LOGNAME': user.pw_name,
        'XDG_RUNTIME_DIR': str(runtime),
        'DBUS_SESSION_BUS_ADDRESS': f'unix:path={runtime / "bus"}',
    })
    return environment


def _busctl_path() -> Path:
    for candidate in (Path('/usr/bin/busctl'), Path('/bin/busctl')):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    _fail('busctl was not found; cannot resolve the running user manager unit path')


def _manager_unit_paths(user: pwd.struct_passwd) -> tuple[Path, ...]:
    paths = {'unit': Path('systemd-manager.service'), 'scope': 'user', 'user': user}
    command = [
        _busctl_path().as_posix(), '--user', '--json=short', 'get-property',
        'org.freedesktop.systemd1', '/org/freedesktop/systemd1',
        'org.freedesktop.systemd1.Manager', 'UnitPath',
    ]
    result = _run_manager_command(paths, command, timeout=15)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f'exit status {result.returncode}'
        _fail(f'cannot query {user.pw_name} user-manager UnitPath: {detail}')
    try:
        response = json.loads(result.stdout)
        unit_paths = response['data']
        if response['type'] != 'as' or not isinstance(unit_paths, list):
            raise ValueError('UnitPath is not a string array')
        raw_paths = tuple(Path(value) for value in unit_paths)
        if any(not value.is_absolute() or '..' in value.parts for value in raw_paths):
            raise ValueError('UnitPath contains a non-absolute or traversing path')
        parsed = tuple(value.resolve(strict=False) for value in raw_paths)
    except (KeyError, TypeError, ValueError, OSError, RuntimeError, json.JSONDecodeError) as error:
        _fail(f'user manager returned an invalid structured UnitPath: {error}')
    return parsed


def _validate_user_unit_directory(
    directory: Path,
    user: pwd.struct_passwd,
    *,
    create: bool,
) -> None:
    if (
        not directory.is_absolute() or '..' in directory.parts
        or directory.name != 'user' or directory.parent.name != 'systemd'
    ):
        _fail('user manager unit directory must be an absolute systemd/user search path')
    base = directory.parent.parent
    managed = {base, base / 'systemd', directory}
    chain: list[Path] = []
    current = Path(directory.anchor)
    chain.append(current)
    for component in directory.parts[1:]:
        current = current / component
        chain.append(current)
    base_seen = False
    for current in chain:
        is_managed = current in managed
        if current == base:
            base_seen = True
        try:
            info = current.lstat()
        except FileNotFoundError:
            if is_managed and create:
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                except OSError as error:
                    raise InstallerError(f'cannot create user unit directory {current}: {error}') from error
                else:
                    os.chmod(current, 0o700)
                    _fsync_directory(current.parent)
                try:
                    info = current.lstat()
                except OSError as error:
                    raise InstallerError(f'user unit directory is unavailable: {error}') from error
            else:
                if not is_managed and (create or base_seen):
                    _fail(f'user unit directory parent is unavailable: {current}')
                continue
        except OSError as error:
            raise InstallerError(f'user unit path is unavailable: {error}') from error
        if not stat.S_ISDIR(info.st_mode):
            _fail(f'refusing non-directory or symlink in user unit path: {current}')
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            _fail(f'user unit path has a group/world-writable parent: {current}')
        if is_managed and (
            info.st_uid != user.pw_uid or info.st_mode & 0o022
        ):
            _fail(f'user unit directory is not owned by {user.pw_name} or is group/world writable: {current}')
    try:
        base.lstat()
    except FileNotFoundError:
        parent = base.parent
        try:
            parent_info = parent.lstat()
        except OSError as error:
            raise InstallerError(f'user unit directory parent is unavailable: {error}') from error
        if (
            not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != user.pw_uid
            or parent_info.st_mode & 0o022
        ):
            _fail(f'cannot safely create user unit directory beneath {parent}')


def _user_unit_directory(
    user: pwd.struct_passwd,
    *,
    create: bool = True,
    value: str | None = None,
) -> Path:
    if value is not None:
        directory = Path(value)
        if not directory.is_absolute() or '..' in directory.parts:
            _fail('user unit directory must be absolute and must not contain parent traversal')
        try:
            directory = directory.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise InstallerError(f'cannot resolve user unit directory: {error}') from error
        unit_paths = _manager_unit_paths(user)
        if directory not in unit_paths:
            _fail(f'user unit directory is not in {user.pw_name} manager UnitPath: {directory}')
        _validate_user_unit_directory(directory, user, create=create)
        return directory
    unit_paths = _manager_unit_paths(user)
    global_roots = tuple(Path(value) for value in (
        '/etc/systemd/user', '/run/systemd/user', '/usr/lib/systemd/user',
        '/usr/local/lib/systemd/user', '/lib/systemd/user', '/usr/share/systemd/user',
        '/usr/local/share/systemd/user',
    ))
    for directory in unit_paths:
        if directory.name != 'user' or directory.parent.name != 'systemd':
            continue
        if any(directory == root or root in directory.parents for root in global_roots):
            continue
        _validate_user_unit_directory(directory, user, create=create)
        return directory
    _fail(f'{user.pw_name} manager UnitPath has no safe user-owned systemd/user directory')


def _normalize_service_name(value: str) -> tuple[str, str]:
    if not value or value.startswith('.') or '/' in value or '\\' in value or any(ord(c) < 33 or ord(c) == 127 for c in value):
        _fail('service name must be a plain systemd service identifier')
    if value.endswith('.service'):
        base = value[:-len('.service')]
    else:
        base = value
    if not base or not re.fullmatch(r'[A-Za-z0-9_.@-]+', base) or base in ('.', '..') or base.endswith('.'):
        _fail(f'invalid service name: {value!r}')
    return base, f'{base}.service'


def _homebrew_path(
    value: str,
    *,
    create: bool = False,
    allow_missing: bool = False,
) -> Path:
    path = Path(value)
    if not path.is_absolute():
        _fail('--homebrew must be an absolute path')
    try:
        resolved = path.resolve(strict=not create and not allow_missing)
    except (OSError, RuntimeError) as error:
        raise InstallerError(f'homebrew directory is unavailable: {error}') from error
    if create and not resolved.exists():
        resolved.mkdir(mode=0o755, parents=True)
    if not resolved.exists() and allow_missing:
        try:
            original = path.lstat()
        except FileNotFoundError:
            original = None
        except OSError as error:
            raise InstallerError(f'homebrew destination is unavailable: {error}') from error
        if original is not None and stat.S_ISLNK(original.st_mode):
            _fail(f'refusing dangling homebrew symlink: {path}')
        ancestor = resolved
        while True:
            try:
                info = ancestor.lstat()
                break
            except FileNotFoundError:
                if ancestor.parent == ancestor:
                    _fail(f'homebrew destination has no existing parent: {resolved}')
                ancestor = ancestor.parent
            except OSError as error:
                raise InstallerError(f'homebrew destination parent is unavailable: {error}') from error
        if not stat.S_ISDIR(info.st_mode):
            _fail(f'homebrew destination parent is not a directory: {ancestor}')
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            _fail(f'homebrew destination parent is not safely owned by uid {os.geteuid()}: {ancestor}')
    elif not resolved.is_dir():
        _fail(f'homebrew path is not a directory: {resolved}')
    if any(character.isspace() or character in ('%', '$', '\\', '"') or ord(character) < 32 for character in str(resolved)):
        _fail('homebrew path contains characters unsafe for systemd unit expansion')
    return resolved


def _load_persisted_unit_directory(state: Path, user: pwd.struct_passwd) -> Path | None:
    record_path = state / 'unit-directory.json'
    info = _lstat_regular_or_absent(record_path)
    if info is None:
        return None
    if (
        info.st_uid != user.pw_uid or info.st_mode & 0o077 or info.st_nlink != 1
    ):
        _fail(f'persisted unit directory record is not protected for {user.pw_name}: {record_path}')
    record = _read_json(record_path)
    if (
        type(record.get('schema')) is not int or record['schema'] != 1
        or type(record.get('user_uid')) is not int or record['user_uid'] != user.pw_uid
        or not isinstance(record.get('unit_directory'), str)
    ):
        _fail(f'persisted unit directory record is invalid: {record_path}')
    return _user_unit_directory(user, create=True, value=record['unit_directory'])


def _persisted_user_unit_directory(state: Path, user: pwd.struct_passwd) -> Path:
    existing = _load_persisted_unit_directory(state, user)
    if existing is not None:
        return existing
    directory = _user_unit_directory(user)
    record = {
        'schema': 1,
        'user_uid': user.pw_uid,
        'unit_directory': str(directory),
    }
    if _write_json_exclusive(state / 'unit-directory.json', record, mode=0o600):
        return directory
    existing = _load_persisted_unit_directory(state, user)
    if existing is None:
        _fail(f'persisted unit directory record disappeared during initialization: {state}')
    return existing


def _paths(homebrew: Path, service_name: str, user: pwd.struct_passwd | None = None) -> dict[str, Any]:
    _, unit_filename = _normalize_service_name(service_name)
    user = user or _current_user()
    _require_user(user)
    services = homebrew / 'services'
    _ensure_directory(services, 0o755)
    service_root = services / '.decky-user-installer'
    _ensure_directory(service_root, 0o700, managed=True)
    state = service_root / unit_filename
    _ensure_directory(state, 0o700, managed=True)
    runtimes = state / 'runtimes'
    _ensure_directory(runtimes, 0o755, managed=True)
    history = state / 'history'
    _ensure_directory(history, 0o700, managed=True)
    os.chmod(history, 0o700)
    queue = state / 'queue'
    _ensure_directory(queue, 0o700, managed=True)
    os.chmod(queue, 0o700)
    unit_dir = _persisted_user_unit_directory(state, user)
    return {
        'homebrew': homebrew,
        'services': services,
        'binary': services / 'PluginLoader',
        'unit': unit_dir / unit_filename,
        'unit_dir': unit_dir,
        'state': state,
        'runtimes': runtimes,
        'history': history,
        'queue': queue,
        'journal': state / 'journal.json',
        'head': state / 'head.json',
        'active': state / 'active.json',
        'lock': state / 'lock',
        'scope': 'user',
        'user': user,
    }

def _systemctl_path() -> Path:
    for candidate in SYSTEMCTL_CANDIDATES:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    _fail('systemctl was not found')


def _run_manager_command(paths: dict[str, Any], command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    if paths.get('scope', 'user') != 'user':
        try:
            return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise InstallerError(f'cannot run system manager command for {paths["unit"].name}: {error}') from error
    user = paths.get('user') or _current_user()
    environment = _manager_environment(user)
    options: dict[str, Any] = {}
    if os.geteuid() != user.pw_uid:
        if os.geteuid() != 0:
            _fail(f'cannot access the user manager for {user.pw_name} from uid {os.geteuid()}')
        try:
            groups = os.getgrouplist(user.pw_name, user.pw_gid)
        except AttributeError:
            groups = [user.pw_gid]

        def drop_privileges() -> None:
            os.setgroups(groups)
            os.setgid(user.pw_gid)
            os.setuid(user.pw_uid)

        options.update(cwd=user.pw_dir if Path(user.pw_dir).is_dir() else '/', preexec_fn=drop_privileges)
    try:
        return subprocess.run(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False, env=environment, **options,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallerError(f'cannot run user manager command for {paths["unit"].name}: {error}') from error


def _systemctl(paths: dict[str, Any], *arguments: str, check: bool = True, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    scope = paths.get('scope', 'user')
    if scope not in ('user', 'system'):
        _fail(f'unsupported systemd manager scope: {scope}')
    command = [_systemctl_path().as_posix(), f'--{scope}', *arguments]
    result = _run_manager_command(paths, command, timeout)
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f'exit status {result.returncode}'
        _fail(f'systemctl {arguments[0]} {paths["unit"].name} failed: {detail}')
    return result


def _system_unit_paths(service_name: str) -> dict[str, Any]:
    _, unit_filename = _normalize_service_name(service_name)
    return {'unit': Path(unit_filename), 'scope': 'system'}


def _unit_properties(paths: dict[str, Any]) -> dict[str, str]:
    result = _systemctl(
        paths,
        'show', paths['unit'].name,
        '--property=LoadState,ActiveState,SubState,MainPID,UnitFileState,ControlGroup,FragmentPath,DropInPaths',
        check=False,
        timeout=15,
    )
    properties: dict[str, str] = {
        key: value for line in result.stdout.splitlines() if '=' in line
        for key, value in [line.split('=', 1)]
    }
    if result.returncode and not properties:
        detail = result.stderr.strip() or result.stdout.strip()
        _fail(f'cannot inspect selected service {paths["unit"].name}: {detail}')
    return properties

def _legacy_migration_record(
    homebrew: Path,
    service_name: str,
    user_uid: int | None = None,
) -> Path:
    _, unit_filename = _normalize_service_name(service_name)
    uid = os.getuid() if user_uid is None else user_uid
    return LEGACY_MIGRATION_ROOT / str(uid) / unit_filename / 'status.json'


def _open_directory_at(
    parent: int,
    name: str,
    *,
    create: bool,
    mode: int,
    private: bool = False,
    exact_mode: bool = False,
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    created = False
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, mode, dir_fd=parent)
        except FileExistsError:
            pass
        else:
            created = True
            os.fsync(parent)
        descriptor = os.open(name, flags, dir_fd=parent)
    if created and stat.S_IMODE(os.fstat(descriptor).st_mode) != mode:
        os.fchmod(descriptor, mode)
    info = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or info.st_mode & 0o022
        or ((private or exact_mode) and stat.S_IMODE(info.st_mode) != mode)
    ):
        os.close(descriptor)
        _fail(f'legacy migration directory is not protected: {name}')
    return descriptor


def _open_legacy_store(
    user_uid: int,
    service_name: str,
    *,
    create: bool,
) -> tuple[Path, dict[str, int]]:
    _, unit_filename = _normalize_service_name(service_name)
    descriptors: dict[str, int] = {}
    try:
        root = os.open('/', os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        descriptors['root'] = root
        current = root
        relative = LEGACY_MIGRATION_ROOT.relative_to('/')
        for index, component in enumerate(relative.parts):
            child = _open_directory_at(
                current,
                component,
                create=create,
                mode=0o755,
                exact_mode=True,
            )
            descriptors[f'path-{index}'] = child
            current = child
        store_fd = current
        uid_fd = _open_directory_at(store_fd, str(user_uid), create=create, mode=0o755, exact_mode=True)
        descriptors['uid'] = uid_fd
        service_fd = _open_directory_at(uid_fd, unit_filename, create=create, mode=0o755, exact_mode=True)
        descriptors['service'] = service_fd
        private_fd = _open_directory_at(
            service_fd,
            'private',
            create=create,
            mode=0o700,
            private=True,
        )
        descriptors['private'] = private_fd
        helper_fd = _open_directory_at(
            service_fd,
            'helper',
            create=create,
            mode=0o755,
            exact_mode=True,
        )
        descriptors['helper'] = helper_fd
        return LEGACY_MIGRATION_ROOT / str(user_uid) / unit_filename, descriptors
    except BaseException:
        for descriptor in reversed(list(descriptors.values())):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _open_legacy_public_directory(
    user_uid: int,
    service_name: str,
) -> tuple[Path, dict[str, int]]:
    _, unit_filename = _normalize_service_name(service_name)
    descriptors: dict[str, int] = {}
    try:
        current = os.open('/', os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        descriptors['root'] = current
        for index, component in enumerate(LEGACY_MIGRATION_ROOT.relative_to('/').parts):
            current = _open_directory_at(
                current, component, create=False, mode=0o755, exact_mode=True
            )
            descriptors[f'path-{index}'] = current
        uid_fd = _open_directory_at(current, str(user_uid), create=False, mode=0o755, exact_mode=True)
        descriptors['uid'] = uid_fd
        service_fd = _open_directory_at(uid_fd, unit_filename, create=False, mode=0o755, exact_mode=True)
        descriptors['service'] = service_fd
        return LEGACY_MIGRATION_ROOT / str(user_uid) / unit_filename, descriptors
    except BaseException:
        for descriptor in reversed(list(descriptors.values())):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _close_legacy_descriptors(descriptors: dict[str, int]) -> None:
    for descriptor in reversed(list(descriptors.values())):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _read_json_at(directory_fd: int, name: str, *, mode: int, private: bool = False) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != mode
            or info.st_mode & 0o022
            or (private and info.st_mode & 0o077)
        ):
            _fail(f'legacy migration record is not protected: {name}')
        with os.fdopen(descriptor, 'r', encoding='utf-8', closefd=False) as stream:
            value = json.load(stream, object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            _fail(f'expected a JSON object in legacy migration record {name}')
        return value
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InstallerError(f'cannot read protected legacy migration record {name}: {error}') from error
    finally:
        os.close(descriptor)


def _atomic_write_at(directory_fd: int, name: str, data: bytes, mode: int) -> None:
    temporary = f'.{name}.{uuid.uuid4().hex}.tmp'
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(temporary, flags, mode, dir_fd=directory_fd)
    try:
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, 'wb', closefd=False) as stream:
            stream.write(data)
            stream.flush()
        os.fsync(descriptor)
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)


def _read_legacy_status(homebrew: Path, service_name: str, user_uid: int) -> dict[str, Any] | None:
    try:
        _, descriptors = _open_legacy_public_directory(user_uid, service_name)
    except FileNotFoundError:
        return None
    try:
        try:
            os.stat('status.json', dir_fd=descriptors['service'], follow_symlinks=False)
        except FileNotFoundError:
            return None
        status = _read_json_at(descriptors['service'], 'status.json', mode=0o644)
        _, unit_filename = _normalize_service_name(service_name)
        if (
            set(status) != {'schema', 'user_uid', 'service', 'homebrew', 'phase'}
            or status.get('schema') != 1
            or status.get('user_uid') != user_uid
            or status.get('service') != unit_filename
            or status.get('homebrew') != str(homebrew)
            or not isinstance(status.get('phase'), str)
        ):
            _fail('legacy migration public status is invalid')
        return status
    finally:
        _close_legacy_descriptors(descriptors)

def _assert_no_system_instance(paths: dict[str, Any]) -> dict[str, str]:
    system = _unit_properties(_system_unit_paths(paths['unit'].stem))
    load_state = system.get('LoadState', 'not-found')
    user = paths.get('user') or _current_user()
    status = _read_legacy_status(paths['homebrew'], paths['unit'].stem, user.pw_uid)
    if load_state == 'not-found':
        if status is None or status.get('phase') == 'restored':
            return system
        _fail('legacy migration state is incomplete; run restore-legacy-system before installing a user service')
    if load_state == 'masked' and (
        status is not None
        and status.get('phase') in ('services-user-owned', 'installed', 'forgetting', 'forgotten')
        and system.get('ActiveState') in ('inactive', 'failed')
        and system.get('MainPID', '0') == '0'
    ):
        return system
    _fail(
        f'system service {paths["unit"].name} is present; run the explicit '
        'migrate-legacy-system command before installing a user service'
    )


def _assert_user_unit_source(paths: dict[str, Any]) -> None:
    properties = _unit_properties(paths)
    if properties.get('LoadState') != 'loaded':
        return
    fragment = properties.get('FragmentPath', '')
    try:
        current = Path(fragment).resolve(strict=False)
        expected = paths['unit'].resolve(strict=False)
    except OSError as error:
        raise InstallerError(f'cannot resolve selected user-service unit: {error}') from error
    if current != expected:
        _fail(
            f'user service {paths["unit"].name} is defined outside the installer-owned unit path '
            f'({fragment}); refusing to replace it'
        )


def _enabled_state(paths: dict[str, Path], load_state: str) -> tuple[bool, str]:
    if load_state == 'not-found':
        return False, 'none'
    result = _systemctl(paths, 'is-enabled', paths['unit'].name, check=False, timeout=15)
    value = (result.stdout or result.stderr).strip().splitlines()
    state = value[-1].strip() if value else ('not-found' if load_state == 'not-found' else 'unknown')
    if state in ('enabled', 'enabled-runtime'):
        return True, 'runtime' if state == 'enabled-runtime' else 'persistent'
    if state in ('disabled', 'static', 'indirect', 'generated', 'transient', 'not-found'):
        return False, 'none'
    if state == 'masked' or state == 'masked-runtime':
        _fail(f'refusing to replace masked service {paths["unit"].name}')
    _fail(f'unsupported enablement state for {paths["unit"].name}: {state}')


def _capture_activation(paths: dict[str, Path]) -> dict[str, object]:
    properties = _unit_properties(paths)
    load_state = properties.get('LoadState', 'not-found')
    if load_state == 'not-found':
        enabled, scope = _enabled_state(paths, load_state)
        return {'running': False, 'enabled': enabled, 'enabled_scope': scope, 'load_state': load_state}
    if load_state != 'loaded':
        _fail(f'selected service {paths["unit"].name} has unsupported LoadState={load_state}')
    active = properties.get('ActiveState', 'inactive')
    substate = properties.get('SubState', '')
    if active == 'active' and substate != 'running':
        _fail(f'selected service {paths["unit"].name} is active in unexpected state {substate!r}')
    try:
        main_pid = int(properties.get('MainPID', '0'))
    except ValueError:
        _fail(f'systemd returned an invalid MainPID for {paths["unit"].name}')
    if active == 'failed':
        if main_pid:
            _fail(f'failed service {paths["unit"].name} still has MainPID={main_pid}')
        running = False
    elif active in ('active', 'inactive'):
        running = active == 'active'
    else:
        _fail(f'selected service {paths["unit"].name} is in transitional state {active!r}')
    if active == 'inactive' and main_pid:
        _fail(f'inactive service {paths["unit"].name} still has MainPID={main_pid}')
    enabled, scope = _enabled_state(paths, load_state)
    return {'running': running, 'enabled': enabled, 'enabled_scope': scope, 'load_state': load_state}


def _fsync_file(path: Path) -> None:
    with path.open('rb') as stream:
        os.fsync(stream.fileno())


def _metadata(path: Path, result: os.stat_result) -> dict[str, object]:
    attributes: dict[str, str] = {}
    if hasattr(os, 'listxattr') and hasattr(os, 'getxattr'):
        try:
            for name in os.listxattr(path, follow_symlinks=False):
                attributes[name] = base64.b64encode(os.getxattr(path, name, follow_symlinks=False)).decode('ascii')
        except OSError as error:
            if error.errno not in (getattr(os, 'ENOTSUP', 95), getattr(os, 'EOPNOTSUPP', 95)):
                raise InstallerError(f'cannot capture metadata for {path}: {error}') from error
    return {
        'mode': stat.S_IMODE(result.st_mode),
        'uid': result.st_uid,
        'gid': result.st_gid,
        'atime_ns': result.st_atime_ns,
        'mtime_ns': result.st_mtime_ns,
        'xattrs': attributes,
    }


def _copy_regular(source: Path, destination: Path) -> None:
    source_stat = _lstat_regular_or_absent(source)
    if source_stat is None:
        _fail(f'required regular file is missing: {source}')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(source, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (source_stat.st_dev, source_stat.st_ino):
            _fail(f'file changed while being opened: {source}')
        with os.fdopen(descriptor, 'rb', closefd=False) as input_stream, destination.open('xb') as output_stream:
            shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    finally:
        os.close(descriptor)


def _snapshot_file(path: Path, destination: Path) -> dict[str, object]:
    current = _lstat_regular_or_absent(path)
    if current is None:
        return {'exists': False}
    saved_metadata = _metadata(path, current)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _copy_regular(path, destination)
    return {'exists': True, 'metadata': saved_metadata}


def _apply_metadata(path: Path, metadata: dict[str, Any]) -> None:
    try:
        os.chown(path, int(metadata['uid']), int(metadata['gid']))
        os.chmod(path, int(metadata['mode']))
        xattrs = metadata.get('xattrs', {})
        if not isinstance(xattrs, dict):
            _fail('invalid xattr snapshot')
        if xattrs and not hasattr(os, 'setxattr'):
            _fail('this platform cannot restore captured extended attributes')
        for name, encoded in xattrs.items():
            value = base64.b64decode(str(encoded), validate=True)
            os.setxattr(path, str(name), value, follow_symlinks=False)
        os.utime(path, ns=(int(metadata['atime_ns']), int(metadata['mtime_ns'])))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise InstallerError(f'cannot restore file metadata for {path}: {error}') from error


def _restore_file(path: Path, snapshot: Path, state: dict[str, Any]) -> None:
    if not state.get('exists'):
        current = _lstat_regular_or_absent(path)
        if current is not None:
            path.unlink()
            _fsync_directory(path.parent)
        return
    metadata = state.get('metadata')
    if not isinstance(metadata, dict):
        _fail(f'missing file metadata in transaction snapshot for {path}')
    temporary_descriptor, temporary_name = tempfile.mkstemp(prefix=f'.{path.name}.restore-', dir=path.parent)
    os.close(temporary_descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.unlink()
        _copy_regular(snapshot, temporary)
        _apply_metadata(temporary, metadata)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _validate_bundle(bundle_value: str, user: pwd.struct_passwd) -> tuple[Path, dict[str, Any]]:
    raw_bundle = Path(bundle_value)
    if raw_bundle.is_symlink():
        _fail('bundle directory must not be a symlink')
    bundle = raw_bundle.resolve(strict=True)
    if not bundle.is_dir():
        _fail(f'bundle must be a real directory: {bundle}')
    manifest_path = bundle / 'bundle.json'
    if not stat.S_ISREG(manifest_path.lstat().st_mode):
        _fail('bundle.json must be a regular file')
    manifest = _read_json(manifest_path)
    if manifest.get('kind') != KIND:
        _fail('bundle has an unexpected runtime kind')
    if manifest.get('rust_executable') != RUST_EXECUTABLE:
        _fail('bundle names an unexpected Rust executable')
    release_version = manifest.get('release_version')
    if not isinstance(release_version, str) or not release_version or any(ord(character) < 32 or ord(character) == 127 for character in release_version):
        _fail('bundle has no valid Rust release identity')
    source = manifest.get('source_baseline')
    if not isinstance(source, dict) or not isinstance(source.get('tag'), str) or not SDK_TAG.fullmatch(source['tag']):
        _fail('bundle has no valid upstream source baseline tag')
    for field in ('commit', 'backend_tree', 'frontend_tree'):
        value = source.get(field)
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}', value):
            _fail(f'bundle source baseline has invalid {field}')
    cargo_hash = manifest.get('cargo_lock_sha256')
    if not isinstance(cargo_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', cargo_hash):
        _fail('bundle has invalid Cargo.lock checksum metadata')
    runtime = manifest.get('runtime')
    if not isinstance(runtime, dict):
        _fail('bundle has no worker runtime description')
    if runtime.get('frozen') is not True or not isinstance(runtime.get('python'), str) or not runtime['python'].startswith('3.11.'):
        _fail('bundle requires a frozen CPython 3.11 worker')
    if runtime.get('decky_version') != source['tag']:
        _fail(f'worker does not report the bundle source baseline {source["tag"]}')
    packaging = manifest.get('worker_packaging')
    if packaging not in ('onefile', 'onedir'):
        _fail('bundle has an unsupported worker packaging variant')
    hashes = manifest.get('sha256')
    if not isinstance(hashes, dict) or not hashes:
        _fail('bundle manifest has no checksums')
    expected_paths: set[str] = set()
    for relative, expected in hashes.items():
        if not isinstance(relative, str) or not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            _fail('bundle contains an invalid checksum entry')
        if '\\' in relative or relative.startswith('/'):
            _fail(f'unsafe bundle member path: {relative!r}')
        relative_path = PurePosixPath(relative)
        if not relative_path.parts or any(part in ('', '.', '..') for part in relative_path.parts):
            _fail(f'unsafe bundle member path: {relative!r}')
        candidate = bundle.joinpath(*relative_path.parts)
        cursor = bundle
        item = None
        for part in relative_path.parts:
            cursor = cursor / part
            try:
                item = cursor.lstat()
            except OSError as error:
                raise InstallerError(f'missing bundle member {relative}: {error}') from error
            if stat.S_ISLNK(item.st_mode):
                _fail(f'bundle member uses a symlink: {relative}')
        if item is None or not stat.S_ISREG(item.st_mode):
            _fail(f'bundle member is not a regular file: {relative}')
        if _digest(candidate) != expected:
            _fail(f'bundle checksum mismatch: {relative}')
        expected_paths.add(relative)
    actual_paths: set[str] = set()
    for path in bundle.rglob('*'):
        relative = path.relative_to(bundle).as_posix()
        item = path.lstat()
        if stat.S_ISLNK(item.st_mode):
            _fail(f'bundle contains an untrusted symlink: {relative}')
        if stat.S_ISREG(item.st_mode):
            if relative != 'bundle.json':
                actual_paths.add(relative)
        elif not stat.S_ISDIR(item.st_mode):
            _fail(f'bundle contains an unsupported filesystem object: {relative}')
    if actual_paths != expected_paths:
        missing = sorted(actual_paths - expected_paths)
        extra = sorted(expected_paths - actual_paths)
        _fail(f'bundle checksum file set does not match (unlisted={missing}, missing={extra})')
    for required in (RUST_EXECUTABLE, WORKER_EXECUTABLE, 'install.py', 'process_observer.py', 'assets/index.js', 'locales/en-US.json'):
        if required not in expected_paths:
            _fail(f'bundle is missing required member {required}')
    runtime_tree = bundle / '_internal'
    if packaging == 'onedir' and (not runtime_tree.is_dir() or runtime_tree.is_symlink() or not any(runtime_tree.rglob('*'))):
        _fail('onedir worker bundle is missing its _internal runtime')
    if packaging == 'onefile' and runtime_tree.exists() and not runtime_tree.is_dir():
        _fail('_internal must be a real directory when present')
    rust = bundle / RUST_EXECUTABLE
    worker_path = bundle / WORKER_EXECUTABLE
    for path in (rust, worker_path):
        mode = stat.S_IMODE(path.stat().st_mode)
        if not mode & 0o111 or mode & 0o6000 or mode & 0o022:
            _fail(f'bundle executable has unsafe permissions: {path.name}')
    if not (bundle / 'assets').is_dir() or not (bundle / 'locales').is_dir():
        _fail('bundle assets and locales must be real directories')
    return bundle, manifest


def _source_user_access(path: Path, user: pwd.struct_passwd, groups: set[int], required: int) -> bool:
    info = path.stat()
    if user.pw_uid == 0:
        return True
    if info.st_uid == user.pw_uid:
        bits = (info.st_mode >> 6) & 7
    elif info.st_gid in groups:
        bits = (info.st_mode >> 3) & 7
    else:
        bits = info.st_mode & 7
    return bits & required == required


def _check_runtime_access(root: Path, user: pwd.struct_passwd, parents: tuple[Path, ...] = ()) -> None:
    try:
        groups = set(os.getgrouplist(user.pw_name, user.pw_gid))
    except (AttributeError, OSError):
        groups = {user.pw_gid}
    for path in (*parents, root, *sorted(root.rglob('*'))):
        required = 1 if path.is_dir() else 4
        if path.is_file() and stat.S_IMODE(path.stat().st_mode) & 0o111:
            required |= 1
        if not _source_user_access(path, user, groups, required):
            _fail(f'packaged runtime is inaccessible to service user {user.pw_name}: {path}')


def _run_as_user(command: list[str], user: pwd.struct_passwd, timeout: float = 20) -> subprocess.CompletedProcess[str]:
    if user.pw_uid == 0 or os.geteuid() not in (0, user.pw_uid):
        _fail('bundle validation must run as the non-root service account or root during migration')
    environment = {
        'HOME': user.pw_dir,
        'USER': user.pw_name,
        'LOGNAME': user.pw_name,
        'PATH': '/usr/bin:/bin',
        'LANG': 'C.UTF-8',
        'LC_ALL': 'C.UTF-8',
    }
    options: dict[str, Any] = {}
    if os.geteuid() == 0:
        try:
            groups = os.getgrouplist(user.pw_name, user.pw_gid)
        except AttributeError:
            groups = [user.pw_gid]

        def drop_privileges() -> None:
            os.setgroups(groups)
            os.setgid(user.pw_gid)
            os.setuid(user.pw_uid)

        options['preexec_fn'] = drop_privileges
    try:
        return subprocess.run(
            command, check=False,
            cwd=user.pw_dir if Path(user.pw_dir).is_dir() else '/',
            env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, start_new_session=True, **options,
        )
    except subprocess.TimeoutExpired as error:
        raise InstallerError(f'bundle executable timed out: {command[0]}') from error
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallerError(f'cannot validate bundle executable {command[0]}: {error}') from error


def _verify_runtime(runtime_root: Path, manifest: dict[str, Any], user: pwd.struct_passwd, rust_executable: Path) -> None:
    worker = runtime_root / WORKER_EXECUTABLE
    result = _run_as_user([str(worker), '--describe'], user)
    if result.returncode:
        _fail(f'worker runtime description failed: {result.stderr.strip()}')
    try:
        described = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise InstallerError(f'worker returned invalid runtime JSON: {error}') from error
    expected = manifest['runtime']
    if not isinstance(described, dict) or not isinstance(expected, dict):
        _fail('worker runtime description is not an object')
    expected = dict(expected)
    expected['executable'] = described.get('executable')
    if described != expected:
        _fail('worker runtime does not match its private bundle manifest')
    rust_result = _run_as_user([str(rust_executable), '--help'], user)
    if rust_result.returncode:
        _fail(f'Rust runtime --help validation failed: {rust_result.stderr.strip()}')


def _copytree_checked(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=False, copy_function=shutil.copy2)


def _make_runtime(bundle: Path, manifest: dict[str, Any], paths: dict[str, Path], transaction_id: str, user: pwd.struct_passwd) -> tuple[Path, Path]:
    runtime_root = paths['runtimes']
    staging = runtime_root / f'.stage-{transaction_id}'
    final = runtime_root / transaction_id
    if staging.exists() or final.exists():
        _fail('runtime staging path already exists')
    staging.mkdir(mode=0o755)
    try:
        shutil.copy2(bundle / WORKER_EXECUTABLE, staging / WORKER_EXECUTABLE)
        installer = staging / 'install.py'
        shutil.copy2(bundle / 'install.py', installer)
        shutil.copy2(bundle / 'process_observer.py', staging / 'process_observer.py')
        os.chmod(installer, 0o755)
        for name in ('assets', 'locales'):
            _copytree_checked(bundle / name, staging / name)
        if (bundle / '_internal').is_dir():
            _copytree_checked(bundle / '_internal', staging / '_internal')
        for path in (staging, *staging.rglob('*')):
            if path.is_dir():
                os.chmod(path, 0o755)
            else:
                mode = stat.S_IMODE(path.stat().st_mode)
                if mode & 0o022 or mode & 0o6000:
                    _fail(f'unsafe copied runtime permissions: {path}')
                if path.name == 'install.py':
                    mode |= 0o111
                os.chmod(path, mode)
        _check_runtime_access(staging, user, (paths['homebrew'], paths['services'], paths['state'], paths['runtimes']))
        hashes = manifest['sha256']
        assert isinstance(hashes, dict)
        for relative, expected in hashes.items():
            if relative == RUST_EXECUTABLE:
                continue
            if relative in (WORKER_EXECUTABLE, 'install.py', 'process_observer.py') or relative.startswith(('assets/', 'locales/', '_internal/')):
                copied = staging / relative
                if not copied.is_file() or _digest(copied) != expected:
                    _fail(f'copied runtime checksum mismatch: {relative}')
        return staging, final
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _copy_stage_file(source: Path, directory: Path, prefix: str, mode_override: int | None = None) -> Path:
    source_info = _lstat_regular_or_absent(source)
    if source_info is None:
        _fail(f'required file is missing: {source}')
    mode = stat.S_IMODE(source_info.st_mode)
    if mode & 0o6000 or mode & 0o022:
        _fail(f'refusing unsafe executable permissions: {source}')
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix='.stage', dir=directory)
    os.close(descriptor)
    stage = Path(name)
    stage.unlink()
    try:
        _copy_regular(source, stage)
        if mode_override is not None:
            mode = mode_override
        os.chmod(stage, mode)
        _fsync_file(stage)
        _fsync_directory(directory)
        return stage
    except BaseException:
        stage.unlink(missing_ok=True)
        raise


def _systemd_quote(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _fail('systemd argument contains a control character')
    escaped = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$')
    return f'"{escaped}"'


def _listen_endpoint(value: str) -> tuple[str, int]:
    if not value or any(character.isspace() or ord(character) < 32 for character in value):
        _fail(f'invalid loopback listen address: {value!r}')
    if value.startswith('['):
        closing = value.find(']')
        if closing < 0 or closing + 1 >= len(value) or value[closing + 1] != ':':
            _fail(f'invalid loopback listen address: {value!r}')
        host = value[1:closing]
        port_text = value[closing + 2:]
    else:
        host, separator, port_text = value.rpartition(':')
        if not separator or ':' in host:
            _fail(f'IPv6 listen addresses must use brackets: {value!r}')
    try:
        address = ipaddress.ip_address(host)
        port = int(port_text)
    except ValueError as error:
        raise InstallerError(f'invalid loopback listen address {value!r}: {error}') from error
    if not address.is_loopback or not 1 <= port <= 65535:
        _fail('the Decky listener must use a loopback IP and port 1-65535')
    return str(address), port


def _validate_cef_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        _fail('CEF URL must be an absolute http(s) URL without user information')
    if any(ord(character) < 32 or character.isspace() for character in value):
        _fail('CEF URL contains whitespace or a control character')
    try:
        _ = parsed.port
    except ValueError as error:
        raise InstallerError(f'invalid CEF URL: {error}') from error
    return value


def _service_environment(paths: dict[str, Path]) -> list[str]:
    result = _systemctl(paths, 'cat', '--no-pager', paths['unit'].name, check=False, timeout=15)
    text = result.stdout
    if not text:
        try:
            if _lstat_regular_or_absent(paths['unit']) is not None:
                text = paths['unit'].read_text(encoding='utf-8')
        except (OSError, UnicodeError) as error:
            raise InstallerError(f'cannot preserve existing service environment: {error}') from error
    section = ''
    environment: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
            continue
        if section != 'Service' or '=' not in line:
            continue
        key, _ = line.split('=', 1)
        if key in ('Environment', 'EnvironmentFile'):
            environment.append(line)
    return environment


def _environment_keys(lines: list[str]) -> set[str]:
    keys: set[str] = set()
    for line in lines:
        if not line.startswith('Environment='):
            continue
        try:
            entries = shlex.split(line.split('=', 1)[1], posix=True)
        except ValueError as error:
            raise InstallerError(f'cannot preserve an invalid existing Environment directive: {error}') from error
        for entry in entries:
            key, separator, _ = entry.partition('=')
            if separator and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
                keys.add(key)
    return keys


def _listener_configuration(environment: list[str], listen: str) -> tuple[list[str], list[str]]:
    host, port = _listen_endpoint(listen)
    assignments = [f'SERVER_HOST={host}', f'SERVER_PORT={port}']
    environment.extend(f'Environment={assignment}' for assignment in assignments)
    # EnvironmentFile and drop-ins can override Environment. Set the selected
    # endpoint immediately before exec, matching Rust's explicit --listen.
    return environment, ['/usr/bin/env', *assignments]


def _generated_unit(paths: dict[str, Path], runtime: Path, user: str, service_name: str, listen: str, cef_url: str, inject_frontend: bool, worker_mode: str, github_api: str, release_repository: str, testing_download_url: str, version: str) -> bytes:
    environment = _service_environment(paths)
    present = _environment_keys(environment)
    defaults = {
        'UNPRIVILEGED_PATH': str(paths['homebrew']),
        'PRIVILEGED_PATH': str(paths['homebrew']),
        'LOG_LEVEL': 'INFO',
    }
    for key, value in defaults.items():
        if key not in present:
            environment.append(f'Environment={key}={value}')
    environment, command_prefix = _listener_configuration(environment, listen)
    arguments = [
        *command_prefix,
        str(paths['binary']),
        '--worker', str(runtime / WORKER_EXECUTABLE),
        '--worker-mode', worker_mode,
        '--worker-bootstrap', 'lean',
        '--release-version', version,
        '--plugin-root', str(paths['homebrew'] / 'plugins'),
        '--homebrew', str(paths['homebrew']),
        '--user', user,
        '--assets', str(runtime / 'assets'),
        '--locales', str(runtime / 'locales'),
        '--listen', listen,
        '--service-name', service_name,
        '--cef-url', cef_url,
        '--inject-frontend', 'true' if inject_frontend else 'false',
        '--installer', str(runtime / 'install.py'),
        '--github-api', github_api,
        '--release-repository', release_repository,
        '--testing-download-url', testing_download_url,
    ]
    command = ' '.join(_systemd_quote(argument) for argument in arguments)
    lines = [
        '[Unit]',
        'Description=SteamDeck Plugin Loader',
        'After=network.target',
        '',
        '[Service]',
        'Type=simple',
        'Restart=always',
        'KillMode=mixed',
        'TimeoutStopSec=15',
        f'ExecStart={command}',
        f'WorkingDirectory={paths["services"]}',
        *environment,
        '',
        '[Install]',
        'WantedBy=default.target',
        '',
    ]
    return ('\n'.join(lines)).encode('utf-8')


def _stage_bytes(data: bytes, directory: Path, prefix: str, mode: int = 0o644) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix='.stage', dir=directory)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(directory)
        return temporary
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _signal_service_processes(processes: dict[int, Process], sig: signal.Signals) -> None:
    if any(process.pid <= 1 or process.pid == os.getpid() for process in processes.values()):
        _fail('refusing to signal PID 1 or the installer itself')
    for process in processes.values():
        if process.state == 'Z':
            continue
        try:
            descriptor = os.pidfd_open(process.pid)
        except ProcessLookupError:
            continue
        except OSError as error:
            raise InstallerError(f'cannot safely open remaining service PID {process.pid}: {error}') from error
        try:
            current = read_process(process.pid)
            if current is not None and current.start_ticks == process.start_ticks:
                signal.pidfd_send_signal(descriptor, sig)
        except ProcessLookupError:
            pass
        except OSError as error:
            raise InstallerError(f'cannot safely signal remaining service PID {process.pid}: {error}') from error
        finally:
            os.close(descriptor)


def _user_stop_record_directory(user: pwd.struct_passwd) -> Path:
    runtime = _user_runtime_directory(user)
    directory = runtime / 'decky-loader' / 'stop-records'
    for current in (runtime / 'decky-loader', directory):
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise InstallerError(f'cannot create user service stop-record directory {current}: {error}') from error
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode):
            _fail(f'user service stop-record path is not a directory: {current}')
        if os.geteuid() == 0 and info.st_uid != user.pw_uid:
            os.chown(current, user.pw_uid, user.pw_gid, follow_symlinks=False)
            info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != user.pw_uid or info.st_mode & 0o077:
            _fail(f'user service stop-record directory is not private and owned by {user.pw_name}: {current}')
    return directory


def _stop_record_directory(scope: str, user: pwd.struct_passwd | None) -> Path:
    if scope == 'system':
        _ensure_directory(SERVICE_STOP_DIRECTORY, 0o700, managed=True)
        return SERVICE_STOP_DIRECTORY
    if scope != 'user':
        _fail(f'unsupported service stop scope: {scope}')
    return _user_stop_record_directory(user or _current_user())


def stop_service(service_name: str, *, scope: str = 'user', user: pwd.struct_passwd | None = None) -> None:
    """Stop one selected unit, retaining ownership until its helpers have drained."""
    _, unit_filename = _normalize_service_name(service_name)
    paths: dict[str, Any] = {'unit': Path(unit_filename), 'scope': scope, 'user': user}
    record_directory = _stop_record_directory(scope, user)

    state_file = record_directory / f'{unit_filename}.json'
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    saved = _read_json(state_file) if _lstat_regular_or_absent(state_file) is not None else None
    pending_groups: set[str] = set()
    retained: dict[int, int] = {}
    if saved is not None:
        groups, identities = saved.get('groups'), saved.get('identities')
        if (
            saved.get('schema') != 1 or saved.get('service') != unit_filename
            or not isinstance(saved.get('boot_id'), str)
            or not isinstance(groups, list) or not all(isinstance(group, str) for group in groups)
            or not isinstance(identities, list)
            or not all(
                isinstance(identity, list) and len(identity) == 2
                and type(identity[0]) is int and identity[0] > 1
                and type(identity[1]) is int and identity[1] >= 0
                for identity in identities
            )
            or (identities and not groups)
        ):
            _fail(f'invalid selected-service shutdown ownership: {state_file}')
        if saved['boot_id'] == boot_id:
            pending_groups.update(groups)
            retained.update(identities)

    def observe() -> dict[int, Process]:
        return {
            pid: process
            for group in pending_groups
            for pid, process in cgroup_processes(group, retained=retained).items()
        }

    def remember(processes: dict[int, Process]) -> None:
        nonlocal saved, retained
        retained = {pid: process.start_ticks for pid, process in processes.items()}
        state = {
            'schema': 1, 'service': unit_filename, 'boot_id': boot_id,
            'groups': sorted(pending_groups),
            'identities': [[pid, ticks] for pid, ticks in sorted(retained.items())],
        }
        if pending_groups and state != saved:
            _write_json(state_file, state)
            saved = state

    properties = _unit_properties(paths)
    control_group = properties.get('ControlGroup') or None
    try:
        main_pid = int(properties.get('MainPID', '0') or '0')
    except ValueError:
        _fail(f'systemd returned an invalid MainPID for {paths["unit"].name}')
    if control_group:
        pending_groups.add(control_group)
    remember(observe())
    if main_pid > 0:
        _service_cgroup_pids(properties, main_pid)
    if properties.get('ActiveState') == 'failed':
        if main_pid:
            _fail(f'failed service {paths["unit"].name} still has MainPID={main_pid}')
        _systemctl(paths, 'reset-failed', paths['unit'].name, timeout=15)
        properties = _unit_properties(paths)
        try:
            main_pid = int(properties.get('MainPID', '0') or '0')
        except ValueError:
            _fail(f'systemd returned an invalid MainPID for {paths["unit"].name}')
    if properties.get('LoadState') != 'not-found' and (properties.get('ActiveState') != 'inactive' or main_pid > 0):
        _systemctl(paths, 'stop', paths['unit'].name, timeout=45)
    started = time.monotonic()
    deadline = started + 20
    signalled: dict[signal.Signals, set[tuple[int, int]]] = {
        signal.SIGTERM: set(), signal.SIGKILL: set(),
    }
    remaining: dict[int, Process] = {}
    while time.monotonic() < deadline:
        current = _unit_properties(paths)
        control_group = current.get('ControlGroup')
        if control_group:
            pending_groups.add(control_group)
        terminal = current.get('LoadState') == 'not-found' or current.get('ActiveState') in ('inactive', 'failed')
        remaining = observe()
        if remaining:
            remember(remaining)
        if terminal and current.get('MainPID', '0') == '0':
            if not remaining:
                if saved is not None:
                    state_file.unlink()
                    _fsync_directory(record_directory)
                return
            elapsed = time.monotonic() - started
            escalation = signal.SIGKILL if elapsed >= 10 else signal.SIGTERM if elapsed >= 5 else None
            if escalation is not None:
                targets = {
                    pid: process for pid, process in remaining.items()
                    if process.state != 'Z'
                    and (pid, process.start_ticks) not in signalled[escalation]
                }
                if targets:
                    details = ', '.join(
                        f'PID {pid} ({process.comm!r}, state {process.state})'
                        for pid, process in targets.items()
                    )
                    print(
                        f'{unit_filename}: main process stopped; sending {escalation.name} '
                        f'to remaining owned helpers: {details}',
                        file=sys.stderr, flush=True,
                    )
                    _signal_service_processes(targets, escalation)
                    signalled[escalation].update(
                        (pid, process.start_ticks) for pid, process in targets.items()
                    )
        time.sleep(0.2)
    details = ', '.join(
        f'PID {pid} ({process.comm!r}, state {process.state}, parent {process.ppid})'
        for pid, process in remaining.items()
    )
    _fail(
        f'selected service {unit_filename} did not stop cleanly; '
        f'remaining owned processes: {details or "none"}; backup or activation cancelled'
    )


def _set_enabled(paths: dict[str, Path], activation: dict[str, object]) -> None:
    enabled = bool(activation.get('enabled'))
    scope = activation.get('enabled_scope')
    if enabled:
        if scope == 'runtime':
            _systemctl(paths, 'enable', '--runtime', paths['unit'].name)
        else:
            _systemctl(paths, 'enable', paths['unit'].name)
    else:
        _systemctl(paths, 'disable', paths['unit'].name, check=False)
        _systemctl(paths, 'disable', '--runtime', paths['unit'].name, check=False)


def _wait_running(paths: dict[str, Path], timeout: float = 30) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    last: dict[str, str] = {}
    while time.monotonic() < deadline:
        last = _unit_properties(paths)
        if last.get('LoadState') == 'loaded' and last.get('ActiveState') == 'active' and last.get('SubState') == 'running' and int(last.get('MainPID', '0') or '0') > 0:
            return last
        if last.get('ActiveState') == 'failed':
            _fail(f'selected service {paths["unit"].name} failed during activation')
        time.sleep(0.25)
    _fail(f'selected service {paths["unit"].name} did not become active/running: {last}')


def _wait_stopped(paths: dict[str, Path], timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        properties = _unit_properties(paths)
        if properties.get('LoadState') == 'not-found' or (properties.get('ActiveState') == 'inactive' and properties.get('MainPID', '0') == '0'):
            return
        time.sleep(0.2)
    _fail(f'selected service {paths["unit"].name} did not return to inactive state')


def _process_is_binary(pid: int, binary: Path) -> bool:
    try:
        running = os.stat(f'/proc/{pid}/exe')
        installed = binary.stat()
    except OSError:
        return False
    return running.st_dev == installed.st_dev and running.st_ino == installed.st_ino


def _service_cgroup_pids(properties: dict[str, str], main_pid: int) -> set[int]:
    control_group = properties.get('ControlGroup', '')
    if not control_group.startswith('/') or any(part in ('', '.', '..') for part in control_group.split('/')[1:]):
        _fail('systemd did not report a safe selected-service ControlGroup')
    root = Path('/sys/fs/cgroup')
    try:
        root_real = root.resolve(strict=True)
        group = (root / control_group.lstrip('/')).resolve(strict=True)
        group.relative_to(root_real)
    except (OSError, ValueError) as error:
        raise InstallerError(f'cannot resolve selected-service cgroup: {error}') from error
    if not group.is_dir():
        _fail('selected-service ControlGroup is not a directory')
    try:
        processes = cgroup_processes(control_group)
    except (OSError, ValueError) as error:
        raise InstallerError(f'cannot observe selected-service processes: {error}') from error
    pids = set(processes)
    if main_pid not in pids:
        _fail('selected service MainPID is not in its reported ControlGroup')
    return pids


def _is_process_descendant(pid: int, ancestor: int) -> bool:
    current = pid
    for _ in range(128):
        if current == ancestor:
            return True
        try:
            status = Path(f'/proc/{current}/status').read_text(encoding='ascii')
        except (OSError, UnicodeError):
            return False
        parent = None
        for line in status.splitlines():
            if line.startswith('PPid:'):
                value = line.partition(':')[2].strip()
                if value.isdecimal():
                    parent = int(value)
                break
        if parent is None or parent <= 1 or parent == current:
            return False
        current = parent
    return False


def _listener_inodes(listen: str) -> set[str]:
    host, port = _listen_endpoint(listen)
    target = ipaddress.ip_address(host)
    result: set[str] = set()
    for filename, family in (('/proc/net/tcp', socket.AF_INET), ('/proc/net/tcp6', socket.AF_INET6)):
        try:
            lines = Path(filename).read_text(encoding='ascii').splitlines()[1:]
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError) as error:
            raise InstallerError(f'cannot inspect kernel TCP listeners: {error}') from error
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != '0A':
                continue
            address_hex, separator, port_hex = fields[1].partition(':')
            if not separator or int(port_hex, 16) != port:
                continue
            try:
                packed = bytes.fromhex(address_hex)
            except ValueError:
                continue
            if family == socket.AF_INET:
                address = ipaddress.ip_address(packed[::-1])
            else:
                if len(packed) != 16:
                    continue
                address = ipaddress.ip_address(b''.join(packed[index:index + 4][::-1] for index in range(0, 16, 4)))
            wildcard = address.is_unspecified and address.version == target.version
            if address == target or wildcard:
                result.add(fields[9])
    return result


def _service_owns_listener(properties: dict[str, str], main_pid: int, listen: str) -> bool:
    owned_inodes = _listener_inodes(listen)
    if not owned_inodes:
        return False
    service_pids = _service_cgroup_pids(properties, main_pid)
    for pid in service_pids:
        if not _is_process_descendant(pid, main_pid):
            continue
        try:
            descriptors = os.scandir(f'/proc/{pid}/fd')
        except OSError:
            continue
        with descriptors:
            for descriptor in descriptors:
                try:
                    target = os.readlink(descriptor.path)
                except OSError:
                    continue
                if target.startswith('socket:[') and target.endswith(']') and target[8:-1] in owned_inodes:
                    return True
    return False


def _read_exact(connection: socket.socket, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        chunk = connection.recv(count - len(result))
        if not chunk:
            raise InstallerError('service closed its health-check connection')
        result.extend(chunk)
    return bytes(result)


def _websocket_send(connection: socket.socket, opcode: int, payload: bytes) -> None:
    if len(payload) > MAX_WEBSOCKET_FRAME:
        _fail('health-check WebSocket message is too large')
    mask = os.urandom(4)
    first = 0x80 | opcode
    length = len(payload)
    if length < 126:
        header = bytes((first, 0x80 | length))
    elif length <= 0xFFFF:
        header = bytes((first, 0x80 | 126)) + struct.pack('!H', length)
    else:
        header = bytes((first, 0x80 | 127)) + struct.pack('!Q', length)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    connection.sendall(header + mask + masked)


def _websocket_receive(connection: socket.socket) -> tuple[int, bool, bytes]:
    first, second = _read_exact(connection, 2)
    opcode = first & 0x0F
    final = bool(first & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack('!H', _read_exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack('!Q', _read_exact(connection, 8))[0]
    if length > MAX_WEBSOCKET_FRAME:
        _fail('service health-check frame exceeds the size limit')
    masked = bool(second & 0x80)
    mask = _read_exact(connection, 4) if masked else b''
    payload = _read_exact(connection, length)
    if masked:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return opcode, final, payload


def _websocket_call(connection: socket.socket, call_id: int, route: str) -> object:
    request = json.dumps({'type': 0, 'id': call_id, 'route': route, 'args': []}, separators=(',', ':')).encode('utf-8')
    _websocket_send(connection, 1, request)
    fragments = bytearray()
    current_opcode = 0
    while True:
        opcode, final, payload = _websocket_receive(connection)
        if opcode == 8:
            _fail(f'service closed WebSocket while checking {route}')
        if opcode == 9:
            _websocket_send(connection, 10, payload)
            continue
        if opcode == 10:
            continue
        if opcode in (1, 2):
            current_opcode = opcode
            fragments.extend(payload)
        elif opcode == 0 and current_opcode:
            fragments.extend(payload)
        else:
            continue
        if not final:
            if len(fragments) > MAX_WEBSOCKET_FRAME:
                _fail('service health-check message exceeds the size limit')
            continue
        if current_opcode != 1:
            fragments.clear()
            current_opcode = 0
            continue
        try:
            response = json.loads(fragments.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError) as error:
            _fail(f'service returned invalid health-check JSON: {error}')
        fragments.clear()
        current_opcode = 0
        if not isinstance(response, dict) or response.get('id') != call_id:
            continue
        acknowledgement = json.dumps({'type': 3, 'id': call_id}, separators=(',', ':')).encode('utf-8')
        _websocket_send(connection, 1, acknowledgement)
        if response.get('type') != 1:
            _fail(f'service RPC {route} failed: {response.get("error", response)}')
        return response.get('result')


def _api_health(paths: dict[str, Path], listen: str) -> None:
    host, port = _listen_endpoint(listen)
    endpoint_host = f'[{host}]' if ':' in host else host
    base = f'http://{endpoint_host}:{port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(f'{base}/auth/token', method='GET'), timeout=3) as response:
            token = response.read(4097).decode('utf-8').strip()
            if response.status != 200 or not token or len(token) > 4096:
                _fail('service returned an invalid authentication token')
        denied = False
        invalid = urllib.request.Request(f'{base}/auth/token?installer-health=1', headers={'X-Decky-Auth': 'invalid-installer-token'}, method='GET')
        try:
            with opener.open(invalid, timeout=3) as response:
                denied = response.status == 403
        except urllib.error.HTTPError as error:
            denied = error.code == 403
            error.close()
        if not denied:
            _fail('service did not reject an invalid authentication token')
        authorized = urllib.request.Request(f'{base}/auth/token?installer-health=1', headers={'X-Decky-Auth': token}, method='GET')
        with opener.open(authorized, timeout=3) as response:
            if response.status != 200 or response.read(4097).decode('utf-8').strip() != token:
                _fail('service authentication check failed')
    except InstallerError:
        raise
    except (OSError, UnicodeError, urllib.error.URLError) as error:
        raise InstallerError(f'service authentication health check failed: {error}') from error
    address = (host, port)
    key = base64.b64encode(os.urandom(16)).decode('ascii')
    query = urllib.parse.urlencode({'auth': token})
    target = f'/ws?{query}'
    host_header = f'[{host}]:{port}' if ':' in host else f'{host}:{port}'
    request = (
        f'GET {target} HTTP/1.1\r\n'
        f'Host: {host_header}\r\n'
        'Upgrade: websocket\r\n'
        'Connection: Upgrade\r\n'
        f'Sec-WebSocket-Key: {key}\r\n'
        'Sec-WebSocket-Version: 13\r\n'
        'Origin: https://steamloopback.host\r\n\r\n'
    ).encode('ascii')
    accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')).digest()).decode('ascii')
    with socket.create_connection(address, timeout=4) as connection:
        connection.settimeout(5)
        connection.sendall(request)
        headers = bytearray()
        while not headers.endswith(b'\r\n\r\n'):
            if len(headers) >= MAX_HTTP_HEADERS:
                _fail('service returned oversized WebSocket headers')
            part = connection.recv(1)
            if not part:
                _fail('service closed the WebSocket handshake')
            headers.extend(part)
        try:
            header_lines = headers.decode('iso-8859-1').split('\r\n')
        except UnicodeError as error:
            raise InstallerError(f'invalid WebSocket handshake: {error}') from error
        if not header_lines or ' 101 ' not in f' {header_lines[0]} ':
            _fail(f'service rejected authenticated WebSocket health check: {header_lines[0] if header_lines else "empty response"}')
        response_headers = {}
        for line in header_lines[1:]:
            if ':' in line:
                name, value = line.split(':', 1)
                response_headers[name.strip().lower()] = value.strip()
        if response_headers.get('sec-websocket-accept') != accept:
            _fail('service returned an invalid WebSocket handshake accept key')
        ping = _websocket_call(connection, HEALTH_RPC_IDS[0], 'utilities/ping')
        if ping != 'pong':
            _fail(f'utilities/ping returned an unexpected value: {ping!r}')
        plugins = _websocket_call(connection, HEALTH_RPC_IDS[1], 'loader/get_plugins')
        if not isinstance(plugins, list):
            _fail('loader/get_plugins did not return a list')
        version = _websocket_call(connection, HEALTH_RPC_IDS[2], 'updater/get_version_info')
        if not isinstance(version, dict) or not isinstance(version.get('current'), str) or not version['current']:
            _fail('updater/get_version_info did not return a current version')
        if paths['active'].exists():
            expected = _read_json(paths['active'])
            if 'version' in expected and version['current'] != expected['version']:
                _fail('service release identity does not match the installed transaction')
            if 'sdk_version' in expected and version.get('sdk_version') != expected['sdk_version']:
                _fail('service SDK compatibility version does not match the installed runtime')
        _websocket_send(connection, 8, b'')


def _health(paths: dict[str, Path], listen: str | None = None, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    last_error = 'service has not become ready'
    while time.monotonic() < deadline:
        properties = _unit_properties(paths)
        if properties.get('LoadState') == 'loaded' and properties.get('ActiveState') == 'active' and properties.get('SubState') == 'running':
            try:
                pid = int(properties.get('MainPID', '0'))
            except ValueError:
                _fail('systemd returned an invalid MainPID')
            if pid > 0 and _process_is_binary(pid, paths['binary']):
                try:
                    checked_listen = _configured_listen(paths) if listen is None else listen
                    if not _service_owns_listener(properties, pid, checked_listen):
                        last_error = 'the configured TCP listener is not owned by the selected service MainPID or its descendants'
                        time.sleep(0.35)
                        continue
                    _api_health(paths, checked_listen)
                except (InstallerError, OSError, urllib.error.URLError) as error:
                    last_error = str(error)
                    time.sleep(0.35)
                    continue
                after = _unit_properties(paths)
                try:
                    same_pid = int(after.get('MainPID', '0')) == pid
                except ValueError:
                    same_pid = False
                if (
                    after.get('ActiveState') == 'active'
                    and after.get('SubState') == 'running'
                    and same_pid
                    and _process_is_binary(pid, paths['binary'])
                    and _service_owns_listener(after, pid, checked_listen)
                ):
                    return
                last_error = 'service MainPID or listener ownership changed during health validation'
            else:
                last_error = f'service MainPID {properties.get("MainPID", "0")} is not the installed executable'
        elif properties.get('ActiveState') == 'failed':
            last_error = 'systemd reports the selected service failed'
        time.sleep(0.35)
    _fail(f'service activation health check failed: {last_error}')


def _apply_activation(paths: dict[str, Path], activation: dict[str, object], check_running: bool = True) -> None:
    _set_enabled(paths, activation)
    if activation.get('running'):
        _systemctl(paths, 'start', paths['unit'].name, timeout=45)
        _wait_running(paths)
        if check_running:
            _health(paths)
    else:
        stop_service(paths['unit'].name)


def _configured_listen(paths: dict[str, Path]) -> str:
    try:
        config = _read_json(paths['active'])
        listen = config.get('listen')
        if isinstance(listen, str):
            _listen_endpoint(listen)
            return listen
    except (InstallerError, OSError):
        pass
    try:
        result = _systemctl(paths, 'cat', '--no-pager', paths['unit'].name, check=False, timeout=10).stdout
        for line in result.splitlines():
            line = line.strip()
            if not line.startswith('ExecStart='):
                continue
            try:
                arguments = shlex.split(line.split('=', 1)[1], posix=True)
            except ValueError:
                continue
            if '--listen' in arguments:
                index = arguments.index('--listen')
                if index + 1 < len(arguments):
                    _listen_endpoint(arguments[index + 1])
                    return arguments[index + 1]
    except InstallerError:
        pass
    properties = _unit_properties(paths)
    try:
        pid = int(properties.get('MainPID', '0') or '0')
    except ValueError:
        _fail('systemd returned an invalid MainPID while determining the listener')
    if pid > 0 and _process_is_binary(pid, paths['binary']):
        try:
            entries = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
            environment: dict[str, str] = {}
            for entry in entries:
                key, separator, value = entry.partition(b'=')
                if separator and key in (b'SERVER_HOST', b'SERVER_PORT'):
                    environment[key.decode('ascii')] = value.decode('utf-8')
        except (OSError, UnicodeError) as error:
            raise InstallerError(f'cannot read the selected backend listener environment: {error}') from error
        host, port = _listen_endpoint(DEFAULT_LISTEN)
        host = environment.get('SERVER_HOST', host)
        port_text = environment.get('SERVER_PORT', str(port))
        listen = f'[{host}]:{port_text}' if ':' in host else f'{host}:{port_text}'
        _listen_endpoint(listen)
        return listen
    return DEFAULT_LISTEN


def _record_paths(paths: dict[str, Path]) -> dict[str, Path]:
    return {'binary': paths['binary'], 'unit': paths['unit'], 'active': paths['active']}


def _snapshot_transaction(paths: dict[str, Path], transaction_id: str, operation: str, version: str | None = None) -> tuple[Path, dict[str, object], dict[str, object]]:
    transaction = paths['history'] / transaction_id
    transaction.mkdir(mode=0o700)
    before = transaction / 'before'
    before.mkdir(mode=0o700)
    files: dict[str, object] = {}
    for key, target in _record_paths(paths).items():
        files[key] = _snapshot_file(target, before / key)
    activation = _capture_activation(paths)
    head = _read_json(paths['head']).get('id') if paths['head'].exists() else None
    if head is not None and (not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{32}', head)):
        _fail('installer history head is invalid')
    record: dict[str, object] = {
        'schema': 1,
        'id': transaction_id,
        'operation': operation,
        'service': paths['unit'].name,
        'homebrew': str(paths['homebrew']),
        'parent': head,
        'version': version,
        'created_at': int(time.time()),
        'activation': activation,
        'files': files,
    }
    _write_json(transaction / 'record.json', record, 0o600)
    _fsync_directory(before)
    _fsync_directory(transaction)
    _fsync_directory(paths['history'])
    return transaction, record, activation


def _write_journal(paths: dict[str, Path], journal: dict[str, object]) -> None:
    _write_json(paths['journal'], journal, 0o600)


def _new_transaction_id() -> str:
    return uuid.uuid4().hex


def _current_record(paths: dict[str, Path], transaction_id: str) -> dict[str, Any]:
    transaction = paths['history'] / transaction_id
    try:
        transaction_info = transaction.lstat()
    except OSError as error:
        raise InstallerError(f'missing installer transaction {transaction_id}: {error}') from error
    if not stat.S_ISDIR(transaction_info.st_mode):
        _fail(f'invalid installer transaction directory: {transaction}')
    record = _read_json(transaction / 'record.json')
    if record.get('schema') != 1 or record.get('id') != transaction_id or record.get('service') != paths['unit'].name or record.get('homebrew') != str(paths['homebrew']):
        _fail(f'invalid installer transaction record: {transaction_id}')
    if not isinstance(record.get('files'), dict) or not isinstance(record.get('activation'), dict):
        _fail(f'incomplete installer transaction record: {transaction_id}')
    return record


def _validate_snapshot(transaction: Path, record: dict[str, Any]) -> None:
    files = record['files']
    assert isinstance(files, dict)
    for key in ('binary', 'unit', 'active'):
        state = files.get(key)
        if not isinstance(state, dict) or not isinstance(state.get('exists'), bool):
            _fail(f'invalid {key} snapshot in installer transaction')
        if state['exists']:
            if not isinstance(state.get('metadata'), dict):
                _fail(f'missing metadata in {key} snapshot')
            _lstat_regular_or_absent(transaction / 'before' / key)
            if not (transaction / 'before' / key).is_file():
                _fail(f'missing saved {key} data in installer transaction')


def _restore_transaction(paths: dict[str, Path], transaction_id: str, record: dict[str, Any], activation: dict[str, Any]) -> None:
    transaction = paths['history'] / transaction_id
    _validate_snapshot(transaction, record)
    stop_service(paths['unit'].name)
    files = record['files']
    assert isinstance(files, dict)
    for key, target in _record_paths(paths).items():
        state = files[key]
        assert isinstance(state, dict)
        _restore_file(target, transaction / 'before' / key, state)
    _systemctl(paths, 'daemon-reload', timeout=30)
    _apply_activation(paths, activation)


def _remove_runtime_from_journal(paths: dict[str, Path], journal: dict[str, object]) -> None:
    runtime_id = journal.get('runtime_id')
    if runtime_id is None:
        return
    if not isinstance(runtime_id, str) or not re.fullmatch(r'[0-9a-f]{32}', runtime_id):
        _fail('invalid runtime identifier in installer journal')
    runtime = paths['runtimes'] / runtime_id
    if runtime.exists():
        for item in runtime.rglob('*'):
            if item.is_symlink():
                _fail(f'refusing symlink in uncommitted runtime: {item}')
        shutil.rmtree(runtime)
        _fsync_directory(paths['runtimes'])


def _cleanup_stages(paths: dict[str, Path], journal: dict[str, object]) -> None:
    txid = journal.get('id')
    if not isinstance(txid, str) or not re.fullmatch(r'[0-9a-f]{32}', txid):
        return
    for key, parent, prefix in (
        ('binary_stage', paths['services'], f'.PluginLoader.{txid}.'),
        ('unit_stage', paths['unit_dir'], f'.{paths["unit"].name}.{txid}.'),
        ('active_stage', paths['state'], f'.active.{txid}.'),
    ):
        value = journal.get(key)
        if value is None:
            continue
        target = Path(str(value))
        if target.parent != parent or not re.fullmatch(re.escape(prefix) + r'[A-Za-z0-9_]+\.stage', target.name):
            _fail('installer journal contains an unsafe staging path')
        if target.exists():
            _lstat_regular_or_absent(target)
            target.unlink()
    runtime_stage = journal.get('runtime_stage')
    if runtime_stage is not None:
        target = Path(str(runtime_stage))
        if target.parent != paths['runtimes'] or target.name != f'.stage-{txid}':
            _fail('installer journal contains an unsafe runtime staging path')
        if target.exists():
            if not target.is_dir() or target.is_symlink():
                _fail('installer runtime staging path is not a real directory')
            shutil.rmtree(target)
    for parent in (paths['services'], paths['unit_dir'], paths['state']):
        _fsync_directory(parent)


def _finalize_committed(paths: dict[str, Path], journal: dict[str, object]) -> None:
    head_after = journal.get('head_after')
    if head_after is None:
        paths['head'].unlink(missing_ok=True)
        _fsync_directory(paths['state'])
    else:
        if not isinstance(head_after, str) or not re.fullmatch(r'[0-9a-f]{32}', head_after):
            _fail('invalid committed history head in installer journal')
        _current_record(paths, head_after)
        _write_json(paths['head'], {'id': head_after}, 0o600)
    paths['journal'].unlink(missing_ok=True)
    _fsync_directory(paths['state'])


def _recover(paths: dict[str, Path]) -> None:
    if not paths['journal'].exists():
        return
    journal = _read_json(paths['journal'])
    transaction_id = journal.get('id')
    if not isinstance(transaction_id, str) or not re.fullmatch(r'[0-9a-f]{32}', transaction_id):
        _fail('invalid transaction identifier in installer journal')
    if journal.get('service') != paths['unit'].name or journal.get('homebrew') != str(paths['homebrew']):
        _fail('installer journal belongs to a different homebrew root or service')
    if journal.get('phase') == 'committed':
        _finalize_committed(paths, journal)
        _cleanup_stages(paths, journal)
        return
    record = _current_record(paths, transaction_id)
    activation = record.get('activation')
    assert isinstance(activation, dict)
    _write_journal(paths, {**journal, 'phase': 'recovering'})
    _restore_transaction(paths, transaction_id, record, activation)
    _remove_runtime_from_journal(paths, journal)
    _cleanup_stages(paths, journal)
    paths['journal'].unlink(missing_ok=True)
    transaction = paths['history'] / transaction_id
    shutil.rmtree(transaction)
    _fsync_directory(paths['history'])
    _fsync_directory(paths['state'])


def _begin_journal(paths: dict[str, Path], transaction_id: str, operation: str, record: dict[str, object], stage_values: dict[str, Path | str | None], head_after: str | None, runtime_id: str | None = None, listen: str | None = None) -> dict[str, object]:
    journal: dict[str, object] = {
        'schema': 1,
        'id': transaction_id,
        'service': paths['unit'].name,
        'homebrew': str(paths['homebrew']),
        'operation': operation,
        'phase': 'prepared',
        'head_after': head_after,
        'stage_values': {key: (str(value) if isinstance(value, Path) else value) for key, value in stage_values.items()},
        'runtime_id': runtime_id,
        'listen': listen,
    }
    for key, value in stage_values.items():
        if key.endswith('_stage') and value is not None:
            journal[key] = str(value) if isinstance(value, Path) else value
    journal['record'] = record
    _write_journal(paths, journal)
    return journal


def _activate_stages(paths: dict[str, Path], journal: dict[str, object], activation: dict[str, object], listen: str, reload_unit: bool, runtime_stage: Path | None = None, runtime_final: Path | None = None, binary_stage: Path | None = None, unit_stage: Path | None = None, active_stage: Path | None = None) -> None:
    _write_journal(paths, {**journal, 'phase': 'activating'})
    stop_service(paths['unit'].name)
    if runtime_stage is not None and runtime_final is not None:
        if runtime_final.exists():
            _fail(f'runtime destination already exists: {runtime_final}')
        os.replace(runtime_stage, runtime_final)
        _fsync_directory(paths['runtimes'])
    if binary_stage is not None:
        _lstat_regular_or_absent(paths['binary'])
        os.replace(binary_stage, paths['binary'])
        _fsync_directory(paths['services'])
    if unit_stage is not None:
        _lstat_regular_or_absent(paths['unit'])
        os.replace(unit_stage, paths['unit'])
        _fsync_directory(paths['unit_dir'])
    if active_stage is not None:
        _lstat_regular_or_absent(paths['active'])
        os.replace(active_stage, paths['active'])
        _fsync_directory(paths['state'])
    if reload_unit:
        _systemctl(paths, 'daemon-reload', timeout=30)
    _set_enabled(paths, activation)
    _systemctl(paths, 'start', paths['unit'].name, timeout=45)
    _wait_running(paths)
    _health(paths, listen)
    if not activation.get('running'):
        stop_service(paths['unit'].name)
        _wait_stopped(paths)
    else:
        final = _unit_properties(paths)
        if final.get('ActiveState') != 'active' or final.get('SubState') != 'running':
            _fail('selected service stopped immediately after passing health checks')


def _commit(paths: dict[str, Path], journal: dict[str, object]) -> None:
    committed = {**journal, 'phase': 'committed'}
    _write_journal(paths, committed)
    _finalize_committed(paths, committed)


def _run_install(args: argparse.Namespace) -> dict[str, object]:
    _, service_file = _normalize_service_name(args.service_name)
    user = pwd.getpwnam(args.user)
    _require_user(user)
    if args.user != user.pw_name:
        _fail('pass the resolved Unix account name with --user')
    homebrew = _homebrew_path(args.homebrew)
    paths = _paths(homebrew, args.service_name, user)
    _assert_no_system_instance(paths)
    _assert_user_unit_source(paths)
    bundle, manifest = _validate_bundle(args.bundle, user)
    listen = args.listen
    _listen_endpoint(listen)
    cef_url = _validate_cef_url(args.cef_url)
    inject_frontend = args.inject_frontend == 'true'
    worker_mode = args.worker_mode
    if args.version is not None and args.version != manifest['release_version']:
        _fail('--version must match the bundle release identity')
    version = args.version or manifest['release_version']
    if not version or any(ord(character) < 32 or ord(character) == 127 for character in version):
        _fail('--version must be a non-empty printable release identifier')
    plugin_root = homebrew / 'plugins'
    if not plugin_root.exists():
        plugin_root.mkdir(mode=0o755)
    if not plugin_root.is_dir() or plugin_root.is_symlink():
        _fail(f'plugin root must be a real directory: {plugin_root}')
    transaction_id = _new_transaction_id()
    runtime_stage: Path | None = None
    binary_stage: Path | None = None
    unit_stage: Path | None = None
    active_stage: Path | None = None
    try:
        runtime_stage, runtime_final = _make_runtime(bundle, manifest, paths, transaction_id, user)
        binary_stage = _copy_stage_file(bundle / RUST_EXECUTABLE, paths['services'], f'.PluginLoader.{transaction_id}.')
        hashes = manifest['sha256']
        assert isinstance(hashes, dict)
        if _digest(binary_stage) != hashes[RUST_EXECUTABLE]:
            _fail('copied Rust runtime checksum mismatch')
        _verify_runtime(runtime_stage, manifest, user, binary_stage)
        if not binary_stage.stat().st_mode & 0o111:
            _fail('staged Rust runtime is not executable')
        unit_data = _generated_unit(
            paths,
            runtime_final,
            user.pw_name,
            _normalize_service_name(args.service_name)[0],
            listen,
            cef_url,
            inject_frontend,
            worker_mode,
            args.github_api,
            args.release_repository,
            args.testing_download_url,
            version,
        )
        unit_stage = _stage_bytes(unit_data, paths['unit_dir'], f'.{service_file}.{transaction_id}.')
        active_stage = _stage_bytes((json.dumps({'listen': listen, 'version': version, 'sdk_version': manifest['runtime']['decky_version']}, sort_keys=True) + '\n').encode(), paths['state'], f'.active.{transaction_id}.', 0o644)
        prior = _capture_activation(paths)
        activation = dict(prior)
        if prior.get('load_state') == 'not-found':
            activation.update({'running': True, 'enabled': True, 'enabled_scope': 'persistent'})
        transaction, record, captured = _snapshot_transaction(paths, transaction_id, 'install', version)
        stage_values: dict[str, Path | str | None] = {
            'binary_stage': binary_stage,
            'unit_stage': unit_stage,
            'active_stage': active_stage,
            'runtime_stage': runtime_stage,
        }
        parent = record.get('parent')
        head_after = transaction_id
        journal = _begin_journal(paths, transaction_id, 'install', record, stage_values, head_after, transaction_id, listen)
        journal['activation'] = activation
        _write_journal(paths, journal)
        try:
            _activate_stages(paths, journal, activation, listen, True, runtime_stage, runtime_final, binary_stage, unit_stage, active_stage)
            _commit(paths, journal)
        except BaseException as error:
            try:
                _recover(paths)
            except BaseException as rollback_error:
                raise InstallerError(f'installation failed ({error}); automatic recovery is pending ({rollback_error})') from error
            raise InstallerError(f'installation activation failed and was rolled back: {error}') from error
        return {
            'ok': True,
            'command': 'install',
            'service': service_file,
            'binary': str(paths['binary']),
            'runtime': str(runtime_final),
            'listen': listen,
            'version': version,
            'rollback_available': True,
            'previous_history': parent,
            'enabled': bool(activation.get('enabled')),
            'active': bool(activation.get('running')),
        }
    finally:
        for stage in (binary_stage, unit_stage, active_stage):
            if stage is not None:
                stage.unlink(missing_ok=True)
        if runtime_stage is not None and runtime_stage.exists():
            shutil.rmtree(runtime_stage, ignore_errors=True)


def _current_head(paths: dict[str, Path]) -> str | None:
    if not paths['head'].exists():
        return None
    value = _read_json(paths['head']).get('id')
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        _fail('installer history head is invalid')
    _current_record(paths, value)
    return value


def _run_rollback(args: argparse.Namespace) -> dict[str, object]:
    _, service_file = _normalize_service_name(args.service_name)
    paths = _paths(_homebrew_path(args.homebrew), args.service_name)
    _assert_no_system_instance(paths)
    _assert_user_unit_source(paths)
    _recover(paths)
    target_id = _current_head(paths)
    if target_id is None:
        _fail('there is no recoverable installation to roll back')
    target_record = _current_record(paths, target_id)
    _validate_snapshot(paths['history'] / target_id, target_record)
    target_activation = target_record.get('activation')
    target_files = target_record.get('files')
    if not isinstance(target_activation, dict) or not isinstance(target_files, dict):
        _fail('rollback transaction has incomplete state')
    transaction_id = _new_transaction_id()
    transaction, record, current_activation = _snapshot_transaction(paths, transaction_id, 'rollback', None)
    stage_values: dict[str, Path | str | None] = {'binary_stage': None, 'unit_stage': None, 'active_stage': None, 'runtime_stage': None}
    parent = target_record.get('parent')
    if parent is not None and not isinstance(parent, str):
        _fail('rollback parent history identifier is invalid')
    journal = _begin_journal(paths, transaction_id, 'rollback', record, stage_values, parent)
    journal.update({'restore_id': target_id, 'target_activation': target_activation})
    _write_journal(paths, journal)
    try:
        _write_journal(paths, {**journal, 'phase': 'activating'})
        stop_service(paths['unit'].name)
        target_directory = paths['history'] / target_id
        for key, target in _record_paths(paths).items():
            state = target_files[key]
            assert isinstance(state, dict)
            _restore_file(target, target_directory / 'before' / key, state)
        _systemctl(paths, 'daemon-reload', timeout=30)
        _apply_activation(paths, target_activation, check_running=False)
        if target_activation.get('running'):
            _health(paths)
        else:
            _wait_stopped(paths)
        _commit(paths, journal)
    except BaseException as error:
        try:
            _recover(paths)
        except BaseException as rollback_error:
            raise InstallerError(f'rollback failed ({error}); recovery is pending ({rollback_error})') from error
        raise InstallerError(f'rollback failed and the current installation was restored: {error}') from error
    return {
        'ok': True,
        'command': 'rollback',
        'service': service_file,
        'restored_transaction': target_id,
        'next_rollback': parent,
        'active': bool(target_activation.get('running')),
        'enabled': bool(target_activation.get('enabled')),
    }


def _systemd_run_path() -> str:
    for candidate in (Path('/usr/bin/systemd-run'), Path('/bin/systemd-run')):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.as_posix()
    _fail('systemd-run was not found')


def _request_directory(paths: dict[str, Any], request_id: str) -> Path:
    if not re.fullmatch(r'[0-9a-f]{32}', request_id):
        _fail('invalid updater handoff request identifier')
    directory = paths['queue'] / request_id
    try:
        info = directory.lstat()
    except OSError as error:
        raise InstallerError(f'updater handoff request is unavailable: {error}') from error
    owner = paths['user'].pw_uid
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
        _fail('updater handoff request directory is not a safe current-account directory')
    return directory


def _cleanup_completed_handoffs(paths: dict[str, Path]) -> None:
    cutoff = time.time() - 7 * 24 * 60 * 60
    for directory in paths['queue'].iterdir():
        if not re.fullmatch(r'[0-9a-f]{32}', directory.name) or directory.is_symlink() or not directory.is_dir():
            continue
        result = directory / 'result.json'
        try:
            if result.is_file() and result.stat().st_mtime < cutoff:
                shutil.rmtree(directory)
        except OSError:
            continue


def _assert_no_pending_handoff(paths: dict[str, Path]) -> None:
    for directory in paths['queue'].iterdir():
        if not re.fullmatch(r'[0-9a-f]{32}', directory.name) or directory.is_symlink() or not directory.is_dir():
            continue
        request_path = directory / 'request.json'
        result_path = directory / 'result.json'
        if not request_path.exists():
            shutil.rmtree(directory)
            _fsync_directory(paths['queue'])
            continue
        if result_path.exists():
            continue
        helper = _systemctl(
            paths,
            'show',
            f'decky-updater-{directory.name}.service',
            '--property=LoadState,ActiveState,MainPID',
            check=False,
            timeout=10,
        )
        state: dict[str, str] = {
            key: value for line in helper.stdout.splitlines() if '=' in line
            for key, value in [line.split('=', 1)]
        }
        if helper.returncode and not state:
            detail = helper.stderr.strip() or helper.stdout.strip()
            _fail(f'cannot inspect pending updater handoff {directory.name}: {detail}')
        if state.get('LoadState') == 'loaded' and state.get('ActiveState') not in ('inactive', 'failed', 'dead', 'not-found'):
            _fail('another updater activation handoff is still pending')
        message = 'independent updater activation helper exited before completing its handoff'
        _write_json(result_path, {'schema': 1, 'id': directory.name, 'ok': False, 'error': message})
        _cleanup_handoff_payloads(directory)

def _cleanup_handoff_payloads(directory: Path) -> None:
    bundle = directory / 'bundle'
    try:
        info = bundle.lstat()
    except FileNotFoundError:
        info = None
    if info is not None:
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(bundle)
        else:
            bundle.unlink()
    _fsync_directory(directory)


def _start_queued_activation(paths: dict[str, Any], request_id: str) -> dict[str, object]:
    unit = f'decky-updater-{request_id}'
    command = [
        _systemd_run_path(),
        '--user',
        '--no-block',
        '--collect',
        '--unit', unit,
        '--property=Type=oneshot',
        '--property=KillMode=control-group',
        '--property=TimeoutStartSec=infinity',
        sys.executable,
        str(Path(__file__).resolve(strict=True)),
        'activate-queued',
        '--homebrew', str(paths['homebrew']),
        '--service-name', paths['unit'].stem,
        '--request-id', request_id,
    ]
    started = _run_manager_command(paths, command, 15)
    if started.returncode:
        detail = started.stderr.strip() or started.stdout.strip() or f'exit status {started.returncode}'
        _fail(f'systemd-run could not queue user updater activation: {detail}')
    return {'request_id': request_id, 'unit': f'{unit}.service'}

def _sync_bundle_tree(bundle: Path) -> None:
    entries = sorted(bundle.rglob('*'), key=lambda path: len(path.parts), reverse=True)
    for path in (*entries, bundle):
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            _fsync_file(path)
        elif stat.S_ISDIR(info.st_mode):
            _fsync_directory(path)
        else:
            _fail(f'bundle contains an unsupported filesystem object: {path}')


def _prepare_install_handoff(args: argparse.Namespace, paths: dict[str, Path]) -> dict[str, object]:
    _normalize_service_name(args.service_name)
    if not args.version or any(ord(character) < 32 or ord(character) == 127 for character in args.version):
        _fail('--version must be a non-empty printable release identifier')
    user = pwd.getpwnam(args.user)
    _require_user(user)
    if args.user != user.pw_name:
        _fail('pass the resolved Unix account name with --user')
    bundle, manifest = _validate_bundle(args.bundle, user)
    if args.version != manifest['release_version']:
        _fail('--version must match the bundle release identity')
    cef_url = _validate_cef_url(args.cef_url)
    was_running = bool(_capture_activation(paths)['running'])
    _cleanup_completed_handoffs(paths)
    _assert_no_pending_handoff(paths)
    request_id = _new_transaction_id()
    directory = paths['queue'] / request_id
    directory.mkdir(mode=0o700)
    try:
        payload = directory / 'bundle'
        try:
            os.rename(bundle, payload)
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
            _copytree_checked(bundle, payload)
            _sync_bundle_tree(payload)
            shutil.rmtree(bundle)
            _fsync_directory(bundle.parent)
        _sync_bundle_tree(payload)
        _, copied_manifest = _validate_bundle(str(payload), user)
        if copied_manifest != manifest:
            _fail('queued Rust bundle manifest changed while staging')
        _write_json(directory / 'request.json', {
            'schema': 1,
            'id': request_id,
            'kind': 'install-bundle',
            'service': paths['unit'].name,
            'homebrew': str(paths['homebrew']),
            'version': args.version,
            'was_running': was_running,
            'user': user.pw_name,
            'listen': args.listen,
            'cef_url': cef_url,
            'inject_frontend': args.inject_frontend,
            'worker_mode': args.worker_mode,
            'github_api': args.github_api,
            'release_repository': args.release_repository,
            'testing_download_url': args.testing_download_url,
            'created_at': int(time.time()),
        })
        _fsync_directory(directory)
        _fsync_directory(paths['queue'])
        return _start_queued_activation(paths, request_id)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise

def _wait_for_handoff(paths: dict[str, Path], request_id: str, unit: str, command: str = 'queue-install-bundle') -> dict[str, object]:
    directory = _request_directory(paths, request_id)
    accepted = directory / 'accepted.json'
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if accepted.exists():
            acknowledgement = _read_json(accepted)
            if acknowledgement.get('schema') != 1 or acknowledgement.get('id') != request_id:
                _fail('updater handoff returned an invalid durable acknowledgement')
            return {'ok': True, 'accepted': True, 'command': command, 'request_id': request_id, 'unit': unit}
        result = directory / 'result.json'
        if result.exists():
            value = _read_json(result)
            _fail(f'updater activation helper failed before accepting its inputs: {value.get("error", "unknown failure")}')
        time.sleep(0.05)
    state = _systemctl(paths, 'show', unit, '--property=ActiveState,SubState,Result', check=False, timeout=10)
    detail = state.stdout.strip() or state.stderr.strip() or 'no service status was reported'
    _fail(f'independent updater activation service did not acknowledge staged inputs: {detail}')


def _release_activation_handoff(args: argparse.Namespace, paths: dict[str, Path]) -> dict[str, object]:
    request_id = args.request_id
    directory = _request_directory(paths, request_id)
    request = _read_json(directory / 'request.json')
    acknowledgement = _read_json(directory / 'accepted.json')
    if (
        request.get('schema') != 1
        or request.get('id') != request_id
        or request.get('service') != paths['unit'].name
        or request.get('homebrew') != str(paths['homebrew'])
        or acknowledgement.get('schema') != 1
        or acknowledgement.get('id') != request_id
    ):
        _fail('updater handoff is not durably accepted for this service')
    decision_path = directory / 'decision.json'
    if decision_path.exists():
        decision = _read_json(decision_path)
        if decision.get('schema') != 1 or decision.get('id') != request_id:
            _fail('updater handoff has an invalid release decision')
        if decision.get('state') == 'released':
            return {'ok': True, 'command': 'release-queued', 'request_id': request_id}
        _fail('updater handoff expired before release')
    if (directory / 'result.json').exists():
        result = _read_json(directory / 'result.json')
        _fail(f'updater handoff is no longer pending: {result.get("error", "activation already completed")}')
    service_status = _systemctl(
        paths,
        'show',
        f'decky-updater-{request_id}.service',
        '--property=LoadState,ActiveState',
        check=False,
        timeout=10,
    )
    helper_state: dict[str, str] = {
        key: value for line in service_status.stdout.splitlines() if '=' in line
        for key, value in [line.split('=', 1)]
    }
    if helper_state.get('LoadState') != 'loaded' or helper_state.get('ActiveState') not in ('activating', 'active'):
        message = 'independent updater activation helper is no longer running'
        _write_json(directory / 'result.json', {'schema': 1, 'id': request_id, 'ok': False, 'error': message})
        _cleanup_handoff_payloads(directory)
        _fail(message)
    decision = {'schema': 1, 'id': request_id, 'state': 'released', 'released_at': int(time.time())}
    if not _write_json_exclusive(decision_path, decision):
        previous = _read_json(decision_path)
        if previous.get('schema') != 1 or previous.get('id') != request_id:
            _fail('updater handoff has an invalid release decision')
        if previous.get('state') != 'released':
            _fail('updater handoff expired before release')
    return {'ok': True, 'command': 'release-queued', 'request_id': request_id}


def _run_queued_activation(args: argparse.Namespace, paths: dict[str, Path]) -> dict[str, object]:
    request_id = args.request_id
    directory = _request_directory(paths, request_id)
    bundle = directory / 'bundle'
    try:
        request = _read_json(directory / 'request.json')
        kind = request.get('kind')
        if (
            request.get('schema') != 1
            or request.get('id') != request_id
            or request.get('service') != paths['unit'].name
            or request.get('homebrew') != str(paths['homebrew'])
            or not isinstance(request.get('version'), str)
            or not request.get('version')
            or any(ord(character) < 32 or ord(character) == 127 for character in request['version'])
            or not isinstance(request.get('was_running'), bool)
        ):
            _fail('updater handoff request metadata is invalid')
        if kind != 'install-bundle':
            _fail('updater handoff request has an unsupported operation')
        config = (
            request.get('user'),
            request.get('listen'),
            request.get('cef_url'),
            request.get('inject_frontend'),
            request.get('worker_mode'),
            request.get('github_api'),
            request.get('release_repository'),
            request.get('testing_download_url'),
        )
        if not all(isinstance(value, str) and value for value in config):
            _fail('Rust bundle handoff configuration is incomplete')
        if request['inject_frontend'] not in ('true', 'false') or request['worker_mode'] not in ('fork', 'exec'):
            _fail('Rust bundle handoff configuration is unsupported')
        user = pwd.getpwnam(request['user'])
        _require_user(user)
        if user.pw_name != request['user']:
            _fail('Rust bundle handoff has an invalid service account')
        _listen_endpoint(request['listen'])
        _validate_cef_url(request['cef_url'])
        if bundle.is_symlink() or not bundle.is_dir():
            _fail('updater Rust bundle payload is missing or unsafe')
        _, manifest = _validate_bundle(str(bundle), user)
        if request['version'] != manifest['release_version']:
            _fail('updater handoff version does not match its bundle release identity')
        decision_path = directory / 'decision.json'
        if decision_path.exists():
            decision = _read_json(decision_path)
            if decision.get('schema') != 1 or decision.get('id') != request_id or decision.get('state') != 'released':
                _fail('updater handoff was expired or has an invalid release decision')
        accepted = directory / 'accepted.json'
        acknowledgement = {'schema': 1, 'id': request_id, 'accepted_at': int(time.time())}
        if not _write_json_exclusive(accepted, acknowledgement):
            previous = _read_json(accepted)
            if previous.get('schema') != 1 or previous.get('id') != request_id:
                _fail('updater handoff has an invalid durable acknowledgement')
        deadline = time.monotonic() + 60
        while True:
            if decision_path.exists():
                decision = _read_json(decision_path)
                if decision.get('schema') != 1 or decision.get('id') != request_id:
                    _fail('updater handoff release decision is invalid')
                if decision.get('state') == 'released':
                    break
                if decision.get('state') == 'expired':
                    _fail('updater handoff expired before release')
                _fail('updater handoff has an unsupported release decision')
            if time.monotonic() >= deadline:
                if _write_json_exclusive(decision_path, {
                    'schema': 1,
                    'id': request_id,
                    'state': 'expired',
                    'expired_at': int(time.time()),
                }):
                    _fail('updater handoff expired before caller release')
                continue
            time.sleep(0.05)
        descriptor = _locked(paths)
        try:
            _recover(paths)
            if request['was_running']:
                # Restart=always can still be activating after the caller is killed.
                # Wait for that transition before taking the rollback snapshot.
                _wait_running(paths, timeout=45)
            result = _run_install(argparse.Namespace(
                bundle=str(bundle),
                version=request['version'],
                homebrew=str(paths['homebrew']),
                user=request['user'],
                service_name=paths['unit'].stem,
                listen=request['listen'],
                cef_url=request['cef_url'],
                inject_frontend=request['inject_frontend'],
                worker_mode=request['worker_mode'],
                github_api=request['github_api'],
                release_repository=request['release_repository'],
                testing_download_url=request['testing_download_url'],
            ))
            _write_json(directory / 'result.json', {'schema': 1, 'id': request_id, 'ok': True, 'result': result})
            return {'ok': True, 'command': 'activate-queued', 'request_id': request_id}
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
    except BaseException as error:
        if not (directory / 'result.json').exists():
            _write_json(directory / 'result.json', {'schema': 1, 'id': request_id, 'ok': False, 'error': str(error)})
        raise
    finally:
        _cleanup_handoff_payloads(directory)

def _run_status(args: argparse.Namespace) -> dict[str, object]:
    _, service_file = _normalize_service_name(args.service_name)
    paths = _paths(_homebrew_path(args.homebrew), args.service_name)
    _assert_no_system_instance(paths)
    _assert_user_unit_source(paths)
    _recover(paths)
    properties = _unit_properties(paths)
    enabled, scope = _enabled_state(paths, properties.get('LoadState', 'not-found'))
    head = _current_head(paths)
    history: list[dict[str, object]] = []
    if paths['history'].is_dir():
        for entry in sorted(paths['history'].iterdir(), key=lambda item: item.name, reverse=True):
            if not re.fullmatch(r'[0-9a-f]{32}', entry.name) or not entry.is_dir() or entry.is_symlink():
                continue
            try:
                record = _current_record(paths, entry.name)
            except InstallerError:
                continue
            history.append({
                'id': entry.name,
                'operation': record.get('operation'),
                'version': record.get('version'),
                'parent': record.get('parent'),
                'created_at': record.get('created_at'),
                'current_head': entry.name == head,
            })
    return {
        'ok': True,
        'command': 'status',
        'service': service_file,
        'homebrew': str(paths['homebrew']),
        'binary': {'path': str(paths['binary']), 'exists': paths['binary'].is_file(), 'sha256': _digest(paths['binary']) if paths['binary'].is_file() else None},
        'unit': {'path': str(paths['unit']), 'exists': paths['unit'].is_file(), 'load_state': properties.get('LoadState', 'not-found'), 'active_state': properties.get('ActiveState', 'inactive'), 'sub_state': properties.get('SubState', ''), 'main_pid': properties.get('MainPID', '0'), 'enabled': enabled, 'enabled_scope': scope},
        'history_head': head,
        'rollback_available': head is not None,
        'history': history,
    }


_UNKNOWN_PLUGIN = '(unknown plugin)'
_PLUGIN_TITLE = re.compile(r'(.+) \(([^()]*)\)')
_SMAPS_FIELDS = {'Pss': 'pss_kb', 'Private_Dirty': 'private_dirty_kb'}


@dataclass(frozen=True)
class _StatSample:
    comm: str
    ppid: int
    cpu_ticks: int
    start_ticks: int


@dataclass(frozen=True)
class _ProcessInfo:
    pid: int
    ppid: int
    comm: str
    argv: tuple[str, ...]


def _parse_stat_sample(text: str) -> _StatSample:
    """Read /proc/<pid>/stat. The comm field may hold spaces and parentheses, so split at the last ')'."""
    head, separator, tail = text.rpartition(')')
    if not separator or ' (' not in head:
        raise ValueError('invalid process stat')
    fields = tail.split()
    if len(fields) < 20:
        raise ValueError('process stat is too short')
    # After comm, fields[0] is stat field 3: ppid=4, utime=14, stime=15, starttime=22.
    return _StatSample(head.partition(' (')[2], int(fields[1]), int(fields[11]) + int(fields[12]), int(fields[19]))


def _parse_smaps_rollup(text: str) -> dict[str, int | None]:
    """Read Pss and Private_Dirty (kB) from /proc/<pid>/smaps_rollup; a missing field is None."""
    found: dict[str, int | None] = {'pss_kb': None, 'private_dirty_kb': None}
    for line in text.splitlines():
        key, separator, rest = line.partition(':')
        field = _SMAPS_FIELDS.get(key)
        parts = rest.split()
        if field and separator and len(parts) == 2 and parts[1] == 'kB' and parts[0].isdecimal():
            found[field] = int(parts[0])
    return found


def _parse_cmdline(raw: bytes) -> tuple[str, ...]:
    return tuple(part for part in raw.decode('utf-8', 'replace').split('\0') if part)


def _worker_identity(argv: tuple[str, ...]) -> tuple[str, str] | None:
    """Plugin name from a worker command line, and where it came from.

    A running worker sets its title to "<plugin name> (<entry file>)", which replaces argv.
    An exec-mode worker that has not set its title yet still has --plugin-directory in argv.
    """
    if '--plugin-directory' in argv:
        index = argv.index('--plugin-directory')
        if index + 1 < len(argv) and argv[index + 1]:
            return argv[index + 1], 'plugin-directory'
    match = _PLUGIN_TITLE.fullmatch(' '.join(argv))
    if match:
        return match.group(1), 'title'
    return None


def _attribute_one(
    info: _ProcessInfo,
    is_backend: bool,
    parent: tuple[str, str | None, str | None] | None,
) -> tuple[str, str | None, str | None]:
    if is_backend:
        return 'backend', None, None
    if parent is None:
        return 'unattributed', None, None
    role = parent[0]
    if role in ('backend', 'shared launcher'):
        # The worker title rewrites argv into one space-joined string, so split it again.
        if role == 'backend' and '--launcher' in ' '.join(info.argv).split():
            return 'shared launcher', None, None
        identity = _worker_identity(info.argv)
        if identity:
            return 'plugin worker', identity[0], identity[1]
        if role == 'shared launcher':
            return 'plugin worker', _UNKNOWN_PLUGIN, 'unknown'
        return 'backend child', None, None
    if role in ('plugin worker', 'plugin child'):
        return 'plugin child', parent[1], parent[2]
    return role, None, None


def _attribute_processes(
    infos: dict[int, _ProcessInfo], main_pid: int,
) -> dict[int, tuple[str, str | None, str | None]]:
    """Give each process a (role, plugin, name source) from its place in the process tree.

    The backend is MainPID. A direct child of the backend that has --launcher is the shared launcher.
    A direct child of the backend (exec mode) or of the launcher (fork mode) that has a plugin identity
    is a plugin worker. Every deeper descendant belongs to the nearest worker above it.
    """
    result: dict[int, tuple[str, str | None, str | None]] = {}
    for start in infos:
        chain: list[int] = []
        pid = start
        while pid not in result:
            chain.append(pid)
            parent_pid = infos[pid].ppid
            if pid == main_pid or parent_pid not in infos or parent_pid in chain:
                break
            pid = parent_pid
        for pid in reversed(chain):
            if pid not in result:
                result[pid] = _attribute_one(infos[pid], pid == main_pid, result.get(infos[pid].ppid))
    return result


def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def total(key: str) -> int | float | None:
        values = [row[key] for row in rows if row[key] is not None]
        return sum(values) if values else None

    seconds = total('cpu_seconds_total')
    percent = total('cpu_percent')
    return {
        'process_count': len(rows),
        'pids': [row['pid'] for row in rows],
        'cpu_seconds_total': None if seconds is None else round(seconds, 2),
        'cpu_percent': None if percent is None else round(percent, 1),
        'pss_kb': total('pss_kb'),
        'private_dirty_kb': total('private_dirty_kb'),
        'memory_unreadable_processes': sum(1 for row in rows if row['pss_kb'] is None or row['private_dirty_kb'] is None),
    }


def _resource_report(
    infos: dict[int, _ProcessInfo],
    samples: dict[int, _StatSample],
    previous: dict[int, _StatSample],
    memory: dict[int, dict[str, int | None]],
    main_pid: int,
    clock_ticks: int,
    elapsed: float,
) -> dict[str, Any]:
    """Build per-process, per-plugin, per-role and whole-service resource numbers from parsed samples."""
    attribution = _attribute_processes(infos, main_pid)
    rows: list[dict[str, Any]] = []
    for pid in sorted(infos):
        info, sample = infos[pid], samples[pid]
        role, plugin, source = attribution[pid]
        before = previous.get(pid)
        percent = None
        if elapsed > 0 and before is not None and before.start_ticks == sample.start_ticks:
            percent = max(0.0, (sample.cpu_ticks - before.cpu_ticks) / clock_ticks / elapsed * 100)
        used = memory.get(pid) or {}
        rows.append({
            'pid': pid,
            'ppid': info.ppid,
            'role': role,
            'plugin': plugin,
            'name_source': source,
            'comm': info.comm,
            'command_line': ' '.join(info.argv),
            'cpu_seconds_total': sample.cpu_ticks / clock_ticks,
            'cpu_percent': percent,
            'pss_kb': used.get('pss_kb'),
            'private_dirty_kb': used.get('private_dirty_kb'),
        })
    plugins = sorted({row['plugin'] for row in rows if row['plugin'] is not None})
    for row in rows:
        row['cpu_seconds_total'] = round(row['cpu_seconds_total'], 2)
        if row['cpu_percent'] is not None:
            row['cpu_percent'] = round(row['cpu_percent'], 1)
    return {
        'processes': rows,
        'plugins': [{'name': name, **_totals([row for row in rows if row['plugin'] == name])} for name in plugins],
        'by_role': {
            role: _totals([row for row in rows if row['role'] == role])
            for role in sorted({row['role'] for row in rows})
        },
        'service': _totals(rows),
    }


def _read_stat_sample(pid: int) -> _StatSample | None:
    try:
        return _parse_stat_sample(Path(f'/proc/{pid}/stat').read_text())
    except (OSError, ValueError):
        return None


def _service_resources(paths: dict[str, Path], sample_seconds: float) -> dict[str, Any]:
    """Per-process CPU and memory of the selected service from readable /proc files only."""
    def unavailable(reason: str) -> dict[str, Any]:
        return {'available': False, 'reason': reason, 'sample_seconds': sample_seconds}

    try:
        properties = _unit_properties(paths)
        control_group = properties.get('ControlGroup') or ''
        main_pid = int(properties.get('MainPID', '0') or '0')
    except (InstallerError, OSError, ValueError) as error:
        return unavailable(f'cannot read service state: {error}')
    if main_pid <= 0 or not control_group:
        return unavailable('the service is not running')
    clock_ticks = os.sysconf('SC_CLK_TCK')
    try:
        previous: dict[int, _StatSample] = {}
        window_start = 0.0
        if sample_seconds > 0:
            for pid in cgroup_processes(control_group):
                sample = _read_stat_sample(pid)
                if sample is not None:
                    previous[pid] = sample
            window_start = time.monotonic()
            time.sleep(sample_seconds)
        members = cgroup_processes(control_group)
    except (OSError, ValueError) as error:
        return unavailable(f'cannot observe service processes: {error}')
    samples: dict[int, _StatSample] = {}
    for pid, member in members.items():
        sample = _read_stat_sample(pid)
        if sample is not None and sample.start_ticks == member.start_ticks:
            samples[pid] = sample
    elapsed = time.monotonic() - window_start if sample_seconds > 0 else 0.0
    infos: dict[int, _ProcessInfo] = {}
    memory: dict[int, dict[str, int | None]] = {}
    for pid, sample in samples.items():
        try:
            argv = _parse_cmdline(Path(f'/proc/{pid}/cmdline').read_bytes())
        except OSError:
            argv = ()
        infos[pid] = _ProcessInfo(pid, sample.ppid, sample.comm, argv)
        try:
            memory[pid] = _parse_smaps_rollup(Path(f'/proc/{pid}/smaps_rollup').read_text())
        except (OSError, UnicodeError):
            pass
    if not infos:
        return unavailable('no service process could be read')
    report = _resource_report(infos, samples, previous, memory, main_pid, clock_ticks, elapsed)
    return {
        'available': True,
        'reason': None,
        'sample_seconds': sample_seconds,
        'window_seconds': round(elapsed, 3),
        'clock_ticks_per_second': clock_ticks,
        'control_group': control_group,
        'main_pid': main_pid,
        **report,
    }


def _legacy_migration_directory(
    homebrew: Path,
    service_name: str,
    *,
    create: bool = False,
    user_uid: int | None = None,
) -> Path:
    _, unit_filename = _normalize_service_name(service_name)
    uid = os.getuid() if user_uid is None else user_uid
    directory = LEGACY_MIGRATION_ROOT / str(uid) / unit_filename
    if create:
        path, descriptors = _open_legacy_store(uid, unit_filename, create=True)
        try:
            return path
        finally:
            _close_legacy_descriptors(descriptors)
    return directory


def _write_legacy_record(path: Path, record: dict[str, Any]) -> None:
    try:
        uid = int(record['user_uid'])
        service_name = str(record['service'])
        homebrew = str(record['homebrew'])
        phase = str(record['phase'])
    except (KeyError, ValueError, TypeError) as error:
        raise InstallerError(f'cannot write incomplete legacy migration record: {error}') from error
    _, descriptors = _open_legacy_store(uid, service_name, create=False)
    try:
        if path != LEGACY_MIGRATION_ROOT / str(uid) / service_name / 'private' / 'record.json':
            _fail('legacy migration record path differs from its protected archive')
        public = {
            'schema': 1,
            'user_uid': uid,
            'service': service_name,
            'homebrew': homebrew,
            'phase': phase,
        }
        data = (json.dumps(record, sort_keys=True, indent=2) + '\n').encode('utf-8')
        _atomic_write_at(descriptors['private'], 'record.json', data, 0o600)
        _atomic_write_at(
            descriptors['service'],
            'status.json',
            (json.dumps(public, sort_keys=True, indent=2) + '\n').encode('utf-8'),
            0o644,
        )
    finally:
        _close_legacy_descriptors(descriptors)


def _read_legacy_record(
    homebrew: Path,
    service_name: str,
    user_uid: int | None = None,
) -> tuple[Path, dict[str, Any]]:
    _, unit_filename = _normalize_service_name(service_name)
    uid = os.getuid() if user_uid is None else user_uid
    directory, descriptors = _open_legacy_store(uid, unit_filename, create=False)
    try:
        try:
            public = _read_json_at(descriptors['service'], 'status.json', mode=0o644)
        except FileNotFoundError:
            public = None
        record = _read_json_at(descriptors['private'], 'record.json', mode=0o600, private=True)
    finally:
        _close_legacy_descriptors(descriptors)
    phases = (
        'prepared',
        'system-stopped',
        'parked',
        'binary-removal-pending',
        'binary-removed',
        'services-handoff-pending',
        'services-user-owned',
        'installed',
        'restoring',
        'services-lock-pending',
        'restored',
        'forgetting',
        'forgotten',
    )
    if (
        record.get('phase') not in phases
        or record.get('schema') != 1
        or record.get('service') != unit_filename
        or record.get('homebrew') != str(homebrew)
        or not isinstance(record.get('user'), str)
        or record.get('user_uid') != uid
        or not isinstance(record.get('activation'), dict)
        or not isinstance(record.get('binary'), dict)
        or not isinstance(record.get('services_directory'), dict)
        or not isinstance(record.get('files'), list)
        or not isinstance(record.get('user_unit_directory'), str)
    ):
        _fail('legacy migration snapshot metadata is invalid')
    private_phase = phases.index(record['phase'])
    if public is not None:
        if (
            set(public) != {'schema', 'user_uid', 'service', 'homebrew', 'phase'}
            or public.get('schema') != 1
            or public.get('user_uid') != uid
            or public.get('service') != unit_filename
            or public.get('homebrew') != str(homebrew)
            or public.get('phase') not in phases
        ):
            _fail('legacy migration public status is invalid')
        public_phase = phases.index(public['phase'])
        if public_phase > private_phase:
            _fail('legacy migration public status is newer than its protected record')
    else:
        public_phase = -1
    if public_phase < private_phase:
        _write_legacy_record(directory / 'private' / 'record.json', record)
    return directory, record


def _legacy_store_preflight(user_uid: int, service_name: str) -> None:
    _, unit_filename = _normalize_service_name(service_name)
    current = Path('/')
    missing = False
    for component in (*LEGACY_MIGRATION_ROOT.relative_to('/').parts, str(user_uid), unit_filename):
        current = current / component
        if missing:
            continue
        try:
            info = current.lstat()
        except FileNotFoundError:
            missing = True
            continue
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & 0o022
        ):
            _fail(f'legacy migration target is not root-protected: {current}')


def _install_legacy_helpers(directory: Path, user_uid: int, service_name: str) -> None:
    _, descriptors = _open_legacy_store(user_uid, service_name, create=True)
    try:
        for source in (
            Path(__file__),
            Path(__file__).with_name('process_observer.py'),
        ):
            name = source.name
            try:
                before = source.lstat()
                source_fd = os.open(
                    source,
                    os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NOATIME', 0),
                )
            except OSError as error:
                raise InstallerError(f'cannot read trusted legacy helper source {source}: {error}') from error
            temporary = f'.{name}.{uuid.uuid4().hex}.tmp'
            destination_fd = -1
            try:
                opened = os.fstat(source_fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                    or opened.st_size <= 0
                ):
                    _fail(f'legacy helper source is not a stable regular file: {source}')
                destination_fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
                    0o755 if name == 'install.py' else 0o644,
                    dir_fd=descriptors['helper'],
                )
                mode = 0o755 if name == 'install.py' else 0o644
                os.fchown(destination_fd, 0, 0)
                os.fchmod(destination_fd, mode)
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        count = os.write(destination_fd, view)
                        view = view[count:]
                after = os.fstat(source_fd)
                if (
                    (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                ):
                    _fail(f'legacy helper source changed while being copied: {source}')
                os.fsync(destination_fd)
                os.replace(
                    temporary,
                    name,
                    src_dir_fd=descriptors['helper'],
                    dst_dir_fd=descriptors['helper'],
                )
                os.fsync(descriptors['helper'])
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=descriptors['helper'])
                except OSError:
                    pass
                raise
            finally:
                if destination_fd >= 0:
                    os.close(destination_fd)
                os.close(source_fd)
    finally:
        _close_legacy_descriptors(descriptors)


def _clear_directory_fd(directory_fd: int) -> None:
    """Remove every entry below an opened directory without following symlinks."""
    for name in os.listdir(directory_fd):
        if name in ('', '.', '..') or '/' in name:
            _fail('legacy archive contains an unsafe name')
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
                dir_fd=directory_fd,
            )
            try:
                child_info = os.fstat(child_fd)
                if (info.st_dev, info.st_ino) != (child_info.st_dev, child_info.st_ino):
                    _fail('legacy archive directory changed while opening')
                _clear_directory_fd(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            os.unlink(name, dir_fd=directory_fd)
        else:
            _fail(f'legacy archive contains an unsupported object: {name}')
    os.fsync(directory_fd)


def _discard_incomplete_legacy_snapshots(private_fd: int) -> None:
    try:
        root_info = os.stat('snapshots', dir_fd=private_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != 0
        or stat.S_IMODE(root_info.st_mode) != 0o700
    ):
        _fail('incomplete legacy snapshot archive is not root-private')
    root_fd = os.open(
        'snapshots',
        os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
        dir_fd=private_fd,
    )
    try:
        opened = os.fstat(root_fd)
        if (root_info.st_dev, root_info.st_ino) != (opened.st_dev, opened.st_ino):
            _fail('incomplete legacy snapshot archive changed while opening')
        _clear_directory_fd(root_fd)
        current = os.stat('snapshots', dir_fd=private_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            _fail('incomplete legacy snapshot archive changed during cleanup')
        os.rmdir('snapshots', dir_fd=private_fd)
        os.fsync(private_fd)
    finally:
        os.close(root_fd)


def _open_services_directory(
    homebrew: Path,
    user: pwd.struct_passwd,
) -> tuple[Path, int, int, os.stat_result]:
    try:
        homebrew_fd = os.open(
            homebrew,
            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
        )
        services_fd = os.open(
            'services',
            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
            dir_fd=homebrew_fd,
        )
    except OSError as error:
        try:
            os.close(homebrew_fd)
        except (UnboundLocalError, OSError):
            pass
        raise InstallerError(f'legacy services directory is unavailable: {error}') from error
    info = os.fstat(services_fd)
    try:
        path_info = os.stat('services', dir_fd=homebrew_fd, follow_symlinks=False)
    except OSError as error:
        os.close(services_fd)
        os.close(homebrew_fd)
        raise InstallerError(f'legacy services directory changed while opening: {error}') from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)
        or info.st_uid not in (0, user.pw_uid)
        or stat.S_IMODE(info.st_mode) & (0o022 | 0o6000)
    ):
        os.close(services_fd)
        os.close(homebrew_fd)
        _fail(f'legacy services directory has unsafe ownership or permissions: {homebrew / "services"}')
    return homebrew / 'services', homebrew_fd, services_fd, info


def _metadata_from_fd(descriptor: int, result: os.stat_result | None = None) -> dict[str, object]:
    info = result or os.fstat(descriptor)
    attributes: dict[str, str] = {}
    if hasattr(os, 'listxattr') and hasattr(os, 'getxattr'):
        try:
            for name in os.listxattr(descriptor):
                attributes[name] = base64.b64encode(os.getxattr(descriptor, name)).decode('ascii')
        except OSError as error:
            if error.errno not in (getattr(os, 'ENOTSUP', 95), getattr(os, 'EOPNOTSUPP', 95)):
                raise InstallerError(f'cannot capture descriptor metadata: {error}') from error
    return {
        'mode': stat.S_IMODE(info.st_mode),
        'uid': info.st_uid,
        'gid': info.st_gid,
        'atime_ns': info.st_atime_ns,
        'mtime_ns': info.st_mtime_ns,
        'xattrs': attributes,
    }


def _copy_descriptor(source_fd: int, destination: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    target_fd = os.open(destination, flags, 0o600)
    try:
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                count = os.write(target_fd, view)
                view = view[count:]
        os.fsync(target_fd)
    finally:
        os.close(target_fd)


def _snapshot_binary_at(services_fd: int, destination: Path) -> tuple[dict[str, Any], list[int] | None]:
    try:
        before = os.stat('PluginLoader', dir_fd=services_fd, follow_symlinks=False)
    except FileNotFoundError:
        return {'exists': False}, None
    if not stat.S_ISREG(before.st_mode):
        _fail('legacy backend binary is not a regular file')
    source_fd = os.open(
        'PluginLoader',
        os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NOATIME', 0),
        dir_fd=services_fd,
    )
    try:
        opened = os.fstat(source_fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            _fail('legacy backend binary changed while opening its rollback snapshot')
        metadata = _metadata_from_fd(source_fd, opened)
        _copy_descriptor(source_fd, destination)
        _fsync_directory(destination.parent)
        after = os.fstat(source_fd)
        current = os.stat('PluginLoader', dir_fd=services_fd, follow_symlinks=False)
        if (
            (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            _fail('legacy backend binary changed while its rollback snapshot was captured')
        return {
            'exists': True,
            'metadata': metadata,
            'sha256': _digest(destination),
        }, [opened.st_dev, opened.st_ino]
    finally:
        os.close(source_fd)
def _metadata_for_symlink_at(parent_fd: int, name: str, info: os.stat_result) -> dict[str, object]:
    attributes: dict[str, str] = {}
    if hasattr(os, 'listxattr') and hasattr(os, 'getxattr'):
        path = f'/proc/self/fd/{parent_fd}/{name}'
        try:
            for attribute in os.listxattr(path, follow_symlinks=False):
                attributes[attribute] = base64.b64encode(
                    os.getxattr(path, attribute, follow_symlinks=False)
                ).decode('ascii')
        except OSError as error:
            if error.errno not in (getattr(os, 'ENOTSUP', 95), getattr(os, 'EOPNOTSUPP', 95)):
                raise InstallerError(f'cannot capture symlink metadata for {name}: {error}') from error
    return {
        'mode': stat.S_IMODE(info.st_mode),
        'uid': info.st_uid,
        'gid': info.st_gid,
        'atime_ns': info.st_atime_ns,
        'mtime_ns': info.st_mtime_ns,
        'xattrs': attributes,
    }


def _snapshot_legacy_file(source: Path, destination: Path) -> dict[str, Any]:
    parent_fd = os.open(
        source.parent,
        os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
    )
    try:
        parent_info = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != 0 or parent_info.st_mode & 0o022:
            _fail(f'legacy system unit parent is not root-protected: {source.parent}')
        info = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)) or info.st_uid != 0:
            _fail(f'legacy system unit path is not a root-owned regular file or symlink: {source}')
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        identity = [info.st_dev, info.st_ino]
        if stat.S_ISREG(info.st_mode):
            source_fd = os.open(
                source.name,
                os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NOATIME', 0),
                dir_fd=parent_fd,
            )
            try:
                opened = os.fstat(source_fd)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    _fail(f'legacy system unit changed while opening its snapshot: {source}')
                metadata = _metadata_from_fd(source_fd, opened)
                _copy_descriptor(source_fd, destination)
                _fsync_directory(destination.parent)
                after = os.fstat(source_fd)
                current = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                ):
                    _fail(f'legacy system unit changed while its snapshot was captured: {source}')
            finally:
                os.close(source_fd)
            return {
                'kind': 'file',
                'metadata': metadata,
                'sha256': _digest(destination),
                'identity': identity,
            }
        target = os.readlink(source.name, dir_fd=parent_fd)
        metadata = _metadata_for_symlink_at(parent_fd, source.name, info)
        current = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or os.readlink(
            source.name, dir_fd=parent_fd
        ) != target:
            _fail(f'legacy system symlink changed while its snapshot was captured: {source}')
        os.symlink(target, destination)
        os.chown(destination, info.st_uid, info.st_gid, follow_symlinks=False)
        os.utime(destination, ns=(info.st_atime_ns, info.st_mtime_ns), follow_symlinks=False)
        _fsync_directory(destination.parent)
        return {'kind': 'symlink', 'metadata': metadata, 'target': target, 'identity': identity}
    finally:
        os.close(parent_fd)
def _legacy_system_sources(properties: dict[str, str]) -> list[Path]:
    fragment = properties.get('FragmentPath', '')
    if not fragment:
        _fail('legacy system service has no persistent unit file to preserve')
    values = [fragment]
    values.extend(shlex.split(properties.get('DropInPaths', '')))
    sources: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        source = Path(value)
        if not source.is_absolute() or '..' in source.parts or source.name in ('', '.', '..'):
            _fail(f'legacy system unit reported an unsafe file path: {value!r}')
        parent_fd = _open_root_directory(source.parent)
        try:
            parent_info = os.fstat(parent_fd)
            info = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise InstallerError(f'legacy system unit file is unavailable: {error}') from error
        finally:
            os.close(parent_fd)
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_mode & 0o022
            or info.st_uid != 0
            or not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode))
        ):
            _fail(f'legacy system unit path is not a root-owned protected file: {source}')
        if source not in seen:
            seen.add(source)
            sources.append(source)
    return sources




def _renameat2(
    source_directory: int,
    source_name: str,
    destination_directory: int,
    destination_name: str,
    flags: int,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, 'renameat2', None)
    if function is None:
        _fail('atomic no-replace publication requires Linux renameat2')
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    if function(
        source_directory,
        os.fsencode(source_name),
        destination_directory,
        os.fsencode(destination_name),
        flags,
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination_name)


def _open_root_directory(path: Path) -> int:
    if not path.is_absolute() or '..' in path.parts:
        _fail(f'legacy restore parent is not an absolute safe path: {path}')
    descriptor = os.open('/', os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    try:
        for component in path.relative_to('/').parts:
            child = _open_directory_at(descriptor, component, create=False, mode=0o755)
            if descriptor != 0:
                os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _apply_metadata_fd(descriptor: int, metadata: dict[str, Any]) -> None:
    try:
        os.fchown(descriptor, int(metadata['uid']), int(metadata['gid']))
        xattrs = metadata.get('xattrs', {})
        if not isinstance(xattrs, dict):
            _fail('invalid xattr snapshot')
        if xattrs and not hasattr(os, 'setxattr'):
            _fail('this platform cannot restore captured extended attributes')
        for name, encoded in xattrs.items():
            os.setxattr(descriptor, str(name), base64.b64decode(str(encoded), validate=True))
        os.fchmod(descriptor, int(metadata['mode']))
        os.utime(
            descriptor,
            ns=(int(metadata['atime_ns']), int(metadata['mtime_ns'])),
        )
        os.fsync(descriptor)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise InstallerError(f'cannot restore descriptor metadata: {error}') from error


def _metadata_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in ('uid', 'gid', 'mode', 'mtime_ns', 'xattrs'))


def _digest_descriptor(descriptor: int) -> str:
    result = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            return result.hexdigest()
        result.update(chunk)
        offset += len(chunk)


def _object_matches_snapshot(parent_fd: int, name: str, item: dict[str, Any]) -> bool:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    metadata = item.get('metadata')
    if not isinstance(metadata, dict):
        _fail(f'legacy migration has invalid metadata for {name}')
    if item.get('kind') == 'file':
        if not stat.S_ISREG(info.st_mode):
            return False
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NOATIME', 0),
            dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return False
            return (
                _digest_descriptor(descriptor) == item.get('sha256')
                and _metadata_matches(_metadata_from_fd(descriptor, opened), metadata)
            )
        finally:
            os.close(descriptor)
    if item.get('kind') == 'symlink':
        if not stat.S_ISLNK(info.st_mode):
            return False
        target = os.readlink(name, dir_fd=parent_fd)
        current_metadata = _metadata_for_symlink_at(parent_fd, name, info)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return (
            target == item.get('target')
            and (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino)
            and _metadata_matches(current_metadata, metadata)
        )
    _fail(f'legacy migration has an unsupported saved file type for {name}')


def _create_restore_stage(parent_fd: int) -> tuple[str, int]:
    for _ in range(8):
        name = f'.decky-legacy-restore-{uuid.uuid4().hex}'
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
            dir_fd=parent_fd,
        )
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o700)
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(descriptor)
            _fail('legacy restore staging directory is not root-private')
        return name, descriptor
    _fail('cannot allocate a unique legacy restore staging directory')

def _remove_empty_restore_stage(parent_fd: int, name: str) -> None:
    if not name.startswith('.decky-legacy-restore-') or '/' in name:
        _fail('legacy restore journal contains an unsafe staging directory')
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return
    try:
        info = os.fstat(descriptor)
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700 or os.listdir(descriptor):
            _fail('legacy restore staging directory is not empty and root-private')
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if [current.st_dev, current.st_ino] != [info.st_dev, info.st_ino]:
            _fail('legacy restore staging directory changed during cleanup')
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(descriptor)


def _stage_legacy_object(
    snapshot: Path,
    item: dict[str, Any],
    directory_fd: int,
    name: str,
) -> list[int]:
    metadata = item.get('metadata')
    if not isinstance(metadata, dict):
        _fail('legacy migration has invalid staged file metadata')
    if item.get('kind') == 'file':
        if _digest(snapshot) != item.get('sha256'):
            _fail(f'legacy migration snapshot checksum changed: {snapshot}')
        source_fd = os.open(snapshot, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            source_info = os.fstat(source_fd)
            if not stat.S_ISREG(source_info.st_mode):
                _fail(f'legacy migration snapshot is not a regular file: {snapshot}')
            destination_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
                0o600,
                dir_fd=directory_fd,
            )
            try:
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        count = os.write(destination_fd, view)
                        view = view[count:]
                _apply_metadata_fd(destination_fd, metadata)
                staged = os.fstat(destination_fd)
            finally:
                os.close(destination_fd)
        finally:
            os.close(source_fd)
        os.fsync(directory_fd)
        return [staged.st_dev, staged.st_ino]
    if item.get('kind') == 'symlink':
        target = item.get('target')
        if not isinstance(target, str):
            _fail('legacy migration symlink target is invalid')
        os.symlink(target, name, dir_fd=directory_fd)
        try:
            os.chown(name, int(metadata['uid']), int(metadata['gid']), dir_fd=directory_fd, follow_symlinks=False)
            xattrs = metadata.get('xattrs', {})
            if not isinstance(xattrs, dict):
                _fail('legacy symlink metadata is invalid')
            path = f'/proc/self/fd/{directory_fd}/{name}'
            for attribute, encoded in xattrs.items():
                os.setxattr(
                    path,
                    str(attribute),
                    base64.b64decode(str(encoded), validate=True),
                    follow_symlinks=False,
                )
            os.utime(
                name,
                ns=(int(metadata['atime_ns']), int(metadata['mtime_ns'])),
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            staged = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            os.fsync(directory_fd)
            return [staged.st_dev, staged.st_ino]
        except BaseException:
            os.unlink(name, dir_fd=directory_fd)
            raise
    _fail('legacy migration has an unsupported saved file type')


def _restore_legacy_file(
    source: Path,
    snapshot: Path,
    item: dict[str, Any],
    record: dict[str, Any],
    record_path: Path,
    item_index: int,
) -> None:
    identity = item.get('identity')
    if (
        not isinstance(identity, list)
        or len(identity) != 2
        or any(type(value) is not int for value in identity)
    ):
        _fail(f'legacy migration has invalid inode identity for {source}')
    parent_fd = _open_root_directory(source.parent)
    try:
        journal = item.get('restore_journal')
        if journal is not None and not isinstance(journal, dict):
            _fail(f'legacy migration restore journal is invalid for {source}')
        try:
            current = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None:
            current_identity = [current.st_dev, current.st_ino]
            if current_identity == identity:
                if not _object_matches_snapshot(parent_fd, source.name, item):
                    _fail(f'legacy unit file changed in place after migration: {source}')
                item['restore_journal'] = {
                    'state': 'restored-original',
                    'restored_identity': identity,
                }
                _write_legacy_record(record_path, record)
                return
            if isinstance(journal, dict) and current_identity == journal.get('staged_identity'):
                if not _object_matches_snapshot(parent_fd, source.name, item):
                    _fail(f'legacy restore publication changed after interruption: {source}')
                if journal.get('state') not in {'staged', 'published'}:
                    _fail(f'legacy restore journal state is invalid: {source}')
                journal['state'] = 'published'
                journal['restored_identity'] = current_identity
                _write_legacy_record(record_path, record)
                stage_name = journal.get('stage_directory')
                if isinstance(stage_name, str):
                    _remove_empty_restore_stage(parent_fd, stage_name)
                return
            _fail(f'legacy unit path changed after migration; refusing to replace an outside inode: {source}')

        parked_path = item.get('parked_path')
        if isinstance(parked_path, str):
            parked = Path(parked_path)
            if parked.parent != source.parent or parked.name == source.name:
                _fail(f'legacy migration contains an unsafe parked path for {source}')
            try:
                parked_info = os.stat(parked.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                parked_info = None
            if parked_info is not None:
                if [parked_info.st_dev, parked_info.st_ino] != identity:
                    _fail(f'parked legacy unit file changed; refusing to restore {source}')
                _renameat2(parent_fd, parked.name, parent_fd, source.name, 1)
                os.fsync(parent_fd)
                if not _object_matches_snapshot(parent_fd, source.name, item):
                    _fail(f'parked legacy unit file does not match its snapshot: {source}')
                item['restore_journal'] = {
                    'state': 'restored-parked',
                    'restored_identity': identity,
                }
                _write_legacy_record(record_path, record)
                return

        stage_name: str
        stage_fd: int
        staged_name: str
        staged_identity: list[int]
        if isinstance(journal, dict) and isinstance(journal.get('stage_directory'), str):
            stage_name = journal['stage_directory']
            staged_name = str(journal.get('stage_name', ''))
            try:
                stage_fd = os.open(
                    stage_name,
                    os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
                    dir_fd=parent_fd,
                )
                info = os.fstat(stage_fd)
                if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
                    os.close(stage_fd)
                    _fail(f'legacy restore stage is not root-private: {source.parent / stage_name}')
                staged_info = os.stat(staged_name, dir_fd=stage_fd, follow_symlinks=False)
                staged_identity = [staged_info.st_dev, staged_info.st_ino]
                if staged_identity != journal.get('staged_identity'):
                    os.close(stage_fd)
                    _fail(f'legacy restore stage changed after journaling: {source}')
            except FileNotFoundError:
                stage_name, stage_fd = _create_restore_stage(parent_fd)
                staged_name = f'restored-{uuid.uuid4().hex}'
                staged_identity = _stage_legacy_object(snapshot, item, stage_fd, staged_name)
                journal = {
                    'state': 'staged',
                    'stage_directory': stage_name,
                    'stage_name': staged_name,
                    'staged_identity': staged_identity,
                }
                item['restore_journal'] = journal
                _write_legacy_record(record_path, record)
        else:
            stage_name, stage_fd = _create_restore_stage(parent_fd)
            staged_name = f'restored-{uuid.uuid4().hex}'
            staged_identity = _stage_legacy_object(snapshot, item, stage_fd, staged_name)
            journal = {
                'state': 'staged',
                'stage_directory': stage_name,
                'stage_name': staged_name,
                'staged_identity': staged_identity,
            }
            item['restore_journal'] = journal
            _write_legacy_record(record_path, record)
        try:
            try:
                current = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if current is not None:
                if [current.st_dev, current.st_ino] == staged_identity and _object_matches_snapshot(parent_fd, source.name, item):
                    journal['state'] = 'published'
                    journal['restored_identity'] = staged_identity
                    _write_legacy_record(record_path, record)
                    return
                _fail(f'legacy unit path changed before restoration publication: {source}')
            _renameat2(stage_fd, staged_name, parent_fd, source.name, 1)
            os.fsync(parent_fd)
            if not _object_matches_snapshot(parent_fd, source.name, item):
                _fail(f'legacy unit restore did not publish the snapshotted object: {source}')
            journal['state'] = 'published'
            journal['restored_identity'] = staged_identity
            _write_legacy_record(record_path, record)
        finally:
            os.close(stage_fd)
        _remove_empty_restore_stage(parent_fd, stage_name)
    finally:
        os.close(parent_fd)
def _safe_legacy_destination(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        _fail(f'legacy migration contains an unsafe restore path: {value!r}')
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as error:
        raise InstallerError(f'legacy restore parent is unavailable: {error}') from error
    if parent != path.parent:
        _fail(f'legacy restore parent changed during migration: {path.parent}')
    return path




def _check_legacy_migration(
    homebrew: Path,
    service_name: str,
    user: pwd.struct_passwd,
    *,
    user_unit_directory: str | None = None,
) -> dict[str, object]:
    service_file = _normalize_service_name(service_name)[1]
    if user.pw_uid == 0 or pwd.getpwuid(user.pw_uid).pw_name != user.pw_name:
        _fail('legacy migration requires a resolved non-root service account')
    unit_directory = _user_unit_directory(
        user,
        create=False,
        value=user_unit_directory,
    )
    user_unit = unit_directory / service_file
    user_paths = {'unit': user_unit, 'scope': 'user', 'user': user}
    try:
        user_unit_info = user_unit.lstat()
    except FileNotFoundError:
        user_unit_info = None
    if user_unit_info is not None or _unit_properties(user_paths).get('LoadState', 'not-found') != 'not-found':
        _fail(f'user service {service_file} already exists; refusing cross-scope duplicate migration')

    system_paths = _system_unit_paths(service_name)
    activation = _capture_activation(system_paths)
    if activation.get('load_state') != 'loaded':
        _fail(f'legacy system service {service_file} is not loaded')
    properties = _unit_properties(system_paths)
    working = _systemctl(
        system_paths,
        'show',
        system_paths['unit'].name,
        '--property=WorkingDirectory',
        check=False,
        timeout=15,
    )
    working_directory = next(
        (line.split('=', 1)[1] for line in working.stdout.splitlines() if line.startswith('WorkingDirectory=')),
        '',
    )
    services = homebrew / 'services'
    if working_directory != str(services):
        _fail(
            f'legacy system service WorkingDirectory must be the selected services directory '
            f'({services}), got {working_directory!r}'
        )
    services_path, homebrew_fd, services_fd, _ = _open_services_directory(homebrew, user)
    try:
        _lstat_regular_or_absent(services_path / 'PluginLoader')
        try:
            runtime_info = os.stat('.decky-installer', dir_fd=services_fd, follow_symlinks=False)
        except FileNotFoundError:
            runtime_info = None
        if runtime_info is not None and (
            not stat.S_ISDIR(runtime_info.st_mode)
            or runtime_info.st_uid != 0
            or runtime_info.st_mode & 0o022
        ):
            _fail('legacy root runtime directory is not protected from the service account')
    finally:
        os.close(services_fd)
        os.close(homebrew_fd)
    sources = _legacy_system_sources(properties)
    mask_parent = Path('/etc/systemd/system')
    mask_fd = _open_root_directory(mask_parent)
    try:
        mask_name = service_file
        try:
            mask_info = os.stat(mask_name, dir_fd=mask_fd, follow_symlinks=False)
        except FileNotFoundError:
            mask_info = None
        if mask_info is not None and not any(source == mask_parent / mask_name for source in sources):
            _fail(f'legacy migration would overwrite an unrelated system unit path: {mask_parent / mask_name}')
        park_name = f'{service_file}.decky-legacy-{user.pw_uid}.parked'
        try:
            os.stat(park_name, dir_fd=mask_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _fail(f'legacy migration park path already exists: {mask_parent / park_name}')
    finally:
        os.close(mask_fd)
    _legacy_store_preflight(user.pw_uid, service_file)
    status = _read_legacy_status(homebrew, service_name, user.pw_uid)
    if status is not None and status.get('phase') != 'restored':
        _fail(f'legacy migration already exists; use restore-legacy-system: {service_file}')
    for source in (Path(__file__), Path(__file__).with_name('process_observer.py')):
        try:
            info = source.lstat()
        except OSError as error:
            raise InstallerError(f'legacy migration helper source is unavailable: {error}') from error
        if not stat.S_ISREG(info.st_mode):
            _fail(f'legacy migration helper source is not a regular file: {source}')
    return {
        'ok': True,
        'command': 'check-legacy-migration',
        'service': service_file,
        'user': user.pw_name,
        'homebrew': str(homebrew),
        'user_unit_directory': str(unit_directory),
        'previously_running': bool(activation.get('running')),
        'enabled': bool(activation.get('enabled')),
        'root_owned_user_data': _user_data_ownership(homebrew, user, apply=False),
    }


def _snapshot_legacy_system(args: argparse.Namespace, user: pwd.struct_passwd) -> dict[str, Any]:
    service_file = _normalize_service_name(args.service_name)[1]
    homebrew = _homebrew_path(args.homebrew)
    readiness = _check_legacy_migration(
        homebrew,
        args.service_name,
        user,
        user_unit_directory=args.user_unit_directory,
    )
    directory = _legacy_migration_directory(
        homebrew,
        args.service_name,
        create=True,
        user_uid=user.pw_uid,
    )
    record_path = directory / 'private' / 'record.json'
    _, archive_fds = _open_legacy_store(user.pw_uid, service_file, create=False)
    try:
        try:
            os.stat('record.json', dir_fd=archive_fds['private'], follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            previous = _read_json_at(archive_fds['private'], 'record.json', mode=0o600, private=True)
            if previous.get('phase') != 'restored':
                _fail(f'legacy system service has already been migrated; use restore-legacy-system: {service_file}')
            # A completed restore is history only; start a fresh archive for this migration.
            os.unlink('record.json', dir_fd=archive_fds['private'])
            os.fsync(archive_fds['private'])
        _discard_incomplete_legacy_snapshots(archive_fds['private'])
        os.mkdir('snapshots', 0o700, dir_fd=archive_fds['private'])
        os.fsync(archive_fds['private'])
    finally:
        _close_legacy_descriptors(archive_fds)
    # Replace helpers only after confirming no unrestored migration depends on them.
    _install_legacy_helpers(directory, user.pw_uid, service_file)

    unit_directory = Path(str(readiness['user_unit_directory']))
    system_paths = _system_unit_paths(args.service_name)
    activation = _capture_activation(system_paths)
    properties = _unit_properties(system_paths)
    services, homebrew_fd, services_fd, services_info = _open_services_directory(homebrew, user)
    try:
        services_metadata = _metadata_from_fd(services_fd, services_info)
        services_metadata['identity'] = [services_info.st_dev, services_info.st_ino]
        state_directory = directory / 'private' / 'snapshots'
        state_directory.mkdir(mode=0o700, exist_ok=True)
        _fsync_directory(directory / 'private')
        binary_state, binary_identity = _snapshot_binary_at(
            services_fd,
            state_directory / 'binary',
        )
        legacy_runtime = _inventory_legacy_runtime(services_fd, service_file)
        system_directory = state_directory / 'system'
        system_directory.mkdir(mode=0o700)
        _fsync_directory(state_directory)
        files: list[dict[str, Any]] = []
        mask_path = Path('/etc/systemd/system') / service_file
        park_path = mask_path.with_name(f'{service_file}.decky-legacy-{user.pw_uid}.parked')
        for index, source in enumerate(_legacy_system_sources(properties)):
            snapshot = system_directory / f'{index:04d}'
            item = {
                'source': str(source),
                'snapshot': f'snapshots/system/{index:04d}',
                **_snapshot_legacy_file(source, snapshot),
            }
            if source == mask_path:
                item['parked_path'] = str(park_path)
            files.append(item)
    finally:
        os.close(services_fd)
        os.close(homebrew_fd)
    record: dict[str, Any] = {
        'schema': 1,
        'operation': 'legacy-system-migration',
        'service': service_file,
        'homebrew': str(homebrew),
        'user': user.pw_name,
        'user_uid': user.pw_uid,
        'user_unit_directory': str(unit_directory),
        'phase': 'prepared',
        'activation': activation,
        'binary': binary_state,
        'services_directory': services_metadata,
        'binary_identity': binary_identity,
        'files': files,
        'persistent_mask': {
            'path': str(mask_path),
            'identity': None,
            'state': 'planned',
        },
        'legacy_runtime': legacy_runtime,
        'created_at': int(time.time()),
    }
    _write_legacy_record(record_path, record)
    return {
        'directory': directory,
        'record_path': record_path,
        'record': record,
        'system_paths': system_paths,
    }

def _inventory_legacy_runtime(services_fd: int, service_name: str) -> dict[str, Any]:
    try:
        root_info = os.stat('.decky-installer', dir_fd=services_fd, follow_symlinks=False)
    except FileNotFoundError:
        return {'exists': False, 'entries': []}
    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != 0 or root_info.st_mode & 0o022:
        _fail('legacy root runtime directory is not protected from the service account')
    root_fd = os.open(
        '.decky-installer',
        os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
        dir_fd=services_fd,
    )
    try:
        root_opened = os.fstat(root_fd)
        if (root_info.st_dev, root_info.st_ino) != (root_opened.st_dev, root_opened.st_ino):
            _fail('legacy root runtime directory changed while opening')
        try:
            unit_info = os.stat(service_name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return {
                'exists': True,
                'root_identity': [root_info.st_dev, root_info.st_ino],
                'unit_exists': False,
                'entries': [],
            }
        if not stat.S_ISDIR(unit_info.st_mode) or unit_info.st_uid != 0 or unit_info.st_mode & 0o022:
            _fail('legacy root runtime unit directory is not root-protected')
        unit_fd = os.open(
            service_name,
            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
            dir_fd=root_fd,
        )
        try:
            unit_opened = os.fstat(unit_fd)
            if (unit_info.st_dev, unit_info.st_ino) != (unit_opened.st_dev, unit_opened.st_ino):
                _fail('legacy root runtime unit directory changed while opening')
            entries: list[dict[str, Any]] = []

            def walk(directory_fd: int, prefix: str) -> None:
                for name in sorted(os.listdir(directory_fd)):
                    if name in ('', '.', '..') or '/' in name:
                        _fail('legacy root runtime contains an unsafe path component')
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    relative = f'{prefix}/{name}' if prefix else name
                    identity = [info.st_dev, info.st_ino]
                    entry: dict[str, Any] = {
                        'path': relative,
                        'identity': identity,
                        'mode': stat.S_IMODE(info.st_mode),
                        'uid': info.st_uid,
                        'gid': info.st_gid,
                    }
                    if stat.S_ISDIR(info.st_mode):
                        child_fd = os.open(
                            name,
                            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
                            dir_fd=directory_fd,
                        )
                        try:
                            child_info = os.fstat(child_fd)
                            if (child_info.st_dev, child_info.st_ino) != (info.st_dev, info.st_ino):
                                _fail(f'legacy root runtime changed while opening {relative}')
                            entry['kind'] = 'directory'
                            entries.append(entry)
                            walk(child_fd, relative)
                        finally:
                            os.close(child_fd)
                    elif stat.S_ISREG(info.st_mode):
                        file_fd = os.open(
                            name,
                            os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0),
                            dir_fd=directory_fd,
                        )
                        try:
                            opened = os.fstat(file_fd)
                            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                                _fail(f'legacy root runtime changed while opening {relative}')
                            entry['kind'] = 'file'
                            entry['sha256'] = _digest_descriptor(file_fd)
                            after = os.fstat(file_fd)
                            if (
                                (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                            ):
                                _fail(f'legacy root runtime changed while hashing {relative}')
                            entries.append(entry)
                        finally:
                            os.close(file_fd)
                    elif stat.S_ISLNK(info.st_mode):
                        entry['kind'] = 'symlink'
                        entry['target'] = os.readlink(name, dir_fd=directory_fd)
                        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                            _fail(f'legacy root runtime changed while reading {relative}')
                        entries.append(entry)
                    else:
                        _fail(f'legacy root runtime contains an unsupported object: {relative}')

            walk(unit_fd, '')
            return {
                'exists': True,
                'root_identity': [root_info.st_dev, root_info.st_ino],
                'unit_exists': True,
                'unit_identity': [unit_info.st_dev, unit_info.st_ino],
                'entries': entries,
            }
        finally:
            os.close(unit_fd)
    finally:
        os.close(root_fd)


def _verify_legacy_runtime(services_fd: int, service_name: str, expected: object) -> None:
    if not isinstance(expected, dict):
        _fail('legacy system runtime integrity record is invalid')
    current = _inventory_legacy_runtime(services_fd, service_name)
    if current != expected:
        _fail('legacy Rust runtime/worker/control paths changed; refusing to start the system service')


def _park_legacy_system_files(record: dict[str, Any], record_path: Path) -> None:
    service_name = str(record['service'])
    mask = record.get('persistent_mask')
    if not isinstance(mask, dict) or mask.get('state') not in ('planned', 'creating', 'installed'):
        _fail('legacy persistent mask journal is invalid')
    mask['state'] = 'creating'
    _write_legacy_record(record_path, record)
    for item in record['files']:
        if not isinstance(item, dict) or not isinstance(item.get('parked_path'), str):
            continue
        source = _safe_legacy_destination(str(item['source']))
        parked = _safe_legacy_destination(str(item['parked_path']))
        if parked.parent != source.parent or source.parent != Path('/etc/systemd/system'):
            _fail(f'legacy migration contains an unsafe persistent park path: {parked}')
        parent_fd = _open_root_directory(source.parent)
        try:
            identity = item.get('identity')
            try:
                source_info = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                source_info = None
            try:
                parked_info = os.stat(parked.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                parked_info = None
            if source_info is not None and [source_info.st_dev, source_info.st_ino] == identity:
                if parked_info is not None:
                    _fail(f'legacy park destination already exists: {parked}')
                _renameat2(parent_fd, source.name, parent_fd, parked.name, 1)
                os.fsync(parent_fd)
            elif (
                source_info is None
                and parked_info is not None
                and [parked_info.st_dev, parked_info.st_ino] == identity
            ):
                pass
            else:
                _fail(f'legacy system unit changed before persistent parking: {source}')
        finally:
            os.close(parent_fd)

    mask_path = Path(str(mask.get('path', '')))
    if mask_path != Path('/etc/systemd/system') / service_name:
        _fail('legacy persistent mask path is invalid')
    parent_fd = _open_root_directory(mask_path.parent)
    try:
        try:
            current = os.stat(mask_path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is None:
            os.symlink('/dev/null', mask_path.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            current = os.stat(mask_path.name, dir_fd=parent_fd, follow_symlinks=False)
        else:
            if not stat.S_ISLNK(current.st_mode) or os.readlink(mask_path.name, dir_fd=parent_fd) != '/dev/null':
                _fail(f'legacy migration refuses to overwrite an unrelated system unit: {mask_path}')
            expected_identity = mask.get('identity')
            if expected_identity is not None and [current.st_dev, current.st_ino] != expected_identity:
                _fail(f'legacy persistent mask changed during migration: {mask_path}')
        mask['identity'] = [current.st_dev, current.st_ino]
        mask['state'] = 'installed'
        _write_legacy_record(record_path, record)
    finally:
        os.close(parent_fd)


def _remove_legacy_persistent_mask(record: dict[str, Any], record_path: Path) -> None:
    mask = record.get('persistent_mask')
    if not isinstance(mask, dict):
        _fail('legacy persistent mask journal is invalid')
    path = Path(str(mask.get('path', '')))
    if path != Path('/etc/systemd/system') / str(record['service']):
        _fail('legacy persistent mask path is invalid')
    if mask.get('state') == 'removed':
        # A retry after partial restore: the per-file restore validates the destination.
        return
    parent_fd = _open_root_directory(path.parent)
    try:
        try:
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if mask.get('state') == 'planned':
            mask['state'] = 'removed'
            _write_legacy_record(record_path, record)
            return
        if current is not None:
            identity = mask.get('identity')
            if identity is None and mask.get('state') == 'creating':
                original = next(
                    (
                        item for item in record.get('files', [])
                        if isinstance(item, dict)
                        and item.get('source') == str(path)
                        and item.get('identity') == [current.st_dev, current.st_ino]
                    ),
                    None,
                )
                if original is not None:
                    mask['state'] = 'removed'
                    _write_legacy_record(record_path, record)
                    return
                if (
                    not stat.S_ISLNK(current.st_mode)
                    or current.st_uid != 0
                    or os.readlink(path.name, dir_fd=parent_fd) != '/dev/null'
                ):
                    _fail(f'legacy persistent mask is not the journaled root mask: {path}')
                mask['identity'] = [current.st_dev, current.st_ino]
            elif identity != [current.st_dev, current.st_ino]:
                _fail(f'legacy persistent unit path changed; refusing to remove it: {path}')
            if not stat.S_ISLNK(current.st_mode) or os.readlink(path.name, dir_fd=parent_fd) != '/dev/null':
                _fail(f'legacy persistent unit mask was replaced: {path}')
            os.unlink(path.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        mask['state'] = 'removed'
        _write_legacy_record(record_path, record)
    finally:
        os.close(parent_fd)


def _open_recorded_services(
    homebrew: Path,
    record: dict[str, Any],
) -> tuple[int, int, os.stat_result]:
    services_state = record.get('services_directory')
    if not isinstance(services_state, dict):
        _fail('legacy services-directory journal is invalid')
    identity = services_state.get('identity')
    if not isinstance(identity, list) or len(identity) != 2 or any(type(value) is not int for value in identity):
        _fail('legacy services-directory inode identity is invalid')
    try:
        homebrew_fd = os.open(
            homebrew,
            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
        )
        services_fd = os.open(
            'services',
            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
            dir_fd=homebrew_fd,
        )
    except OSError as error:
        try:
            os.close(homebrew_fd)
        except (UnboundLocalError, OSError):
            pass
        raise InstallerError(f'legacy services directory is unavailable: {error}') from error
    info = os.fstat(services_fd)
    try:
        path_info = os.stat('services', dir_fd=homebrew_fd, follow_symlinks=False)
    except OSError as error:
        os.close(services_fd)
        os.close(homebrew_fd)
        raise InstallerError(f'legacy services directory changed during restore: {error}') from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or [info.st_dev, info.st_ino] != identity
        or [path_info.st_dev, path_info.st_ino] != identity
    ):
        os.close(services_fd)
        os.close(homebrew_fd)
        _fail('legacy services directory changed after its migration snapshot')
    if info.st_uid not in (0, record.get('user_uid')) or info.st_mode & 0o6000:
        os.close(services_fd)
        os.close(homebrew_fd)
        _fail('legacy services directory ownership changed unexpectedly')
    return homebrew_fd, services_fd, info


def _restore_services_directory_metadata(
    homebrew: Path,
    record: dict[str, Any],
) -> None:
    services_state = record.get('services_directory')
    if not isinstance(services_state, dict):
        _fail('legacy services-directory metadata is invalid')
    metadata = {key: value for key, value in services_state.items() if key != 'identity'}
    homebrew_fd, services_fd, _ = _open_recorded_services(homebrew, record)
    try:
        os.fchown(services_fd, int(metadata['uid']), int(metadata['gid']))
        xattrs = metadata.get('xattrs', {})
        if not isinstance(xattrs, dict):
            _fail('legacy services-directory xattr snapshot is invalid')
        for name, encoded in xattrs.items():
            os.setxattr(services_fd, str(name), base64.b64decode(str(encoded), validate=True))
        os.fchmod(services_fd, int(metadata['mode']))
        os.utime(services_fd, ns=(int(metadata['atime_ns']), int(metadata['mtime_ns'])))
        os.fsync(services_fd)
        path_info = os.stat('services', dir_fd=homebrew_fd, follow_symlinks=False)
        info = os.fstat(services_fd)
        if (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino):
            _fail('legacy services path changed while restoring its directory metadata')
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise InstallerError(f'cannot safely restore legacy services-directory metadata: {error}') from error
    finally:
        os.close(services_fd)
        os.close(homebrew_fd)


def _handoff_services_directory(
    homebrew: Path,
    user: pwd.struct_passwd,
    record: dict[str, Any],
    record_path: Path,
) -> None:
    metadata = record.get('services_directory')
    if not isinstance(metadata, dict):
        _fail('legacy services-directory snapshot is invalid')
    mode = int(metadata['mode'])
    record['phase'] = 'services-handoff-pending'
    _write_legacy_record(record_path, record)
    homebrew_fd, services_fd, _ = _open_recorded_services(homebrew, record)
    try:
        os.fchmod(services_fd, mode & ~0o222)
        os.fchown(services_fd, user.pw_uid, user.pw_gid)
        os.fchmod(services_fd, mode)
        os.fsync(services_fd)
        path_info = os.stat('services', dir_fd=homebrew_fd, follow_symlinks=False)
        info = os.fstat(services_fd)
        if (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino):
            _fail('legacy services path changed during ownership handoff')
    finally:
        os.close(services_fd)
        os.close(homebrew_fd)
    record['phase'] = 'services-user-owned'
    _write_legacy_record(record_path, record)


def _user_data_ownership(homebrew: Path, user: pwd.struct_passwd, *, apply: bool) -> int:
    """Count, or hand to the user, root-owned homebrew data outside services/.

    A root loader leaves plugins/, each plugin directory, plugin.json files and
    settings it wrote owned by root. A user service cannot use or secure them.
    The walk uses directory descriptors and never follows symbolic links.
    """
    count = 0
    nofollow_dir = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def adopt(parent_fd: int, name: str, info: os.stat_result, label: str) -> None:
        nonlocal count
        if info.st_uid != 0:
            return
        count += 1
        if not apply:
            return
        if stat.S_ISREG(info.st_mode):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent_fd)
            try:
                current = os.fstat(descriptor)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or current.st_nlink != 1:
                    _fail(f'homebrew file changed during ownership handoff: {label}')
                os.fchown(descriptor, user.pw_uid, user.pw_gid)
            finally:
                os.close(descriptor)
        else:
            # Directories were opened by descriptor; symlinks change only themselves.
            os.chown(name, user.pw_uid, user.pw_gid, dir_fd=parent_fd, follow_symlinks=False)

    def visit(directory_fd: int, label: str) -> None:
        nonlocal count
        for name in sorted(os.listdir(directory_fd)):
            if not label and name == 'services':
                continue
            child_label = f'{label}/{name}' if label else name
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                try:
                    child_fd = os.open(name, nofollow_dir, dir_fd=directory_fd)
                except PermissionError:
                    if apply or info.st_uid != 0:
                        raise
                    # An unprivileged preflight cannot read a private root directory.
                    count += 1
                    continue
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        _fail(f'homebrew directory changed during ownership handoff: {child_label}')
                    if info.st_uid == 0:
                        count += 1
                        if apply:
                            os.fchown(child_fd, user.pw_uid, user.pw_gid)
                    visit(child_fd, child_label)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(info.st_mode):
                if info.st_uid == 0 and info.st_nlink != 1:
                    _fail(f'refusing to hand over a multiply-linked root-owned file: {child_label}')
                adopt(directory_fd, name, info, child_label)
            elif stat.S_ISLNK(info.st_mode):
                adopt(directory_fd, name, info, child_label)
            elif info.st_uid == 0:
                _fail(f'unsupported root-owned homebrew object: {child_label}')

    # Checked in preflight too, so a migration never stops the service and then fails here.
    try:
        protected = Path('/proc/sys/fs/protected_hardlinks').read_text().strip()
    except OSError:
        protected = ''
    if protected != '1':
        _fail('fs.protected_hardlinks must be enabled before handing homebrew data to the service account')
    homebrew_fd = os.open(homebrew, nofollow_dir)
    try:
        info = os.fstat(homebrew_fd)
        if info.st_uid == 0:
            count += 1
            if apply:
                os.fchown(homebrew_fd, user.pw_uid, user.pw_gid)
        visit(homebrew_fd, '')
    finally:
        os.close(homebrew_fd)
    return count


def _legacy_user_manager_environment(user: pwd.struct_passwd) -> dict[str, str]:
    runtime = _user_runtime_directory(user)
    return {
        'HOME': user.pw_dir,
        'USER': user.pw_name,
        'LOGNAME': user.pw_name,
        'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
        'XDG_RUNTIME_DIR': str(runtime),
        'DBUS_SESSION_BUS_ADDRESS': f'unix:path={runtime / "bus"}',
        'PYTHONDONTWRITEBYTECODE': '1',
        'LC_ALL': 'C',
    }


def _run_legacy_user_stop(args: argparse.Namespace) -> dict[str, object]:
    user = _current_user()
    if os.geteuid() == 0 or args.user != user.pw_name:
        _fail('legacy user-service cleanup must run as the selected non-root account')
    if pwd.getpwuid(user.pw_uid).pw_name != args.user:
        _fail('legacy user-service cleanup requires the resolved account name')
    _, unit_name = _normalize_service_name(args.service_name)
    unit_directory = _user_unit_directory(
        user,
        create=False,
        value=args.user_unit_directory,
    )
    unit_path = unit_directory / unit_name
    paths: dict[str, Any] = {
        'unit': unit_path,
        'unit_dir': unit_directory,
        'scope': 'user',
        'user': user,
    }
    _assert_user_unit_source(paths)
    stop_service(unit_name, scope='user', user=user)
    properties = _unit_properties(paths)
    if (
        properties.get('ActiveState') not in ('inactive', 'failed', 'dead', '')
        or properties.get('MainPID', '0') != '0'
    ):
        _fail('user service remains active after drainage; refusing to remove its unit')
    try:
        unit_directory_fd = os.open(
            unit_directory,
            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
        )
    except FileNotFoundError:
        # The user install never created the directory, so it holds no unit.
        unit_directory_fd = None
    except OSError as error:
        raise InstallerError(f'user service unit directory is unavailable: {error}') from error
    try:
        info = None
        if unit_directory_fd is not None:
            try:
                info = os.stat(unit_name, dir_fd=unit_directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                info = None
        if info is not None:
            if not stat.S_ISREG(info.st_mode) or info.st_uid != user.pw_uid:
                _fail(f'refusing to remove an uncontrolled user service unit: {unit_path}')
            _systemctl(paths, 'disable', unit_name, check=False)
            _systemctl(paths, 'disable', '--runtime', unit_name, check=False)
            os.unlink(unit_name, dir_fd=unit_directory_fd)
            os.fsync(unit_directory_fd)
        _systemctl(paths, 'daemon-reload', timeout=30)
    finally:
        if unit_directory_fd is not None:
            os.close(unit_directory_fd)
    after = _unit_properties(paths)
    if after.get('LoadState') != 'not-found' or after.get('MainPID', '0') != '0':
        _fail('user service unit remains loaded after removal; refusing system activation')
    return {
        'ok': True,
        'command': 'legacy-user-stop',
        'service': unit_name,
        'load_state': after.get('LoadState'),
        'active_state': after.get('ActiveState'),
    }


def _stop_legacy_user_service_as_user(
    directory: Path,
    user: pwd.struct_passwd,
    unit_name: str,
    unit_directory: Path,
) -> None:
    helper = directory / 'helper' / 'install.py'
    info = helper.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) != 0o755
        or info.st_mode & 0o022
    ):
        _fail(f'protected legacy user cleanup helper is unavailable: {helper}')
    try:
        groups = os.getgrouplist(user.pw_name, user.pw_gid)
    except AttributeError:
        groups = [user.pw_gid]

    def drop_privileges() -> None:
        os.setgroups(groups)
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)

    command = [
        os.path.realpath(sys.executable),
        str(helper),
        'legacy-user-stop',
        '--user',
        user.pw_name,
        '--service-name',
        unit_name,
        '--user-unit-directory',
        str(unit_directory),
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
            cwd=user.pw_dir if Path(user.pw_dir).is_dir() else '/',
            env=_legacy_user_manager_environment(user),
            preexec_fn=drop_privileges,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallerError(f'cannot run protected user-service cleanup for {unit_name}: {error}') from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f'exit status {result.returncode}'
        _fail(f'protected user-service cleanup failed for {unit_name}: {detail}')


def _run_legacy_migration(args: argparse.Namespace) -> dict[str, object]:
    _require_root()
    user = pwd.getpwnam(args.user)
    if user.pw_uid == 0 or user.pw_name != args.user:
        _fail('legacy migration requires the resolved non-root account named by --user')
    homebrew = _homebrew_path(args.homebrew)
    snapshot = _snapshot_legacy_system(args, user)
    record = snapshot['record']
    paths = snapshot['system_paths']
    record_path = snapshot['record_path']
    directory = snapshot['directory']
    stop_service(args.service_name, scope='system')
    record['phase'] = 'system-stopped'
    _write_legacy_record(record_path, record)

    services, homebrew_fd, services_fd, _ = _open_services_directory(homebrew, user)
    try:
        record['legacy_runtime'] = _inventory_legacy_runtime(services_fd, str(record['service']))
        _write_legacy_record(record_path, record)
    finally:
        os.close(services_fd)
        os.close(homebrew_fd)

    _systemctl(paths, 'disable', paths['unit'].name, check=False)
    _systemctl(paths, 'disable', '--runtime', paths['unit'].name, check=False)
    _park_legacy_system_files(record, record_path)
    _systemctl(paths, 'daemon-reload', timeout=30)
    properties = _unit_properties(paths)
    if (
        properties.get('LoadState') != 'masked'
        or properties.get('ActiveState') not in ('inactive', 'failed')
        or properties.get('MainPID', '0') != '0'
    ):
        _fail('legacy system unit was not safely parked after process drainage')
    record['phase'] = 'parked'
    _write_legacy_record(record_path, record)

    binary_identity = record.get('binary_identity')
    record['phase'] = 'binary-removal-pending'
    _write_legacy_record(record_path, record)
    services_state = record.get('services_directory')
    if not isinstance(services_state, dict):
        _fail('legacy services-directory snapshot is invalid')
    homebrew_fd, services_fd, _ = _open_recorded_services(homebrew, record)
    try:
        os.fchmod(services_fd, int(services_state['mode']) & ~0o222)
        if isinstance(binary_identity, list) and len(binary_identity) == 2:
            current = os.stat('PluginLoader', dir_fd=services_fd, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode) or [current.st_dev, current.st_ino] != binary_identity:
                _fail('legacy backend binary changed after its rollback snapshot; refusing to remove it')
            binary_state = record.get('binary')
            if (
                not isinstance(binary_state, dict)
                or binary_state.get('exists') is not True
                or not _object_matches_snapshot(
                    services_fd,
                    'PluginLoader',
                    {'kind': 'file', **binary_state},
                )
            ):
                _fail('legacy backend binary contents or metadata changed after its rollback snapshot')
            binary_fd = os.open(
                'PluginLoader',
                os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NOATIME', 0),
                dir_fd=services_fd,
            )
            try:
                opened = os.fstat(binary_fd)
                if [opened.st_dev, opened.st_ino] != binary_identity:
                    _fail('legacy backend binary changed before removal')
            finally:
                os.close(binary_fd)
            os.unlink('PluginLoader', dir_fd=services_fd)
            os.fsync(services_fd)
        else:
            try:
                os.stat('PluginLoader', dir_fd=services_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                _fail('legacy backend binary appeared after its rollback snapshot')
        record['phase'] = 'binary-removed'
        _write_legacy_record(record_path, record)
    finally:
        os.close(services_fd)
        os.close(homebrew_fd)
    _handoff_services_directory(homebrew, user, record, record_path)
    # The root service is drained, so it cannot re-lock plugin directories now.
    adopted = _user_data_ownership(homebrew, user, apply=True)
    return {
        'ok': True,
        'command': 'migrate-legacy-system',
        'user_data_adopted': adopted,
        'service': paths['unit'].name,
        'user': user.pw_name,
        'state': str(record_path),
        'status': str(directory / 'status.json'),
        'helper': str(directory / 'helper' / 'install.py'),
        'previously_running': bool(record['activation'].get('running')),
        'restore_command': 'restore-legacy-system',
    }


def _lock_services_directory_for_restore(
    homebrew: Path,
    record: dict[str, Any],
    record_path: Path,
) -> None:
    state = record.get('services_directory')
    if not isinstance(state, dict):
        _fail('legacy services-directory snapshot is invalid')
    record['phase'] = 'services-lock-pending'
    _write_legacy_record(record_path, record)
    homebrew_fd, services_fd, _ = _open_recorded_services(homebrew, record)
    try:
        # Take ownership first: clearing write bits alone can be undone by the owner.
        os.fchown(services_fd, 0, -1)
        os.fchmod(services_fd, int(state['mode']) & ~0o222)
        os.fsync(services_fd)
        current = os.stat('services', dir_fd=homebrew_fd, follow_symlinks=False)
        opened = os.fstat(services_fd)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            _fail('legacy services path changed while securing restore publication')
    finally:
        os.close(services_fd)
        os.close(homebrew_fd)


def _cleanup_legacy_binary_stage(
    services_fd: int,
    journal: dict[str, Any],
    user: pwd.struct_passwd,
) -> None:
    stage_name, staged_name = journal.get('stage_directory'), journal.get('stage_name')
    if not isinstance(stage_name, str) or not isinstance(staged_name, str):
        return
    try:
        stage_fd = os.open(
            stage_name,
            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
            dir_fd=services_fd,
        )
    except FileNotFoundError:
        return
    try:
        try:
            displaced = os.stat(staged_name, dir_fd=stage_fd, follow_symlinks=False)
        except FileNotFoundError:
            displaced = None
        if displaced is not None:
            expected = journal.get('expected_destination_identity')
            if (
                not isinstance(expected, list)
                or [displaced.st_dev, displaced.st_ino] != expected
                or not stat.S_ISREG(displaced.st_mode)
                or displaced.st_uid != user.pw_uid
            ):
                _fail('legacy binary restore stage contains an unexpected displaced file')
            os.unlink(staged_name, dir_fd=stage_fd)
        os.fsync(stage_fd)
    finally:
        os.close(stage_fd)
    try:
        os.rmdir(stage_name, dir_fd=services_fd)
        os.fsync(services_fd)
    except OSError:
        pass


def _discard_unpublished_legacy_binary_stage(
    services_fd: int,
    journal: dict[str, Any],
) -> None:
    stage_name, staged_name = journal.get('stage_directory'), journal.get('stage_name')
    staged_identity = journal.get('staged_identity')
    if not isinstance(stage_name, str) or not isinstance(staged_name, str):
        return
    try:
        stage_fd = os.open(
            stage_name,
            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
            dir_fd=services_fd,
        )
    except FileNotFoundError:
        return
    try:
        stage_info = os.fstat(stage_fd)
        if stage_info.st_uid != 0 or stat.S_IMODE(stage_info.st_mode) != 0o700:
            _fail('legacy binary stage is not root-private')
        try:
            staged = os.stat(staged_name, dir_fd=stage_fd, follow_symlinks=False)
        except FileNotFoundError:
            staged = None
        if staged is not None:
            if (
                [staged.st_dev, staged.st_ino] != staged_identity
                or not stat.S_ISREG(staged.st_mode)
                or staged.st_uid != 0
            ):
                _fail('legacy binary stage no longer contains its unpublished root file')
            os.unlink(staged_name, dir_fd=stage_fd)
        os.fsync(stage_fd)
    finally:
        os.close(stage_fd)
    try:
        os.rmdir(stage_name, dir_fd=services_fd)
        os.fsync(services_fd)
    except OSError:
        pass


def _finish_legacy_binary_removal(
    services_fd: int,
    journal: dict[str, Any],
    user: pwd.struct_passwd,
    *,
    sentinel_at_destination: bool,
) -> None:
    stage_name, staged_name = journal.get('stage_directory'), journal.get('stage_name')
    if not isinstance(stage_name, str) or not isinstance(staged_name, str):
        _fail('legacy binary removal journal is invalid')
    stage_fd = os.open(
        stage_name,
        os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0),
        dir_fd=services_fd,
    )
    try:
        stage_info = os.fstat(stage_fd)
        if stage_info.st_uid != 0 or stat.S_IMODE(stage_info.st_mode) != 0o700:
            _fail('legacy binary removal stage is not root-private')
        try:
            staged = os.stat(staged_name, dir_fd=stage_fd, follow_symlinks=False)
        except FileNotFoundError:
            staged = None
        if staged is not None:
            staged_identity = [staged.st_dev, staged.st_ino]
            if staged_identity == journal.get('expected_destination_identity'):
                if not stat.S_ISREG(staged.st_mode) or staged.st_uid != user.pw_uid:
                    _fail('legacy binary removal displaced an uncontrolled file')
            elif staged_identity == journal.get('staged_identity'):
                if not stat.S_ISREG(staged.st_mode) or staged.st_uid != 0:
                    _fail('legacy binary removal sentinel changed')
            else:
                _fail('legacy binary removal stage contains an outside inode')
            os.unlink(staged_name, dir_fd=stage_fd)
        if sentinel_at_destination:
            try:
                current = os.stat('PluginLoader', dir_fd=services_fd, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if current is not None:
                if (
                    [current.st_dev, current.st_ino] != journal.get('staged_identity')
                    or not stat.S_ISREG(current.st_mode)
                    or current.st_uid != 0
                ):
                    _fail('legacy binary removal sentinel was replaced')
                os.unlink('PluginLoader', dir_fd=services_fd)
        os.fsync(stage_fd)
        os.fsync(services_fd)
    finally:
        os.close(stage_fd)
    try:
        os.rmdir(stage_name, dir_fd=services_fd)
        os.fsync(services_fd)
    except OSError:
        pass


def _restore_legacy_binary(
    homebrew: Path,
    record: dict[str, Any],
    record_path: Path,
    snapshot: Path,
    user: pwd.struct_passwd,
) -> None:
    state = record.get('binary')
    if not isinstance(state, dict) or not isinstance(state.get('exists'), bool):
        _fail('legacy migration binary snapshot is invalid')
    homebrew_fd, services_fd, _ = _open_recorded_services(homebrew, record)
    try:
        journal = state.get('restore_journal')
        if journal is not None and not isinstance(journal, dict):
            _fail('legacy binary restore journal is invalid')
        try:
            current = os.stat('PluginLoader', dir_fd=services_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        original_identity = record.get('binary_identity')
        if not state['exists']:
            if current is None:
                if isinstance(journal, dict) and journal.get('state') == 'removing':
                    try:
                        _finish_legacy_binary_removal(
                            services_fd,
                            journal,
                            user,
                            sentinel_at_destination=False,
                        )
                    except FileNotFoundError:
                        pass
                state['restore_journal'] = {'state': 'removed'}
                _write_legacy_record(record_path, record)
                return
            current_identity = [current.st_dev, current.st_ino]
            if (
                isinstance(journal, dict)
                and journal.get('state') == 'removing'
                and current_identity == journal.get('staged_identity')
            ):
                _finish_legacy_binary_removal(
                    services_fd,
                    journal,
                    user,
                    sentinel_at_destination=True,
                )
                state['restore_journal'] = {'state': 'removed'}
                _write_legacy_record(record_path, record)
                return
            if isinstance(journal, dict) and journal.get('state') == 'removing':
                if current_identity != journal.get('expected_destination_identity'):
                    _fail('legacy backend destination changed during interrupted removal')
                _discard_unpublished_legacy_binary_stage(services_fd, journal)
            if not stat.S_ISREG(current.st_mode) or current.st_uid != user.pw_uid or current.st_mode & 0o6000:
                _fail('legacy backend destination is not the selected user runtime binary')
            stage_name, stage_fd = _create_restore_stage(services_fd)
            staged_name = f'sentinel-{uuid.uuid4().hex}'
            sentinel_fd = os.open(
                staged_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
                0o600,
                dir_fd=stage_fd,
            )
            try:
                os.fchown(sentinel_fd, 0, 0)
                os.fchmod(sentinel_fd, 0o600)
                os.fsync(sentinel_fd)
                sentinel = os.fstat(sentinel_fd)
            finally:
                os.close(sentinel_fd)
            journal = {
                'state': 'removing',
                'stage_directory': stage_name,
                'stage_name': staged_name,
                'staged_identity': [sentinel.st_dev, sentinel.st_ino],
                'expected_destination_identity': current_identity,
            }
            state['restore_journal'] = journal
            _write_legacy_record(record_path, record)
            try:
                _renameat2(stage_fd, staged_name, services_fd, 'PluginLoader', 2)
                displaced = os.stat(staged_name, dir_fd=stage_fd, follow_symlinks=False)
                if (
                    [displaced.st_dev, displaced.st_ino] != current_identity
                    or not stat.S_ISREG(displaced.st_mode)
                    or displaced.st_uid != user.pw_uid
                ):
                    _renameat2(stage_fd, staged_name, services_fd, 'PluginLoader', 2)
                    _fail('legacy backend destination changed while removing the user runtime binary')
                _finish_legacy_binary_removal(
                    services_fd,
                    journal,
                    user,
                    sentinel_at_destination=True,
                )
                state['restore_journal'] = {'state': 'removed'}
                _write_legacy_record(record_path, record)
            finally:
                os.close(stage_fd)
            return

        metadata = state.get('metadata')
        digest = state.get('sha256')
        if not isinstance(metadata, dict) or not isinstance(digest, str):
            _fail('legacy migration binary snapshot metadata or checksum is invalid')
        item = {'kind': 'file', 'metadata': metadata, 'sha256': digest}
        # Inode numbers can be reused after migration removed the original, so the
        # identity shortcut also requires matching contents and metadata.
        if (
            current is not None
            and isinstance(original_identity, list)
            and [current.st_dev, current.st_ino] == original_identity
            and _object_matches_snapshot(services_fd, 'PluginLoader', item)
        ):
            state['restore_journal'] = {'state': 'restored-original', 'restored_identity': original_identity}
            _write_legacy_record(record_path, record)
            return
        if current is not None and isinstance(journal, dict) and [
            current.st_dev, current.st_ino
        ] == journal.get('staged_identity'):
            if not _object_matches_snapshot(services_fd, 'PluginLoader', item):
                _fail('legacy backend restore publication changed after interruption')
            journal['state'] = 'published'
            journal['restored_identity'] = [current.st_dev, current.st_ino]
            _write_legacy_record(record_path, record)
            _cleanup_legacy_binary_stage(services_fd, journal, user)
            return
        expected: list[int] | None = None
        if current is not None:
            if not stat.S_ISREG(current.st_mode) or current.st_uid != user.pw_uid or current.st_mode & 0o6000:
                _fail('legacy backend destination is not the selected user runtime binary')
            expected = [current.st_dev, current.st_ino]
        if isinstance(journal, dict):
            if (
                journal.get('state') != 'staged'
                or journal.get('expected_destination_identity') != expected
            ):
                _fail('legacy backend restore destination changed after journaling')
            _discard_unpublished_legacy_binary_stage(services_fd, journal)
        stage_name, stage_fd = _create_restore_stage(services_fd)
        staged_name = f'binary-{uuid.uuid4().hex}'
        staged_identity = _stage_legacy_object(snapshot, item, stage_fd, staged_name)
        journal = {
            'state': 'staged',
            'stage_directory': stage_name,
            'stage_name': staged_name,
            'staged_identity': staged_identity,
            'expected_destination_identity': expected,
        }
        state['restore_journal'] = journal
        _write_legacy_record(record_path, record)
        try:
            if expected is None:
                _renameat2(stage_fd, staged_name, services_fd, 'PluginLoader', 1)
            else:
                _renameat2(stage_fd, staged_name, services_fd, 'PluginLoader', 2)
                displaced = os.stat(staged_name, dir_fd=stage_fd, follow_symlinks=False)
                if (
                    [displaced.st_dev, displaced.st_ino] != expected
                    or not stat.S_ISREG(displaced.st_mode)
                    or displaced.st_uid != user.pw_uid
                ):
                    _renameat2(stage_fd, staged_name, services_fd, 'PluginLoader', 2)
                    _fail('legacy backend destination changed during atomic publication')
                os.unlink(staged_name, dir_fd=stage_fd)
            os.fsync(stage_fd)
            os.fsync(services_fd)
            journal['state'] = 'published'
            journal['restored_identity'] = staged_identity
            _write_legacy_record(record_path, record)
        finally:
            os.close(stage_fd)
        _cleanup_legacy_binary_stage(services_fd, journal, user)
    finally:
        os.close(services_fd)
        os.close(homebrew_fd)


def _run_legacy_restore(args: argparse.Namespace) -> dict[str, object]:
    _require_root()
    user = pwd.getpwnam(args.user)
    homebrew = _homebrew_path(args.homebrew)
    directory, record = _read_legacy_record(homebrew, args.service_name, user.pw_uid)
    if user.pw_uid == 0 or user.pw_uid != record['user_uid'] or user.pw_name != record['user']:
        _fail('legacy migration snapshot belongs to a different service account')
    if record.get('phase') == 'restored':
        _fail('legacy system service has already been restored')
    if record.get('phase') in ('forgetting', 'forgotten'):
        _fail('the saved legacy system service was deleted with forget-legacy-system; it cannot be restored')
    protected_helper = directory / 'helper' / 'install.py'
    helper_info = protected_helper.lstat()
    if (
        not stat.S_ISREG(helper_info.st_mode)
        or helper_info.st_uid != 0
        or stat.S_IMODE(helper_info.st_mode) != 0o755
        or helper_info.st_mode & 0o022
        or Path(__file__).resolve(strict=True) != protected_helper
    ):
        _fail(f'legacy restore must run from its protected offline helper: {protected_helper}')
    if args.user_unit_directory and str(Path(args.user_unit_directory)) != record['user_unit_directory']:
        _fail('restore user unit directory differs from the legacy migration snapshot')
    record_path = directory / 'private' / 'record.json'
    if record.get('phase') not in ('restoring', 'services-lock-pending'):
        # Never move a retried restore's journal phase backwards.
        record['phase'] = 'restoring'
        _write_legacy_record(record_path, record)

    unit_name = str(record['service'])
    unit_directory = _user_unit_directory(
        user,
        create=False,
        value=record['user_unit_directory'],
    )
    _stop_legacy_user_service_as_user(directory, user, unit_name, unit_directory)
    user_paths: dict[str, Any] = {
        'unit': unit_directory / unit_name,
        'unit_dir': unit_directory,
        'scope': 'user',
        'user': user,
    }
    user_properties = _unit_properties(user_paths)
    if user_properties.get('LoadState') != 'not-found' or user_properties.get('MainPID', '0') != '0':
        _fail('user service unit remains loaded after unprivileged removal; refusing system activation')

    system_paths = _system_unit_paths(unit_name)
    # Retained root stop identities remain authoritative across retries and boots.
    # Do not unmask or activate while any recorded root helper remains undrained.
    stop_service(unit_name, scope='system')
    _remove_legacy_persistent_mask(record, record_path)
    _systemctl(system_paths, 'daemon-reload', timeout=30)

    for index, item in enumerate(record['files']):
        if not isinstance(item, dict):
            _fail('legacy migration file manifest is invalid')
        source = _safe_legacy_destination(str(item.get('source', '')))
        relative = Path(str(item.get('snapshot', '')))
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            _fail('legacy migration snapshot contains an unsafe relative path')
        _restore_legacy_file(
            source,
            directory / 'private' / relative,
            item,
            record,
            record_path,
            index,
        )

    _lock_services_directory_for_restore(homebrew, record, record_path)
    homebrew_fd, services_fd, _ = _open_recorded_services(homebrew, record)
    try:
        _verify_legacy_runtime(
            services_fd,
            unit_name,
            record.get('legacy_runtime'),
        )
    finally:
        os.close(services_fd)
        os.close(homebrew_fd)
    binary_state = record.get('binary')
    if not isinstance(binary_state, dict):
        _fail('legacy migration binary snapshot is invalid')
    _restore_legacy_binary(
        homebrew,
        record,
        record_path,
        directory / 'private' / 'snapshots' / 'binary',
        user,
    )
    _restore_services_directory_metadata(homebrew, record)

    homebrew_fd, services_fd, _ = _open_recorded_services(homebrew, record)
    try:
        _verify_legacy_runtime(
            services_fd,
            unit_name,
            record.get('legacy_runtime'),
        )
        # services/ is root-owned again; confirm the binary root will start is the snapshot.
        if binary_state.get('exists') is True:
            if not _object_matches_snapshot(services_fd, 'PluginLoader', {'kind': 'file', **binary_state}):
                _fail('restored legacy backend binary differs from its migration snapshot')
        else:
            try:
                os.stat('PluginLoader', dir_fd=services_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                _fail('a backend binary appeared where the legacy service had none')
    finally:
        os.close(services_fd)
        os.close(homebrew_fd)
    _systemctl(system_paths, 'daemon-reload', timeout=30)
    activation = record.get('activation')
    if not isinstance(activation, dict):
        _fail('legacy migration activation snapshot is invalid')
    _set_enabled(system_paths, activation)
    if activation.get('running'):
        _systemctl(system_paths, 'start', unit_name, timeout=45)
        _wait_running(system_paths)
        legacy_active = homebrew / 'services' / '.decky-installer' / unit_name / 'active.json'
        health_paths = {
            **system_paths,
            'binary': homebrew / 'services' / 'PluginLoader',
            'active': legacy_active,
            'homebrew': homebrew,
        }
        if legacy_active.exists():
            _health(health_paths)
    else:
        _wait_stopped(system_paths)
    record['phase'] = 'restored'
    _write_legacy_record(record_path, record)
    return {
        'ok': True,
        'command': 'restore-legacy-system',
        'service': unit_name,
        'user': user.pw_name,
        'status': str(directory / 'status.json'),
        'restored_running': bool(activation.get('running')),
        'restored_enabled': bool(activation.get('enabled')),
    }

def _remove_root_tree_at(parent_fd: int, name: str) -> bool:
    """Remove a root-owned directory tree by descriptor; return False when absent."""
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        _fail(f'refusing to remove {name}: it is not a root-owned directory')
    directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        opened = os.fstat(directory_fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            _fail(f'{name} changed while opening it for removal')
        _clear_directory_fd(directory_fd)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)
    os.fsync(parent_fd)
    return True


def _remove_legacy_runtime_state(homebrew: Path, unit_name: str) -> None:
    """Remove the old root loader's per-unit state below homebrew/services."""
    homebrew_fd = os.open(homebrew, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        services_fd = os.open('services', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=homebrew_fd)
    finally:
        os.close(homebrew_fd)
    try:
        for control in ('.decky-installer', '.decky-deploy'):
            try:
                info = os.stat(control, dir_fd=services_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
                _fail(f'refusing to clean services/{control}: it is not a root-owned directory')
            control_fd = os.open(control, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=services_fd)
            try:
                opened = os.fstat(control_fd)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    _fail(f'services/{control} changed while opening it')
                _remove_root_tree_at(control_fd, unit_name)
                empty = not os.listdir(control_fd)
            finally:
                os.close(control_fd)
            if empty:
                os.rmdir(control, dir_fd=services_fd)
                os.fsync(services_fd)
    finally:
        os.close(services_fd)


def _run_legacy_forget(args: argparse.Namespace) -> dict[str, object]:
    """Delete the saved legacy service after a completed migration. Restore is then impossible."""
    _require_root()
    user = pwd.getpwnam(args.user)
    homebrew = _homebrew_path(args.homebrew)
    directory, record = _read_legacy_record(homebrew, args.service_name, user.pw_uid)
    if user.pw_uid == 0 or user.pw_uid != record['user_uid'] or user.pw_name != record['user']:
        _fail('legacy migration snapshot belongs to a different service account')
    phase = record.get('phase')
    if phase not in ('services-user-owned', 'forgetting', 'forgotten'):
        _fail(f'forget-legacy-system needs a completed migration; the current phase is {phase}')
    record_path = directory / 'private' / 'record.json'
    unit_name = str(record['service'])
    if phase != 'forgotten':
        system = _unit_properties(_system_unit_paths(unit_name))
        if system.get('LoadState') != 'masked' or system.get('MainPID', '0') != '0':
            _fail('the legacy system service is not masked and stopped; refusing to delete its saved copy')
        user_unit = Path(str(record['user_unit_directory'])) / unit_name
        if _lstat_regular_or_absent(user_unit) is None:
            _fail(f'the user service unit is missing ({user_unit}); install it before deleting the legacy copy')
        record['phase'] = 'forgetting'
        _write_legacy_record(record_path, record)
        # Keep the /dev/null mask so the legacy unit can never start again.
        for item in record['files']:
            parked = item.get('parked_path') if isinstance(item, dict) else None
            if not parked:
                continue
            path = _safe_legacy_destination(str(parked))
            parent_fd = _open_root_directory(path.parent)
            try:
                try:
                    info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if [info.st_dev, info.st_ino] != item.get('identity'):
                    _fail(f'parked legacy unit changed; refusing to delete it: {path}')
                os.unlink(path.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        _remove_legacy_dropins(record, unit_name)
        _remove_legacy_runtime_state(homebrew, unit_name)
        _, descriptors = _open_legacy_store(user.pw_uid, unit_name, create=False)
        try:
            _discard_incomplete_legacy_snapshots(descriptors['private'])
        finally:
            _close_legacy_descriptors(descriptors)
        record['phase'] = 'forgotten'
        _write_legacy_record(record_path, record)
    # The helper goes last, so an interrupted run can still be repeated from it.
    _, descriptors = _open_legacy_store(user.pw_uid, unit_name, create=False)
    try:
        for name in ('install.py', 'process_observer.py'):
            try:
                os.unlink(name, dir_fd=descriptors['helper'])
            except FileNotFoundError:
                pass
        os.fsync(descriptors['helper'])
    finally:
        _close_legacy_descriptors(descriptors)
    return {'ok': True, 'command': 'forget-legacy-system', 'service': unit_name, 'phase': 'forgotten'}


def _remove_legacy_dropins(record: dict[str, Any], unit_name: str) -> None:
    """Delete the legacy unit's /etc drop-ins that are unchanged since migration.

    They are inert behind the mask. A drop-in that changed after migration is kept.
    """
    directory = Path('/etc/systemd/system') / f'{unit_name}.d'
    try:
        parent_fd = _open_root_directory(directory)
    except FileNotFoundError:
        return
    try:
        for item in record['files']:
            if not isinstance(item, dict):
                continue
            source = _safe_legacy_destination(str(item.get('source', '')))
            if source.parent != directory:
                continue
            try:
                info = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if [info.st_dev, info.st_ino] == item.get('identity') and _object_matches_snapshot(
                parent_fd, source.name, item
            ):
                os.unlink(source.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        empty = not os.listdir(parent_fd)
    finally:
        os.close(parent_fd)
    if empty:
        try:
            directory.rmdir()
        except OSError:
            pass


# Root plugin host (docs/root-plugin-host.md). Root plugins run in a separate,
# socket-activated system service; everything else stays in the user service.

def _root_host_unit_name(user: pwd.struct_passwd) -> str:
    return f'decky-root-plugins-{user.pw_name}'


def _root_host_units(user: pwd.struct_passwd, group: str, runtime: Path, config: Path) -> dict[str, str]:
    """The socket and service unit files, by file name."""
    for path in (runtime, config):
        if not re.fullmatch(r'/[A-Za-z0-9._/-]+', str(path)):
            _fail(f'root host path has unsupported characters: {path}')
    name = _root_host_unit_name(user)
    socket_path = ROOT_HOST_RUN / str(user.pw_uid) / 'root-host.sock'
    return {
        f'{name}.socket': (
            '[Unit]\n'
            f'Description=Decky Loader root plugin host socket for {user.pw_name}\n\n'
            '[Socket]\n'
            f'ListenStream={socket_path}\n'
            f'SocketUser={user.pw_name}\n'
            f'SocketGroup={group}\n'
            'SocketMode=0600\n'
            'DirectoryMode=0711\n'
            'RemoveOnStop=yes\n'
            'Accept=no\n\n'
            '[Install]\n'
            'WantedBy=sockets.target\n'
        ),
        f'{name}.service': (
            '[Unit]\n'
            f'Description=Decky Loader root plugin host for {user.pw_name}\n'
            f'Requires={name}.socket\n'
            f'After={name}.socket\n\n'
            '[Service]\n'
            'Type=simple\n'
            f'ExecStart={runtime / WORKER_EXECUTABLE} --root-host --config {config}\n'
            'KillMode=control-group\n'
            'Restart=no\n'
        ),
    }


def _root_directory(path: Path, mode: int) -> None:
    """Create or check a folder that only root can change."""
    try:
        path.mkdir(mode=mode)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        _fail(f'{path} must be a root-owned folder')
    os.chmod(path, mode)


def _change_tree_owner(directory_fd: int, uid: int, gid: int, *, clear_group_other_write: bool) -> None:
    """Give a plugin folder tree to uid:gid without following links.

    Each item is changed through its own descriptor, so a renamed or replaced
    item cannot redirect the change. Regular files with more than one link are
    refused: they could be another file outside the folder.
    """
    def adjust(descriptor: int) -> None:
        info = os.fstat(descriptor)
        os.fchown(descriptor, uid, gid)
        mode = stat.S_IMODE(info.st_mode) & ~0o6000
        if clear_group_other_write:
            mode &= ~0o022
        os.fchmod(descriptor, mode)

    adjust(directory_fd)
    with os.scandir(directory_fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            os.chown(name, uid, gid, dir_fd=directory_fd, follow_symlinks=False)
        elif stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
            try:
                _change_tree_owner(child, uid, gid, clear_group_other_write=clear_group_other_write)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory_fd)
            try:
                opened = os.fstat(child)
                if opened.st_nlink != 1 or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    _fail(f'plugin file has extra links or changed while it was adopted: {name}')
                adjust(child)
            finally:
                os.close(child)
        else:
            _fail(f'plugin folder contains an unsupported item: {name}')


_PLUGIN_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_plugins_root(homebrew: Path, user: pwd.struct_passwd) -> int:
    """Open <homebrew>/plugins one level at a time without following links.

    The folder must belong to the user, so a redirected path cannot make root
    change a root-owned folder somewhere else. The root host uses the same rule.
    """
    descriptor = os.open('/', _PLUGIN_DIRECTORY_FLAGS)
    try:
        for part in (*homebrew.parts[1:], 'plugins'):
            child = os.open(part, _PLUGIN_DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        if os.fstat(descriptor).st_uid != user.pw_uid:
            _fail(f'{homebrew / "plugins"} must belong to {user.pw_name}')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_plugin_directory(homebrew: Path, directory: str, user: pwd.struct_passwd) -> int:
    if directory in ('', '.', '..') or '/' in directory:
        _fail(f'plugin folder must be a plain name: {directory!r}')
    root = _open_plugins_root(homebrew, user)
    try:
        return os.open(directory, _PLUGIN_DIRECTORY_FLAGS, dir_fd=root)
    finally:
        os.close(root)


def _plugin_requires_root(directory_fd: int) -> bool:
    descriptor = os.open('plugin.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory_fd)
    with os.fdopen(descriptor, 'rb') as stream:
        metadata = json.loads(stream.read(1 << 20))
    flags = metadata.get('flags') if isinstance(metadata, dict) else None
    return isinstance(flags, list) and 'root' in flags


def _copy_tree_at(source_fd: int, destination_fd: int) -> None:
    """Copy a plugin tree into new files that root creates.

    New files matter: a user process may hold files of the old tree open for
    writing, and it must not be able to change code that root later runs.
    """
    with os.scandir(source_fd) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        info = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            os.mkdir(name, 0o755, dir_fd=destination_fd)
            source = os.open(name, _PLUGIN_DIRECTORY_FLAGS, dir_fd=source_fd)
            try:
                destination = os.open(name, _PLUGIN_DIRECTORY_FLAGS, dir_fd=destination_fd)
                try:
                    os.fchmod(destination, 0o755)
                    _copy_tree_at(source, destination)
                finally:
                    os.close(destination)
            finally:
                os.close(source)
        elif stat.S_ISREG(info.st_mode):
            source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=source_fd)
            try:
                if not stat.S_ISREG(os.fstat(source).st_mode):
                    _fail(f'plugin item changed while it was copied: {name}')
                destination = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=destination_fd)
                try:
                    while chunk := os.read(source, 1024 * 1024):
                        os.write(destination, chunk)
                    os.fchmod(destination, stat.S_IMODE(info.st_mode) & 0o755)
                finally:
                    os.close(destination)
            finally:
                os.close(source)
        elif stat.S_ISLNK(info.st_mode):
            # The root host's folder check refuses links that leave the folder.
            os.symlink(os.readlink(name, dir_fd=source_fd), name, dir_fd=destination_fd)
        else:
            _fail(f'plugin folder contains an unsupported item: {name}')


def _root_host_tree_hash(runtime: Path, directory_fd: int, label: str) -> dict[str, Any]:
    """Check and hash a root-owned plugin folder with the root runtime's own code."""
    result = subprocess.run(
        [str(runtime / WORKER_EXECUTABLE), '--root-host-admin', 'tree-hash', '--fd', str(directory_fd)],
        check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
        env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/root'}, cwd='/', pass_fds=(directory_fd,),
    )
    if result.returncode:
        _fail(f'could not check root plugin {label}: {result.stderr.strip() or result.stdout.strip()}')
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise InstallerError(f'root plugin check returned invalid JSON for {label}') from error
    if not isinstance(value, dict) or not re.fullmatch(r'[0-9a-f]{64}', str(value.get('tree_sha256'))):
        _fail(f'root plugin check returned no tree hash for {label}')
    return {'name': value.get('name'), 'version': value.get('version'), 'tree_sha256': value['tree_sha256']}


def _adopt_root_plugin(runtime: Path, homebrew: Path, directory: str, user: pwd.struct_passwd) -> dict[str, Any]:
    """Approve an installed plugin: replace its folder with a root-owned copy and hash the copy."""
    if directory in ('', '.', '..') or '/' in directory:
        _fail(f'plugin folder must be a plain name: {directory!r}')
    plugins_fd = _open_plugins_root(homebrew, user)
    stage = f'.decky-root-adopt-{uuid.uuid4().hex}'
    try:
        source_fd = os.open(directory, _PLUGIN_DIRECTORY_FLAGS, dir_fd=plugins_fd)
        try:
            if not _plugin_requires_root(source_fd):
                _fail(f'{directory} is not marked as a plugin that needs root')
            os.mkdir(stage, 0o700, dir_fd=plugins_fd)
            stage_fd = os.open(stage, _PLUGIN_DIRECTORY_FLAGS, dir_fd=plugins_fd)
            try:
                info = os.fstat(stage_fd)
                if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
                    _fail('the root staging folder was replaced')
                _copy_tree_at(source_fd, stage_fd)
                os.fchmod(stage_fd, 0o755)
                approved = _root_host_tree_hash(runtime, stage_fd, directory)
            finally:
                os.close(stage_fd)
        finally:
            os.close(source_fd)
        old = f'.decky-root-old-{uuid.uuid4().hex}'
        os.rename(directory, old, src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd)
        os.rename(stage, directory, src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd)
        stage = ''
        shutil.rmtree(old, dir_fd=plugins_fd, ignore_errors=True)
        return approved
    finally:
        if stage:
            shutil.rmtree(stage, dir_fd=plugins_fd, ignore_errors=True)
        os.close(plugins_fd)


def _read_root_host_json(path: Path) -> dict[str, Any] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        _fail(f'{path} must be a root-owned file that only root can change')
    value = _read_json(path)
    return value if isinstance(value, dict) else None


def _install_root_runtime(bundle: Path, user: pwd.struct_passwd, state: Path, created: list[Path]) -> tuple[Path, dict[str, Any]]:
    """Copy a bundle to a root-owned runtime folder, then check the copy.

    The copy is checked, not the source, because the user can change the source.
    """
    parent = ROOT_HOST_STATE.parent.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022:
        _fail(f'{ROOT_HOST_STATE.parent.parent} must be a folder that only root can change')
    for path in (ROOT_HOST_STATE.parent, ROOT_HOST_STATE, state, state / 'runtimes'):
        if not path.exists():
            created.append(path)
        _root_directory(path, 0o755)
    stage = state / 'runtimes' / f'.stage-{uuid.uuid4().hex}'
    created.append(stage)
    shutil.copytree(bundle, stage, symlinks=True, copy_function=shutil.copy2)
    stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _change_tree_owner(stage_fd, 0, 0, clear_group_other_write=True)
        # The copy keeps its source's mode; a download unpacks into a 0700 folder.
        # The user service checks and reads the runtime, so it must be 0755.
        os.fchmod(stage_fd, 0o755)
    finally:
        os.close(stage_fd)
    _, manifest = _validate_bundle(str(stage), user)
    runtime = state / 'runtimes' / _digest(stage / 'bundle.json')[:24]
    if runtime.exists():
        shutil.rmtree(stage)
    else:
        stage.rename(runtime)
        created.append(runtime)
    created.remove(stage)
    _validate_bundle(str(runtime), user)
    _verify_runtime(runtime, manifest, user, runtime / RUST_EXECUTABLE)
    return runtime, manifest


def _write_root_host_files(user: pwd.struct_passwd, homebrew: Path, state: Path, runtime: Path, manifest: dict[str, Any]) -> None:
    """Write config.json and the units for `runtime`, then reload systemd."""
    import grp

    config = state / 'config.json'
    previous = _read_root_host_json(config) or {}
    _write_json(config, {
        'protocol': ROOT_HOST_PROTOCOL, 'user': user.pw_name, 'uid': user.pw_uid, 'gid': user.pw_gid,
        'homebrew': str(homebrew), 'runtime': runtime.name, 'release_version': manifest.get('release_version'),
        'store_hosts': list(ROOT_HOST_STORE_HOSTS),
        'release_repository': previous.get('release_repository') or DEFAULT_RELEASE_REPOSITORY,
    }, 0o644)
    group = grp.getgrgid(user.pw_gid).gr_name
    for name, text in _root_host_units(user, group, runtime, config).items():
        _write_atomic(ROOT_HOST_UNIT_DIRECTORY / name, text.encode(), 0o644)
    _systemctl({'unit': Path(f'{_root_host_unit_name(user)}.socket'), 'scope': 'system'}, 'daemon-reload')


def _prune_root_runtimes(state: Path, keep: set[str]) -> None:
    for runtime in (state / 'runtimes').iterdir():
        if runtime.name not in keep and not runtime.is_symlink():
            shutil.rmtree(runtime, ignore_errors=True)


def _run_enable_root_plugins(args: argparse.Namespace) -> dict[str, object]:
    _require_root()
    user = pwd.getpwnam(args.user)
    if user.pw_uid == 0 or user.pw_name != args.user:
        _fail('--user must name the non-root account that runs Decky')
    homebrew = _homebrew_path(args.homebrew)
    state = ROOT_HOST_STATE / str(user.pw_uid)
    paths: dict[str, Any] = {'unit': Path(f'{_root_host_unit_name(user)}.socket'), 'scope': 'system'}
    created: list[Path] = []
    adopted: list[str] = []
    try:
        runtime, manifest = _install_root_runtime(Path(args.bundle), user, state, created)
        plugins: dict[str, Any] = dict((_read_root_host_json(state / 'state.json') or {}).get('plugins') or {})
        for directory in args.adopt or ():
            plugins[directory] = _adopt_root_plugin(runtime, homebrew, directory, user)
            adopted.append(directory)
        _write_json(state / 'state.json', {'plugins': plugins}, 0o644)
        _write_root_host_files(user, homebrew, state, runtime, manifest)
        # A running host keeps its old runtime until it stops; it holds no state.
        _systemctl(paths, 'stop', f'{_root_host_unit_name(user)}.service', check=False)
        _systemctl(paths, 'enable', '--now', paths['unit'].name)
        _prune_root_runtimes(state, {runtime.name})
    except BaseException:
        for directory in adopted:
            try:
                directory_fd = _open_plugin_directory(homebrew, directory, user)
                try:
                    _change_tree_owner(directory_fd, user.pw_uid, user.pw_gid, clear_group_other_write=False)
                finally:
                    os.close(directory_fd)
            except (OSError, InstallerError):
                pass
        for path in reversed(created):
            shutil.rmtree(path, ignore_errors=True)
        raise
    return {
        'ok': True, 'root_host': 'enabled', 'runtime': runtime.name, 'adopted': adopted,
        'plugins': sorted(plugins), 'socket': str(ROOT_HOST_RUN / str(user.pw_uid) / 'root-host.sock'),
    }


ROOT_HOST_RELEASE_TAG = re.compile(r'v\d+\.\d+\.\d+-rust\.\d+(-rc\.\d+)?')
ROOT_HOST_BUNDLE_ASSET = 'decky-loader-runtime-linux-x86_64.zip'


def _download_release_file(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent': 'Decky root plugin host'})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            data = response.read(limit + 1)
    except (OSError, urllib.error.URLError) as error:
        raise InstallerError(f'could not download {url}: {error}') from error
    if len(data) > limit:
        _fail(f'download is larger than {limit} bytes: {url}')
    return data


def _extract_runtime_zip(archive: Path, destination: Path) -> None:
    """Unpack a release bundle ZIP with the same member rules as install-to-deck.sh."""
    import zipfile

    destination.mkdir(mode=0o700)
    with zipfile.ZipFile(archive) as source:
        names = [info.filename for info in source.infolist()]
        if len(names) != len(set(names)):
            _fail('runtime ZIP contains a duplicate member')
        for info in source.infolist():
            name = info.filename.rstrip('/') if info.is_dir() else info.filename
            path = PurePosixPath(name)
            mode = info.external_attr >> 16
            if not name or path.is_absolute() or '..' in path.parts or path.as_posix() != name or '\\' in name or '\x00' in name:
                _fail(f'runtime ZIP contains an unsafe member path: {info.filename}')
            target = destination.joinpath(*path.parts)
            if info.is_dir():
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            if not stat.S_ISREG(mode) or mode & 0o6022:
                _fail(f'runtime ZIP contains a non-regular or unsafe file: {name}')
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with os.fdopen(descriptor, 'wb') as output, source.open(info) as member:
                shutil.copyfileobj(member, output)
                os.fchmod(output.fileno(), stat.S_IMODE(mode) & 0o755)


def _run_update_root_host(args: argparse.Namespace) -> dict[str, object]:
    """Install the root runtime of release `--tag`, downloaded from the fork's releases.

    The root host starts this when the loader reports a different version. It
    only writes files; the running host switches at its next start.
    """
    _require_root()
    user = pwd.getpwnam(args.user)
    if user.pw_uid == 0 or user.pw_name != args.user:
        _fail('--user must name the non-root account that runs Decky')
    if not ROOT_HOST_RELEASE_TAG.fullmatch(args.tag):
        _fail(f'not a release tag of this fork: {args.tag}')
    homebrew = _homebrew_path(args.homebrew)
    state = ROOT_HOST_STATE / str(user.pw_uid)
    config = _read_root_host_json(state / 'config.json')
    if config is None:
        _fail('root plugin support is not enabled')
    if config.get('release_version') == args.tag:
        return {'ok': True, 'root_host': 'current', 'release_version': args.tag}
    repository = str(config.get('release_repository') or DEFAULT_RELEASE_REPOSITORY)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        _fail(f'invalid release repository: {repository}')
    base = f'https://github.com/{repository}/releases/download/{args.tag}/{ROOT_HOST_BUNDLE_ASSET}'
    sidecar = _download_release_file(base + '.sha256', 1024).decode('utf-8', 'replace')
    match = re.fullmatch(rf'([0-9a-f]{{64}})  {re.escape(ROOT_HOST_BUNDLE_ASSET)}\n?', sidecar)
    if match is None:
        _fail(f'{ROOT_HOST_BUNDLE_ASSET}.sha256 is malformed')
    work = state / 'runtimes' / f'.download-{uuid.uuid4().hex}'
    created: list[Path] = []
    _root_directory(state / 'runtimes', 0o755)
    work.mkdir(mode=0o700)
    try:
        archive = work / ROOT_HOST_BUNDLE_ASSET
        _write_atomic(archive, _download_release_file(base, 512 * 1024 * 1024), 0o600)
        if _digest(archive) != match.group(1):
            _fail(f'downloaded {ROOT_HOST_BUNDLE_ASSET} does not match its checksum file')
        _extract_runtime_zip(archive, work / 'bundle')
        runtime, manifest = _install_root_runtime(work / 'bundle', user, state, created)
        if manifest.get('release_version') != args.tag:
            _fail(f'release {args.tag} contains bundle {manifest.get("release_version")}')
        _write_root_host_files(user, homebrew, state, runtime, manifest)
        # Keep the runtime that the host runs now; it switches at its next start.
        _prune_root_runtimes(state, {runtime.name, str(config.get('runtime'))})
    except BaseException:
        for path in reversed(created):
            if path.parent == state / 'runtimes':
                shutil.rmtree(path, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {'ok': True, 'root_host': 'updated', 'runtime': runtime.name, 'release_version': args.tag}


def _run_disable_root_plugins(args: argparse.Namespace) -> dict[str, object]:
    _require_root()
    user = pwd.getpwnam(args.user)
    if user.pw_uid == 0 or user.pw_name != args.user:
        _fail('--user must name the non-root account that runs Decky')
    homebrew = _homebrew_path(args.homebrew)
    state = ROOT_HOST_STATE / str(user.pw_uid)
    name = _root_host_unit_name(user)
    paths: dict[str, Any] = {'unit': Path(f'{name}.socket'), 'scope': 'system'}
    _systemctl(paths, 'disable', '--now', f'{name}.socket', check=False)
    _systemctl(paths, 'stop', f'{name}.service', check=False)
    for unit in (f'{name}.socket', f'{name}.service'):
        (ROOT_HOST_UNIT_DIRECTORY / unit).unlink(missing_ok=True)
    _systemctl(paths, 'daemon-reload')
    returned: list[str] = []
    plugins = (_read_root_host_json(state / 'state.json') or {}).get('plugins') or {}
    for directory in sorted(plugins):
        try:
            directory_fd = _open_plugin_directory(homebrew, directory, user)
        except FileNotFoundError:
            continue
        try:
            if os.fstat(directory_fd).st_uid != 0:
                continue
            _change_tree_owner(directory_fd, user.pw_uid, user.pw_gid, clear_group_other_write=False)
        finally:
            os.close(directory_fd)
        returned.append(directory)
    shutil.rmtree(state, ignore_errors=True)
    shutil.rmtree(ROOT_HOST_RUN / str(user.pw_uid), ignore_errors=True)
    for path in (ROOT_HOST_STATE, ROOT_HOST_STATE.parent):
        try:
            path.rmdir()
        except OSError:
            break
    return {'ok': True, 'root_host': 'disabled', 'returned_to_user': returned}


def _root_host_status(user: pwd.struct_passwd) -> dict[str, object]:
    state = ROOT_HOST_STATE / str(user.pw_uid)
    config = _read_root_host_json(state / 'config.json')
    if config is None:
        return {'enabled': False}
    plugins = (_read_root_host_json(state / 'state.json') or {}).get('plugins') or {}
    name = _root_host_unit_name(user)
    paths: dict[str, Any] = {'unit': Path(f'{name}.socket'), 'scope': 'system'}
    states = {}
    for unit in (f'{name}.socket', f'{name}.service'):
        result = _systemctl(paths, 'show', unit, '-p', 'ActiveState', '-p', 'MainPID', check=False)
        states[unit] = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    return {
        'enabled': True,
        'runtime': config.get('runtime'),
        'release_version': config.get('release_version'),
        'socket': states[f'{name}.socket'].get('ActiveState'),
        'host': states[f'{name}.service'].get('ActiveState'),
        'host_pid': int(states[f'{name}.service'].get('MainPID') or 0) or None,
        'plugins': {directory: entry.get('name') for directory, entry in sorted(plugins.items()) if isinstance(entry, dict)},
    }


def _verify_bundle(args: argparse.Namespace) -> tuple[pwd.struct_passwd, Path, str, Path, dict[str, Any]]:
    user = pwd.getpwnam(args.user)
    _require_user(user)
    if args.user != user.pw_name:
        _fail('pass the resolved Unix account name with --user')
    _, service_filename = _normalize_service_name(args.service_name)
    homebrew = _homebrew_path(args.homebrew, allow_missing=True)
    bundle, manifest = _validate_bundle(args.bundle, user)
    if args.version != manifest['release_version']:
        _fail('--version must match the bundle release identity')
    _verify_runtime(bundle, manifest, user, bundle / RUST_EXECUTABLE)
    return user, homebrew, service_filename, bundle, manifest


def _verify_bundle_command(args: argparse.Namespace) -> dict[str, object]:
    user, homebrew, service_filename, bundle, manifest = _verify_bundle(args)
    return {
        'ok': True,
        'command': 'verify-bundle',
        'service': service_filename,
        'user': user.pw_name,
        'homebrew': str(homebrew),
        'version': manifest['release_version'],
        'bundle': str(bundle),
    }


def _check_bundle(args: argparse.Namespace) -> dict[str, object]:
    user, homebrew, service_filename, bundle, manifest = _verify_bundle(args)
    unit_directory = _user_unit_directory(user, create=False)
    paths: dict[str, Any] = {
        'homebrew': homebrew,
        'unit': unit_directory / service_filename,
        'unit_dir': unit_directory,
        'scope': 'user',
        'user': user,
    }
    migration_preflight: dict[str, object] | None = None
    if getattr(args, 'migrate_legacy', False):
        migration_preflight = _check_legacy_migration(
            homebrew,
            service_filename,
            user,
            user_unit_directory=str(unit_directory),
        )
    else:
        _assert_no_system_instance(paths)
    _assert_user_unit_source(paths)
    response: dict[str, object] = {
        'ok': True,
        'command': 'check-bundle',
        'service': service_filename,
        'version': manifest['release_version'],
        'bundle': str(bundle),
        'unit_directory': str(unit_directory),
    }
    if migration_preflight is not None:
        response['legacy_migration'] = migration_preflight
    return response


def _activate_binary(args: argparse.Namespace, paths: dict[str, Any]) -> dict[str, object]:
    _assert_no_system_instance(paths)
    _assert_user_unit_source(paths)
    activation = _capture_activation(paths)
    if activation.get('load_state') != 'loaded':
        _fail(f'user service {paths["unit"].name} is not installed')
    stop_service(paths['unit'].name)
    if activation.get('running'):
        _systemctl(paths, 'start', paths['unit'].name, timeout=45)
        _wait_running(paths)
        _health(paths)
    else:
        _wait_stopped(paths)
    return {
        'ok': True,
        'command': 'activate-binary',
        'service': paths['unit'].name,
        'active': bool(activation.get('running')),
    }

def _locked(paths: dict[str, Path]):
    lock_path = paths['lock']
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(lock_path, flags, 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        os.close(descriptor)
        _fail('installer lock is not a protected file owned by the effective account')
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--homebrew', required=True, metavar='DIR')
    parser.add_argument('--service-name', required=True, metavar='NAME')


def _sample_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f'not a number: {value!r}') from None
    if not 0 <= seconds <= 60:
        raise argparse.ArgumentTypeError('must be from 0 to 60 seconds')
    return seconds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    install = subparsers.add_parser('install', help='transactionally install a verified Rust runtime bundle')
    install.add_argument('--bundle', required=True, metavar='DIR')
    install.add_argument('--homebrew', required=True, metavar='DIR')
    install.add_argument('--user', required=True, metavar='USER')
    install.add_argument('--service-name', required=True, metavar='NAME')
    install.add_argument('--listen', default=DEFAULT_LISTEN, metavar='ADDRESS')
    install.add_argument('--cef-url', default=DEFAULT_CEF_URL, metavar='URL')
    install.add_argument('--inject-frontend', choices=('true', 'false'), default='true')
    install.add_argument('--worker-mode', choices=('exec', 'fork'), default='fork', help='forked plugin processes by default; exec starts each Python worker independently')
    install.add_argument('--version', metavar='STRING')
    install.add_argument('--github-api', default=DEFAULT_GITHUB_API, metavar='URL')
    install.add_argument('--release-repository', default=DEFAULT_RELEASE_REPOSITORY, metavar='OWNER/REPOSITORY')
    install.add_argument('--testing-download-url', default=DEFAULT_TESTING_DOWNLOAD_URL, metavar='URL')
    rollback = subparsers.add_parser('rollback', help='restore the previous recoverable binary, unit, and activation state')
    _add_common(rollback)
    status = subparsers.add_parser('status', help='report selected service and recoverable installer state')
    _add_common(status)
    status.add_argument('--sample-seconds', type=_sample_seconds, default=1.0, metavar='SECONDS', help='CPU percentage sampling window; 0 skips sampling (default 1)')
    check_bundle = subparsers.add_parser('check-bundle', help='verify a bundle and user-service target without installing')
    check_bundle.add_argument('--bundle', required=True, metavar='DIR')
    check_bundle.add_argument('--version', required=True, metavar='STRING')
    check_bundle.add_argument('--homebrew', required=True, metavar='DIR')
    check_bundle.add_argument('--user', required=True, metavar='USER')
    check_bundle.add_argument('--service-name', required=True, metavar='NAME')
    check_bundle.add_argument('--migrate-legacy', action='store_true', help='check migration from the old system service without changing it')
    verify_bundle = subparsers.add_parser('verify-bundle', help='verify a runtime bundle without touching service state')
    verify_bundle.add_argument('--bundle', required=True, metavar='DIR')
    verify_bundle.add_argument('--version', required=True, metavar='STRING')
    verify_bundle.add_argument('--homebrew', required=True, metavar='DIR')
    verify_bundle.add_argument('--user', required=True, metavar='USER')
    verify_bundle.add_argument('--service-name', required=True, metavar='NAME')
    activate = subparsers.add_parser('activate-binary', help='restart the selected user service after binary replacement')
    _add_common(activate)
    install_bundle = subparsers.add_parser('queue-install-bundle', help='durably hand off a verified Rust bundle installation')
    install_bundle.add_argument('--bundle', required=True, metavar='DIR')
    install_bundle.add_argument('--version', required=True, metavar='STRING')
    install_bundle.add_argument('--homebrew', required=True, metavar='DIR')
    install_bundle.add_argument('--service-name', required=True, metavar='NAME')
    install_bundle.add_argument('--user', required=True, metavar='USER')
    install_bundle.add_argument('--listen', required=True, metavar='ADDRESS')
    install_bundle.add_argument('--cef-url', required=True, metavar='URL')
    install_bundle.add_argument('--inject-frontend', choices=('true', 'false'), required=True)
    install_bundle.add_argument('--worker-mode', choices=('exec', 'fork'), required=True)
    install_bundle.add_argument('--github-api', default=DEFAULT_GITHUB_API, metavar='URL')
    install_bundle.add_argument('--release-repository', default=DEFAULT_RELEASE_REPOSITORY, metavar='OWNER/REPOSITORY')
    install_bundle.add_argument('--testing-download-url', default=DEFAULT_TESTING_DOWNLOAD_URL, metavar='URL')
    queued = subparsers.add_parser('activate-queued', help=argparse.SUPPRESS)
    _add_common(queued)
    queued.add_argument('--request-id', required=True)
    release = subparsers.add_parser('release-queued', help=argparse.SUPPRESS)
    _add_common(release)
    release.add_argument('--request-id', required=True)
    migration = subparsers.add_parser('migrate-legacy-system', help='park a legacy system service before user-service installation')
    _add_common(migration)
    migration.add_argument('--user', required=True, metavar='USER')
    migration.add_argument('--user-unit-directory', metavar='DIR')
    restoration = subparsers.add_parser('restore-legacy-system', help='restore the saved legacy system service and activation state')
    _add_common(restoration)
    restoration.add_argument('--user', required=True, metavar='USER')
    restoration.add_argument('--user-unit-directory', metavar='DIR')
    forgetting = subparsers.add_parser('forget-legacy-system', help='delete the saved legacy system service after a completed migration')
    _add_common(forgetting)
    forgetting.add_argument('--user', required=True, metavar='USER')
    legacy_user_stop = subparsers.add_parser('legacy-user-stop', help=argparse.SUPPRESS)
    legacy_user_stop.add_argument('--user', required=True, metavar='USER')
    legacy_user_stop.add_argument('--service-name', required=True, metavar='NAME')
    legacy_user_stop.add_argument('--user-unit-directory', required=True, metavar='DIR')
    enable_root = subparsers.add_parser('enable-root-plugins', help='run plugins that need root in a separate root service (root only)')
    enable_root.add_argument('--bundle', required=True, metavar='DIR')
    enable_root.add_argument('--homebrew', required=True, metavar='DIR')
    enable_root.add_argument('--user', required=True, metavar='USER')
    enable_root.add_argument('--adopt', action='append', metavar='FOLDER', help='approve this installed plugin folder; it becomes owned by root')
    disable_root = subparsers.add_parser('disable-root-plugins', help='remove the root plugin service and give root plugins back to the user (root only)')
    disable_root.add_argument('--homebrew', required=True, metavar='DIR')
    disable_root.add_argument('--user', required=True, metavar='USER')
    update_root = subparsers.add_parser('update-root-host', help=argparse.SUPPRESS)
    update_root.add_argument('--homebrew', required=True, metavar='DIR')
    update_root.add_argument('--user', required=True, metavar='USER')
    update_root.add_argument('--tag', required=True, metavar='TAG')
    args = parser.parse_args()
    try:
        if args.command == 'legacy-user-stop':
            output = _run_legacy_user_stop(args)
        elif args.command == 'enable-root-plugins':
            output = _run_enable_root_plugins(args)
        elif args.command == 'disable-root-plugins':
            output = _run_disable_root_plugins(args)
        elif args.command == 'update-root-host':
            output = _run_update_root_host(args)
        elif args.command in ('migrate-legacy-system', 'restore-legacy-system', 'forget-legacy-system'):
            _require_root()
            user = pwd.getpwnam(args.user)
            homebrew = _homebrew_path(args.homebrew)
            directory = _legacy_migration_directory(
                homebrew,
                args.service_name,
                create=args.command == 'migrate-legacy-system',
                user_uid=user.pw_uid,
            )
            _, descriptors = _open_legacy_store(
                user.pw_uid,
                args.service_name,
                create=args.command == 'migrate-legacy-system',
            )
            try:
                lock_fd = os.open(
                    'lock',
                    os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0),
                    0o600,
                    dir_fd=descriptors['private'],
                )
                try:
                    lock_info = os.fstat(lock_fd)
                    if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != 0 or lock_info.st_mode & 0o077:
                        _fail('legacy migration lock is not a protected root file')
                    os.fchown(lock_fd, 0, 0)
                    os.fchmod(lock_fd, 0o600)
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    if args.command == 'migrate-legacy-system':
                        output = _run_legacy_migration(args)
                    elif args.command == 'forget-legacy-system':
                        output = _run_legacy_forget(args)
                    else:
                        output = _run_legacy_restore(args)
                finally:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)
            finally:
                _close_legacy_descriptors(descriptors)
        elif args.command in ('check-bundle', 'verify-bundle'):
            output = _check_bundle(args) if args.command == 'check-bundle' else _verify_bundle_command(args)
        else:
            user = _current_user()
            _require_user(user)
            if hasattr(args, 'user'):
                target = pwd.getpwnam(args.user)
                _require_user(target)
                if target.pw_name != args.user:
                    _fail('pass the resolved Unix account name with --user')
                user = target
            if args.command == 'install':
                _normalize_service_name(args.service_name)
                _validate_bundle(args.bundle, user)
            paths = _paths(
                _homebrew_path(args.homebrew, create=args.command == 'install'),
                args.service_name,
                user,
            )
            _assert_no_system_instance(paths)
            _assert_user_unit_source(paths)
            if args.command == 'queue-install-bundle':
                descriptor = _locked(paths)
                try:
                    _recover(paths)
                    handoff = _prepare_install_handoff(args, paths)
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
                output = _wait_for_handoff(
                    paths,
                    str(handoff['request_id']),
                    str(handoff['unit']),
                    args.command,
                )
            elif args.command == 'release-queued':
                output = _release_activation_handoff(args, paths)
            elif args.command == 'activate-queued':
                output = _run_queued_activation(args, paths)
            else:
                descriptor = _locked(paths)
                try:
                    _recover(paths)
                    if args.command == 'install':
                        output = _run_install(args)
                    elif args.command == 'rollback':
                        output = _run_rollback(args)
                    elif args.command == 'status':
                        output = _run_status(args)
                    elif args.command == 'activate-binary':
                        output = _activate_binary(args, paths)
                    else:
                        _fail('unsupported installer command')
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
                if args.command == 'status':
                    output['resources'] = _service_resources(paths, args.sample_seconds)
                    output['root_host'] = _root_host_status(user)
        print(json.dumps(output, sort_keys=True))
        return 0
    except (InstallerError, OSError, KeyError, ValueError) as error:
        print(f'{parser.prog}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
