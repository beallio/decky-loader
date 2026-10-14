#!/usr/bin/env python3
"""Reproducibly compare the installed Decky backend and Rust runtime workers on SteamOS."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import pwd
import random
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import time
import tempfile
import traceback
import uuid
from urllib.parse import quote

from process_observer import cgroup_processes

HOME_BREW = Path('/home/deck/homebrew')
NORMAL_SERVICE = 'plugin_loader.service'
NORMAL_BINARY = HOME_BREW / 'services/PluginLoader'
NORMAL_SETTINGS = HOME_BREW / 'settings/loader.json'
CGROUP_ROOT = Path('/sys/fs/cgroup')
LISTEN = '127.0.0.1:1337'
BASE_URL = 'http://127.0.0.1:1337'
PLUGIN_NAME = 'Decky Runtime Benchmark'
PLUGIN_DIRECTORY = 'DeckyRuntimeBenchmark'
EXPECTED_ARITHMETIC = 2048 * 2049 * 4097 // 6
EXPECTED_ACTIVE_PLUGINS = ('CSS Loader', 'Decky Metadata', 'SDH-Ludusavi')
EXPECTED_PASSIVE_PLUGINS = ('HLTB for Deck',)
SCALING_PLUGIN_COUNTS = (0, 4, 8, 12)
SCALING_VARIANTS = ('python-installed', 'rust-lean-fork')
SCALING_EXPECTED_INSTALLED_PLUGINS = (*EXPECTED_ACTIVE_PLUGINS, *EXPECTED_PASSIVE_PLUGINS)
SCALING_FIXTURE_NAME_PREFIX = 'Decky Runtime Scaling Fixture'
SCALING_FIXTURE_DIRECTORY_PREFIX = 'DeckyRuntimeScalingFixture'
SDK_VERSION = 'v3.2.10'
INPUT_DIRECTORIES = ('plugins', 'settings', 'data', 'themes')
STOP_TIMEOUT_SECONDS = 55
UNIT_STOP_SECONDS = 45

VARIANTS = (
    ('python-installed', None, None),
    ('rust-full-exec', 'exec', 'full'),
    ('rust-lean-exec', 'exec', 'lean'),
    ('rust-lean-fork', 'fork', 'lean'),
)

PLUGIN_METADATA = {
    'name': PLUGIN_NAME,
    'author': 'Decky Runtime Benchmark',
    'flags': [],
    'api_version': 1,
}
PLUGIN_SOURCE = '''"""Marked, deterministic arithmetic-only runtime benchmark plugin."""
import multiprocessing
import os
import sys
VECTOR = tuple(range(1, 2049))

class Plugin:
    async def sum_squares(self):
        return sum(value * value for value in VECTOR)
    async def runtime_info(self):
        return {
            'python': sys.version.split()[0],
            'multiprocessing_start_method': multiprocessing.get_start_method(),
            'pid': os.getpid(),
            'ppid': os.getppid(),
            'module_count': len(sys.modules),
            'main_graph_loaded': 'decky_loader.main' in sys.modules,
        }
'''


class BenchmarkError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def _hash_record(hasher, value: object) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    hasher.update(len(encoded).to_bytes(8, 'big'))
    hasher.update(encoded)



def stable_fingerprint(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def tree_digest(root: Path, *, include_inventory: bool = False) -> dict[str, object]:
    """Hash paths, file contents, modes, and symlink targets without following links."""
    root = root.resolve(strict=True)
    hasher = hashlib.sha256()
    entries = 0
    inventory = []
    pending = [root]
    while pending:
        directory = pending.pop()
        children = sorted(directory.iterdir(), key=lambda item: item.name)
        for child in children:
            relative = child.relative_to(root).as_posix()
            info = child.lstat()
            mode = info.st_mode
            if child.is_symlink():
                record = [relative, 'symlink', mode & 0o7777, os.readlink(child)]
            elif child.is_dir():
                record = [relative, 'directory', mode & 0o7777]
                pending.append(child)
            elif child.is_file():
                record = [relative, 'file', mode & 0o7777, info.st_size, sha256_file(child)]
            else:
                raise BenchmarkError(f'unsupported special file in benchmark input: {child}')
            _hash_record(hasher, record)
            if include_inventory:
                inventory.append(record)
            entries += 1
    result = {'sha256': hasher.hexdigest(), 'entries': entries}
    if include_inventory:
        result['inventory'] = inventory
    return result


def inputs_digest(homebrew: Path) -> dict[str, object]:
    hasher = hashlib.sha256()
    entries = 0
    components = {}
    for name in INPUT_DIRECTORIES:
        path = homebrew / name
        value = tree_digest(path)
        _hash_record(hasher, [name, value['sha256'], value['entries']])
        entries += int(value['entries'])
        components[name] = value['sha256']
    return {'sha256': hasher.hexdigest(), 'entries': entries, 'components': components}


def run_command(
    *args: str,
    timeout: float = 30,
    check: bool = True,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args),
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise BenchmarkError(f'{shlex.join(args)} exited {result.returncode}: {detail}')
    return result


def systemctl_properties(unit: str) -> dict[str, str]:
    properties = (
        'LoadState', 'Description', 'ControlGroup', 'ActiveState', 'SubState',
        'MainPID', 'ExecMainPID', 'ExecMainCode', 'ExecMainStatus',
        'ExecMainStartTimestampMonotonic', 'Result',
    )
    result = run_command(
        'systemctl', '--system', 'show', unit,
        *[f'--property={name}' for name in properties],
        timeout=10,
    )
    return dict(line.partition('=')[::2] for line in result.stdout.splitlines() if '=' in line)


def process_identity(pid: int) -> tuple[int, str] | None:
    try:
        tail = (Path('/proc') / str(pid) / 'stat').read_text().rpartition(')')[2].split()
        return int(tail[19]), tail[0]
    except (OSError, ValueError, IndexError):
        return None


def service_baseline() -> dict[str, object]:
    properties = systemctl_properties(NORMAL_SERVICE)
    if properties.get('LoadState') != 'loaded' or properties.get('ActiveState') != 'active':
        raise BenchmarkError(f'{NORMAL_SERVICE} must already be active; it will not be started or stopped')
    pid = int(properties.get('MainPID') or 0)
    identity = process_identity(pid) if pid else None
    if pid <= 0 or identity is None or identity[1] == 'Z':
        raise BenchmarkError(f'{NORMAL_SERVICE} has no live MainPID')
    if not NORMAL_BINARY.is_file() or not NORMAL_SETTINGS.is_file():
        raise BenchmarkError('installed PluginLoader binary or settings/loader.json is missing')
    return {
        'unit': NORMAL_SERVICE,
        'active_state': properties.get('ActiveState'),
        'main_pid': pid,
        'main_pid_start_ticks': identity[0],
        'binary_path': str(NORMAL_BINARY.resolve()),
        'binary_sha256': sha256_file(NORMAL_BINARY),
        'loader_json_path': str(NORMAL_SETTINGS.resolve()),
        'loader_json_sha256': sha256_file(NORMAL_SETTINGS),
    }


def assert_service_unchanged(baseline: dict[str, object]) -> None:
    current = service_baseline()
    keys = (
        'active_state', 'main_pid', 'main_pid_start_ticks',
        'binary_sha256', 'loader_json_sha256',
    )
    differences = {key: (baseline[key], current[key]) for key in keys if baseline[key] != current[key]}
    if differences:
        raise BenchmarkError(f'normal plugin_loader.service or its protected inputs changed: {differences}')


def validate_path(path_value: str, label: str, *, executable: bool = False, directory: bool = False) -> Path:
    path = Path(path_value).expanduser().resolve(strict=True)
    if directory and not path.is_dir():
        raise BenchmarkError(f'{label} is not a directory: {path}')
    if not directory and not path.is_file():
        raise BenchmarkError(f'{label} is not a regular file: {path}')
    if executable and not os.access(path, os.X_OK):
        raise BenchmarkError(f'{label} is not executable: {path}')
    return path


def worker_description(path: Path, temp_dir: Path) -> dict[str, object]:
    temp_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env['TMPDIR'] = str(temp_dir)
    result = run_command(str(path), '--describe', timeout=45, env=env)
    try:
        description = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise BenchmarkError(f'{path} --describe did not return JSON: {error}') from error
    if not isinstance(description, dict):
        raise BenchmarkError(f'{path} --describe returned a non-object')
    if not str(description.get('python', '')).startswith('3.11.'):
        raise BenchmarkError(f'{path} is not the required CPython 3.11 worker: {description}')
    if description.get('decky_version') != SDK_VERSION or description.get('frozen') is not True:
        raise BenchmarkError(f'{path} does not describe the frozen Decky {SDK_VERSION} worker: {description}')
    return description


def parse_settings(settings_file: Path) -> dict[str, object]:
    try:
        value = json.loads(settings_file.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise BenchmarkError(f'cannot parse installed loader settings {settings_file}: {error}') from error
    if not isinstance(value, dict):
        raise BenchmarkError(f'installed loader settings must be a JSON object: {settings_file}')
    return value


def installed_plugin_catalog(plugin_root: Path) -> dict[str, dict[str, object]]:
    catalog: dict[str, dict[str, object]] = {}
    for directory in sorted(plugin_root.iterdir(), key=lambda item: item.name):
        if not directory.is_dir():
            continue
        metadata_path = directory / 'plugin.json'
        if not metadata_path.is_file():
            continue
        try:
            metadata = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise BenchmarkError(f'invalid installed plugin metadata {metadata_path}: {error}') from error
        if not isinstance(metadata, dict) or not isinstance(metadata.get('name'), str):
            raise BenchmarkError(f'installed plugin metadata has no name: {metadata_path}')
        name = metadata['name']
        if name in catalog:
            raise BenchmarkError(f'multiple installed plugin directories claim the name {name!r}')
        catalog[name] = {
            'directory': directory.name,
            'version': metadata.get('version'),
            'api_version': metadata.get('api_version', 0),
            'passive': not (directory / 'main.py').is_file(),
            'metadata': metadata,
        }
    return catalog


def validate_installed_backends(snapshot: Path, settings: dict[str, object]) -> dict[str, object]:
    catalog = installed_plugin_catalog(snapshot / 'plugins')
    required = (*EXPECTED_ACTIVE_PLUGINS, *EXPECTED_PASSIVE_PLUGINS)
    missing = [name for name in required if name not in catalog]
    if missing:
        raise BenchmarkError(f'installed homebrew snapshot is missing required plugins: {missing}')
    disabled = settings.get('disabled_plugins', [])
    if not isinstance(disabled, list) or any(not isinstance(value, str) for value in disabled):
        raise BenchmarkError('settings/loader.json disabled_plugins must be a list of strings')
    disabled_active = sorted(set(disabled).intersection(EXPECTED_ACTIVE_PLUGINS))
    if disabled_active:
        raise BenchmarkError(f'required active plugins are disabled in loader.json: {disabled_active}')
    passive_errors = [name for name in EXPECTED_PASSIVE_PLUGINS if not catalog[name]['passive']]
    active_errors = [name for name in EXPECTED_ACTIVE_PLUGINS if catalog[name]['passive']]
    if passive_errors or active_errors:
        raise BenchmarkError(
            f'installed backend types do not match the contract: passive={passive_errors}, active={active_errors}'
        )
    if PLUGIN_NAME in catalog:
        raise BenchmarkError(f'benchmark plugin name already exists in the live snapshot: {PLUGIN_NAME}')
    if (snapshot / 'plugins' / PLUGIN_DIRECTORY).exists():
        raise BenchmarkError(f'benchmark plugin directory already exists in the live snapshot: {PLUGIN_DIRECTORY}')
    return {
        name: {
            'directory': catalog[name]['directory'],
            'version': catalog[name]['version'],
            'passive': catalog[name]['passive'],
        }
        for name in required
    }


def copy_snapshot(source: Path, snapshot: Path) -> dict[str, object]:
    snapshot.mkdir(parents=True)
    source_info: dict[str, object] = {}
    for name in INPUT_DIRECTORIES:
        source_path = source / name
        destination = snapshot / name
        if source_path.exists() or source_path.is_symlink():
            resolved_source = source_path.resolve(strict=True)
            if not resolved_source.is_dir():
                raise BenchmarkError(f'homebrew input is not a directory: {source_path}')
            shutil.copytree(resolved_source, destination, symlinks=True)
            source_info[name] = {
                'source': str(source_path),
                'resolved_source': str(resolved_source),
                'present': True,
            }
        else:
            destination.mkdir()
            source_info[name] = {'source': str(source_path), 'present': False}
    for name in INPUT_DIRECTORIES:
        inventory = tree_digest(snapshot / name, include_inventory=True)
        source_info[name]['snapshot_sha256'] = inventory['sha256']
        source_info[name]['snapshot_entries'] = inventory['entries']
        source_info[name]['snapshot_inventory'] = inventory['inventory']
    return source_info


def scaling_fixture_name(index: int) -> str:
    return f'{SCALING_FIXTURE_NAME_PREFIX} {index:03d}'


def scaling_fixture_directory(index: int) -> str:
    return f'{SCALING_FIXTURE_DIRECTORY_PREFIX}{index:03d}'


def create_scaling_fixture_plugins(plugin_root: Path, fixture_count: int) -> list[str]:
    names = []
    for index in range(1, fixture_count + 1):
        name = scaling_fixture_name(index)
        directory = plugin_root / scaling_fixture_directory(index)
        if name in installed_plugin_catalog(plugin_root) or directory.exists():
            raise BenchmarkError(f'scaling fixture already exists in the captured plugin catalog: {name}')
        directory.mkdir()
        metadata = dict(PLUGIN_METADATA, name=name)
        (directory / 'plugin.json').write_text(json.dumps(metadata, sort_keys=True) + '\n')
        package = {'name': f'decky-runtime-scaling-fixture-{index:03d}', 'version': '0.0.0', 'type': 'module'}
        (directory / 'package.json').write_text(json.dumps(package, sort_keys=True) + '\n')
        (directory / 'main.py').write_text(PLUGIN_SOURCE)
        names.append(name)
    return names


def runtime_catalog(plugin_root: Path) -> dict[str, dict[str, object]]:
    catalog = installed_plugin_catalog(plugin_root)
    result = {}
    for name, entry in catalog.items():
        package_path = plugin_root / str(entry['directory']) / 'package.json'
        package_version = None
        load_type = 0
        if package_path.is_file():
            try:
                package = json.loads(package_path.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise BenchmarkError(f'invalid package metadata {package_path}: {error}') from error
            if not isinstance(package, dict):
                raise BenchmarkError(f'package metadata must be an object: {package_path}')
            package_version = package.get('version')
            load_type = 1 if package.get('type') == 'module' else 0
        result[name] = {
            'directory': entry['directory'],
            'version': entry['version'],
            'package_version': package_version,
            'api_version': entry['api_version'],
            'passive': entry['passive'],
            'load_type': load_type,
            'flags': entry['metadata'].get('flags'),
        }
    return result


def create_scaling_snapshots(snapshot: Path, work_root: Path) -> dict[int, dict[str, object]]:
    source_catalog = runtime_catalog(snapshot / 'plugins')
    if set(source_catalog) != set(SCALING_EXPECTED_INSTALLED_PLUGINS):
        raise BenchmarkError(
            'plugin-count scaling requires exactly the four installed backend plugins; '
            f'found {sorted(source_catalog)}'
        )
    if any(name.startswith(SCALING_FIXTURE_NAME_PREFIX) for name in source_catalog):
        raise BenchmarkError('installed plugin catalog conflicts with reserved scaling fixture names')

    snapshots: dict[int, dict[str, object]] = {}
    for plugin_count in SCALING_PLUGIN_COUNTS:
        fixture_count = max(0, plugin_count - len(source_catalog))
        destination = work_root / 'plugin-count-snapshots' / f'count-{plugin_count:02d}'
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(snapshot, destination, symlinks=True)
        plugin_root = destination / 'plugins'
        fixture_names: list[str] = []
        if plugin_count == 0:
            for child in plugin_root.iterdir():
                if child.is_symlink() or not child.is_dir():
                    child.unlink()
                else:
                    shutil.rmtree(child)
        elif fixture_count:
            fixture_names = create_scaling_fixture_plugins(plugin_root, fixture_count)

        catalog = runtime_catalog(plugin_root)
        if len(catalog) != plugin_count:
            raise BenchmarkError(
                f'plugin-count snapshot {plugin_count} contains {len(catalog)} catalog entries'
            )
        active_count = sum(not bool(entry['passive']) for entry in catalog.values())
        passive_count = len(catalog) - active_count
        if plugin_count == 0 and (active_count or passive_count):
            raise BenchmarkError('empty plugin-count snapshot unexpectedly contains catalog entries')
        if plugin_count >= 4:
            for name in SCALING_EXPECTED_INSTALLED_PLUGINS:
                if name not in catalog:
                    raise BenchmarkError(f'plugin-count snapshot lost installed plugin {name!r}')
            if set(catalog).intersection(SCALING_EXPECTED_INSTALLED_PLUGINS) != set(SCALING_EXPECTED_INSTALLED_PLUGINS):
                raise BenchmarkError('plugin-count snapshot does not preserve the installed plugin catalog')

        input_inventory = tree_digest(destination, include_inventory=True)
        digest = inputs_digest(destination)
        if input_inventory['entries'] < digest['entries']:
            raise BenchmarkError(f'plugin-count snapshot {plugin_count} inventory is incomplete')
        snapshots[plugin_count] = {
            'path': destination,
            'digest': digest,
            'source_inventory': input_inventory,
            'catalog': catalog,
            'active_plugin_count': active_count,
            'passive_plugin_count': passive_count,
            'fixture_names': fixture_names,
        }
    return snapshots


def chown_tree(path: Path, uid: int, gid: int) -> None:
    for directory, subdirectories, files in os.walk(path, followlinks=False):
        os.chown(directory, uid, gid, follow_symlinks=False)
        for name in subdirectories + files:
            os.chown(Path(directory) / name, uid, gid, follow_symlinks=False)


def create_benchmark_plugin(plugin_root: Path) -> dict[str, object]:
    plugin_dir = plugin_root / PLUGIN_DIRECTORY
    plugin_dir.mkdir()
    (plugin_dir / 'plugin.json').write_text(json.dumps(PLUGIN_METADATA, sort_keys=True) + '\n')
    package = {'name': 'decky-runtime-benchmark', 'version': '0.0.0', 'type': 'module'}
    (plugin_dir / 'package.json').write_text(json.dumps(package, sort_keys=True) + '\n')
    (plugin_dir / 'main.py').write_text(PLUGIN_SOURCE)
    return tree_digest(plugin_dir)


def validate_control_group(control_group: str | None) -> Path:
    if not control_group or not control_group.startswith('/system.slice/'):
        raise BenchmarkError(f'benchmark unit has an unexpected control group: {control_group!r}')
    relative = Path(control_group.lstrip('/'))
    if '..' in relative.parts:
        raise BenchmarkError(f'benchmark unit has an unsafe control group: {control_group!r}')
    return CGROUP_ROOT / relative


def cgroup_members(control_group: str | None) -> list[int]:
    if not control_group:
        return []
    validate_control_group(control_group)
    try:
        return list(cgroup_processes(control_group))
    except (OSError, ValueError) as error:
        raise BenchmarkError(f'cannot observe owned processes for {control_group}: {error}') from error


def cgroup_usage_usec(control_group: str | None) -> int:
    root = validate_control_group(control_group)
    try:
        values = root.joinpath('cpu.stat').read_text().splitlines()
    except OSError as error:
        raise BenchmarkError(f'cannot read CPU usage for {control_group}: {error}') from error
    for line in values:
        key, separator, value = line.partition(' ')
        if separator and key == 'usage_usec':
            return int(value)
    raise BenchmarkError(f'cgroup {control_group} has no cpu.stat usage_usec')


def cgroup_pss(control_group: str | None) -> dict[str, object]:
    pss_kib = 0
    private_dirty_kib = 0
    processes = []
    skipped_vanished = 0
    validate_control_group(control_group)
    try:
        members = cgroup_processes(control_group)
    except (OSError, ValueError) as error:
        raise BenchmarkError(f'cannot observe owned processes for {control_group}: {error}') from error
    for pid, record in members.items():
        details = {
            'pid': pid, 'start_ticks': record.start_ticks, 'ppid': record.ppid,
            'pgrp': record.pgrp, 'session': record.session, 'comm': record.comm,
            'kernel_state': record.state, 'cgroups': list(record.cgroups),
            'state': 'zombie' if record.state == 'Z' else 'live',
        }
        if record.state == 'Z':
            processes.append(details)
            continue
        try:
            smaps = (Path('/proc') / str(pid) / 'smaps_rollup').read_text().splitlines()
        except (FileNotFoundError, ProcessLookupError):
            skipped_vanished += 1
            continue
        except OSError as error:
            raise BenchmarkError(f'cannot read memory details for owned process {pid}: {error}') from error
        identity = process_identity(pid)
        if identity is None or identity[0] != record.start_ticks:
            skipped_vanished += 1
            continue
        if identity[1] == 'Z':
            details.update(state='zombie', kernel_state='Z')
            processes.append(details)
            continue
        values = {}
        for line in smaps:
            key, separator, value = line.partition(':')
            if separator and key in ('Pss', 'Private_Dirty'):
                values[key] = int(value.split()[0])
        if 'Pss' not in values or 'Private_Dirty' not in values:
            raise BenchmarkError(f'process {pid} smaps_rollup has no Pss or Private_Dirty entry')
        pss_kib += values['Pss']
        private_dirty_kib += values['Private_Dirty']
        details.update(pss_mib=values['Pss'] / 1024, private_dirty_mib=values['Private_Dirty'] / 1024)
        processes.append(details)
    return {
        'pss_mib': pss_kib / 1024,
        'private_dirty_mib': private_dirty_kib / 1024,
        'process_count': len(processes),
        'live_process_count': sum(process['state'] == 'live' for process in processes),
        'zombie_count': sum(process['state'] == 'zombie' for process in processes),
        'discovery': '/proc cgroup membership, tracked PID/start-tick identities, ancestry and anchored process groups',
        'vanished_during_sample': skipped_vanished,
        'processes': processes,
    }


def wait_for_main_pid(unit: str, description: str, timeout: float) -> tuple[int, str, dict[str, str]]:
    deadline = time.monotonic() + timeout
    last_properties: dict[str, str] = {}
    last_group: str | None = None
    while time.monotonic() < deadline:
        properties = systemctl_properties(unit)
        last_properties = properties
        if properties.get('LoadState') in (None, 'not-found'):
            raise BenchmarkError(f'transient benchmark unit {unit} disappeared during startup')
        if properties.get('Description') != description:
            raise BenchmarkError(f'transient benchmark unit {unit} lost its UUID ownership description')
        last_group = properties.get('ControlGroup') or last_group
        pid = int(properties.get('MainPID') or 0)
        # systemd can publish MainPID before applying the network namespace.
        if pid > 0 and properties.get('ActiveState') == 'active' and properties.get('SubState') == 'running':
            group = properties.get('ControlGroup') or last_group
            validate_control_group(group)
            namespace = os.readlink(f'/proc/{pid}/ns/net')
            if namespace == os.readlink('/proc/self/ns/net'):
                raise BenchmarkError(f'owned unit {unit} did not enter its private network namespace')
            return pid, str(group), properties
        if properties.get('ActiveState') == 'failed':
            raise BenchmarkError(f'transient benchmark unit {unit} failed before obtaining a MainPID')
        if properties.get('ActiveState') != 'activating' and pid <= 0 and int(properties.get('ExecMainStartTimestampMonotonic') or 0) > 0:
            raise BenchmarkError(f'transient benchmark unit {unit} exited before becoming ready')
        time.sleep(0.1)
    raise TimeoutError(f'transient benchmark unit {unit} did not obtain a MainPID: {last_properties}')


def _unit_inspect(unit: str, description: str, control_group: str | None):
    properties = systemctl_properties(unit)
    if properties.get('LoadState') in (None, 'not-found'):
        members = cgroup_members(control_group)
        return properties, control_group, members, True
    if properties.get('Description') != description:
        raise BenchmarkError(f'refusing to manage unit {unit}: UUID ownership description does not match')
    group = properties.get('ControlGroup') or control_group
    members = cgroup_members(group)
    stopped = properties.get('ActiveState') in ('inactive', 'failed')
    return properties, group, members, stopped


def finish_unit(
    unit: str,
    description: str,
    control_group: str | None,
    launch_attempted: bool,
) -> dict[str, object]:
    proof: dict[str, object] = {
        'unit': unit,
        'description': description,
        'cgroup': control_group,
        'unit_stopped': not launch_attempted,
        'drained': not launch_attempted,
        'members_after_stop': [],
        'forced_kill': False,
        'shutdown_latency_seconds': None,
        'errors': [],
    }
    if not launch_attempted:
        return proof

    started: float | None = None
    properties, control_group, members, stopped = _unit_inspect(unit, description, control_group)
    if not (stopped and not members):
        started = time.monotonic()
        try:
            result = run_command(
                'systemctl', '--system', 'kill', '--kill-whom=main', '--signal=SIGTERM', unit,
                timeout=10, check=False,
            )
            if result.returncode:
                proof['errors'].append(f'initial stop returned {result.returncode}: {result.stderr.strip()}')
        except (OSError, subprocess.SubprocessError) as error:
            proof['errors'].append(f'initial stop: {type(error).__name__}: {error}')

        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        while True:
            properties, control_group, members, stopped = _unit_inspect(unit, description, control_group)
            if stopped and not members:
                break
            if not members and int(properties.get('MainPID') or 0) == 0:
                result = run_command('systemctl', '--system', 'stop', unit, timeout=10, check=False)
                if result.returncode:
                    proof['errors'].append(f'empty-unit stop returned {result.returncode}: {result.stderr.strip()}')
                properties, control_group, members, stopped = _unit_inspect(unit, description, control_group)
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)

    if not stopped or members:
        properties, control_group, members, stopped = _unit_inspect(unit, description, control_group)
        if not (stopped and not members):
            proof['forced_kill'] = True
            try:
                result = run_command(
                    'systemctl', '--system', 'kill', '--kill-whom=all', '--signal=SIGKILL', unit,
                    timeout=10, check=False,
                )
                if result.returncode:
                    proof['errors'].append(f'owned-unit kill returned {result.returncode}: {result.stderr.strip()}')
            except (OSError, subprocess.SubprocessError) as error:
                proof['errors'].append(f'owned-unit kill: {type(error).__name__}: {error}')
            try:
                result = run_command('systemctl', '--system', 'stop', unit, timeout=10, check=False)
                if result.returncode:
                    proof['errors'].append(f'post-kill stop returned {result.returncode}: {result.stderr.strip()}')
            except (OSError, subprocess.SubprocessError) as error:
                proof['errors'].append(f'post-kill stop: {type(error).__name__}: {error}')

            deadline = time.monotonic() + 15
            while True:
                properties, control_group, members, stopped = _unit_inspect(unit, description, control_group)
                if stopped and not members:
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.1)

    if properties.get('Result') not in (None, 'success'):
        proof['errors'].append(f"systemd stop result: {properties['Result']}")
    if properties.get('ExecMainCode') in ('2', '3') and properties.get('ExecMainStatus') == str(signal.SIGKILL):
        proof['forced_kill'] = True

    proof.update(
        cgroup=control_group,
        unit_stopped=stopped,
        drained=stopped and not members,
        members_after_stop=members,
        main_pid=int(properties.get('MainPID') or 0) or None,
        shutdown_latency_seconds=0.0 if started is None else time.monotonic() - started,
        active_state=properties.get('ActiveState'),
    )
    if not proof['drained']:
        proof['errors'].append(
            f'owned unit did not drain: state={properties.get("ActiveState")}, members={members}'
        )
    return proof


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + '\n')
    temporary.chmod(0o644)
    os.replace(temporary, path)
    path.chmod(0o644)


def ensure_output_directory(path: Path, repository_root: Path) -> Path:
    output = path.expanduser().resolve()
    if output == repository_root or repository_root in output.parents:
        raise BenchmarkError(f'benchmark output must be outside the repository: {output}')
    if output == HOME_BREW.resolve() or HOME_BREW.resolve() in output.parents:
        raise BenchmarkError(f'benchmark output must not be inside live homebrew: {output}')
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise BenchmarkError(f'benchmark output directory must be empty: {output}')
    (output / 'logs').mkdir()
    return output


def system_information() -> dict[str, object]:
    os_release: dict[str, str] = {}
    try:
        for line in Path('/etc/os-release').read_text().splitlines():
            key, separator, value = line.partition('=')
            if separator:
                os_release[key] = value.strip().strip('"')
    except OSError:
        pass
    systemd_version = None
    try:
        systemd_version = run_command('systemctl', '--version', timeout=10).stdout.splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError, BenchmarkError):
        pass
    return {
        'captured_at_utc': utc_now(),
        'platform': platform.platform(),
        'system': platform.system(),
        'release': platform.release(),
        'machine': platform.machine(),
        'libc': platform.libc_ver(),
        'logical_cpu_count': os.cpu_count(),
        'python_harness': {
            'executable': sys.executable,
            'version': sys.version,
        },
        'os_release': os_release,
        'systemd_version': systemd_version,
    }


def _normal_service_command(path: Path) -> list[str]:
    return [str(path)]


def _rust_command(
    rust_executable: Path,
    worker: Path,
    homebrew: Path,
    assets: Path,
    locales: Path,
    mode: str,
    bootstrap: str,
    release_version: str,
) -> list[str]:
    return [
        str(rust_executable),
        '--listen', LISTEN,
        '--inject-frontend', 'false',
        '--worker', str(worker),
        '--release-version', release_version,
        '--worker-mode', mode,
        '--worker-bootstrap', bootstrap,
        '--plugin-root', str(homebrew / 'plugins'),
        '--homebrew', str(homebrew),
        '--user', 'deck',
        '--assets', str(assets),
        '--locales', str(locales),
    ]


def _trial_launcher(
    unit: str,
    description: str,
    homebrew: Path,
    snapshot: Path,
    tmpdir: Path,
    log_path: Path,
    command: list[str],
) -> list[str]:
    env_values = {
        'UNPRIVILEGED_PATH': str(homebrew),
        'PRIVILEGED_PATH': str(homebrew),
        'PLUGIN_PATH': str(homebrew / 'plugins'),
        'UNPRIVILEGED_USER': 'deck',
        'CHOWN_PLUGIN_PATH': '0',
        'SERVER_HOST': '127.0.0.1',
        'SERVER_PORT': '1337',
        'LIVE_RELOAD': '0',
        'TMPDIR': str(tmpdir),
    }
    launcher = [
        'systemd-run', '--system', '--quiet', '--no-block',
        '--unit', unit,
        '--description', description,
        '--slice=system.slice',
        '--service-type=exec',
        '--property=RemainAfterExit=yes',
        '--property=KillMode=control-group',
        '--property=SendSIGKILL=yes',
        '--property=TimeoutStartSec=30s',
        f'--property=TimeoutStopSec={UNIT_STOP_SECONDS}s',
        '--property=RuntimeMaxSec=1800s',
        '--property=PrivateNetwork=yes',
        '--property=InaccessiblePaths=-/run/systemd/private -/run/dbus/system_bus_socket',
        f'--property=ReadOnlyPaths=/home/deck/homebrew /home/deck/.local/share/Steam -/home/deck/.steam {snapshot}',
        f'--property=WorkingDirectory={homebrew}',
        f'--property=StandardOutput=append:{log_path}',
        f'--property=StandardError=append:{log_path}',
    ]
    launcher.extend(f'--setenv={key}={value}' for key, value in env_values.items())
    launcher.extend(['--', *command])
    return launcher


def run_client_helper(target_pid: int, request: dict[str, object], timeout: float) -> dict[str, object]:
    if target_pid <= 0:
        raise BenchmarkError('invalid namespace target PID')
    namespace = os.readlink(f'/proc/{target_pid}/ns/net')
    if namespace == os.readlink('/proc/self/ns/net'):
        raise BenchmarkError('refusing to enter the live host network namespace')
    request = dict(request, expected_network_namespace=namespace)
    command = [
        'nsenter', '--target', str(target_pid), '--net', '--',
        sys.executable, str(Path(__file__).resolve()), '--_client-helper',
    ]
    try:
        result = run_command(
            *command,
            timeout=timeout,
            check=False,
            input_text=json.dumps(request),
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout.decode(errors='replace') if isinstance(error.stdout, bytes) else (error.stdout or '')
        stderr = error.stderr.decode(errors='replace') if isinstance(error.stderr, bytes) else (error.stderr or '')
        raise TimeoutError(f'namespace client exceeded {timeout}s; stdout={stdout!r}, stderr={stderr!r}') from error
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise BenchmarkError(
            f'namespace client exited {result.returncode} without JSON; stderr={result.stderr.strip()!r}'
        )
    try:
        response = json.loads(lines[-1])
    except json.JSONDecodeError as error:
        raise BenchmarkError(
            f'namespace client returned invalid JSON: stdout={result.stdout!r}, stderr={result.stderr.strip()!r}'
        ) from error
    if not isinstance(response, dict):
        raise BenchmarkError(f'namespace client returned non-object JSON: {response!r}')
    if result.returncode and response.get('ok') is True:
        raise BenchmarkError(f'namespace client returned success with exit code {result.returncode}')
    if result.stderr:
        response['stderr'] = result.stderr[-4000:]
    return response


def cgroup_path_string(control_group: str) -> str:
    return str(validate_control_group(control_group))


def run_trial(
    *,
    variant: str,
    replication: int,
    order_position: int,
    profile: str,
    plugin_count: int | None,
    fixture_names: list[str],
    snapshot: Path,
    expected_snapshot_digest: dict[str, object],
    account: pwd.struct_passwd,
    output: Path,
    work_root: Path,
    python_executable: Path,
    rust_executable: Path,
    worker: Path,
    assets: Path,
    locales: Path,
    release_version: str,
    python_inventory_fingerprint: str,
    rust_bundle_fingerprint: str,
    startup_timeout: float,
    rpc_timeout: float,
    idle_seconds: float,
) -> tuple[dict[str, object], bool]:
    run_uuid = uuid.uuid4().hex
    unit = f'decky-runtime-benchmark-{run_uuid}.service'
    description = f'Decky Runtime Benchmark {run_uuid}'
    trial_root = work_root / f'trial-{run_uuid}'
    homebrew = trial_root / 'homebrew'
    # Keep worker Unix socket paths below AF_UNIX's 108-byte limit.
    tmpdir = Path(tempfile.mkdtemp(prefix='t', dir=work_root.parent))
    log_path = output / 'logs' / f'{unit}.log'
    trial: dict[str, object] = {
        'trial_uuid': run_uuid,
        'unit': unit,
        'description': description,
        'variant': variant,
        'profile': profile,
        'plugin_count': plugin_count,
        'rust_bundle_fingerprint': rust_bundle_fingerprint if variant != 'python-installed' else None,
        'python_source_inventory_fingerprint': (
            stable_fingerprint({
                'python_executable_inventory': python_inventory_fingerprint,
                'input_snapshot_sha256': expected_snapshot_digest['sha256'],
            })
            if variant == 'python-installed' else None
        ),
        'replication': replication,
        'order_position': order_position,
        'started_at_utc': utc_now(),
        'status': 'running',
        'snapshot_sha256': expected_snapshot_digest['sha256'],
        'log_path': str(log_path),
        'comparability_note': (
            'python-installed runs the official Python Decky backend; Rust runs the private Linux/SteamOS '
            'backend build. These isolated backend trials do not measure the live Steam frontend.'
        ),
    }
    launch_attempted = False
    control_group: str | None = None
    keep_runtime = False
    trial_error: BaseException | None = None
    trial_root.mkdir(parents=True)
    os.chown(tmpdir, account.pw_uid, account.pw_gid)
    try:
        shutil.copytree(snapshot, homebrew, symlinks=True)
        copied_inputs = inputs_digest(homebrew)
        trial['clone_inputs_sha256'] = copied_inputs['sha256']
        trial['clone_input_entries'] = copied_inputs['entries']
        if copied_inputs != expected_snapshot_digest:
            raise BenchmarkError(
                f'clone differs from immutable input snapshot: expected {expected_snapshot_digest}, got {copied_inputs}'
            )
        (homebrew / 'logs').mkdir(exist_ok=True)
        if profile == 'standard':
            plugin_digest = create_benchmark_plugin(homebrew / 'plugins')
            trial['benchmark_plugin_sha256'] = plugin_digest['sha256']
        elif profile != 'plugin-count-scaling':
            raise BenchmarkError(f'unknown benchmark profile: {profile}')
        if profile == 'plugin-count-scaling':
            if plugin_count not in SCALING_PLUGIN_COUNTS:
                raise BenchmarkError(f'invalid scaling plugin count: {plugin_count}')
            if len(runtime_catalog(homebrew / 'plugins')) != plugin_count:
                raise BenchmarkError(f'plugin-count clone does not contain exactly {plugin_count} entries')
        expected_catalog = runtime_catalog(homebrew / 'plugins')
        if profile == 'plugin-count-scaling' and set(expected_catalog).intersection(fixture_names) != set(fixture_names):
            raise BenchmarkError('fixture names do not match the plugin-count clone catalog')
        trial['expected_catalog_active_plugin_count'] = sum(
            not bool(entry['passive']) for entry in expected_catalog.values()
        )
        trial['expected_catalog_passive_plugin_count'] = sum(
            bool(entry['passive']) for entry in expected_catalog.values()
        )
        trial['expected_catalog_entry_count'] = len(expected_catalog)
        chown_tree(homebrew, account.pw_uid, account.pw_gid)

        variant_config = next((entry for entry in VARIANTS if entry[0] == variant), None)
        if variant_config is None:
            raise BenchmarkError(f'unknown benchmark variant: {variant}')
        _, worker_mode, worker_bootstrap = variant_config
        expected_current_version = SDK_VERSION if worker_mode is None else release_version
        expected_sdk_version = None if worker_mode is None else SDK_VERSION
        if worker_mode is None:
            service_command = _normal_service_command(python_executable)
        else:
            service_command = _rust_command(
                rust_executable, worker, homebrew, assets, locales, worker_mode, worker_bootstrap,
                release_version,
            )

        existing = systemctl_properties(unit)
        if existing.get('LoadState') not in (None, 'not-found'):
            raise BenchmarkError(f'UUID transient unit already exists; refusing to launch or manage it: {unit}')
        start_monotonic = time.monotonic()
        trial['immutable_snapshot_read_only'] = str(snapshot)
        launcher = _trial_launcher(unit, description, homebrew, snapshot, tmpdir, log_path, service_command)
        launch_attempted = True
        result = run_command(*launcher, timeout=45, check=False)
        if result.returncode:
            raise BenchmarkError(
                f'systemd-run failed for owned unit {unit} ({result.returncode}): '
                f'{result.stderr.strip() or result.stdout.strip()}'
            )
        trial['launch_succeeded'] = True

        target_pid, control_group, properties = wait_for_main_pid(unit, description, startup_timeout)
        trial['main_pid'] = target_pid
        trial['control_group'] = control_group
        trial['unit_start_state'] = properties.get('ActiveState')
        startup = run_client_helper(
            target_pid,
            {
                'mode': 'startup',
                'base_url': BASE_URL,
                'started_monotonic': start_monotonic,
                'startup_timeout': startup_timeout,
                'rpc_timeout': rpc_timeout,
                'expected_arithmetic': EXPECTED_ARITHMETIC,
                'expected_catalog': expected_catalog,
                'expected_current_version': expected_current_version,
                'expected_sdk_version': expected_sdk_version,
                'benchmark_plugin': PLUGIN_NAME if profile == 'standard' else None,
                'real_plugin_rpc': profile == 'standard' or (plugin_count is not None and plugin_count >= 4),
                'fixture_plugins': fixture_names,
            },
            startup_timeout + (8 + len(fixture_names)) * rpc_timeout + 10,
        )
        trial['startup'] = startup
        if startup.get('ok') is not True:
            raise BenchmarkError(f'auth/plugin/backend startup readiness failed: {startup.get("error", startup)}')
        trial['startup_to_readiness_seconds'] = startup['startup_seconds']
        trial['catalog_active_plugin_count'] = startup['catalog_active_plugin_count']
        trial['catalog_passive_plugin_count'] = startup['catalog_passive_plugin_count']
        trial['catalog_entry_count'] = startup['catalog_entry_count']
        trial['catalog_plugin_names'] = startup['catalog_plugin_names']
        trial['plugin_load_errors'] = startup['plugin_load_errors']
        assert_service_unchanged(_ACTIVE_BASELINE)

        pss = cgroup_pss(control_group)
        trial['pss_after_readiness_mib'] = pss['pss_mib']
        trial['private_dirty_after_readiness_mib'] = pss['private_dirty_mib']
        trial['process_count_after_readiness'] = pss['process_count']
        trial['live_process_count_after_readiness'] = pss['live_process_count']
        trial['zombie_count_after_readiness'] = pss['zombie_count']
        trial['pss_sample'] = pss

        cpu_before = cgroup_usage_usec(control_group)
        idle_start = time.monotonic()
        time.sleep(idle_seconds)
        idle_wall = time.monotonic() - idle_start
        cpu_after = cgroup_usage_usec(control_group)
        idle_cpu = cpu_after - cpu_before
        trial['idle_cpu'] = {
            'usage_usec_before': cpu_before,
            'usage_usec_after': cpu_after,
            'usage_usec_delta': idle_cpu,
            'wall_seconds': idle_wall,
            'percent_of_one_core': 100 * idle_cpu / (idle_wall * 1_000_000),
        }

        current = systemctl_properties(unit)
        if current.get('Description') != description or int(current.get('MainPID') or 0) <= 0:
            raise BenchmarkError(f'benchmark unit exited before loaded RPC measurement: {current}')
        if profile == 'plugin-count-scaling':
            operation_plan = [{
                'name': 'backend_ping',
                'route': 'updater/get_version_info',
                'args': [],
                'validation': {
                    'kind': 'version',
                    'expected_current': expected_current_version,
                    'expected_sdk_version': expected_sdk_version,
                },
            }]
            if plugin_count is not None and plugin_count >= 4:
                operation_plan.append({
                    'name': 'common_real_plugin',
                    'route': 'loader/call_legacy_plugin_method',
                    'args': ['CSS Loader', 'get_backend_version', {}],
                    'validation': {'kind': 'css_version'},
                })
            if fixture_names:
                operation_plan.append({
                    'name': 'fixture_plugin',
                    'route': 'loader/call_plugin_method',
                    'args': [fixture_names[0], 'sum_squares'],
                    'validation': {'kind': 'arithmetic', 'expected': EXPECTED_ARITHMETIC},
                    'plugin_name': fixture_names[0],
                })
        else:
            operation_plan = [{
                'name': 'benchmark_arithmetic',
                'route': 'loader/call_plugin_method',
                'args': [PLUGIN_NAME, 'sum_squares'],
                'validation': {'kind': 'arithmetic', 'expected': EXPECTED_ARITHMETIC},
                'plugin_name': PLUGIN_NAME,
            }]
        measurement = run_client_helper(
            int(current['MainPID']),
            {
                'mode': 'measure',
                'base_url': BASE_URL,
                'operations': operation_plan,
                'rpc_timeout': rpc_timeout,
                'warmup_calls': 10,
                'rpc_calls': 200,
                'cgroup_path': cgroup_path_string(control_group),
            },
            max(120, (rpc_timeout * (210 * len(operation_plan) + 1)) + 30),
        )
        trial['rpc_measurements'] = measurement.get('measurements', {})
        if profile == 'standard':
            arithmetic = trial['rpc_measurements'].get('benchmark_arithmetic')
            if arithmetic is not None:
                trial['rpc_measurement'] = arithmetic
        else:
            trial['rpc_measurement_status'] = {
                'ok': measurement.get('ok'),
                'failed_operations': measurement.get('failed_operations', []),
            }
        if measurement.get('ok') is not True:
            raise BenchmarkError(f'loaded RPC measurement failed: {measurement.get("error", measurement)}')
        if profile == 'standard':
            arithmetic = trial['rpc_measurements']['benchmark_arithmetic']
            trial['rpc'] = {
                'warmup_calls': arithmetic['warmup_calls'],
                'planned_calls': arithmetic['planned_calls'],
                'completed_calls': arithmetic['completed_calls'],
                'latency_ms': arithmetic['latency_ms'],
                'latency_p50_ms': arithmetic['latency_p50_ms'],
                'latency_p95_ms': arithmetic['latency_p95_ms'],
                'throughput_calls_per_second': arithmetic['throughput_calls_per_second'],
                'loaded_wall_seconds': arithmetic['loaded_wall_seconds'],
                'loaded_cpu_usage_usec': arithmetic['loaded_cpu_usage_usec'],
                'loaded_cpu_usec_per_completed_call': arithmetic['loaded_cpu_usec_per_completed_call'],
            }
    except BaseException as error:
        trial_error = error
        trial['error'] = f'{type(error).__name__}: {error}'
        trial['error_traceback'] = ''.join(traceback.format_exception(type(error), error, error.__traceback__))[-12000:]
        if isinstance(error, KeyboardInterrupt):
            trial['interrupted'] = True
    finally:
        try:
            containment = finish_unit(unit, description, control_group, launch_attempted)
            trial['containment'] = containment
            trial['shutdown_latency_seconds'] = containment['shutdown_latency_seconds']
            if not containment['drained']:
                keep_runtime = True
                trial['status'] = 'failed'
                details = '; '.join(str(item) for item in containment['errors'])
                if trial_error is None:
                    trial_error = BenchmarkError(f'owned cgroup did not drain; runtime data retained: {details}')
                    trial['error'] = f'{type(trial_error).__name__}: {trial_error}'
                else:
                    trial['cleanup_error'] = details
            elif containment['forced_kill'] or containment['errors']:
                trial['status'] = 'failed'
                detail = '; '.join(str(item) for item in containment['errors'])
                if containment['forced_kill']:
                    detail = '; '.join(filter(None, [detail, 'owned unit required SIGKILL']))
                trial['cleanup_error'] = detail
                if trial_error is None:
                    trial_error = BenchmarkError(detail)
                    trial['error'] = f'{type(trial_error).__name__}: {trial_error}'
            else:
                trial['status'] = 'success' if trial_error is None else 'failed'
        except BaseException as error:
            keep_runtime = True
            trial['status'] = 'failed'
            trial['cleanup_error'] = f'{type(error).__name__}: {error}'
            trial['cleanup_traceback'] = ''.join(traceback.format_exception(type(error), error, error.__traceback__))[-12000:]

        if not keep_runtime:
            try:
                shutil.rmtree(trial_root)
                shutil.rmtree(tmpdir)
                trial['runtime_data_removed_after_drain'] = True
            except OSError as error:
                trial['runtime_data_removed_after_drain'] = False
                trial['cleanup_error'] = f'could not remove drained trial data {trial_root}: {error}'
                trial['status'] = 'failed'
        else:
            trial['runtime_data_retained'] = str(trial_root)
            trial['runtime_temp_retained'] = str(tmpdir)

        try:
            assert_service_unchanged(_ACTIVE_BASELINE)
            trial['normal_service_unchanged_after_trial'] = True
        except BaseException as error:
            trial['normal_service_unchanged_after_trial'] = False
            if trial_error is None:
                trial_error = error
            trial['status'] = 'failed'
            trial['normal_service_check_error'] = f'{type(error).__name__}: {error}'

        trial['finished_at_utc'] = utc_now()
        if trial['status'] == 'running':
            trial['status'] = 'success' if trial_error is None else 'failed'

    safe_to_continue = (
        not keep_runtime
        and trial.get('normal_service_unchanged_after_trial') is True
        and trial.get('interrupted') is not True
    )
    return trial, safe_to_continue


def percentile(values: list[float], percentage: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentage / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def describe(values: list[float]) -> dict[str, object]:
    if not values:
        return {'n': 0, 'mean': None, 'median': None, 'stdev': None, 'min': None, 'max': None}
    return {
        'n': len(values),
        'mean': statistics.mean(values),
        'median': statistics.median(values),
        'stdev': statistics.stdev(values) if len(values) > 1 else None,
        'min': min(values),
        'max': max(values),
    }


def bootstrap_mean_interval(
    values: list[float],
    seed_key: str,
    *,
    paired: bool = False,
    resamples: int = 10000,
) -> dict[str, object] | None:
    if len(values) < 2:
        return None
    seed = int(hashlib.sha256(seed_key.encode('utf-8')).hexdigest()[:16], 16)
    generator = random.Random(seed)
    count = len(values)
    means = [
        sum(values[generator.randrange(count)] for _ in range(count)) / count
        for _ in range(resamples)
    ]
    return {
        'confidence_level': 0.95,
        'method': ('paired ' if paired else '') + 'percentile bootstrap of the mean',
        'resamples': resamples,
        'seed': seed,
        'lower': percentile(means, 2.5),
        'upper': percentile(means, 97.5),
    }


def scaling_metric_value(trial: dict[str, object], metric: str) -> float | None:
    direct = {
        'startup_to_readiness_seconds': trial.get('startup_to_readiness_seconds'),
        'pss_after_readiness_mib': trial.get('pss_after_readiness_mib'),
        'private_dirty_after_readiness_mib': trial.get('private_dirty_after_readiness_mib'),
        'process_count_after_readiness': trial.get('process_count_after_readiness'),
        'live_process_count_after_readiness': trial.get('live_process_count_after_readiness'),
        'catalog_entry_count': trial.get('catalog_entry_count'),
        'catalog_active_plugin_count': trial.get('catalog_active_plugin_count'),
        'catalog_passive_plugin_count': trial.get('catalog_passive_plugin_count'),
        'idle_cpu_percent_of_one_core': (trial.get('idle_cpu') or {}).get('percent_of_one_core'),
        'shutdown_latency_seconds': trial.get('shutdown_latency_seconds'),
    }
    if metric in direct:
        value = direct[metric]
    elif metric.startswith('rpc.'):
        _, operation_name, field = metric.split('.', 2)
        value = (trial.get('rpc_measurements') or {}).get(operation_name, {}).get(field)
    else:
        raise KeyError(f'unknown scaling metric {metric!r}')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def scaling_metric_summary(values: list[float], key: str, *, paired: bool = False) -> dict[str, object]:
    return {
        **describe(values),
        'ci95_mean': bootstrap_mean_interval(values, key, paired=paired),
    }


def summarize_scaling(raw: dict[str, object]) -> dict[str, object]:
    trials = raw.get('trials', [])
    configuration = raw.get('configuration', {})
    expected_iterations = int(configuration.get('iterations_per_variant_and_count', 0))
    base_metrics = (
        'startup_to_readiness_seconds',
        'pss_after_readiness_mib',
        'private_dirty_after_readiness_mib',
        'process_count_after_readiness',
        'live_process_count_after_readiness',
        'catalog_entry_count',
        'catalog_active_plugin_count',
        'catalog_passive_plugin_count',
        'idle_cpu_percent_of_one_core',
        'shutdown_latency_seconds',
    )
    operation_fields = (
        'latency_p50_ms',
        'latency_p95_ms',
        'throughput_calls_per_second',
        'loaded_cpu_usec_per_completed_call',
        'loaded_cpu_usage_usec',
    )

    summary: dict[str, object] = {
        'schema_version': 2,
        'created_at_utc': utc_now(),
        'raw_file': 'raw.json',
        'profile': 'plugin-count-scaling',
        'comparability_limitation': raw.get('comparability_limitation'),
        'memory_sharing_note': raw.get('memory_sharing_note'),
        'trial_count': len(trials),
        'successful_trial_count': sum(trial.get('status') == 'success' for trial in trials),
        'failed_trial_count': sum(trial.get('status') != 'success' for trial in trials),
        'drained_trial_count': sum((trial.get('containment') or {}).get('drained') is True for trial in trials),
        'expected_iterations_per_variant_and_count': expected_iterations,
        'cells': [],
        'comparison_table': [],
        'paired_comparisons': [],
        'per_added_plugin_memory_slopes': [],
        'failed_trials': [
            {
                'trial_uuid': trial.get('trial_uuid'),
                'plugin_count': trial.get('plugin_count'),
                'variant': trial.get('variant'),
                'replication': trial.get('replication'),
                'error': trial.get('error') or trial.get('cleanup_error') or trial.get('normal_service_check_error'),
                'cleanup': trial.get('containment'),
            }
            for trial in trials if trial.get('status') != 'success'
        ],
    }
    cells = summary['cells']
    comparison_table = summary['comparison_table']
    paired_comparisons = summary['paired_comparisons']
    assert isinstance(cells, list) and isinstance(comparison_table, list) and isinstance(paired_comparisons, list)

    count_operation_names = {
        count: ['backend_ping']
        + (['common_real_plugin'] if count >= 4 else [])
        + (['fixture_plugin'] if count >= 8 else [])
        for count in SCALING_PLUGIN_COUNTS
    }
    for count in SCALING_PLUGIN_COUNTS:
        for variant in SCALING_VARIANTS:
            selected = [
                trial for trial in trials
                if trial.get('plugin_count') == count and trial.get('variant') == variant
            ]
            successful = [trial for trial in selected if trial.get('status') == 'success']
            metrics = {}
            names = list(base_metrics)
            names.extend(
                f'rpc.{operation}.{field}'
                for operation in count_operation_names[count]
                for field in operation_fields
            )
            for metric in names:
                values = [
                    value for trial in successful
                    if (value := scaling_metric_value(trial, metric)) is not None
                ]
                metrics[metric] = scaling_metric_summary(
                    values, f'cell:{variant}:{count}:{metric}'
                )
            cell = {
                'plugin_count': count,
                'variant': variant,
                'trial_count': len(selected),
                'successful_trial_count': len(successful),
                'failed_trial_count': len(selected) - len(successful),
                'drained_trial_count': sum((trial.get('containment') or {}).get('drained') is True for trial in selected),
                'active_plugin_count': (
                    successful[0].get('catalog_active_plugin_count') if successful else None
                ),
                'passive_plugin_count': (
                    successful[0].get('catalog_passive_plugin_count') if successful else None
                ),
                'catalog_entry_count': successful[0].get('catalog_entry_count') if successful else None,
                'metrics': metrics,
            }
            cells.append(cell)
            table_row = {
                'plugin_count': count,
                'variant': variant,
                'successful_trial_count': len(successful),
                'active_plugin_count': cell['active_plugin_count'],
                'passive_plugin_count': cell['passive_plugin_count'],
                'catalog_entry_count': cell['catalog_entry_count'],
                'metrics': {
                    metric: {
                        'mean': values['mean'],
                        'ci95_mean': values['ci95_mean'],
                    }
                    for metric, values in metrics.items()
                },
            }
            comparison_table.append(table_row)

    for count in SCALING_PLUGIN_COUNTS:
        by_variant = {}
        for variant in SCALING_VARIANTS:
            by_variant[variant] = {
                int(trial['replication']): trial
                for trial in trials
                if trial.get('plugin_count') == count and trial.get('variant') == variant
                and trial.get('status') == 'success'
            }
        python_trials = by_variant['python-installed']
        rust_trials = by_variant['rust-lean-fork']
        paired_reps = sorted(set(python_trials).intersection(rust_trials))
        metrics = list(base_metrics)
        metrics.extend(
            f'rpc.{operation}.{field}'
            for operation in count_operation_names[count]
            for field in operation_fields
        )
        paired_rows = []
        differences_by_metric: dict[str, list[tuple[int, float]]] = {metric: [] for metric in metrics}
        python_values_by_metric: dict[str, list[tuple[int, float]]] = {metric: [] for metric in metrics}
        rust_values_by_metric: dict[str, list[tuple[int, float]]] = {metric: [] for metric in metrics}
        for replication in paired_reps:
            python_trial = python_trials[replication]
            rust_trial = rust_trials[replication]
            differences = {}
            python_values = {}
            rust_values = {}
            for metric in metrics:
                python_value = scaling_metric_value(python_trial, metric)
                rust_value = scaling_metric_value(rust_trial, metric)
                if python_value is None or rust_value is None:
                    continue
                difference = rust_value - python_value
                differences[metric] = difference
                python_values[metric] = python_value
                rust_values[metric] = rust_value
                differences_by_metric[metric].append((replication, difference))
                python_values_by_metric[metric].append((replication, python_value))
                rust_values_by_metric[metric].append((replication, rust_value))
            paired_rows.append({
                'replication': replication,
                'python_trial_uuid': python_trial.get('trial_uuid'),
                'rust_trial_uuid': rust_trial.get('trial_uuid'),
                'python_order_position': python_trial.get('order_position'),
                'rust_order_position': rust_trial.get('order_position'),
                'python_values': python_values,
                'rust_values': rust_values,
                'rust_minus_python': differences,
            })
        paired_metrics = {}
        for metric, values in differences_by_metric.items():
            differences = [difference for _, difference in values]
            python_values = [value for _, value in python_values_by_metric[metric]]
            rust_values = [value for _, value in rust_values_by_metric[metric]]
            relative_changes = [
                (rust_value - python_value) * 100 / python_value
                for python_value, rust_value in zip(python_values, rust_values)
                if python_value != 0
            ]
            lower_is_better = not metric.endswith('throughput_calls_per_second')
            paired_metrics[metric] = {
                'direction': 'rust-lean-fork minus python-installed',
                'lower_is_better': lower_is_better,
                'n_pairs': len(differences),
                'python_mean': statistics.mean(python_values) if python_values else None,
                'rust_mean': statistics.mean(rust_values) if rust_values else None,
                'mean_difference': statistics.mean(differences) if differences else None,
                'ci95_mean_difference': bootstrap_mean_interval(
                    differences, f'paired:count:{count}:{metric}', paired=True
                ),
                'mean_relative_change_percent': (
                    statistics.mean(relative_changes) if relative_changes else None
                ),
                'ci95_mean_relative_change_percent': bootstrap_mean_interval(
                    relative_changes, f'paired-relative:count:{count}:{metric}', paired=True
                ),
                'replication_differences': [
                    {'replication': replication, 'difference': difference}
                    for replication, difference in values
                ],
            }
        comparison = {
            'plugin_count': count,
            'matched_successful_pair_count': len(paired_reps),
            'paired_trials': paired_rows,
            'metrics': paired_metrics,
        }
        paired_comparisons.append(comparison)

    slope_rows = summary['per_added_plugin_memory_slopes']
    assert isinstance(slope_rows, list)
    for variant in SCALING_VARIANTS:
        for lower_count, upper_count in zip(SCALING_PLUGIN_COUNTS, SCALING_PLUGIN_COUNTS[1:]):
            lower_trials = {
                int(trial['replication']): trial
                for trial in trials
                if trial.get('plugin_count') == lower_count and trial.get('variant') == variant
                and trial.get('status') == 'success'
            }
            upper_trials = {
                int(trial['replication']): trial
                for trial in trials
                if trial.get('plugin_count') == upper_count and trial.get('variant') == variant
                and trial.get('status') == 'success'
            }
            slopes = {'pss_after_readiness_mib': [], 'private_dirty_after_readiness_mib': []}
            for replication in sorted(set(lower_trials).intersection(upper_trials)):
                for metric in slopes:
                    lower_value = scaling_metric_value(lower_trials[replication], metric)
                    upper_value = scaling_metric_value(upper_trials[replication], metric)
                    if lower_value is not None and upper_value is not None:
                        slopes[metric].append({
                            'replication': replication,
                            'slope_mib_per_added_plugin': (
                                upper_value - lower_value
                            ) / (upper_count - lower_count),
                        })
            slope_metrics = {}
            for metric, observations in slopes.items():
                values = [item['slope_mib_per_added_plugin'] for item in observations]
                slope_metrics[metric] = {
                    'n_matched_replications': len(values),
                    'mean_mib_per_added_plugin': statistics.mean(values) if values else None,
                    'ci95_mean_mib_per_added_plugin': bootstrap_mean_interval(
                        values, f'slope:{variant}:{lower_count}-{upper_count}:{metric}', paired=True
                    ),
                    'replication_slopes': observations,
                }
            slope_rows.append({
                'variant': variant,
                'from_plugin_count': lower_count,
                'to_plugin_count': upper_count,
                'added_plugins': upper_count - lower_count,
                'added_plugin_composition': (
                    'four installed plugins' if lower_count == 0
                    else 'deterministic scaling fixtures'
                ),
                'metrics': slope_metrics,
            })

    incomplete_cells = [
        (cell['plugin_count'], cell['variant'])
        for cell in cells
        if cell['trial_count'] != expected_iterations or cell['successful_trial_count'] != expected_iterations
    ]
    all_drained = all(
        (trial.get('containment') or {}).get('drained') is True for trial in trials
    )
    memory_nonregression = True
    cpu_nonregression = True
    memory_benefit = False
    startup_rpc_wins = []
    cpu_metrics = {'idle_cpu_percent_of_one_core'}
    for count in SCALING_PLUGIN_COUNTS:
        cpu_metrics.update(
            f'rpc.{operation}.loaded_cpu_usec_per_completed_call'
            for operation in count_operation_names[count]
        )
    for comparison in paired_comparisons:
        for metric in ('pss_after_readiness_mib', 'private_dirty_after_readiness_mib'):
            metric_result = comparison['metrics'][metric]
            interval = metric_result['ci95_mean_difference']
            if metric_result['n_pairs'] < expected_iterations or interval is None or interval['upper'] > 0:
                memory_nonregression = False
            if metric == 'pss_after_readiness_mib' and interval is not None and interval['upper'] < 0:
                memory_benefit = True
        for metric in cpu_metrics:
            metric_result = comparison['metrics'].get(metric)
            if metric_result is None:
                continue
            interval = metric_result['ci95_mean_difference']
            if metric_result['n_pairs'] < expected_iterations or interval is None or interval['upper'] > 0:
                cpu_nonregression = False
        for metric, metric_result in comparison['metrics'].items():
            is_startup_or_rpc = metric == 'startup_to_readiness_seconds' or metric.startswith('rpc.')
            if not is_startup_or_rpc or metric_result['n_pairs'] < expected_iterations:
                continue
            interval = metric_result['ci95_mean_difference']
            if interval is None:
                continue
            lower_is_better = metric_result['lower_is_better']
            is_improvement = interval['upper'] < 0 if lower_is_better else interval['lower'] > 0
            if not is_improvement:
                continue
            relative_change = metric_result['mean_relative_change_percent']
            startup_rpc_wins.append({
                'plugin_count': comparison['plugin_count'],
                'metric': metric,
                'n_pairs': metric_result['n_pairs'],
                'mean_difference_rust_minus_python': metric_result['mean_difference'],
                'ci95_mean_difference_rust_minus_python': interval,
                'mean_relative_change_percent_rust_minus_python': relative_change,
                'mean_improvement_percent': (
                    -relative_change if relative_change is not None and lower_is_better
                    else relative_change
                ),
                'improvement_direction': 'lower' if lower_is_better else 'higher',
            })
    recommendation_reasons = []
    if raw.get('completion') != 'complete':
        recommendation_reasons.append(
            'campaign did not complete its snapshot, runtime-artifact, and normal-service integrity checks'
        )
    if incomplete_cells:
        recommendation_reasons.append(f'not all variant/count cells completed {expected_iterations} successful trials: {incomplete_cells}')
    if not all_drained:
        recommendation_reasons.append('at least one owned transient service did not drain cleanly')
    if not memory_nonregression:
        recommendation_reasons.append(
            'paired 95% intervals do not establish non-increasing PSS and Private_Dirty at every plugin count'
        )
    if not cpu_nonregression:
        recommendation_reasons.append(
            'paired 95% intervals do not establish non-increasing idle CPU and per-operation RPC CPU at every count'
        )
    if not memory_benefit:
        recommendation_reasons.append('no plugin count establishes lower Rust PSS with a 95% paired interval')
    summary['startup_rpc_repeatable_wins'] = startup_rpc_wins
    summary['proceed_or_stop_recommendation'] = {
        'decision': 'PROCEED' if not recommendation_reasons else 'STOP',
        'gate': (
            f'at least {expected_iterations} successful matched trials per variant/count; all owned units drained; '
            'Rust-minus-Python paired 95% mean-difference intervals for PSS, Private_Dirty, idle CPU, and each '
            'available operation’s cgroup CPU per call are non-positive at every count; at least one count shows '
            'a PSS interval strictly below zero'
        ),
        'startup_rpc_interpretation': (
            'No numeric speed threshold is assumed. startup_rpc_repeatable_wins lists only startup or RPC '
            'metrics whose paired 95% interval supports an improvement; evaluate and report each measured '
            'latency, throughput, or CPU result rather than claiming a generic speedup.'
        ),
        'reasons': recommendation_reasons,
    }
    return summary


def summarize(raw: dict[str, object]) -> dict[str, object]:
    if raw.get('profile') == 'plugin-count-scaling':
        return summarize_scaling(raw)
    trials = raw.get('trials', [])
    summary: dict[str, object] = {
        'schema_version': 1,
        'created_at_utc': utc_now(),
        'raw_file': 'raw.json',
        'comparability_limitation': raw.get('comparability_limitation'),
        'memory_sharing_note': raw.get('memory_sharing_note'),
        'trial_count': len(trials),
        'successful_trial_count': sum(trial.get('status') == 'success' for trial in trials),
        'failed_trial_count': sum(trial.get('status') != 'success' for trial in trials),
        'variants': {},
        'failed_trials': [
            {
                'trial_uuid': trial.get('trial_uuid'),
                'variant': trial.get('variant'),
                'replication': trial.get('replication'),
                'error': trial.get('error') or trial.get('cleanup_error') or trial.get('normal_service_check_error'),
                'cleanup': trial.get('containment'),
            }
            for trial in trials if trial.get('status') != 'success'
        ],
    }
    variants = summary['variants']
    assert isinstance(variants, dict)
    metric_getters = {
        'startup_to_readiness_seconds': lambda trial: trial.get('startup_to_readiness_seconds'),
        'pss_after_readiness_mib': lambda trial: trial.get('pss_after_readiness_mib'),
        'private_dirty_after_readiness_mib': lambda trial: trial.get('private_dirty_after_readiness_mib'),
        'process_count_after_readiness': lambda trial: trial.get('process_count_after_readiness'),
        'idle_cpu_percent_of_one_core': lambda trial: (trial.get('idle_cpu') or {}).get('percent_of_one_core'),
        'rpc_latency_p50_ms': lambda trial: (trial.get('rpc') or {}).get('latency_p50_ms'),
        'rpc_latency_p95_ms': lambda trial: (trial.get('rpc') or {}).get('latency_p95_ms'),
        'rpc_throughput_calls_per_second': lambda trial: (trial.get('rpc') or {}).get('throughput_calls_per_second'),
        'loaded_cpu_usec_per_completed_call': lambda trial: (trial.get('rpc') or {}).get('loaded_cpu_usec_per_completed_call'),
        'loaded_cpu_usage_usec': lambda trial: (trial.get('rpc') or {}).get('loaded_cpu_usage_usec'),
        'loaded_wall_seconds': lambda trial: (trial.get('rpc') or {}).get('loaded_wall_seconds'),
        'shutdown_latency_seconds': lambda trial: trial.get('shutdown_latency_seconds'),
    }
    for variant, _, _ in VARIANTS:
        selected = [trial for trial in trials if trial.get('variant') == variant]
        successes = [trial for trial in selected if trial.get('status') == 'success']
        metrics = {}
        for name, getter in metric_getters.items():
            values = [getter(trial) for trial in successes]
            values = [float(value) for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
            metrics[name] = describe(values)
        variants[variant] = {
            'trial_count': len(selected),
            'successful_trial_count': len(successes),
            'failed_trial_count': len(selected) - len(successes),
            'successful_trials_only': True,
            'drained_trial_count': sum((trial.get('containment') or {}).get('drained') is True for trial in selected),
            'metrics': metrics,
        }
    return summary


def build_schedule(profile: str, iterations: int) -> list[dict[str, object]]:
    schedule = []
    for replication_index in range(iterations):
        replication = replication_index + 1
        if profile == 'standard':
            order = [VARIANTS[(replication_index + offset) % len(VARIANTS)][0] for offset in range(len(VARIANTS))]
            for position, variant in enumerate(order, 1):
                schedule.append({
                    'replication': replication,
                    'position': position,
                    'variant': variant,
                    'plugin_count': None,
                })
            continue
        count_offset = replication_index % len(SCALING_PLUGIN_COUNTS)
        counts = SCALING_PLUGIN_COUNTS[count_offset:] + SCALING_PLUGIN_COUNTS[:count_offset]
        position = 1
        for plugin_count in counts:
            pair_start = (replication_index + SCALING_PLUGIN_COUNTS.index(plugin_count)) % len(SCALING_VARIANTS)
            variants = SCALING_VARIANTS[pair_start:] + SCALING_VARIANTS[:pair_start]
            for variant in variants:
                schedule.append({
                    'replication': replication,
                    'position': position,
                    'variant': variant,
                    'plugin_count': plugin_count,
                })
                position += 1
    return schedule


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-executable', required=True, help='installed Python backend executable (PluginLoader)')
    parser.add_argument('--rust-executable', required=True, help='private Rust compatibility probe executable')
    parser.add_argument('--worker', required=True, help='single PyInstaller onedir DeckyPluginWorker executable')
    parser.add_argument('--assets', required=True, help='existing Rust probe frontend assets directory')
    parser.add_argument('--locales', required=True, help='existing Rust probe locale directory')
    parser.add_argument('--output', required=True, help='new or empty output directory outside the repository')
    parser.add_argument(
        '--profile', choices=('standard', 'plugin-count-scaling'), default='standard',
        help='standard compatibility matrix or matched 0/4/8/12 plugin-count scaling',
    )
    parser.add_argument(
        '--release-version', default=None,
        help='Rust identity passed to the backend; required for scaling (standard default: dev)',
    )
    parser.add_argument(
        '--iterations', type=int, default=10,
        help='replications per variant (and per plugin count in scaling profile); minimum 10 (default: 10)',
    )
    parser.add_argument('--startup-timeout', type=float, default=120.0, help='startup/readiness deadline in seconds')
    parser.add_argument('--rpc-timeout', type=float, default=15.0, help='timeout per WebSocket RPC in seconds')
    parser.add_argument('--idle-seconds', type=float, default=4.0, help='idle cgroup CPU sampling interval')
    args = parser.parse_args()
    if args.profile == 'plugin-count-scaling' and args.release_version is None:
        parser.error('plugin-count-scaling requires an explicit --release-version')
    if args.release_version is None:
        args.release_version = 'dev'
    if not args.release_version.strip():
        parser.error('--release-version must not be empty')
    if args.iterations < 10:
        parser.error('--iterations must be at least 10 per variant/count')
    if args.startup_timeout <= 0 or args.rpc_timeout <= 0 or args.idle_seconds <= 0:
        parser.error('startup, RPC, and idle timeouts must be positive')
    return args


def prepare_inputs(args: argparse.Namespace, work_root: Path) -> dict[str, object]:
    python_executable = validate_path(args.python_executable, 'installed Python backend executable', executable=True)
    if python_executable != NORMAL_BINARY.resolve(strict=True):
        raise BenchmarkError(
            f'--python-executable must identify the untouched installed backend at {NORMAL_BINARY.resolve(strict=True)}'
        )
    rust_executable = validate_path(args.rust_executable, 'Rust executable', executable=True)
    worker = validate_path(args.worker, 'onedir worker', executable=True)
    if not (worker.parent / '_internal').is_dir():
        raise BenchmarkError(f'onedir worker must have a sibling _internal directory: {worker.parent / "_internal"}')
    assets = validate_path(args.assets, 'frontend assets', directory=True)
    locales = validate_path(args.locales, 'locale directory', directory=True)
    worker_info = worker_description(worker, work_root / 'worker-describe-tmp')
    python_hash = sha256_file(python_executable)
    rust_hash = sha256_file(rust_executable)
    worker_hash = sha256_file(worker)
    assets_digest = tree_digest(assets)
    locales_digest = tree_digest(locales)
    worker_internal_digest = tree_digest(worker.parent / '_internal')
    python_inventory = {
        'executable': str(python_executable),
        'executable_sha256': python_hash,
        'sdk_version': SDK_VERSION,
        'homebrew_input_snapshot': 'recorded after one read-only capture',
    }
    rust_bundle = {
        'release_version': args.release_version,
        'sdk_version': SDK_VERSION,
        'rust_executable': str(rust_executable),
        'rust_executable_sha256': rust_hash,
        'worker': str(worker),
        'worker_sha256': worker_hash,
        'worker_onedir_internal_tree': worker_internal_digest,
        'assets_tree': assets_digest,
        'locales_tree': locales_digest,
        'worker_description': worker_info,
    }
    return {
        'paths': {
            'python_executable': str(python_executable),
            'rust_executable': str(rust_executable),
            'worker': str(worker),
            'assets': str(assets),
            'locales': str(locales),
            'live_homebrew_snapshot_source': str(HOME_BREW),
        },
        'binary_sha256': {
            'python_executable': python_hash,
            'rust_executable': rust_hash,
            'worker': worker_hash,
        },
        'runtime_descriptions': {'worker': worker_info},
        'assets_tree': assets_digest,
        'locales_tree': locales_digest,
        'worker_onedir_internal_tree': worker_internal_digest,
        'python_source_inventory': python_inventory,
        'python_source_inventory_fingerprint': stable_fingerprint(python_inventory),
        'rust_bundle_inventory': rust_bundle,
        'rust_bundle_fingerprint': stable_fingerprint(rust_bundle),
    }


def run_benchmark(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        raise BenchmarkError('run this harness as root so transient cgroups and namespace clients are owned safely')
    repository_root = Path(__file__).resolve().parents[2]
    output = ensure_output_directory(Path(args.output), repository_root)
    if not (CGROUP_ROOT / 'cgroup.controllers').is_file():
        raise BenchmarkError('benchmark requires the systemd cgroup v2 hierarchy')
    temporary_root = Path(os.environ.get('TMPDIR') or '/var/tmp/omp-1000').expanduser().resolve()
    temporary_root.mkdir(mode=0o700, parents=True, exist_ok=True)

    release_version = args.release_version
    active_variants = (
        VARIANTS if args.profile == 'standard'
        else tuple(entry for entry in VARIANTS if entry[0] in SCALING_VARIANTS)
    )

    raw: dict[str, object] = {
        'schema_version': 1,
        'profile': args.profile,
        'release_version': release_version,
        'started_at_utc': utc_now(),
        'output_directory': str(output),
        'comparability_limitation': (
            'python-installed is the official installed Python backend, reporting its SDK loader version. Rust '
            'runs the private Linux/SteamOS backend with the same packaged SDK/frontend baseline. Scaling cells '
            'share the same selected plugin catalog per count; frontend injection is disabled and each unit uses '
            'a private network with no live CEF. Results do not establish full-product, physical Steam Deck, or '
            'long-duration performance. Startup is process-cold only and does not flush OS caches.'
        ),
        'memory_sharing_note': (
            'PSS apportions shared resident pages across mapped processes, while Private_Dirty reports pages private '
            'to one process and modified since sharing; forked workers may share copy-on-write pages until modified. '
            'Use cgroup totals and per-process PSS/private-dirty together; do not add RSS values.'
        ),
        'measurement_definitions': {
            'startup_to_readiness_seconds': 'monotonic time from before systemd-run until authenticated WebSocket, '
            'exact catalog discovery with no load errors, expected version identity, and the count-appropriate '
            'read-only plugin checks succeed',
            'catalog_active_plugin_count': 'discovered catalog entries classified active by the captured clone '
            'having main.py; checked against the exact runtime name set',
            'catalog_passive_plugin_count': 'discovered catalog entries classified passive by the captured clone '
            'lacking main.py; checked against the exact runtime name set',
            'pss_after_readiness_mib': 'sum of Pss from /proc/<pid>/smaps_rollup for every live process in the '
            'owned transient unit cgroup immediately after readiness, including backend, worker launcher, and plugin workers',
            'private_dirty_after_readiness_mib': 'sum of Private_Dirty from /proc/<pid>/smaps_rollup for every live '
            'process in the owned transient unit cgroup; per-process PSS/private-dirty, PID, PPID, and comm are recorded',
            'idle_cpu_percent_of_one_core': 'owned cgroup cpu.stat usage_usec delta divided by wall interval; '
            '100 percent means one fully occupied CPU core',
            'rpc_latency_ms': 'sequential authenticated WebSocket CALL/REPLY latency after 10 warmups; every reply '
            'is acknowledged and validated; scaling profile measures backend ping at every count and only present plugin RPCs',
            'loaded_cpu_usec_per_completed_call': 'owned cgroup cpu.stat usage_usec delta during each measured RPC '
            'loop divided by its completed calls',
            'shutdown_latency_seconds': 'time from SIGTERM to the owned MainPID until the unit is stopped and its '
            'cgroup is empty',
        },
        'configuration': {
            'profile': args.profile,
            'iterations_per_variant': args.iterations,
            'iterations_per_variant_and_count': args.iterations,
            'release_version': release_version,
            'sdk_version': SDK_VERSION,
            'version_identity_contract': {
                'python_current': SDK_VERSION,
                'rust_current': release_version,
                'rust_sdk_version': SDK_VERSION,
            },
            'variants': [
                {'name': name, 'worker_mode': mode, 'worker_bootstrap': bootstrap}
                for name, mode, bootstrap in active_variants
            ],
            'plugin_counts': list(SCALING_PLUGIN_COUNTS) if args.profile == 'plugin-count-scaling' else None,
            'temporary_directory': str(temporary_root),
            'rpc_calls_per_operation_per_trial': 200,
            'rpc_calls_per_trial': 200,
            'rpc_warmup_calls': 10,
            'idle_interval_seconds': args.idle_seconds,
            'listen': LISTEN,
            'private_network_for_every_trial': True,
            'frontend_injection': False,
            'no_live_cef_access': True,
            'no_plugin_loader_service_stop_or_reconfiguration': True,
            'unit_kill_mode': 'control-group',
            'inaccessible_paths': ['/run/systemd/private', '/run/dbus/system_bus_socket'],
            'read_only_paths': ['/home/deck/homebrew', '/home/deck/.local/share/Steam', '/home/deck/.steam (optional)'],
            'environment': {
                'UNPRIVILEGED_USER': 'deck', 'CHOWN_PLUGIN_PATH': '0', 'SERVER_HOST': '127.0.0.1',
                'SERVER_PORT': '1337', 'LIVE_RELOAD': '0',
            },
        },
        'system': system_information(),
        'trials': [],
    }
    work_root: Path | None = None
    safe_to_remove_work = True
    fatal_error: BaseException | None = None
    try:
        baseline = service_baseline()
        raw['normal_service_baseline'] = baseline
        if Path(str(baseline['binary_path'])) != Path(args.python_executable).expanduser().resolve(strict=True):
            raise BenchmarkError('normal service binary and explicit Python variant path differ')
        assert_service_unchanged(baseline)
        global _ACTIVE_BASELINE
        _ACTIVE_BASELINE = baseline

        work_root = temporary_root / f'decky-runtime-benchmark-{uuid.uuid4().hex}'
        work_root.mkdir(mode=0o755)
        preflight = prepare_inputs(args, work_root)
        raw['inputs'] = preflight

        snapshot = work_root / 'snapshot'
        source_info = copy_snapshot(HOME_BREW, snapshot)
        snapshot_digest = inputs_digest(snapshot)
        settings = parse_settings(snapshot / 'settings' / 'loader.json')
        installed_plugins = validate_installed_backends(snapshot, settings)
        source_catalog = runtime_catalog(snapshot / 'plugins')
        python_inventory = dict(preflight['python_source_inventory'])
        python_inventory['homebrew_snapshot'] = snapshot_digest
        python_inventory['homebrew_source_inventory'] = source_info
        preflight['python_source_inventory'] = python_inventory
        preflight['python_source_inventory_fingerprint'] = stable_fingerprint(python_inventory)
        raw['homebrew_snapshot'] = {
            'source_directories': source_info,
            'input_directories': list(INPUT_DIRECTORIES),
            'sha256': snapshot_digest,
            'installed_backends': installed_plugins,
            'catalog': source_catalog,
            'captured_from_live_homebrew_once': True,
        }
        scaling_snapshots = {}
        if args.profile == 'plugin-count-scaling':
            scaling_snapshots = create_scaling_snapshots(snapshot, work_root)
            raw['plugin_count_snapshots'] = [
                {
                    'plugin_count': count,
                    'sha256': details['digest']['sha256'],
                    'input_entries': details['digest']['entries'],
                    'active_plugin_count': details['active_plugin_count'],
                    'passive_plugin_count': details['passive_plugin_count'],
                    'catalog': details['catalog'],
                    'source_inventory': details['source_inventory'],
                }
                for count, details in scaling_snapshots.items()
            ]
        raw['schedule'] = build_schedule(args.profile, args.iterations)

        account = pwd.getpwnam('deck')
        path_map = preflight['paths']
        python_executable = Path(path_map['python_executable'])
        rust_executable = Path(path_map['rust_executable'])
        worker = Path(path_map['worker'])
        assets = Path(path_map['assets'])
        locales = Path(path_map['locales'])

        for schedule in raw['schedule']:
            assert_service_unchanged(baseline)
            if args.profile == 'plugin-count-scaling':
                count = int(schedule['plugin_count'])
                cell = scaling_snapshots[count]
                trial_snapshot = Path(cell['path'])
                trial_snapshot_digest = cell['digest']
                fixture_names = list(cell['fixture_names'])
            else:
                count = None
                trial_snapshot = snapshot
                trial_snapshot_digest = snapshot_digest
                fixture_names = []
            trial, safe = run_trial(
                variant=schedule['variant'],
                replication=schedule['replication'],
                order_position=schedule['position'],
                profile=args.profile,
                plugin_count=count,
                fixture_names=fixture_names,
                snapshot=trial_snapshot,
                expected_snapshot_digest=trial_snapshot_digest,
                account=account,
                output=output,
                work_root=work_root,
                python_executable=python_executable,
                rust_executable=rust_executable,
                worker=worker,
                assets=assets,
                locales=locales,
                release_version=release_version,
                python_inventory_fingerprint=preflight['python_source_inventory_fingerprint'],
                rust_bundle_fingerprint=preflight['rust_bundle_fingerprint'],
                startup_timeout=args.startup_timeout,
                rpc_timeout=args.rpc_timeout,
                idle_seconds=args.idle_seconds,
            )
            raw['trials'].append(trial)
            atomic_json(output / 'raw.json', raw)
            count_label = f" plugins={trial['plugin_count']}" if trial['plugin_count'] is not None else ''
            print(
                f"{trial['variant']}{count_label} replication {trial['replication']}: {trial['status']}"
                + (f" ({trial.get('error')})" if trial.get('error') else ''),
                flush=True,
            )
            if trial.get('interrupted') is True:
                raise KeyboardInterrupt()
            if not safe:
                safe_to_remove_work = False
                raise BenchmarkError('stopping further trials because owned-unit drainage or normal-service safety failed')

        final_snapshot_digest = inputs_digest(snapshot)
        raw['homebrew_snapshot']['final_sha256'] = final_snapshot_digest
        raw['homebrew_snapshot']['immutable_snapshot_unchanged'] = final_snapshot_digest == snapshot_digest
        if final_snapshot_digest != snapshot_digest:
            raise BenchmarkError('immutable captured homebrew snapshot changed during benchmark execution')
        final_scaling_digests = []
        for count, details in scaling_snapshots.items():
            final_digest = inputs_digest(Path(details['path']))
            unchanged = final_digest == details['digest']
            final_scaling_digests.append({
                'plugin_count': count,
                'final_sha256': final_digest,
                'unchanged': unchanged,
            })
            if not unchanged:
                raise BenchmarkError(f'plugin-count snapshot {count} changed during benchmark execution')
        if scaling_snapshots:
            raw['plugin_count_snapshots_final'] = final_scaling_digests
        postflight = prepare_inputs(args, work_root)
        artifacts_unchanged = (
            postflight['binary_sha256']['python_executable'] == preflight['binary_sha256']['python_executable']
            and postflight['rust_bundle_fingerprint'] == preflight['rust_bundle_fingerprint']
        )
        raw['runtime_artifacts_final_check'] = {
            'python_executable_sha256': postflight['binary_sha256']['python_executable'],
            'python_executable_unchanged': (
                postflight['binary_sha256']['python_executable'] == preflight['binary_sha256']['python_executable']
            ),
            'rust_bundle_fingerprint': postflight['rust_bundle_fingerprint'],
            'rust_bundle_unchanged': (
                postflight['rust_bundle_fingerprint'] == preflight['rust_bundle_fingerprint']
            ),
            'all_selected_runtime_artifacts_unchanged': artifacts_unchanged,
        }
        if not artifacts_unchanged:
            raise BenchmarkError('selected runtime artifact contents changed during benchmark execution')
        assert_service_unchanged(baseline)
        raw['normal_service_final_check'] = {
            'unchanged': True,
            'main_pid': baseline['main_pid'],
            'binary_sha256': baseline['binary_sha256'],
            'loader_json_sha256': baseline['loader_json_sha256'],
        }
    except BaseException as error:
        fatal_error = error
        raw['fatal_error'] = f'{type(error).__name__}: {error}'
        raw['fatal_error_traceback'] = ''.join(traceback.format_exception(type(error), error, error.__traceback__))[-16000:]
    finally:
        if work_root is not None:
            if safe_to_remove_work:
                try:
                    shutil.rmtree(work_root)
                    raw['runtime_work_directory_removed'] = True
                except OSError as error:
                    raw['runtime_work_directory_removed'] = False
                    raw['runtime_work_directory_cleanup_error'] = str(error)
            else:
                raw['runtime_work_directory_retained'] = str(work_root)
        raw['finished_at_utc'] = utc_now()
        raw['completion'] = 'complete' if fatal_error is None else 'incomplete'
        summary = summarize(raw)
        atomic_json(output / 'raw.json', raw)
        atomic_json(output / 'summary.json', summary)

    if fatal_error is not None:
        print(f'Benchmark stopped: {type(fatal_error).__name__}: {fatal_error}', file=sys.stderr)
    else:
        print(f"Raw trials: {output / 'raw.json'}")
        print(f"Summary: {output / 'summary.json'}")
    return 1 if fatal_error is not None or summary['failed_trial_count'] else 0


class _WebSocketClient:
    def __init__(self, session, base_url: str, token: str, rpc_timeout: float):
        self.session = session
        self.base_url = base_url
        self.token = token
        self.rpc_timeout = rpc_timeout
        self.socket = None
        self.sequence = 0
        self.events = 0

    async def connect(self) -> None:
        self.socket = await self.session.ws_connect(
            self.base_url.replace('http:', 'ws:') + '/ws?auth=' + quote(self.token)
        )
        await self.socket.send_json({'type': 4, 'messages': []})

    async def call(self, route: str, *args):
        self.sequence += 1
        request = {'type': 0, 'id': self.sequence, 'route': route, 'args': list(args)}
        await self.socket.send_json(request)
        async with asyncio.timeout(self.rpc_timeout):
            while True:
                frame = await self.socket.receive_json()
                if frame.get('type') == 5:
                    self.events += 1
                if frame.get('id') != request['id']:
                    continue
                await self.socket.send_json({'type': 3, 'id': request['id']})
                if frame.get('type') != 1:
                    raise RuntimeError(f'{route} failed: {frame}')
                if 'result' not in frame:
                    raise RuntimeError(f'{route} reply has no result: {frame}')
                return frame['result']

    async def close(self) -> None:
        if self.socket is not None:
            await self.socket.close()


async def _readiness_client(request: dict[str, object]) -> dict[str, object]:
    import aiohttp

    base_url = str(request['base_url'])
    timeout = float(request['startup_timeout'])
    rpc_timeout = float(request['rpc_timeout'])
    deadline = time.monotonic() + timeout
    session_timeout = aiohttp.ClientTimeout(total=None, connect=5, sock_connect=5, sock_read=rpc_timeout)
    async with aiohttp.ClientSession(timeout=session_timeout, auto_decompress=False) as session:
        token = None
        while time.monotonic() < deadline:
            try:
                async with session.get(base_url + '/auth/token') as response:
                    if response.status != 200:
                        raise RuntimeError(f'/auth/token returned HTTP {response.status}')
                    token = (await response.text()).strip()
                    if not token:
                        raise RuntimeError('/auth/token returned an empty token')
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                await asyncio.sleep(0.1)
        if token is None:
            raise TimeoutError('auth token was not available before the startup deadline')

        client = _WebSocketClient(session, base_url, token, rpc_timeout)
        try:
            await client.connect()
            expected_catalog = request['expected_catalog']
            expected_names = set(expected_catalog)
            while True:
                plugins = await client.call('loader/get_plugins')
                if not isinstance(plugins, list):
                    raise RuntimeError(f'loader/get_plugins returned non-list: {plugins!r}')
                by_name = {}
                for plugin in plugins:
                    if not isinstance(plugin, dict) or not isinstance(plugin.get('name'), str):
                        raise RuntimeError(f'loader/get_plugins returned malformed plugin entry: {plugin!r}')
                    name = plugin['name']
                    if name in by_name:
                        raise RuntimeError(f'loader/get_plugins returned duplicate plugin name: {name!r}')
                    by_name[name] = plugin
                errors = {
                    name: plugin.get('error')
                    for name, plugin in by_name.items()
                    if plugin.get('error')
                }
                if errors:
                    raise RuntimeError(f'plugin load errors: {errors}')
                unexpected = sorted(set(by_name) - expected_names)
                if unexpected:
                    raise RuntimeError(f'loader catalog contains unexpected plugins: {unexpected}')
                missing = sorted(expected_names - set(by_name))
                if not missing:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f'expected plugins did not finish discovery: {missing}')
                await asyncio.sleep(0.05)

            for name, expected in expected_catalog.items():
                plugin = by_name[name]
                if plugin.get('disabled') is True:
                    raise RuntimeError(f'expected plugin is disabled: {name}')
                if plugin.get('load_type') != expected['load_type']:
                    raise RuntimeError(
                        f'plugin {name!r} load_type mismatch: expected {expected["load_type"]}, '
                        f'got {plugin.get("load_type")!r}'
                    )
                if plugin.get('version') != expected['package_version']:
                    raise RuntimeError(
                        f'plugin {name!r} package version mismatch: expected {expected["package_version"]!r}, '
                        f'got {plugin.get("version")!r}'
                    )

            active_names = sorted(name for name, entry in expected_catalog.items() if not entry['passive'])
            passive_names = sorted(name for name, entry in expected_catalog.items() if entry['passive'])
            active_count = len(active_names)
            passive_count = len(passive_names)

            version = await client.call('updater/get_version_info')
            expected_current = str(request['expected_current_version'])
            if not isinstance(version, dict) or version.get('current') != expected_current:
                raise RuntimeError(f'backend current version mismatch: expected {expected_current!r}, got {version!r}')
            expected_sdk = request.get('expected_sdk_version')
            if expected_sdk is not None and version.get('sdk_version') != expected_sdk:
                raise RuntimeError(
                    f'backend SDK version mismatch: expected {expected_sdk!r}, got {version.get("sdk_version")!r}'
                )

            metadata = None
            if 'Decky Metadata' in expected_catalog:
                metadata = await client.call(
                    'loader/call_plugin_method', 'Decky Metadata', 'get_platform_capabilities'
                )
                if not isinstance(metadata, dict) or metadata.get('platform') != 'linux' or metadata.get('is_steamos') is not True:
                    raise RuntimeError(f'Decky Metadata did not report SteamOS Linux readiness: {metadata!r}')

            ludusavi = None
            if 'SDH-Ludusavi' in expected_catalog:
                ludusavi = await client.call('loader/call_plugin_method', 'SDH-Ludusavi', 'get_settings')
                if not isinstance(ludusavi, dict) or 'auto_sync_enabled' not in ludusavi:
                    raise RuntimeError(f'SDH-Ludusavi read-only settings RPC failed: {ludusavi!r}')

            css = None
            if request['real_plugin_rpc']:
                css = await client.call(
                    'loader/call_legacy_plugin_method', 'CSS Loader', 'get_backend_version', {}
                )
                if not isinstance(css, dict) or css.get('success') is not True or not isinstance(css.get('result'), int):
                    raise RuntimeError(f'CSS Loader read-only backend RPC failed: {css!r}')

            arithmetic = None
            runtime = None
            benchmark_plugin = request.get('benchmark_plugin')
            if benchmark_plugin is not None:
                arithmetic = await client.call(
                    'loader/call_plugin_method', str(benchmark_plugin), 'sum_squares'
                )
                if type(arithmetic) is not int or arithmetic != int(request['expected_arithmetic']):
                    raise RuntimeError(f'benchmark arithmetic result mismatch: {arithmetic!r}')
                runtime = await client.call(
                    'loader/call_plugin_method', str(benchmark_plugin), 'runtime_info'
                )

            fixture_plugins = request.get('fixture_plugins', [])
            if not isinstance(fixture_plugins, list):
                raise RuntimeError(f'fixture_plugins must be a list, got {fixture_plugins!r}')
            fixture_results = {}
            for fixture_plugin in fixture_plugins:
                fixture_arithmetic = await client.call(
                    'loader/call_plugin_method', str(fixture_plugin), 'sum_squares'
                )
                if type(fixture_arithmetic) is not int or fixture_arithmetic != int(request['expected_arithmetic']):
                    raise RuntimeError(
                        f'scaling fixture {fixture_plugin!r} arithmetic result mismatch: {fixture_arithmetic!r}'
                    )
                fixture_results[fixture_plugin] = fixture_arithmetic

            return {
                'ok': True,
                'startup_seconds': time.monotonic() - float(request['started_monotonic']),
                'plugins': [
                    {
                        'name': item.get('name'),
                        'version': item.get('version'),
                        'load_type': item.get('load_type'),
                        'disabled': item.get('disabled'),
                        'error': item.get('error'),
                    }
                    for item in plugins
                ],
                'catalog_plugin_names': sorted(by_name),
                'catalog_entry_count': len(by_name),
                'catalog_active_plugin_count': active_count,
                'catalog_passive_plugin_count': passive_count,
                'active_plugins_discovered': active_names,
                'passive_plugins_discovered': passive_names,
                'plugin_load_errors': errors,
                'version': version,
                'metadata_capabilities': metadata,
                'ludusavi_settings_read': ludusavi is not None,
                'css_backend_version': css.get('result') if css else None,
                'arithmetic_result': arithmetic,
                'plugin_runtime': runtime,
                'fixture_rpc_ready_names': sorted(fixture_results),
                'fixture_arithmetic_results': fixture_results,
                'websocket_events_observed': client.events,
            }
        finally:
            await client.close()


def _validate_operation_result(result: object, validation: dict[str, object]) -> None:
    kind = validation.get('kind')
    if kind == 'arithmetic':
        expected = int(validation['expected'])
        if type(result) is not int or result != expected:
            raise RuntimeError(f'arithmetic result mismatch: expected {expected}, got {result!r}')
    elif kind == 'version':
        expected_current = str(validation['expected_current'])
        if not isinstance(result, dict) or result.get('current') != expected_current:
            raise RuntimeError(f'backend version mismatch: expected {expected_current!r}, got {result!r}')
        expected_sdk = validation.get('expected_sdk_version')
        if expected_sdk is not None and result.get('sdk_version') != expected_sdk:
            raise RuntimeError(f'backend SDK version mismatch: expected {expected_sdk!r}, got {result!r}')
    elif kind == 'css_version':
        if (
            not isinstance(result, dict) or result.get('success') is not True
            or not isinstance(result.get('result'), int) or isinstance(result.get('result'), bool)
        ):
            raise RuntimeError(f'CSS Loader read-only backend RPC failed: {result!r}')
    else:
        raise ValueError(f'unknown RPC result validation kind: {kind!r}')


async def _measurement_client(request: dict[str, object]) -> dict[str, object]:
    import aiohttp

    base_url = str(request['base_url'])
    rpc_timeout = float(request['rpc_timeout'])
    planned = int(request['rpc_calls'])
    warmups = int(request['warmup_calls'])
    operations = request['operations']
    if planned < 200:
        raise ValueError('measured RPC call count must be at least 200')
    if not isinstance(operations, list) or not operations:
        raise ValueError('measurement operations must be a non-empty list')
    names = [str(operation.get('name')) for operation in operations if isinstance(operation, dict)]
    if len(names) != len(operations) or len(set(names)) != len(names):
        raise ValueError('measurement operations must have unique names')

    session_timeout = aiohttp.ClientTimeout(total=None, connect=5, sock_connect=5, sock_read=rpc_timeout)
    async with aiohttp.ClientSession(timeout=session_timeout, auto_decompress=False) as session:
        async with session.get(base_url + '/auth/token') as response:
            if response.status != 200:
                raise RuntimeError(f'/auth/token returned HTTP {response.status}')
            token = (await response.text()).strip()
        if not token:
            raise RuntimeError('/auth/token returned an empty token')
        client = _WebSocketClient(session, base_url, token, rpc_timeout)
        try:
            await client.connect()
            plugin_list = await client.call('loader/get_plugins')
            if not isinstance(plugin_list, list):
                raise RuntimeError(f'loader/get_plugins returned non-list before measurement: {plugin_list!r}')
            load_errors = {
                item.get('name'): item.get('error')
                for item in plugin_list
                if isinstance(item, dict) and item.get('error')
            }
            if load_errors:
                raise RuntimeError(f'plugin load errors before measurement: {load_errors}')

            cgroup = Path(str(request['cgroup_path']))
            cpu_file = cgroup / 'cpu.stat'
            measurements = {}
            for operation in operations:
                name = str(operation['name'])
                route = str(operation['route'])
                args = operation['args']
                validation = operation['validation']
                if not isinstance(args, list) or not isinstance(validation, dict):
                    raise ValueError(f'malformed RPC operation {name!r}')
                entry: dict[str, object] = {
                    'name': name,
                    'route': route,
                    'args': args,
                    'warmup_calls': warmups,
                    'planned_calls': planned,
                    'completed_calls': 0,
                    'latency_ms': [],
                    'ok': False,
                }
                try:
                    plugin_name = operation.get('plugin_name')
                    if plugin_name is not None:
                        by_name = {
                            item.get('name'): item
                            for item in plugin_list if isinstance(item, dict)
                        }
                        plugin = by_name.get(plugin_name)
                        if plugin is None or plugin.get('disabled') is True:
                            raise RuntimeError(f'RPC plugin is not ready: {plugin_name!r} ({plugin!r})')
                    for _ in range(warmups):
                        result = await client.call(route, *args)
                        _validate_operation_result(result, validation)

                    before = _read_usage_usec(cpu_file)
                    started = time.perf_counter()
                    latencies: list[float] = []
                    failure = None
                    for index in range(planned):
                        call_start = time.perf_counter_ns()
                        try:
                            result = await client.call(route, *args)
                            _validate_operation_result(result, validation)
                            latencies.append((time.perf_counter_ns() - call_start) / 1_000_000)
                        except BaseException as error:
                            failure = {
                                'call_index': index + 1,
                                'error': f'{type(error).__name__}: {error}',
                            }
                            break
                    wall_seconds = time.perf_counter() - started
                    after = _read_usage_usec(cpu_file)
                    cpu_usec = after - before
                    completed = len(latencies)
                    entry.update(
                        ok=failure is None and completed == planned,
                        completed_calls=completed,
                        latency_ms=latencies,
                        latency_p50_ms=percentile(latencies, 50),
                        latency_p95_ms=percentile(latencies, 95),
                        throughput_calls_per_second=completed / wall_seconds if wall_seconds else None,
                        loaded_wall_seconds=wall_seconds,
                        loaded_cpu_usage_usec=cpu_usec,
                        loaded_cpu_usec_per_completed_call=cpu_usec / completed if completed else None,
                    )
                    if failure:
                        entry['failure'] = failure
                        entry['error'] = failure['error']
                    elif completed != planned:
                        entry['error'] = f'only {completed} of {planned} RPCs completed'
                except BaseException as error:
                    entry['error'] = f'{type(error).__name__}: {error}'
                measurements[name] = entry

            failed = [name for name, entry in measurements.items() if entry['ok'] is not True]
            return {
                'ok': not failed,
                'warmup_calls': warmups,
                'planned_calls': planned,
                'measurements': measurements,
                'failed_operations': failed,
                'error': f'failed RPC operations: {failed}' if failed else None,
            }
        finally:
            await client.close()


def _read_usage_usec(cpu_file: Path) -> int:
    for line in cpu_file.read_text().splitlines():
        key, separator, value = line.partition(' ')
        if separator and key == 'usage_usec':
            return int(value)
    raise RuntimeError(f'cgroup cpu.stat has no usage_usec: {cpu_file}')


async def _client_main() -> int:
    request = json.loads(sys.stdin.read())
    mode = request.get('mode')
    try:
        if os.readlink('/proc/self/ns/net') != request['expected_network_namespace']:
            raise BenchmarkError('namespace client did not enter the owned private network')
        if mode == 'startup':
            response = await _readiness_client(request)
        elif mode == 'measure':
            response = await _measurement_client(request)
        else:
            raise ValueError(f'unknown namespace-client mode {mode!r}')
        print(json.dumps(response, ensure_ascii=False, separators=(',', ':')))
        return 0 if response.get('ok', True) else 2
    except BaseException as error:
        response = {
            'ok': False,
            'stage': locals().get('stage', str(mode)),
            'error': f'{type(error).__name__}: {error}',
            'traceback': ''.join(traceback.format_exception(type(error), error, error.__traceback__))[-10000:],
        }
        print(json.dumps(response, ensure_ascii=False, separators=(',', ':')))
        return 2


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--_client-helper':
        return asyncio.run(_client_main())
    args = parse_args()
    return run_benchmark(args)


_ACTIVE_BASELINE: dict[str, object] = {}


if __name__ == '__main__':
    raise SystemExit(main())
