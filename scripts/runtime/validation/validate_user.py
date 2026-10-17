#!/usr/bin/env python3
"""Check a bundle, then exercise a temporary user service without stopping Decky."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
OBSERVER_DIR = HERE if (HERE / 'process_observer.py').is_file() else HERE.parents[2] / 'backend-rust'
sys.path.insert(0, str(OBSERVER_DIR))
from process_observer import cgroup_processes, read_process


def run(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(arguments, text=True, capture_output=True, timeout=180)
    if check and result.returncode:
        raise RuntimeError(f'{arguments[0]} failed: {result.stderr.strip() or result.stdout.strip()}')
    return result


def properties(unit: str, *, user: bool = True) -> dict[str, str]:
    result = run('systemctl', '--user' if user else '--system', 'show', unit,
                 '--property=LoadState,ActiveState,SubState,MainPID,ControlGroup', check=False)
    if result.returncode not in (0, 4):
        raise RuntimeError(f'cannot inspect {unit}: {result.stderr.strip()}')
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def verify_bundle(bundle: Path) -> dict:
    manifest = json.loads((bundle / 'bundle.json').read_text())
    if manifest.get('kind') != 'decky-loader-runtime':
        raise RuntimeError('not a Decky runtime bundle')
    actual = set()
    for path in bundle.rglob('*'):
        if path.is_symlink():
            raise RuntimeError(f'bundle contains a symbolic link: {path}')
        if path.is_file() and path != bundle / 'bundle.json':
            actual.add(path.relative_to(bundle).as_posix())
    if actual != set(manifest['sha256']):
        raise RuntimeError('bundle file inventory differs from its manifest')
    for name, expected in manifest['sha256'].items():
        relative = PurePosixPath(name)
        path = bundle / relative
        if (relative.is_absolute() or '..' in relative.parts or path.is_symlink()
                or not path.is_file() or not path.resolve().is_relative_to(bundle)):
            raise RuntimeError(f'unsafe or missing bundle file: {name}')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f'bundle checksum mismatch: {name}')
    runtime = json.loads(run(str(bundle / 'DeckyPluginWorker'), '--describe').stdout)
    expected = dict(manifest['runtime'], executable=runtime['executable'])
    if runtime != expected or not runtime.get('frozen'):
        raise RuntimeError('packaged worker does not match its manifest')
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--fixtures', type=Path, help='prepared fixture directory beside the bundle by default')
    parser.add_argument('--verify-only', action='store_true', help='check files without starting a service')
    parser.add_argument('--output', type=Path, help='write a JSON report to a new file')
    args = parser.parse_args()
    bundle = args.bundle.resolve(strict=True)
    manifest = verify_bundle(bundle)
    if args.verify_only:
        print(json.dumps({'bundle': 'verified', 'python': manifest['runtime']['python'], 'files': len(manifest['sha256'])}))
        return 0
    if os.name != 'posix' or os.geteuid() == 0:
        parser.error('run as your normal Linux user, without sudo')
    if args.output and args.output.exists():
        parser.error('--output must name a new file')
    account = pwd.getpwuid(os.geteuid())
    run('systemctl', '--user', 'show-environment')
    fixtures = (args.fixtures or bundle.parent / 'fixtures/plugins').resolve(strict=True)
    for name in ('ProbeLegacy', 'ProbeModern'):
        if not (fixtures / name / 'plugin.json').is_file():
            raise RuntimeError(f'prepared fixture is missing: {name}')
    temporary = Path(os.environ.get('TMPDIR') or '/var/tmp/omp-1000')
    temporary.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='decky-user-probe-', dir=temporary))
    homebrew = work / 'homebrew'
    homebrew.mkdir()
    shutil.copytree(fixtures, homebrew / 'plugins')
    root = homebrew / 'plugins/ProbeRoot'
    shutil.copytree(homebrew / 'plugins/ProbeModern', root)
    metadata = json.loads((root / 'plugin.json').read_text())
    metadata.update(name='Rust Probe Root', flags=['root'])
    (root / 'plugin.json').write_text(json.dumps(metadata))
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    unit = f'decky-user-probe-{uuid.uuid4().hex}.service'
    if properties(unit).get('LoadState') not in (None, 'not-found'):
        raise RuntimeError('temporary unit already exists')
    normal = {scope: properties('plugin_loader.service', user=scope == 'user') for scope in ('user', 'system')}
    report = {'unit': unit, 'uid': account.pw_uid, 'work': str(work), 'phases': [], 'ok': False}
    epochs: dict[int, int] = {}
    installed = False

    def install_command(command: str, *extra: str) -> dict:
        result = run(sys.executable, '-B', str(bundle / 'install.py'), command,
                     '--homebrew', str(homebrew), '--service-name', unit, *extra)
        return json.loads(result.stdout)

    def observe() -> dict[str, str]:
        state = properties(unit)
        if state.get('ActiveState') != 'active' or state.get('SubState') != 'running':
            raise RuntimeError(f'temporary user service is not running: {state}')
        records = cgroup_processes(state['ControlGroup'])
        main_pid = int(state['MainPID'])
        if main_pid not in records:
            raise RuntimeError('service main process is absent from its cgroup')
        for pid, record in records.items():
            status = Path(f'/proc/{pid}/status').read_text()
            uid = next(line for line in status.splitlines() if line.startswith('Uid:')).split()[1:]
            if any(int(value) != account.pw_uid for value in uid):
                raise RuntimeError(f'owned process {pid} has unexpected credentials: {uid}')
            epochs[pid] = record.start_ticks
        return state

    def assert_drained(previous: dict[int, int]) -> None:
        surviving = [pid for pid, epoch in previous.items()
                     if (record := read_process(pid)) is not None and record.start_ticks == epoch]
        if surviving:
            raise RuntimeError(f'previous service processes remain: {surviving}')

    def exercise(mode: str) -> None:
        state = observe()
        result = run(sys.executable, '-B', str(HERE / 'verify.py'), '--base', f'http://127.0.0.1:{port}',
                     '--expected-version', manifest['release_version'], '--expected-user', account.pw_name,
                     '--expect-root-rejection')
        proof = json.loads(result.stdout)
        identities = proof['worker_processes']
        parents = {item['ppid'] for item in identities.values()}
        if mode == 'fork' and (len(parents) != 1 or int(state['MainPID']) in parents):
            raise RuntimeError('fork workers do not have a separate shared parent')
        if mode == 'exec' and parents != {int(state['MainPID'])}:
            raise RuntimeError('exec workers are not independently supervised by the backend')
        observe()
        report['phases'].append({'mode': mode, 'verification': proof})

    try:
        for mode in ('fork', 'exec'):
            previous = dict(epochs)
            install_command('install', '--bundle', str(bundle), '--user', account.pw_name,
                            '--listen', f'127.0.0.1:{port}', '--inject-frontend', 'false',
                            '--worker-mode', mode, '--version', manifest['release_version'])
            installed = True
            assert_drained(previous)
            exercise(mode)
        previous = dict(epochs)
        install_command('rollback')
        assert_drained(previous)
        exercise('fork')
        previous = dict(epochs)
        install_command('rollback')
        assert_drained(previous)
        installed = False
        if properties(unit).get('ActiveState') not in (None, 'inactive', 'failed'):
            raise RuntimeError('fresh-install rollback left the temporary service active')
        report['ok'] = True
    except BaseException as error:
        report['error'] = str(error)
    finally:
        cleanup_error = None
        try:
            if installed:
                for _ in range(3):
                    status = install_command('status')
                    if not status['rollback_available']:
                        break
                    install_command('rollback')
            spec = importlib.util.spec_from_file_location('probe_installer', bundle / 'install.py')
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            module.stop_service(unit)
            assert_drained(epochs)
            remaining = properties(unit)
            if remaining.get('LoadState') not in (None, 'not-found'):
                raise RuntimeError(f'temporary unit was not removed: {remaining}')
        except BaseException as error:
            cleanup_error = str(error)
            report.update(ok=False, cleanup_error=cleanup_error)
        report['normal_unchanged'] = normal == {
            scope: properties('plugin_loader.service', user=scope == 'user') for scope in ('user', 'system')
        }
        if not report['normal_unchanged']:
            report.update(ok=False, normal_service_error='normal service state changed during validation')
        if cleanup_error is None:
            shutil.rmtree(work)
            report['work_removed'] = True
        else:
            report['work_removed'] = False
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
        if args.output:
            with args.output.open('x') as stream:
                stream.write(encoded)
        print(encoded, end='')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
