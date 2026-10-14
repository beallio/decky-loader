#!/usr/bin/env python3
"""Transactional private Rust-runtime installer and updater activation helper."""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import ipaddress
import json
import os
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


KIND = 'decky-rust-runtime-probe'
BASELINE_TAG = 'v3.2.10'
UNIT_DIRECTORY = Path('/etc/systemd/system')
SYSTEMCTL_CANDIDATES = (Path('/usr/bin/systemctl'), Path('/bin/systemctl'))
DEFAULT_LISTEN = '127.0.0.1:1337'
DEFAULT_CEF_URL = 'http://127.0.0.1:8080'
RUST_EXECUTABLE = 'decky-rust-runtime-probe'
WORKER_EXECUTABLE = 'DeckyPluginWorker'
MAX_HTTP_HEADERS = 65536
MAX_WEBSOCKET_FRAME = 1024 * 1024


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
        return
    if not stat.S_ISDIR(result.st_mode):
        _fail(f'refusing non-directory or symlink: {path}')
    if managed and (result.st_uid != 0 or result.st_mode & 0o022):
        _fail(f'managed installer directory must be root-owned and not group/world writable: {path}')

def _require_root() -> None:
    if os.name != 'posix' or os.geteuid() != 0:
        _fail('installer mutations and status require root on Linux')


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


def _homebrew_path(value: str, *, create: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute():
        _fail('--homebrew must be an absolute path')
    try:
        resolved = path.resolve(strict=not create)
    except OSError as error:
        raise InstallerError(f'homebrew directory is unavailable: {error}') from error
    if create and not resolved.exists():
        resolved.mkdir(mode=0o755, parents=True)
    if not resolved.is_dir():
        _fail(f'homebrew path is not a directory: {resolved}')
    if any(character.isspace() or character in ('%', '$', '\\', '"') or ord(character) < 32 for character in str(resolved)):
        _fail('homebrew path contains characters unsafe for systemd unit expansion')
    return resolved


def _unit_directory() -> Path:
    for candidate in SYSTEMCTL_CANDIDATES:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            break
    else:
        _fail('systemctl was not found at /usr/bin/systemctl or /bin/systemctl')
    try:
        result = UNIT_DIRECTORY.lstat()
    except OSError as error:
        raise InstallerError(f'systemd unit directory is unavailable: {error}') from error
    if not stat.S_ISDIR(result.st_mode):
        _fail(f'refusing non-directory systemd unit path: {UNIT_DIRECTORY}')
    return UNIT_DIRECTORY


def _paths(homebrew: Path, service_name: str) -> dict[str, Path]:
    _, unit_filename = _normalize_service_name(service_name)
    services = homebrew / 'services'
    _ensure_directory(services, 0o755)
    service_root = services / '.decky-installer'
    _ensure_directory(service_root, 0o755, managed=True)
    state = service_root / unit_filename
    _ensure_directory(state, 0o755, managed=True)
    runtimes = state / 'runtimes'
    _ensure_directory(runtimes, 0o755, managed=True)
    history = state / 'history'
    _ensure_directory(history, 0o700, managed=True)
    os.chmod(history, 0o700)
    queue = state / 'queue'
    _ensure_directory(queue, 0o700, managed=True)
    os.chmod(queue, 0o700)
    unit_dir = _unit_directory()
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
    }


def _systemctl_path() -> Path:
    for candidate in SYSTEMCTL_CANDIDATES:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    _fail('systemctl was not found')


