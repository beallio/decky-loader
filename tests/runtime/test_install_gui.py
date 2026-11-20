from __future__ import annotations

import configparser
import json
import os
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
INSTALLER_WRAPPER = REPOSITORY / 'scripts' / 'install-to-deck.sh'
GUI_SCRIPT = REPOSITORY / 'scripts' / 'install-gui.sh'
DESKTOP_FILE = REPOSITORY / 'scripts' / 'Install-Decky-Rust.desktop'


class InstallGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name != 'posix' or sys.platform != 'linux':
            raise unittest.SkipTest('GUI installer tests require Linux')
        cls.account = pwd.getpwuid(os.geteuid()).pw_name

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='decky-gui-test-')
        self.root = Path(self.temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.homebrew = self.home / 'homebrew'
        self.homebrew.mkdir()
        self.system_units = self.root / 'etc_systemd_system'
        self.system_units.mkdir()
        self.legacy_root = self.root / 'legacy_migrations'
        self.legacy_root.mkdir()
        self.fake_bin = self.root / 'bin'
        self.fake_bin.mkdir()
        self.log_dir = self.root / 'logs'
        self.log_dir.mkdir()
        self.installer_calls = self.log_dir / 'installer_calls.jsonl'
        self.sudo_calls = self.log_dir / 'sudo_calls.log'
        self.askpass_dir_log = self.log_dir / 'askpass_path.txt'

        # Stub install-to-deck.sh so GUI tests never download or touch system services.
        self.stub_installer = self.root / 'stub-install-to-deck.sh'
        self.stub_installer.write_text(
            '#!/usr/bin/env python3\n'
            'import json, os, sys\n'
            'with open(os.environ["TEST_INSTALLER_CALLS"], "a", encoding="utf-8") as fh:\n'
            '    fh.write(json.dumps({\n'
            '        "argv": sys.argv[1:],\n'
            '        "sudo_askpass": os.environ.get("SUDO_ASKPASS", ""),\n'
            '        "askpass_exists": os.path.exists(os.environ["SUDO_ASKPASS"]) if os.environ.get("SUDO_ASKPASS") else False,\n'
            '    }) + "\\n")\n'
            'if os.environ.get("TEST_INSTALLER_FAIL") == "1":\n'
            '    print("Error: synthetic installer failure", file=sys.stderr)\n'
            '    raise SystemExit(2)\n'
            'print("Synthetic installer complete.")\n',
            encoding='utf-8',
        )
        self.stub_installer.chmod(0o755)

        # Stub sudo: accepts password "secret" on -S -v, records SUDO_ASKPASS path when -A.
        self.stub_sudo = self.fake_bin / 'sudo'
        self.stub_sudo.write_text(
            '#!/usr/bin/env python3\n'
            'import os, sys\n'
            'args = sys.argv[1:]\n'
            'with open(os.environ["TEST_SUDO_CALLS"], "a", encoding="utf-8") as fh:\n'
            '    fh.write(" ".join(args) + "\\n")\n'
            'if "-S" in args:\n'
            '    pw = sys.stdin.read().rstrip("\\r\\n")\n'
            '    raise SystemExit(0 if pw == "secret" else 1)\n'
            'if "-A" in args:\n'
            '    ap = os.environ.get("SUDO_ASKPASS", "")\n'
            '    with open(os.environ["TEST_ASKPASS_LOG"], "w", encoding="utf-8") as fh:\n'
            '        fh.write(ap)\n'
            '    raise SystemExit(0 if ap and os.path.isfile(ap) else 1)\n'
            'raise SystemExit(0)\n',
            encoding='utf-8',
        )
        self.stub_sudo.chmod(0o755)

        # Stub zenity and kdialog controlled by environment variables.
        self.stub_zenity = self.fake_bin / 'zenity'
        self.stub_zenity.write_text(
            '#!/usr/bin/env python3\n'
            'import os, sys\n'
            'args = sys.argv[1:]\n'
            'with open(os.environ["TEST_GUI_LOG"], "a", encoding="utf-8") as fh:\n'
            '    fh.write("zenity " + " ".join(args) + "\\n")\n'
            'if "--list" in args:\n'
            '    choice = os.environ.get("TEST_GUI_CHOICE", "")\n'
            '    if choice == "__CANCEL__":\n'
            '        raise SystemExit(1)\n'
            '    print(choice)\n'
            '    raise SystemExit(0)\n'
            'if "--password" in args:\n'
            '    pw = os.environ.get("TEST_GUI_PASSWORD", "secret")\n'
            '    if pw == "__CANCEL__":\n'
            '        raise SystemExit(1)\n'
            '    print(pw)\n'
            '    raise SystemExit(0)\n'
            'if "--question" in args:\n'
            '    raise SystemExit(0 if os.environ.get("TEST_GUI_CONFIRM", "1") == "1" else 1)\n'
            'if "--progress" in args:\n'
            '    sys.stdin.read()\n'
            '    raise SystemExit(0)\n'
            'raise SystemExit(0)\n',
            encoding='utf-8',
        )
        self.stub_zenity.chmod(0o755)

        self.stub_kdialog = self.fake_bin / 'kdialog'
        self.stub_kdialog.write_text(
            '#!/usr/bin/env python3\n'
            'import os, sys\n'
            'args = sys.argv[1:]\n'
            'with open(os.environ["TEST_GUI_LOG"], "a", encoding="utf-8") as fh:\n'
            '    fh.write("kdialog " + " ".join(args) + "\\n")\n'
            'if "--radiolist" in args:\n'
            '    choice = os.environ.get("TEST_GUI_CHOICE", "")\n'
            '    if choice == "__CANCEL__":\n'
            '        raise SystemExit(1)\n'
            '    print(choice)\n'
            '    raise SystemExit(0)\n'
            'if "--password" in args:\n'
            '    pw = os.environ.get("TEST_GUI_PASSWORD", "secret")\n'
            '    if pw == "__CANCEL__":\n'
            '        raise SystemExit(1)\n'
            '    print(pw)\n'
            '    raise SystemExit(0)\n'
            'if "--yesno" in args or "--warningyesno" in args:\n'
            '    raise SystemExit(0 if os.environ.get("TEST_GUI_CONFIRM", "1") == "1" else 1)\n'
            'if "--progressbar" in args:\n'
            '    print("org.kde.kdialog-test /ProgressDialog")\n'
            '    raise SystemExit(0)\n'
            'raise SystemExit(0)\n',
            encoding='utf-8',
        )
        self.stub_kdialog.chmod(0o755)

        self.stub_qdbus = self.fake_bin / 'qdbus'
        self.stub_qdbus.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        self.stub_qdbus.chmod(0o755)

    def tearDown(self):
        self.temporary.cleanup()

    def base_env(self, **extra: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update({
            'HOME': str(self.home),
            'USER': self.account,
            'DECK_USER': self.account,
            'DECKY_HOMEBREW_DIR': str(self.homebrew),
            'DECKY_INSTALLER_SCRIPT': str(self.stub_installer),
            'DECKY_SYSTEM_UNIT_DIRS': str(self.system_units),
            'DECKY_LEGACY_MIGRATION_ROOT': str(self.legacy_root),
            'TMPDIR': str(self.root),
            'TEST_INSTALLER_CALLS': str(self.installer_calls),
            'TEST_SUDO_CALLS': str(self.sudo_calls),
            'TEST_ASKPASS_LOG': str(self.askpass_dir_log),
            'TEST_GUI_LOG': str(self.log_dir / 'gui_calls.log'),
            'PATH': str(self.fake_bin) + os.pathsep + env['PATH'],
        })
        env.pop('SUDO_ASKPASS', None)
        env.update(extra)
        return env

    def create_fork_install(self, version: str = 'v3.3.1-rust.1-rc.9', history_count: int = 2, *, root_enabled: bool = False):
        state_dir = self.homebrew / 'services' / '.decky-user-installer' / 'plugin_loader.service'
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / 'active.json').write_text(json.dumps({'version': version}), encoding='utf-8')
        (state_dir / 'head.json').write_text(json.dumps({'id': 'id0'}), encoding='utf-8')
        for idx in range(history_count):
            (state_dir / 'history' / f'id{idx}').mkdir(parents=True, exist_ok=True)
        if root_enabled:
            (self.system_units / f'decky-root-plugins-{self.account}.socket').write_text('[Socket]\n', encoding='utf-8')

    def create_official_unit(self, *, masked: bool = False):
        unit = self.system_units / 'plugin_loader.service'
        if masked:
            unit.symlink_to('/dev/null')
        else:
            unit.write_text('[Unit]\nDescription=Official Decky\n', encoding='utf-8')

    def create_legacy_backup(self, *, forgotten: bool = False):
        uid = str(os.geteuid())
        rec_dir = self.legacy_root / uid / 'plugin_loader.service'
        rec_dir.mkdir(parents=True, exist_ok=True)
        phase = 'forgotten' if forgotten else 'services-user-owned'
        (rec_dir / 'status.json').write_text(json.dumps({'phase': phase, 'schema': 1}), encoding='utf-8')

    def test_install_to_deck_uses_sudo_askpass_when_exported(self):
        stage = self.root / 'decky-user-deploy.abcdefgh'
        stage.mkdir(mode=0o700)
        shutil.copy2(INSTALLER_WRAPPER, stage / 'deploy.sh')
        kit = self.homebrew / 'services' / '.decky-user-deploy' / 'plugin_loader.service'
        kit.mkdir(parents=True)
        (kit / 'install.py').write_text('import sys\nprint("disabled")\n', encoding='utf-8')
        systemctl = self.fake_bin / 'systemctl'
        systemctl.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        systemctl.chmod(0o755)
        askpass = self.root / 'askpass.sh'
        askpass.write_text('#!/bin/sh\necho secret\n', encoding='utf-8')
        askpass.chmod(0o700)

        common_args = [
            'bash', str(stage / 'deploy.sh'), '--_target', 'disable-root-plugins',
            str(self.homebrew), self.account, 'plugin_loader', '', '',
            '127.0.0.1:1337', 'http://127.0.0.1:8080', 'true', 'false', 'true', '',
        ]
        res_ask = subprocess.run(
            common_args, cwd=REPOSITORY,
            env=self.base_env(SUDO_ASKPASS=str(askpass)),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        self.assertEqual(res_ask.returncode, 0, res_ask.stdout)
        calls = self.sudo_calls.read_text(encoding='utf-8').splitlines()
        self.assertTrue(any(line.startswith('-A ') for line in calls), f'Expected sudo -A, got: {calls}')

        self.sudo_calls.unlink()
        stage.mkdir(mode=0o700)
        shutil.copy2(INSTALLER_WRAPPER, stage / 'deploy.sh')
        res_plain = subprocess.run(
            common_args, cwd=REPOSITORY, env=self.base_env(),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        self.assertEqual(res_plain.returncode, 0, res_plain.stdout)
        calls_plain = self.sudo_calls.read_text(encoding='utf-8').splitlines()
        self.assertTrue(any(line.startswith('-n ') for line in calls_plain), f'Expected sudo -n without SUDO_ASKPASS, got: {calls_plain}')

    def test_print_state_detects_all_installation_states(self):
        # 1. Fresh system
        res = subprocess.run(
            ['bash', str(GUI_SCRIPT), '--print-state'],
            env=self.base_env(), text=True, capture_output=True, check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        st = json.loads(res.stdout)
        self.assertEqual(st['installed'], False)
        self.assertEqual(st['official_installed'], False)
        self.assertEqual(st['default_action'], 'install')

        # 2. Official Decky installed (not masked)
        self.create_official_unit(masked=False)
        res = subprocess.run(
            ['bash', str(GUI_SCRIPT), '--print-state'],
            env=self.base_env(), text=True, capture_output=True, check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        st = json.loads(res.stdout)
        self.assertEqual(st['official_installed'], True)
        self.assertEqual(st['default_action'], 'migrate')

        # 3. Official masked (/dev/null) + Fork installed with rollback history
        (self.system_units / 'plugin_loader.service').unlink()
        self.create_official_unit(masked=True)
        self.create_fork_install('v3.3.1-rust.1-rc.9', history_count=3, root_enabled=False)
        res = subprocess.run(
            ['bash', str(GUI_SCRIPT), '--print-state'],
            env=self.base_env(), text=True, capture_output=True, check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        st = json.loads(res.stdout)
        self.assertEqual(st['official_installed'], False)
        self.assertEqual(st['installed'], True)
        self.assertEqual(st['version'], 'v3.3.1-rust.1-rc.9')
        self.assertEqual(st['rollback_count'], 2)
        self.assertEqual(st['root_enabled'], False)
        self.assertEqual(st['default_action'], 'update')

        # 4. Root enabled + legacy backup active, then forgotten
        self.create_fork_install('v3.3.1-rust.1-rc.9', history_count=1, root_enabled=True)
        self.create_legacy_backup(forgotten=False)
        res = subprocess.run(
            ['bash', str(GUI_SCRIPT), '--print-state'],
            env=self.base_env(), text=True, capture_output=True, check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        st = json.loads(res.stdout)
        self.assertEqual(st['root_enabled'], True)
        self.assertEqual(st['legacy_backup'], True)

        self.create_legacy_backup(forgotten=True)
        res = subprocess.run(
            ['bash', str(GUI_SCRIPT), '--print-state'],
            env=self.base_env(), text=True, capture_output=True, check=False,
        )
        st = json.loads(res.stdout)
        self.assertEqual(st['legacy_backup'], False)

    def run_gui(self, backend: str, choice: str, *, extra_env: dict[str, str] | None = None, stdin_text: str | None = None):
        env = self.base_env(
            DECKY_GUI_BACKEND=backend,
            TEST_GUI_CHOICE=choice,
            **(extra_env or {}),
        )
        return subprocess.run(
            ['bash', str(GUI_SCRIPT)],
            env=env, input=stdin_text, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )

    def read_installer_call(self) -> dict:
        lines = self.installer_calls.read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(lines), 1, f'Expected 1 installer call, got: {lines}')
        return json.loads(lines[0])

    def test_zenity_and_kdialog_and_tty_install_skip_sudo(self):
        for backend in ('zenity', 'kdialog'):
            with self.subTest(backend=backend):
                if self.installer_calls.exists():
                    self.installer_calls.unlink()
                if self.sudo_calls.exists():
                    self.sudo_calls.unlink()
                res = self.run_gui(backend, 'install')
                self.assertEqual(res.returncode, 0, res.stdout)
                call = self.read_installer_call()
                self.assertEqual(call['argv'], ['--channel', 'preview'])
                self.assertEqual(call['sudo_askpass'], '')
                self.assertFalse(self.sudo_calls.exists())

        if self.installer_calls.exists():
            self.installer_calls.unlink()
        res_tty = self.run_gui('tty', '', stdin_text='1\n')
        self.assertEqual(res_tty.returncode, 0, res_tty.stdout)
        call = self.read_installer_call()
        self.assertEqual(call['argv'], ['--channel', 'preview'])

    def test_auto_backend_selection_across_desktops(self):
        # KDE session prefers kdialog even when zenity is present
        env_kde = self.base_env(
            XDG_CURRENT_DESKTOP='KDE',
            TEST_GUI_CHOICE='install',
        )
        res_kde = subprocess.run(['bash', str(GUI_SCRIPT)], env=env_kde, text=True, capture_output=True, check=False)
        self.assertEqual(res_kde.returncode, 0, res_kde.stdout)
        gui_log = (self.log_dir / 'gui_calls.log').read_text(encoding='utf-8')
        self.assertIn('kdialog ', gui_log)

        # GNOME / non-KDE prefers zenity when available
        (self.log_dir / 'gui_calls.log').unlink()
        self.installer_calls.unlink()
        env_gnome = self.base_env(
            XDG_CURRENT_DESKTOP='GNOME',
            TEST_GUI_CHOICE='install',
        )
        res_gnome = subprocess.run(['bash', str(GUI_SCRIPT)], env=env_gnome, text=True, capture_output=True, check=False)
        self.assertEqual(res_gnome.returncode, 0, res_gnome.stdout)
        gui_log = (self.log_dir / 'gui_calls.log').read_text(encoding='utf-8')
        self.assertIn('zenity ', gui_log)

        # Both zenity and kdialog absent -> falls back to tty even if host /usr/bin has zenity/kdialog
        self.stub_zenity.unlink()
        self.stub_kdialog.unlink()
        no_gui_bin = self.root / 'no_gui_bin'
        no_gui_bin.mkdir()
        for tool in ('bash', 'cat', 'chmod', 'dirname', 'id', 'mktemp', 'python3', 'rm', 'sh', 'uname'):
            resolved = shutil.which(tool)
            if resolved:
                (no_gui_bin / tool).symlink_to(resolved)
        which_res = subprocess.run(
            [str(no_gui_bin / 'bash'), str(GUI_SCRIPT), '--print-backend'],
            env=self.base_env(PATH=f'{self.fake_bin}:{no_gui_bin}'),
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(which_res.returncode, 0, which_res.stderr)
        self.assertEqual(which_res.stdout.strip(), 'tty')

    def test_migrate_enable_disable_undo_restore_and_askpass_cleanup(self):
        # Migrate
        self.create_official_unit()
        res = self.run_gui('zenity', 'migrate')
        self.assertEqual(res.returncode, 0, res.stdout)
        call = self.read_installer_call()
        self.assertEqual(call['argv'], ['--migrate-legacy', '--channel', 'preview', '--non-interactive'])
        self.assertTrue(call['askpass_exists'])
        self.assertFalse(Path(call['sudo_askpass']).exists(), 'Askpass file must be cleaned up on exit')
        self.assertFalse(Path(call['sudo_askpass']).parent.exists(), 'Askpass dir must be cleaned up on exit')

        # Enable root plugins (even on failure, askpass must be cleaned up!)
        self.installer_calls.unlink()
        self.create_fork_install()
        res_fail = self.run_gui('kdialog', 'enable-root', extra_env={'TEST_INSTALLER_FAIL': '1'})
        self.assertNotEqual(res_fail.returncode, 0)
        call = self.read_installer_call()
        self.assertEqual(call['argv'], ['--enable-root-plugins', '--channel', 'preview', '--non-interactive'])
        self.assertTrue(call['askpass_exists'])
        self.assertFalse(Path(call['sudo_askpass']).exists(), 'Askpass must be cleaned up even when installer fails')

        # Disable root plugins
        self.installer_calls.unlink()
        self.create_fork_install(root_enabled=True)
        res = self.run_gui('zenity', 'disable-root')
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertEqual(self.read_installer_call()['argv'], ['--disable-root-plugins', '--non-interactive'])

        # Undo (no sudo)
        self.installer_calls.unlink()
        self.sudo_calls.unlink()
        self.create_fork_install(history_count=2)
        res = self.run_gui('kdialog', 'undo')
        self.assertEqual(res.returncode, 0, res.stdout)
        call = self.read_installer_call()
        self.assertEqual(call['argv'], ['--undo'])
        self.assertEqual(call['sudo_askpass'], '')
        self.assertFalse(self.sudo_calls.exists())

        # Restore legacy
        self.installer_calls.unlink()
        self.create_legacy_backup()
        res = self.run_gui('zenity', 'restore-legacy')
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertEqual(self.read_installer_call()['argv'], ['--restore-legacy', '--non-interactive'])

    def test_cancel_exits_zero_without_running_installer(self):
        for backend in ('zenity', 'kdialog'):
            with self.subTest(backend=backend):
                res = self.run_gui(backend, '__CANCEL__')
                self.assertEqual(res.returncode, 0, res.stdout)
                self.assertFalse(self.installer_calls.exists())

    def _decode_xdg_exec(self, raw_value: str) -> list[str]:
        # Step 1: XDG Desktop Entry string unescaping.
        step1: list[str] = []
        i = 0
        while i < len(raw_value):
            if raw_value[i] == '\\' and i + 1 < len(raw_value):
                nxt = raw_value[i + 1]
                escapes = {'s': ' ', 'n': '\n', 't': '\t', 'r': '\r', '\\': '\\'}
                if nxt in escapes:
                    step1.append(escapes[nxt])
                    i += 2
                    continue
            step1.append(raw_value[i])
            i += 1
        unescaped = ''.join(step1)

        # Step 2: XDG Exec field splitting and double-quote unescaping.
        args: list[str] = []
        cur: list[str] = []
        in_arg = False
        in_quotes = False
        j = 0
        while j < len(unescaped):
            ch = unescaped[j]
            if in_quotes:
                if ch == '\\' and j + 1 < len(unescaped) and unescaped[j + 1] in ('"', '`', '$', '\\'):
                    cur.append(unescaped[j + 1])
                    j += 2
                    continue
                if ch == '"':
                    in_quotes = False
                    j += 1
                    continue
                cur.append(ch)
                j += 1
                continue
            if ch.isspace():
                if in_arg:
                    args.append(''.join(cur))
                    cur.clear()
                    in_arg = False
                j += 1
                continue
            in_arg = True
            if ch == '"':
                in_quotes = True
            else:
                cur.append(ch)
            j += 1
        self.assertFalse(in_quotes, f'Unterminated quotes in Exec: {raw_value}')
        if in_arg:
            args.append(''.join(cur))
        return args

    def test_desktop_entry_passes_desktop_file_validate_and_bash_syntax(self):
        self.assertTrue(DESKTOP_FILE.is_file(), f'Missing {DESKTOP_FILE}')
        if shutil.which('desktop-file-validate'):
            val = subprocess.run(
                ['desktop-file-validate', str(DESKTOP_FILE)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(val.returncode, 0, val.stdout + val.stderr)
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(DESKTOP_FILE, encoding='utf-8')
        self.assertTrue(parser.has_section('Desktop Entry'))
        entry = parser['Desktop Entry']
        self.assertEqual(entry.get('Type'), 'Application')
        self.assertEqual(entry.get('Terminal'), 'false')
        self.assertTrue(entry.get('Name'))
        exec_line = entry.get('Exec', '')
        self.assertTrue(exec_line)
        parts = self._decode_xdg_exec(exec_line)
        self.assertGreaterEqual(len(parts), 3)
        self.assertEqual(parts[:2], ['bash', '-c'])
        self.assertIn('install-gui.sh', parts[2])
        check = subprocess.run(['bash', '-n', '-c', parts[2]], text=True, capture_output=True, check=False)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_kdialog_progress_uses_qdbus6_fallback(self):
        self.stub_qdbus.unlink()
        qdbus6_calls = self.log_dir / 'qdbus6_calls.log'
        stub_qdbus6 = self.fake_bin / 'qdbus6'
        stub_qdbus6.write_text(
            '#!/usr/bin/env python3\n'
            'import os, sys\n'
            'with open(os.environ["TEST_QDBUS6_LOG"], "a", encoding="utf-8") as fh:\n'
            '    fh.write(" ".join(sys.argv[1:]) + "\\n")\n',
            encoding='utf-8',
        )
        stub_qdbus6.chmod(0o755)
        no_qdbus_bin = self.root / 'no_qdbus_bin'
        no_qdbus_bin.mkdir()
        for tool in ('bash', 'cat', 'chmod', 'dirname', 'id', 'mktemp', 'python3', 'rm', 'sh', 'uname'):
            resolved = shutil.which(tool)
            if resolved:
                (no_qdbus_bin / tool).symlink_to(resolved)
        res = self.run_gui(
            'kdialog',
            'install',
            extra_env={
                'PATH': f'{self.fake_bin}:{no_qdbus_bin}',
                'TEST_QDBUS6_LOG': str(qdbus6_calls),
            },
        )
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertTrue(qdbus6_calls.is_file(), 'Expected qdbus6 to be called when qdbus is unavailable')
        self.assertIn('org.kde.kdialog-test /ProgressDialog close', qdbus6_calls.read_text(encoding='utf-8'))

    def test_process_substitution_invocation(self):
        res = subprocess.run(
            ['bash', '-c', f'bash <(cat {shlex.quote(str(GUI_SCRIPT))}) --print-backend'],
            env=self.base_env(), text=True, capture_output=True, check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(res.stdout.strip(), ('zenity', 'kdialog', 'tty'))

    def test_desktop_launcher_is_valid_and_exec_shell_parses(self):
        self.assertTrue(DESKTOP_FILE.is_file(), f'Missing {DESKTOP_FILE}')
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(DESKTOP_FILE, encoding='utf-8')
        self.assertTrue(parser.has_section('Desktop Entry'))
        entry = parser['Desktop Entry']
        self.assertEqual(entry.get('Type'), 'Application')
        self.assertEqual(entry.get('Terminal'), 'false')
        self.assertTrue(entry.get('Name'))
        parts = self._decode_xdg_exec(entry.get('Exec', ''))
        self.assertEqual(parts[:2], ['bash', '-c'])
        check = subprocess.run(['bash', '-n', '-c', parts[2]], text=True, capture_output=True, check=False)
        self.assertEqual(check.returncode, 0, check.stderr)


if __name__ == '__main__':
    unittest.main()
