#!/usr/bin/env python3
"""Run the compatibility probe without replacing the installed Decky loader."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import sys
import signal
import shutil
import subprocess
import tempfile
import time
import uuid
import urllib.request

_VALIDATION_DIR = Path(__file__).resolve().parent
_OBSERVER_DIR = (_VALIDATION_DIR if (_VALIDATION_DIR / 'process_observer.py').is_file()
                 else _VALIDATION_DIR.parents[2] / 'backend-rust')
if not sys.path or sys.path[0] != str(_OBSERVER_DIR):
    sys.path.insert(0, str(_OBSERVER_DIR))
from process_observer import cgroup_processes

BASE = Path('/home/deck/.local/share/decky-loader-runtime')
BUNDLE = BASE / 'bundle'
NORMAL = Path('/home/deck/homebrew')
SERVICE = 'plugin_loader.service'
CGROUP_ROOT = Path('/sys/fs/cgroup')


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def command(*args: str, timeout: float = 90) -> str:
    return subprocess.check_output(args, text=True, timeout=timeout).strip()


def verify_bundle() -> dict:
    manifest = json.loads((BUNDLE / 'bundle.json').read_text())
    if manifest.get('kind') != 'decky-loader-runtime':
        raise RuntimeError('not a Decky runtime candidate bundle')
    for relative, expected in manifest['sha256'].items():
        path = (BUNDLE / relative).resolve()
        if not path.is_relative_to(BUNDLE.resolve()) or not path.is_file():
            raise RuntimeError(f'unsafe or missing bundle member: {relative}')
        if digest(path) != expected:
            raise RuntimeError(f'bundle checksum mismatch: {relative}')
    runtime = json.loads(command(str(BUNDLE / 'DeckyPluginWorker'), '--describe'))
    if runtime != manifest['runtime']:
        # The deployed executable pathname is installation-specific.
        expected = dict(manifest['runtime'])
        expected['executable'] = runtime['executable']
        if runtime != expected:
            raise RuntimeError('worker runtime does not match its bundle manifest')
    if not runtime['python'].startswith('3.11.') or not runtime['frozen']:
        raise RuntimeError('the probe requires a packaged CPython 3.11 worker')
    if runtime['decky_version'] != 'v3.2.10' or manifest.get('source_baseline', {}).get('tag') != runtime['decky_version']:
        raise RuntimeError('the worker and bundle must match the v3.2.10 source baseline')
    return manifest


def identity(pid: int) -> tuple[int, str] | None:
    try:
        tail = (Path('/proc') / str(pid) / 'stat').read_text().rpartition(')')[2].split()
        return int(tail[19]), tail[0]
    except (OSError, ValueError, IndexError):
        return None


def owned_processes() -> dict[int, int]:
    group = command('systemctl', 'show', SERVICE, '--property=ControlGroup', '--value')
    if not group:
        raise RuntimeError('the active Decky service has no control group')
    return {pid: process.start_ticks for pid, process in cgroup_processes(group).items()}


def stop_remaining(processes: dict[int, int]) -> None:
    def remains(pid: int, ticks: int) -> bool:
        state = identity(pid)
        return state is not None and state[0] == ticks and state[1] != 'Z'
    for pid, ticks in processes.items():
        if remains(pid, ticks):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(remains(pid, ticks) for pid, ticks in processes.items()):
        time.sleep(0.05)
    for pid, ticks in processes.items():
        if remains(pid, ticks):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def unit_properties(unit: str) -> dict[str, str]:
    names = (
        'LoadState', 'Description', 'ControlGroup', 'ActiveState', 'SubState',
        'MainPID', 'ExecMainPID', 'ExecMainCode', 'ExecMainStatus',
        'ExecMainStartTimestampMonotonic',
    )
    output = subprocess.check_output(
        ['systemctl', '--system', 'show', unit, *[f'--property={name}' for name in names]],
        text=True,
        timeout=10,
    )
    return dict(line.partition('=')[::2] for line in output.splitlines() if '=' in line)


def cgroup_members(control_group: str | None) -> list[int]:
    if not control_group:
        return []
    relative = Path(control_group.lstrip('/'))
    if not control_group.startswith('/system.slice/') or '..' in relative.parts:
        raise RuntimeError(f'probe unit has an unexpected control group: {control_group}')
    return list(cgroup_processes(control_group))


def rust_exitcode(properties: dict[str, str], started: bool) -> int | None:
    if not started:
        return None
    try:
        code = properties['ExecMainCode']
        status = int(properties['ExecMainStatus'])
    except (KeyError, ValueError):
        return None
    if code in ('1', 'CLD_EXITED', 'exited'):
        return status
    if code in ('2', '3', 'CLD_KILLED', 'CLD_DUMPED', 'killed', 'dumped'):
        return -status
    return None


def finish_containment(
    unit: str,
    description: str,
    control_group: str | None,
    launch_attempted: bool,
    launch_succeeded: bool,
    proof: dict,
) -> int | None:
    if not launch_attempted:
        proof.update(unit_stopped=True, drained=True, members_after_stop=[])
        return None

    properties = unit_properties(unit)
    load_state = properties.get('LoadState')
    if load_state in (None, 'not-found'):
        members = cgroup_members(control_group)
        proof.update(cgroup=control_group, unit_stopped=True, drained=not members, members_after_stop=members)
        if members:
            raise RuntimeError(f'probe cgroup {control_group} still contains processes after its unit disappeared')
        return None
    if properties.get('Description') != description:
        if launch_succeeded:
            raise RuntimeError(f'transient unit {unit} no longer has its probe ownership description')
        proof.update(unit_stopped=True, drained=True, members_after_stop=[])
        return None

    control_group = properties.get('ControlGroup') or control_group
    proof['cgroup'] = control_group
    probe_pid = int(properties.get('ExecMainPID') or properties.get('MainPID') or 0)
    started = launch_succeeded or probe_pid > 0 or int(properties.get('ExecMainStartTimestampMonotonic') or 0) > 0
    proof['started'] = started
    proof['probe_pid'] = probe_pid or None

    def inspect():
        current = unit_properties(unit)
        if current.get('LoadState') == 'not-found':
            group = proof.get('cgroup') or control_group
            members = cgroup_members(group)
            return current, group, members, True
        if current.get('Description') != description:
            raise RuntimeError(f'transient unit {unit} no longer has its probe ownership description')
        group = current.get('ControlGroup') or proof.get('cgroup') or control_group
        members = cgroup_members(group)
        stopped = current.get('ActiveState') in ('inactive', 'failed')
        return current, group, members, stopped

    try:
        command('systemctl', '--system', 'stop', unit, timeout=25)
    except (OSError, subprocess.SubprocessError):
        pass

    properties, control_group, members, stopped = inspect()
    forced_kill = False
    if not stopped or members:
        forced_kill = True
        try:
            command(
                'systemctl', '--system', 'kill', '--kill-whom=all', '--signal=SIGKILL', unit,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            command('systemctl', '--system', 'stop', unit, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
        deadline = time.monotonic() + 15
        while True:
            properties, control_group, members, stopped = inspect()
            if stopped and not members:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)

    proof.update(
        cgroup=control_group,
        unit_stopped=stopped,
        drained=stopped and not members,
        members_after_stop=members,
        forced_kill=forced_kill,
    )
    if not proof['drained']:
        raise RuntimeError(
            f'probe containment {unit} did not drain: state={properties.get("ActiveState")}, members={members}'
        )
    return rust_exitcode(properties, started)


def validate_fixture_tree(path: Path) -> list[Path]:
    if not path.is_dir():
        raise RuntimeError(f'probe fixtures are missing: {path}')
    fixtures = sorted(child for child in path.iterdir() if child.is_dir())
    names = {fixture.name for fixture in fixtures}
    required = {'ProbeModern', 'ProbeLegacy'}
    if not required.issubset(names):
        raise RuntimeError(f'probe fixtures are incomplete: missing {sorted(required - names)}')
    for fixture in fixtures:
        metadata_path = fixture / 'plugin.json'
        if not metadata_path.is_file() or not isinstance(json.loads(metadata_path.read_text()), dict):
            raise RuntimeError(f'probe fixture has invalid plugin metadata: {fixture.name}')
        if not (fixture / 'dist/index.js').is_file():
            raise RuntimeError(f'probe fixture is missing its frontend entrypoint: {fixture.name}')
    return fixtures


def verify_staged_bundle(staged: Path, manifest: dict) -> None:
    if json.loads((staged / 'bundle.json').read_text()) != manifest:
        raise RuntimeError('staged bundle manifest changed during copying')
    for relative, expected in manifest['sha256'].items():
        path = (staged / relative).resolve()
        if not path.is_relative_to(staged.resolve()) or not path.is_file() or digest(path) != expected:
            raise RuntimeError(f'staged bundle checksum mismatch: {relative}')
    runtime = json.loads(command(str(staged / 'DeckyPluginWorker'), '--describe'))
    if runtime != manifest['runtime']:
        expected = dict(manifest['runtime'])
        expected['executable'] = runtime['executable']
        if runtime != expected:
            raise RuntimeError('staged worker runtime does not match its bundle manifest')
    if not runtime['python'].startswith('3.11.') or not runtime['frozen']:
        raise RuntimeError('the probe requires a packaged CPython 3.11 worker')


def user_owned_tree(path: Path, uid: int, gid: int) -> None:
    for parent, directories, files in os.walk(path, followlinks=False):
        os.chown(parent, uid, gid)
        for name in directories + files:
            os.chown(Path(parent) / name, uid, gid, follow_symlinks=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    manifest = verify_bundle()
    if args.verify_only:
        print(json.dumps({'bundle': 'verified', 'python': manifest['runtime']['python'], 'files': len(manifest['sha256'])}))
        return 0
    if os.geteuid() != 0:
        parser.error('run this validator with sudo; no sudo-policy change is required')
    if os.environ.get('SUDO_USER') != 'deck':
        parser.error('run through the deck account to preserve its normal plugin identity')
    if not (CGROUP_ROOT / 'cgroup.controllers').is_file():
        raise RuntimeError('the root validator requires the systemd cgroup v2 hierarchy')
    if command('systemctl', 'is-active', SERVICE) != 'active':
        raise RuntimeError('normal Decky must be active before validation')

    account = pwd.getpwnam('deck')
    baseline = {
        'loader_sha256': digest(NORMAL / 'services/PluginLoader'),
        'settings_sha256': digest(NORMAL / 'settings/loader.json'),
    }
    previous = owned_processes()
    token = uuid.uuid4().hex
    unit = f'decky-rust-probe-{token}.service'
    description = f'Decky Rust Runtime Probe {token}'
    existing = unit_properties(unit)
    if existing.get('LoadState') not in (None, 'not-found'):
        raise RuntimeError(f'unique transient probe unit already exists: {unit}')

    runtime_root = None
    staged = None
    homebrew = None
    ready = BASE / 'root-ready.json'
    finish = BASE / 'control/finish'
    control_group = None
    launch_attempted = False
    launch_succeeded = False
    paused = False
    report = {
        'baseline': baseline,
        'runtime': manifest['runtime'],
        'restored': False,
        'loader_unchanged': False,
        'settings_unchanged': False,
        'probe_pid': None,
        'probe_exitcode': None,
        'containment': {
            'unit': unit,
            'cgroup': None,
            'started': False,
            'unit_stopped': False,
            'drained': False,
            'members_after_stop': None,
            'forced_kill': False,
        },
    }
    run_error = None
    try:
        runtime_root = Path(tempfile.mkdtemp(prefix='dv-', dir=os.environ.get('TMPDIR', '/var/tmp/omp-1000')))
        runtime_root.chmod(0o755)
        staged = runtime_root / 'bundle'
        shutil.copytree(BUNDLE, staged)
        verify_staged_bundle(staged, manifest)

        homebrew = runtime_root / 'homebrew'
        homebrew.mkdir()
        for name in ('plugins', 'settings', 'data', 'themes'):
            source = NORMAL / name
            destination = homebrew / name
            if source.exists():
                shutil.copytree(source, destination, symlinks=True)
            else:
                destination.mkdir()
            if name != 'plugins':
                user_owned_tree(destination, account.pw_uid, account.pw_gid)

        fixture_source = BASE / 'fixtures/plugins'
        validate_fixture_tree(fixture_source)
        fixture_staging = homebrew / 'plugins-fixtures'
        shutil.copytree(fixture_source, fixture_staging)
        fixtures = validate_fixture_tree(fixture_staging)
        for fixture in fixtures:
            destination = homebrew / 'plugins' / fixture.name
            if destination.exists():
                raise RuntimeError(f'probe fixture conflicts with an installed plugin: {fixture.name}')
        for fixture in fixtures:
            shutil.move(str(fixture), homebrew / 'plugins' / fixture.name)
        fixture_staging.rmdir()

        root_fixture = homebrew / 'plugins/ProbeRoot'
        if root_fixture.exists():
            raise RuntimeError('probe root fixture conflicts with an installed plugin')
        shutil.copytree(homebrew / 'plugins/ProbeModern', root_fixture)
        root_metadata = json.loads((root_fixture / 'plugin.json').read_text())
        root_metadata.update(name='Rust Probe Root', flags=['root'])
        (root_fixture / 'plugin.json').write_text(json.dumps(root_metadata))
        root_entrypoint = root_fixture / 'dist/index.js'
        root_entrypoint.write_text(root_entrypoint.read_text().replace('Rust Probe Modern', 'Rust Probe Root'))
        os.chown(homebrew, account.pw_uid, account.pw_gid)
        (homebrew / 'logs').mkdir()
        os.chown(homebrew / 'logs', account.pw_uid, account.pw_gid)

        control = BASE / 'control'
        control.mkdir(exist_ok=True)
        os.chown(control, account.pw_uid, account.pw_gid)
        finish.unlink(missing_ok=True)
        ready.unlink(missing_ok=True)
        log_path = BASE / 'root-probe.log'
        log_path.write_text('')
        log_path.chmod(0o644)

        paused = True
        command('systemctl', 'stop', SERVICE)
        stop_remaining(previous)

        launch_attempted = True
        launcher = [
            'systemd-run', '--system', '--quiet', '--unit', unit,
            '--description', description,
            '--slice=system.slice',
            '--service-type=exec',
            '--remain-after-exit',
            '--property=KillMode=control-group',
            '--property=SendSIGKILL=yes',
            '--property=TimeoutStartSec=30s',
            '--property=TimeoutStopSec=15s',
            '--property=RuntimeMaxSec=1920s',
            f'--property=WorkingDirectory={os.getcwd()}',
            f'--property=StandardOutput=append:{log_path}',
            f'--property=StandardError=append:{log_path}',
        ]
        for name, value in os.environ.items():
            if name != 'TMPDIR':
                launcher.append(f'--setenv={name}={value}')
        launcher.append(f'--setenv=TMPDIR={runtime_root}')
        launcher.extend([
            '--',
            str(staged / 'decky-loader-runtime'),
            '--listen', '127.0.0.1:1337',
            '--release-version', manifest['release_version'],
            '--worker', str(staged / 'DeckyPluginWorker'),
            '--plugin-root', str(homebrew / 'plugins'),
            '--homebrew', str(homebrew),
            '--user', 'deck',
            '--assets', str(staged / 'assets'),
            '--locales', str(staged / 'locales'),
        ])
        command(*launcher, timeout=45)
        launch_succeeded = True

        deadline = time.monotonic() + 120
        while True:
            properties = unit_properties(unit)
            if properties.get('Description') != description:
                raise RuntimeError(f'transient probe unit {unit} failed to start')
            control_group = properties.get('ControlGroup') or control_group
            pid = int(properties.get('MainPID') or 0)
            if pid > 0:
                report['probe_pid'] = pid
            elif int(properties.get('ExecMainStartTimestampMonotonic') or 0) > 0:
                exitcode = rust_exitcode(properties, True)
                report['probe_exitcode'] = exitcode
                raise RuntimeError(f'Rust probe exited with {exitcode}; see root-probe.log')
            elif properties.get('ActiveState') == 'failed':
                raise RuntimeError(f'Rust probe failed to start; see root-probe.log')

            if pid > 0:
                try:
                    with urllib.request.urlopen('http://127.0.0.1:1337/auth/token', timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise TimeoutError('Rust service did not become ready')
            time.sleep(0.1)

        report['containment']['started'] = True
        report['containment']['cgroup'] = control_group
        ready.write_text(json.dumps({
            'pid': report['probe_pid'],
            'homebrew': str(homebrew),
            'bundle': str(staged),
            'listen': '127.0.0.1:1337',
        }))
        ready.chmod(0o644)
        print('Rust validation service is ready on port 1337. Keep this SSH session open.', flush=True)
        print('The normal service will be restored when validation finishes, on Ctrl-C, or after the safety deadline.', flush=True)
        deadline = time.monotonic() + 1800
        while not finish.exists() and time.monotonic() < deadline:
            properties = unit_properties(unit)
            control_group = properties.get('ControlGroup') or control_group
            if int(properties.get('MainPID') or 0) == 0 and int(properties.get('ExecMainStartTimestampMonotonic') or 0) > 0:
                exitcode = rust_exitcode(properties, True)
                report['probe_exitcode'] = exitcode
                raise RuntimeError(f'Rust probe exited with {exitcode}')
            time.sleep(0.2)
    except BaseException as error:
        run_error = error
        raise
    finally:
        cleanup_errors = []
        containment_drained = False
        try:
            report['probe_exitcode'] = finish_containment(
                unit,
                description,
                control_group,
                launch_attempted,
                launch_succeeded,
                report['containment'],
            )
            containment_drained = bool(report['containment']['drained'])
        except BaseException as error:
            cleanup_errors.append(error)

        if containment_drained:
            if paused:
                try:
                    command('systemctl', 'start', SERVICE, timeout=30)
                except BaseException as error:
                    cleanup_errors.append(error)
            try:
                report['restored'] = command('systemctl', 'is-active', SERVICE) == 'active'
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                report['loader_unchanged'] = digest(NORMAL / 'services/PluginLoader') == baseline['loader_sha256']
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                report['settings_unchanged'] = digest(NORMAL / 'settings/loader.json') == baseline['settings_sha256']
            except BaseException as error:
                cleanup_errors.append(error)
            if homebrew is not None and (homebrew / 'data').exists():
                try:
                    for fixture in ('ProbeModern', 'ProbeLegacy', 'ProbeRoot'):
                        lifecycle = homebrew / 'data' / fixture / 'lifecycle.json'
                        if lifecycle.exists():
                            report.setdefault('fixture_lifecycle', {})[fixture] = json.loads(lifecycle.read_text())
                except BaseException as error:
                    cleanup_errors.append(error)

        try:
            output = BASE / 'root-report.json'
            output.write_text(json.dumps(report, indent=2))
            output.chmod(0o644)
            print(json.dumps(report), flush=True)
        except BaseException as error:
            cleanup_errors.append(error)
        finally:
            try:
                ready.unlink(missing_ok=True)
            except BaseException as error:
                cleanup_errors.append(error)
            if runtime_root is not None and containment_drained:
                try:
                    shutil.rmtree(runtime_root)
                except BaseException as error:
                    cleanup_errors.append(error)

        if cleanup_errors:
            details = '; '.join(str(error) for error in cleanup_errors)
            if run_error is None:
                raise RuntimeError(f'probe cleanup failed: {details}') from cleanup_errors[0]
            print(f'Probe cleanup also failed: {details}', file=sys.stderr, flush=True)

    if not report['restored'] or not report['loader_unchanged'] or not report['settings_unchanged']:
        raise RuntimeError('normal service restoration did not match the recorded baseline')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())