def _systemctl(paths: dict[str, Path], *arguments: str, check: bool = True, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    command = [_systemctl_path().as_posix(), '--system', *arguments]
    try:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallerError(f'cannot run systemctl for {paths["unit"].name}: {error}') from error
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f'exit status {result.returncode}'
        _fail(f'systemctl {arguments[0]} {paths["unit"].name} failed: {detail}')
    return result


def _unit_properties(paths: dict[str, Path]) -> dict[str, str]:
    result = _systemctl(
        paths,
        'show', paths['unit'].name,
        '--property=LoadState,ActiveState,SubState,MainPID,UnitFileState,ControlGroup',
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
    if manifest.get('kind') != KIND or manifest.get('private_only') is not True or manifest.get('distribution_approved') is not False:
        _fail('bundle is not the private Rust runtime compatibility bundle')
    if manifest.get('rust_executable') != RUST_EXECUTABLE:
        _fail('bundle names an unexpected Rust executable')
    source = manifest.get('source_baseline')
    if not isinstance(source, dict) or source.get('tag') != BASELINE_TAG:
        _fail(f'bundle must retain the {BASELINE_TAG} source baseline')
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
    if runtime.get('decky_version') != BASELINE_TAG:
        _fail(f'worker does not report the {BASELINE_TAG} source baseline')
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
    for required in (RUST_EXECUTABLE, WORKER_EXECUTABLE, 'install.py', 'assets/index.js', 'locales/en-US.json'):
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
    if user.pw_uid == 0:
        _fail('the service account must be non-root to validate bundle executables safely')
    try:
        groups = os.getgrouplist(user.pw_name, user.pw_gid)
    except AttributeError:
        groups = [user.pw_gid]
    def drop_privileges() -> None:
        os.setgroups(groups)
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
    environment = {
        'HOME': user.pw_dir,
        'USER': user.pw_name,
        'LOGNAME': user.pw_name,
        'PATH': '/usr/bin:/bin',
        'LANG': 'C.UTF-8',
        'LC_ALL': 'C.UTF-8',
    }
    try:
        return subprocess.run(
            command,
            check=False,
            cwd=user.pw_dir if Path(user.pw_dir).is_dir() else '/',
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            start_new_session=True,
            preexec_fn=drop_privileges,
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
        os.chmod(installer, 0o755)
        for name in ('assets', 'locales'):
            _copytree_checked(bundle / name, staging / name)
        if (bundle / '_internal').is_dir():
            _copytree_checked(bundle / '_internal', staging / '_internal')
        for path in (staging, *staging.rglob('*')):
            os.chown(path, 0, 0, follow_symlinks=False)
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
            if relative == WORKER_EXECUTABLE or relative == 'install.py' or relative.startswith(('assets/', 'locales/', '_internal/')):
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
        os.chown(stage, 0, 0)
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
    environment = [
        line for line in environment
        if not line.startswith(('Environment=SERVER_HOST=', 'Environment=SERVER_PORT='))
    ]
    environment.extend(f'Environment={assignment}' for assignment in assignments)
    # EnvironmentFile and drop-ins can override Environment. Set the selected
    # endpoint immediately before exec, matching Rust's explicit --listen.
    return environment, ['/usr/bin/env', *assignments]


def _generated_unit(paths: dict[str, Path], runtime: Path, user: str, service_name: str, listen: str, cef_url: str, inject_frontend: bool, worker_mode: str) -> bytes:
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
    ]
    command = ' '.join(_systemd_quote(argument) for argument in arguments)
    lines = [
        '[Unit]',
        'Description=SteamDeck Plugin Loader',
        'After=network.target',
        '',
        '[Service]',
        'Type=simple',
        'User=root',
        'Restart=always',
        'KillMode=process',
        'TimeoutStopSec=15',
        f'ExecStart={command}',
        f'WorkingDirectory={paths["services"]}',
        *environment,
        '',
        '[Install]',
        'WantedBy=multi-user.target',
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
        os.chown(temporary, 0, 0)
        _fsync_directory(directory)
        return temporary
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _validate_service_template(source: Path, paths: dict[str, Path], *, prepare: bool = True) -> bytes:
    info = _lstat_regular_or_absent(source)
    if info is None:
        _fail(f'service template is missing: {source}')
    try:
        raw = source.read_bytes()
        text = raw.decode('utf-8')
    except (OSError, UnicodeError) as error:
        raise InstallerError(f'cannot read service template: {error}') from error
    if '\x00' in text:
        _fail('service template contains NUL bytes')
    text = text.replace('${HOMEBREW_FOLDER}', str(paths['homebrew']))
    sections: dict[str, list[tuple[str, str, str]]] = {}
    current = ''
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.startswith('[') and line.endswith(']'):
            current = line[1:-1]
            if current not in ('Unit', 'Service', 'Install'):
                _fail(f'unsupported section in service template: {current}')
            sections.setdefault(current, [])
            continue
        if current not in ('Unit', 'Service', 'Install') or '=' not in line:
            _fail('malformed or out-of-section service template directive')
        key, value = line.split('=', 1)
        sections[current].append((key, value, line))
    if not all(section in sections for section in ('Unit', 'Service', 'Install')):
        _fail('service template must contain Unit, Service, and Install sections')
    allowed = {
        'Unit': {'Description', 'After'},
        'Service': {'Type', 'User', 'Restart', 'KillMode', 'TimeoutStopSec', 'ExecStart', 'WorkingDirectory', 'Environment'},
        'Install': {'WantedBy'},
    }
    for section, directives in sections.items():
        if any(key not in allowed[section] for key, _, _ in directives):
            _fail(f'service template contains unsupported directives in [{section}]')
    starts = [value for key, value, _ in sections['Service'] if key == 'ExecStart']
    if len(starts) != 1:
        _fail('service template must define exactly one ExecStart')
    try:
        command = shlex.split(starts[0], posix=True)
    except ValueError as error:
        raise InstallerError(f'invalid ExecStart in service template: {error}') from error
    if command != [str(paths['binary'])]:
        _fail('service template ExecStart must name only the selected services/PluginLoader executable')
    service_values = {key: value for key, value, _ in sections['Service']}
    if service_values.get('Type') != 'simple' or service_values.get('User') != 'root' or service_values.get('WorkingDirectory') != str(paths['services']):
        _fail('service template has an unsupported execution identity or working directory')
    if service_values.get('Restart', 'always') != 'always' or service_values.get('KillMode', 'process') != 'process':
        _fail('service template changes the managed service restart or process-isolation policy')
    for _, value, _ in sections['Install']:
        if value != 'multi-user.target':
            _fail('service template may only install the selected service into multi-user.target')
    if not prepare:
        # The queued helper must validate the original upstream template, not
        # revalidate a unit already modified with this install's environment.
        return raw
    previous_environment = _service_environment(paths)
    existing_lines = [line for key, _, line in sections['Service'] if key == 'Environment']
    preserved = [line for line in previous_environment if line.startswith(('Environment=', 'EnvironmentFile='))]
    merged = existing_lines + [line for line in preserved if line not in existing_lines]
    merged, command_prefix = _listener_configuration(merged, _configured_listen(paths))
    section_lines: list[str] = []
    emitted_environment = False
    for section in ('Unit', 'Service', 'Install'):
        section_lines.append(f'[{section}]')
        for key, _, line in sections[section]:
            if section == 'Service' and key == 'Environment':
                if not emitted_environment:
                    section_lines.extend(merged)
                    emitted_environment = True
                continue
            if section == 'Service' and key == 'ExecStart':
                section_lines.append('ExecStart=' + ' '.join(_systemd_quote(argument) for argument in [*command_prefix, *command]))
                continue
            section_lines.append(line)
        if section == 'Service' and not emitted_environment:
            section_lines.extend(merged)
            emitted_environment = True
        section_lines.append('')
    return ('\n'.join(section_lines)).encode('utf-8')


def _stop_service(paths: dict[str, Path]) -> None:
    properties = _unit_properties(paths)
    if properties.get('LoadState') == 'not-found':
        return
    try:
        main_pid = int(properties.get('MainPID', '0') or '0')
    except ValueError:
        _fail(f'systemd returned an invalid MainPID for {paths["unit"].name}')
    if properties.get('ActiveState') == 'failed':
        if main_pid:
            _fail(f'failed service {paths["unit"].name} still has MainPID={main_pid}')
        _systemctl(paths, 'reset-failed', paths['unit'].name, timeout=15)
        properties = _unit_properties(paths)
        try:
            main_pid = int(properties.get('MainPID', '0') or '0')
        except ValueError:
            _fail(f'systemd returned an invalid MainPID for {paths["unit"].name}')
    if properties.get('ActiveState') != 'inactive' or main_pid > 0:
        _systemctl(paths, 'stop', paths['unit'].name, timeout=45)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        current = _unit_properties(paths)
        if current.get('LoadState') == 'not-found' or (current.get('ActiveState') == 'inactive' and current.get('MainPID', '0') == '0'):
            return
        time.sleep(0.2)
    _fail(f'selected service {paths["unit"].name} did not stop cleanly')


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
    pids: set[int] = set()
    candidates = [group]
    try:
        candidates.extend(path for path in group.rglob('*') if path.is_dir() and not path.is_symlink())
    except OSError as error:
        raise InstallerError(f'cannot enumerate selected-service cgroup: {error}') from error
    for directory in candidates:
        try:
            entries = (directory / 'cgroup.procs').read_text(encoding='ascii').split()
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError) as error:
            raise InstallerError(f'cannot read selected-service cgroup membership: {error}') from error
        for entry in entries:
            if not entry.isdecimal():
                _fail('selected-service cgroup contains an invalid process identifier')
            pids.add(int(entry))
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
        ping = _websocket_call(connection, 1, 'utilities/ping')
        if ping != 'pong':
            _fail(f'utilities/ping returned an unexpected value: {ping!r}')
        plugins = _websocket_call(connection, 2, 'loader/get_plugins')
        if not isinstance(plugins, list):
            _fail('loader/get_plugins did not return a list')
        version = _websocket_call(connection, 3, 'updater/get_version_info')
        if not isinstance(version, dict) or not isinstance(version.get('current'), str) or not version['current']:
            _fail('updater/get_version_info did not return a current version')
        _websocket_send(connection, 8, b'')


def _health(paths: dict[str, Path], listen: str, timeout: float = 45) -> None:
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
                    if not _service_owns_listener(properties, pid, listen):
                        last_error = 'the configured TCP listener is not owned by the selected service MainPID or its descendants'
                        time.sleep(0.35)
                        continue
                    _api_health(paths, listen)
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
                    and _service_owns_listener(after, pid, listen)
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
            _health(paths, _configured_listen(paths))
    else:
        _stop_service(paths)


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
    _stop_service(paths)
    files = record['files']
    assert isinstance(files, dict)
    for key, target in _record_paths(paths).items():
        state = files[key]
        assert isinstance(state, dict)
        _restore_file(target, transaction / 'before' / key, state)
    _systemctl(paths, 'daemon-reload', timeout=30)
    _apply_activation(paths, activation, check_running=False)
    if activation.get('running'):
        properties = _wait_running(paths)
        try:
            pid = int(properties.get('MainPID', '0'))
        except ValueError:
            _fail('systemd returned an invalid restored MainPID')
        if pid <= 0 or not _process_is_binary(pid, paths['binary']):
            _fail('restored service MainPID does not execute the restored PluginLoader')
    else:
        _wait_stopped(paths)


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
    _stop_service(paths)
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
        _stop_service(paths)
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
    _require_root()
    _, service_file = _normalize_service_name(args.service_name)
    homebrew = _homebrew_path(args.homebrew)
    paths = _paths(homebrew, args.service_name)
    user = pwd.getpwnam(args.user)
    if user.pw_uid == 0:
        _fail('--user must resolve to a non-root service account')
    if args.user != user.pw_name:
        _fail('pass the resolved Unix account name with --user')
    bundle, manifest = _validate_bundle(args.bundle, user)
    listen = args.listen
    _listen_endpoint(listen)
    cef_url = _validate_cef_url(args.cef_url)
    inject_frontend = args.inject_frontend == 'true'
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
        unit_data = _generated_unit(paths, runtime_final, user.pw_name, _normalize_service_name(args.service_name)[0], listen, cef_url, inject_frontend, args.worker_mode)
        unit_stage = _stage_bytes(unit_data, paths['unit_dir'], f'.{service_file}.{transaction_id}.')
        active_stage = _stage_bytes((json.dumps({'listen': listen}, sort_keys=True) + '\n').encode(), paths['state'], f'.active.{transaction_id}.', 0o644)
        prior = _capture_activation(paths)
        activation = dict(prior)
        if prior.get('load_state') == 'not-found':
            activation.update({'running': True, 'enabled': True, 'enabled_scope': 'persistent'})
        transaction, record, captured = _snapshot_transaction(paths, transaction_id, 'install', str(manifest['runtime']['decky_version']))
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
            'version': manifest['runtime']['decky_version'],
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
    _require_root()
    _, service_file = _normalize_service_name(args.service_name)
    paths = _paths(_homebrew_path(args.homebrew), args.service_name)
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
        _stop_service(paths)
        target_directory = paths['history'] / target_id
        for key, target in _record_paths(paths).items():
            state = target_files[key]
            assert isinstance(state, dict)
            _restore_file(target, target_directory / 'before' / key, state)
        _systemctl(paths, 'daemon-reload', timeout=30)
        _apply_activation(paths, target_activation, check_running=False)
        if target_activation.get('running'):
            _health(paths, _configured_listen(paths))
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


def _validate_binary(source_value: str) -> Path:
    source = Path(source_value)
    info = _lstat_regular_or_absent(source)
    if info is None:
        _fail(f'updater binary does not exist: {source}')
    source = source.resolve(strict=True)
    mode = stat.S_IMODE(info.st_mode)
    if not mode & 0o111 or mode & 0o6000 or mode & 0o022:
        _fail(f'updater binary has unsafe executable permissions: {source}')
    return source


def _run_activate_binary(args: argparse.Namespace) -> dict[str, object]:
    _require_root()
    _, service_file = _normalize_service_name(args.service_name)
    if not args.version or any(ord(character) < 32 for character in args.version):
        _fail('--version must be a non-empty printable release identifier')
    paths = _paths(_homebrew_path(args.homebrew), args.service_name)
    _recover(paths)
    prior = _capture_activation(paths)
    if prior.get('load_state') != 'loaded':
        _fail('activate-binary requires an existing loaded selected service')
    if _lstat_regular_or_absent(paths['binary']) is None:
        _fail('activate-binary requires an installed services/PluginLoader executable')
    binary_source = _validate_binary(args.binary)
    template_data: bytes | None = None
    if args.service_file:
        template_path = Path(args.service_file)
        if _lstat_regular_or_absent(template_path) is None:
            _fail(f'service template is missing: {template_path}')
        template_data = _validate_service_template(template_path, paths)
    listen = _configured_listen(paths)
    transaction_id = _new_transaction_id()
    binary_stage: Path | None = None
    unit_stage: Path | None = None
    try:
        binary_stage = _copy_stage_file(binary_source, paths['services'], f'.PluginLoader.{transaction_id}.')
        if template_data is not None:
            unit_stage = _stage_bytes(template_data, paths['unit_dir'], f'.{service_file}.{transaction_id}.')
        _, record, activation = _snapshot_transaction(paths, transaction_id, 'activate-binary', args.version)
        stage_values: dict[str, Path | str | None] = {
            'binary_stage': binary_stage,
            'unit_stage': unit_stage,
            'active_stage': None,
            'runtime_stage': None,
        }
        journal = _begin_journal(paths, transaction_id, 'activate-binary', record, stage_values, transaction_id, None, listen)
        try:
            _activate_stages(paths, journal, activation, listen, template_data is not None, binary_stage=binary_stage, unit_stage=unit_stage)
            _commit(paths, journal)
        except BaseException as error:
            try:
                _recover(paths)
            except BaseException as rollback_error:
                raise InstallerError(f'binary activation failed ({error}); recovery is pending ({rollback_error})') from error
            raise InstallerError(f'binary activation failed and was rolled back: {error}') from error
        return {
            'ok': True,
            'command': 'activate-binary',
            'service': service_file,
            'binary': str(paths['binary']),
            'version': args.version,
            'service_template_updated': template_data is not None,
            'rollback_available': True,
            'enabled': bool(activation.get('enabled')),
            'active': bool(activation.get('running')),
        }
    finally:
        for stage in (binary_stage, unit_stage):
            if stage is not None:
                stage.unlink(missing_ok=True)



def _systemd_run_path() -> str:
    for candidate in (Path('/usr/bin/systemd-run'), Path('/bin/systemd-run')):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.as_posix()
    _fail('systemd-run was not found')


def _request_directory(paths: dict[str, Path], request_id: str) -> Path:
    if not re.fullmatch(r'[0-9a-f]{32}', request_id):
        _fail('invalid updater handoff request identifier')
    directory = paths['queue'] / request_id
    try:
        info = directory.lstat()
    except OSError as error:
        raise InstallerError(f'updater handoff request is unavailable: {error}') from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        _fail('updater handoff request directory is not a safe root-owned directory')
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
        (directory / 'PluginLoader').unlink(missing_ok=True)
        (directory / 'service').unlink(missing_ok=True)
        _fsync_directory(directory)




def _remove_updater_temporary(paths: dict[str, Path], source: Path) -> None:
    update_directory = paths['homebrew'] / '.updates'
    if source.parent != update_directory or not re.fullmatch(
        r'decky-update-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(?:binary|service)',
        source.name,
    ):
        return
    try:
        info = source.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        _fail(f'refusing to remove non-regular updater temporary input: {source}')
    source.unlink()
    _fsync_directory(update_directory)
def _prepare_activation_handoff(args: argparse.Namespace, paths: dict[str, Path]) -> dict[str, object]:
    if not args.version or any(ord(character) < 32 for character in args.version):
        _fail('--version must be a non-empty printable release identifier')
    binary_source = _validate_binary(args.binary)
    was_running = bool(_capture_activation(paths)['running'])
    template_data = None
    service_source = None
    if args.service_file:
        service_source = Path(args.service_file)
        if _lstat_regular_or_absent(service_source) is None:
            _fail(f'service template is missing: {service_source}')
        service_source = service_source.resolve(strict=True)
        template_data = _validate_service_template(service_source, paths, prepare=False)
    _cleanup_completed_handoffs(paths)
    _assert_no_pending_handoff(paths)
    request_id = _new_transaction_id()
    directory = paths['queue'] / request_id
    directory.mkdir(mode=0o700)
    try:
        binary = directory / 'PluginLoader'
        _copy_regular(binary_source, binary)
        os.chown(binary, 0, 0)
        os.chmod(binary, 0o755)
        _fsync_file(binary)
        service = None
        if template_data is not None:
            service = directory / 'service'
            _write_atomic(service, template_data, 0o644)
            os.chown(service, 0, 0)
            _fsync_file(service)
        _write_json(directory / 'request.json', {
            'schema': 1,
            'id': request_id,
            'service': paths['unit'].name,
            'homebrew': str(paths['homebrew']),
            'version': args.version,
            'was_running': was_running,
            'service_file': service is not None,
            'created_at': int(time.time()),
        })
        _fsync_directory(directory)
        _remove_updater_temporary(paths, binary_source)
        if service_source is not None:
            _remove_updater_temporary(paths, service_source)
        _fsync_directory(paths['queue'])
        unit = f'decky-updater-{request_id}'
        command = [
            _systemd_run_path(),
            '--system',
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
        try:
            started = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise InstallerError(f'cannot start independent updater activation service: {error}') from error
        if started.returncode:
            detail = started.stderr.strip() or started.stdout.strip() or f'exit status {started.returncode}'
            _fail(f'systemd-run could not queue updater activation: {detail}')
        return {'request_id': request_id, 'unit': f'{unit}.service'}
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _wait_for_handoff(paths: dict[str, Path], request_id: str, unit: str) -> dict[str, object]:
    directory = _request_directory(paths, request_id)
    accepted = directory / 'accepted.json'
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if accepted.exists():
            acknowledgement = _read_json(accepted)
            if acknowledgement.get('schema') != 1 or acknowledgement.get('id') != request_id:
                _fail('updater handoff returned an invalid durable acknowledgement')
            return {'ok': True, 'accepted': True, 'command': 'queue-activate-binary', 'request_id': request_id, 'unit': unit}
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
        (directory / 'PluginLoader').unlink(missing_ok=True)
        (directory / 'service').unlink(missing_ok=True)
        _fsync_directory(directory)
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
    binary = directory / 'PluginLoader'
    service_file = None
    try:
        request = _read_json(directory / 'request.json')
        if (
            request.get('schema') != 1
            or request.get('id') != request_id
            or request.get('service') != paths['unit'].name
            or request.get('homebrew') != str(paths['homebrew'])
            or not isinstance(request.get('version'), str)
            or not request.get('version')
            or not isinstance(request.get('service_file'), bool)
            or not isinstance(request.get('was_running'), bool)
        ):
            _fail('updater handoff request metadata is invalid')
        _validate_binary(str(binary))
        service_file = directory / 'service' if request['service_file'] else None
        if service_file is not None and _lstat_regular_or_absent(service_file) is None:
            _fail('updater handoff service template is missing')
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
            result = _run_activate_binary(argparse.Namespace(
                binary=str(binary),
                version=request['version'],
                homebrew=str(paths['homebrew']),
                service_name=paths['unit'].stem,
                service_file=str(service_file) if service_file is not None else None,
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
        binary.unlink(missing_ok=True)
        if service_file is not None:
            service_file.unlink(missing_ok=True)
        _fsync_directory(directory)

def _run_status(args: argparse.Namespace) -> dict[str, object]:
    _require_root()
    _, service_file = _normalize_service_name(args.service_name)
    paths = _paths(_homebrew_path(args.homebrew), args.service_name)
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


def _locked(paths: dict[str, Path]):
    lock_path = paths['lock']
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(lock_path, flags, 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        _fail('installer lock is not a regular file')
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--homebrew', required=True, metavar='DIR')
    parser.add_argument('--service-name', required=True, metavar='NAME')


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
    install.add_argument('--worker-mode', choices=('exec', 'fork'), default='exec', help='separate workers by default; fork is an explicit opt-in')
    rollback = subparsers.add_parser('rollback', help='restore the previous recoverable binary, unit, and activation state')
    _add_common(rollback)
    status = subparsers.add_parser('status', help='report selected service and recoverable installer state')
    _add_common(status)
    activate = subparsers.add_parser('activate-binary', help='transactionally replace the selected service executable')
    activate.add_argument('--binary', required=True, metavar='FILE')
    activate.add_argument('--version', required=True, metavar='STRING')
    activate.add_argument('--homebrew', required=True, metavar='DIR')
    activate.add_argument('--service-name', required=True, metavar='NAME')
    activate.add_argument('--service-file', metavar='FILE')
    queue = subparsers.add_parser('queue-activate-binary', help='durably hand off an update to an independent activation service')
    queue.add_argument('--binary', required=True, metavar='FILE')
    queue.add_argument('--version', required=True, metavar='STRING')
    queue.add_argument('--homebrew', required=True, metavar='DIR')
    queue.add_argument('--service-name', required=True, metavar='NAME')
    queue.add_argument('--service-file', metavar='FILE')
    queued = subparsers.add_parser('activate-queued', help=argparse.SUPPRESS)
    _add_common(queued)
    queued.add_argument('--request-id', required=True)
    release = subparsers.add_parser('release-queued', help=argparse.SUPPRESS)
    _add_common(release)
    release.add_argument('--request-id', required=True)
    args = parser.parse_args()
    try:
        _require_root()
        if args.command == 'install':
            _normalize_service_name(args.service_name)
            user = pwd.getpwnam(args.user)
            if user.pw_uid == 0:
                _fail('--user must resolve to a non-root service account')
            _validate_bundle(args.bundle, user)
        paths = _paths(_homebrew_path(args.homebrew, create=args.command == 'install'), args.service_name)
        if args.command == 'queue-activate-binary':
            descriptor = _locked(paths)
            try:
                handoff = _prepare_activation_handoff(args, paths)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            output = _wait_for_handoff(paths, str(handoff['request_id']), str(handoff['unit']))
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
                else:
                    output = _run_activate_binary(args)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
        print(json.dumps(output, sort_keys=True))
        return 0
    except (InstallerError, OSError, KeyError, ValueError) as error:
        print(f'{parser.prog}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
