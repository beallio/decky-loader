from __future__ import annotations

import hashlib
import json
import os
import pwd
import shutil
import stat
import tarfile
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
WRAPPER = REPOSITORY / 'scripts' / 'install-to-deck.sh'
FAKE_INSTALLER = r'''import json
import os
import pwd
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace


def event(value):
    with open(os.environ['FAKE_EVENTS'], 'a', encoding='utf-8') as output:
        output.write(value + '\n')


def state():
    return Path(os.environ['FAKE_STATE'])


def _user_unit_directory(user, *, create=True, value=None):
    return Path(value or os.environ['FAKE_UNIT_DIR'])


def _unit_properties(paths):
    active = state().read_text(encoding='utf-8').strip() == 'active'
    return {
        'LoadState': 'loaded',
        'ActiveState': 'active' if active else 'inactive',
        'SubState': 'running' if active else 'dead',
        'MainPID': '4242' if active else '0',
        'FragmentPath': str(paths['unit']),
        'ControlGroup': '/user.slice/fake.service' if active else '',
    }


def _assert_user_unit_source(paths):
    return None


def _systemctl(paths, *arguments, **options):
    if arguments[0] == 'show' and '--property=WorkingDirectory' in arguments:
        return SimpleNamespace(
            stdout='WorkingDirectory=' + os.environ['FAKE_WORKING_DIRECTORY'] + '\n',
            stderr='', returncode=0,
        )
    if arguments[0] == 'start':
        state().write_text('active', encoding='utf-8')
        event('start')
    return SimpleNamespace(stdout='', stderr='', returncode=0)


def stop_service(service_name, *, scope='user', user=None):
    event('stop')
    if os.environ.get('FAKE_STOP_DIAGNOSTIC') == '1':
        print('signalled remaining owned helper 4243', file=sys.stderr)
        print('owned helpers drained')
    if os.environ.get('FAKE_STOP_FAIL') == '1':
        raise RuntimeError('synthetic service drain failure')
    state().write_text('stopped', encoding='utf-8')


def _wait_running(paths, timeout=30):
    if state().read_text(encoding='utf-8').strip() != 'active':
        raise RuntimeError('service did not start')
    event('wait-running')
    return _unit_properties(paths)


def main():
    command = sys.argv[1]
    if command == 'check-bundle':
        event('check-migrate' if '--migrate-legacy' in sys.argv else 'check')
        print(json.dumps({'ok': True, 'command': command}))
    elif command == 'verify-bundle':
        event('verify')
        print(json.dumps({'ok': True, 'command': command}))
    elif command == 'install':
        if state().read_text(encoding='utf-8').strip() != 'active':
            raise RuntimeError('installer was not given the captured active service state')
        homebrew = Path(sys.argv[sys.argv.index('--homebrew') + 1]).resolve()
        archive_directory = Path(os.environ['FAKE_BACKUP_DIR']).resolve()
        if homebrew in archive_directory.parents or archive_directory == homebrew:
            raise RuntimeError('backup is inside homebrew')
        archives = list(archive_directory.glob('homebrew-*.tar'))
        if not archives:
            raise RuntimeError('backup was not completed before activation')
        with tarfile.open(archives[0], 'r') as archive:
            members = set(archive.getnames())
        if 'homebrew/plugins/Demo/main.py' not in members:
            raise RuntimeError('plugin data was omitted from the archive')
        if 'homebrew/settings/loader.json' not in members:
            raise RuntimeError('settings data was omitted from the archive')
        event('install')
        if os.environ.get('FAKE_INSTALL_FAIL') == '1':
            print('synthetic install transaction failure', file=sys.stderr)
            return 7
        print(json.dumps({'ok': True, 'command': command}))
    elif command == 'rollback':
        event('rollback')
        print(json.dumps({'ok': True, 'command': command}))
    elif command == 'status':
        event('status-recovery')
        print(json.dumps({'ok': True, 'command': command}))
    else:
        raise RuntimeError('unexpected fake installer command: ' + command)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
'''


class InstallToDeckWrapperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name != 'posix' or sys.platform != 'linux' or os.uname().machine != 'x86_64':
            raise unittest.SkipTest('deployment wrapper requires Linux x86-64')
        if os.geteuid() == 0:
            raise unittest.SkipTest('deployment wrapper must be exercised as an ordinary user')
        cls.account = pwd.getpwuid(os.geteuid()).pw_name
        if not cls.account or cls.account == 'root':
            raise unittest.SkipTest('deployment wrapper requires a non-root account')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='decky-wrapper-test-')
        self.root = Path(self.temporary.name)
        self.stage = self.root / 'decky-user-deploy.abcdefgh'
        self.stage.mkdir(mode=0o700)
        shutil.copy2(WRAPPER, self.stage / 'deploy.sh')
        self.homebrew = self.root / 'homebrew'
        (self.homebrew / 'plugins' / 'Demo').mkdir(parents=True)
        (self.homebrew / 'settings').mkdir()
        (self.homebrew / 'services').mkdir()
        (self.homebrew / 'plugins' / 'Demo' / 'main.py').write_text('plugin data\n', encoding='utf-8')
        (self.homebrew / 'settings' / 'loader.json').write_text('{"enabled":true}\n', encoding='utf-8')
        self.backup = self.root / 'custom-backups'
        self.unit_directory = self.root / 'manager-unit-path'
        self.unit_directory.mkdir()
        self.state = self.root / 'service.state'
        self.state.write_text('active', encoding='utf-8')
        self.events = self.root / 'events.log'
        self.sudo_log = self.root / 'sudo.log'
        self.fake_bin = self.root / 'bin'
        self.fake_bin.mkdir()
        sudo = self.fake_bin / 'sudo'
        sudo.write_text('#!/bin/sh\nprintf "called\\n" >> "$FAKE_SUDO_LOG"\nexit 99\n', encoding='utf-8')
        sudo.chmod(0o755)
        self.bundle = self.root / 'runtime.zip'
        self.bundle_hash = self.make_bundle(self.bundle)
        shutil.copy2(self.bundle, self.stage / 'runtime.zip')

    def tearDown(self):
        for path in self.homebrew.rglob('*'):
            if path.is_file() and not path.is_symlink():
                path.chmod(0o600)
        self.temporary.cleanup()

    @staticmethod
    def make_bundle(destination: Path) -> str:
        payload = {
            'install.py': FAKE_INSTALLER.encode('utf-8'),
            'process_observer.py': b'# process observer fixture\n',
            'decky-loader-runtime': b'fake runtime payload\n',
        }
        manifest = {
            'kind': 'decky-loader-runtime',
            'release_version': 'dev',
            'sha256': {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()},
        }
        with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in payload.items():
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, data)
            info = zipfile.ZipInfo('bundle.json')
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, json.dumps(manifest).encode('utf-8'))
        return hashlib.sha256(destination.read_bytes()).hexdigest()

    def environment(self, **extra):
        environment = os.environ.copy()
        environment.update({
            'TMPDIR': str(self.root),
            'DECK_USER': self.account,
            'FAKE_BACKUP_DIR': str(self.backup),
            'FAKE_EVENTS': str(self.events),
            'FAKE_STATE': str(self.state),
            'FAKE_UNIT_DIR': str(self.unit_directory),
            'FAKE_WORKING_DIRECTORY': str(self.homebrew / 'services'),
            'FAKE_SUDO_LOG': str(self.sudo_log),
            'PATH': str(self.fake_bin) + os.pathsep + environment['PATH'],
        })
        environment.update({key: str(value) for key, value in extra.items()})
        return environment

    def run_target(self, *, action='install', migrate='false', backup=None, extra_env=None):
        arguments = [
            'bash', str(self.stage / 'deploy.sh'), '--_target', action,
            str(self.homebrew), self.account, 'plugin_loader', self.bundle_hash, 'dev',
            '127.0.0.1:1337', 'http://127.0.0.1:8080', 'true', migrate, 'true',
            str(backup or self.backup),
        ]
        return subprocess.run(
            arguments, cwd=REPOSITORY, env=self.environment(**(extra_env or {})),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )

    def event_list(self):
        if not self.events.exists():
            return []
        return self.events.read_text(encoding='utf-8').splitlines()

    def test_explicit_backup_dir_archives_plugin_data_before_install_activation(self):
        result = self.run_target()
        self.assertEqual(result.returncode, 0, result.stdout)
        archives = list(self.backup.glob('homebrew-*.tar'))
        self.assertEqual(len(archives), 1)
        self.assertNotEqual(archives[0].parent, self.homebrew)
        events = self.event_list()
        self.assertLess(events.index('stop'), events.index('start'))
        self.assertLess(events.index('start'), events.index('install'))
        self.assertFalse(self.sudo_log.exists(), 'ordinary installation must not invoke sudo')
        self.assertFalse(self.stage.exists(), 'owned staging directory should be removed')
        self.assertFalse(list((self.homebrew / 'services' / '.decky-user-deploy').rglob('failure.*')))

    def test_check_with_migration_is_nonmutating_and_forwards_preflight(self):
        command = [
            'bash', str(WRAPPER), '--check', '--migrate-legacy', '--bundle', str(self.bundle),
            '--sha256', self.bundle_hash, '--version', 'dev', '--homebrew', str(self.homebrew),
            '--backup-dir', str(self.backup),
        ]
        result = subprocess.run(
            command, cwd=REPOSITORY, env=self.environment(), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('check-migrate', self.event_list())
        self.assertNotIn('stop', self.event_list())
        self.assertNotIn('start', self.event_list())
        self.assertFalse(list(self.backup.glob('homebrew-*.tar')))
        self.assertFalse(self.sudo_log.exists(), 'migration preflight must not invoke sudo')

    def test_failed_drain_never_starts_a_duplicate_backend(self):
        result = self.run_target(extra_env={'FAKE_STOP_FAIL': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('stop', self.event_list())
        self.assertNotIn('start', self.event_list())
        self.assertNotIn('install', self.event_list())
        self.assertFalse(list(self.backup.glob('homebrew-*.tar')))

    def failing_archive_environment(self, **extra):
        module_directory = self.root / 'failing-python-modules'
        module_directory.mkdir(exist_ok=True)
        (module_directory / 'tarfile.py').write_text(
            'PAX_FORMAT = 2\n'
            'def open(*args, **kwargs):\n'
            '    raise OSError("synthetic archive write failure")\n',
        )
        return {'PYTHONPATH': str(module_directory), **extra}

    def test_archive_write_failure_restarts_only_the_confirmed_stopped_backend(self):
        result = self.run_target(extra_env=self.failing_archive_environment())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('synthetic archive write failure', result.stdout)
        events = self.event_list()
        self.assertIn('stop', events)
        self.assertIn('start', events)
        self.assertNotIn('install', events)
        self.assertFalse(list(self.backup.glob('homebrew-*.tar')))
        self.assertFalse(list(self.backup.glob('.decky-homebrew-backup.*')))
        self.assertFalse(self.stage.exists(), 'owned staging directory should be removed')

    def test_drain_diagnostics_keep_stop_confirmation_for_restart(self):
        result = self.run_target(extra_env=self.failing_archive_environment(FAKE_STOP_DIAGNOSTIC='1'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('signalled remaining owned helper', result.stdout)
        self.assertIn('synthetic archive write failure', result.stdout)
        events = self.event_list()
        self.assertLess(events.index('stop'), events.index('start'))
        self.assertNotIn('install', events)

    def test_archive_restores_extended_attributes_and_acls(self):
        data = self.homebrew / 'plugins' / 'Demo' / 'main.py'
        # POSIX ACL xattr: version 2; user_obj rw, named uid 54321 r, group_obj r, mask r, other none.
        entries = [(0x01, 6, 0xFFFFFFFF), (0x02, 4, 54321), (0x04, 4, 0xFFFFFFFF), (0x10, 4, 0xFFFFFFFF), (0x20, 0, 0xFFFFFFFF)]
        acl = (2).to_bytes(4, 'little') + b''.join(
            tag.to_bytes(2, 'little') + perm.to_bytes(2, 'little') + ident.to_bytes(4, 'little')
            for tag, perm, ident in entries
        )
        try:
            os.setxattr(data, 'user.decky.test', b'\xffbinary value')
            os.setxattr(data, 'system.posix_acl_access', acl)
        except OSError as error:
            self.skipTest(f'filesystem does not support user xattrs and ACLs: {error}')
        expected_acl = os.getxattr(data, 'system.posix_acl_access')
        result = self.run_target()
        self.assertEqual(result.returncode, 0, result.stdout)
        archive = next(self.backup.glob('homebrew-*.tar'))
        destination = self.root / 'restored'
        destination.mkdir()
        extracted = subprocess.run(
            ['tar', '--xattrs', '--xattrs-include=user.*', '--acls', '-xpf', str(archive), '-C', str(destination)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        self.assertEqual(extracted.returncode, 0, extracted.stdout)
        restored = destination / 'homebrew' / 'plugins' / 'Demo' / 'main.py'
        self.assertEqual(os.getxattr(restored, 'user.decky.test'), b'\xffbinary value')
        self.assertEqual(os.getxattr(restored, 'system.posix_acl_access'), expected_acl)

    def test_failed_install_recovers_from_retained_kit_and_keeps_failure_log(self):
        result = self.run_target(extra_env={'FAKE_INSTALL_FAIL': '1'})
        self.assertNotEqual(result.returncode, 0)
        events = self.event_list()
        self.assertIn('install', events)
        self.assertIn('status-recovery', events)
        logs = list((self.homebrew / 'services' / '.decky-user-deploy').rglob('failure.*'))
        self.assertEqual(len(logs), 1)
        contents = logs[0].read_text(encoding='utf-8')
        self.assertIn('synthetic install transaction failure', contents)
        self.assertIn('"command": "status"', contents)
        self.assertFalse(self.stage.exists(), 'recovery must finish before owned staging is removed')

    def test_backup_inside_homebrew_is_refused_before_stopping_service(self):
        result = self.run_target(backup=self.homebrew / 'inside')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('outside the homebrew directory', result.stdout)
        self.assertNotIn('stop', self.event_list())

    def test_unreadable_user_data_is_not_silently_omitted(self):
        data = self.homebrew / 'plugins' / 'Demo' / 'main.py'
        data.chmod(0)
        try:
            result = self.run_target()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Cannot read homebrew file', result.stdout)
            self.assertNotIn('stop', self.event_list())
        finally:
            data.chmod(0o600)

    def test_user_owned_legacy_control_name_is_preserved_in_archive(self):
        (self.homebrew / 'services' / '.decky-installer').mkdir()
        result = self.run_target()
        self.assertEqual(result.returncode, 0, result.stdout)
        archives = list(self.backup.glob('homebrew-*.tar'))
        self.assertEqual(len(archives), 1)
        with tarfile.open(archives[0], 'r') as archive:
            self.assertIn('homebrew/services/.decky-installer', archive.getnames())


if __name__ == '__main__':
    unittest.main()
