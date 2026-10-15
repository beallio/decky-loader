"""Exercise selected imports and loader reads during real interpreter exit."""
import json
import os
from pathlib import Path
import pwd
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_worker_imports import (
    WORKER_PATH, _TestLoop, _approved_launch, _fake_sdk_modules,
    _load_sandboxed_plugin, worker,
)


def _run_worker(config_path):
    config = json.loads(Path(config_path).read_text())
    homebrew = Path(config['homebrew'])
    modules = _fake_sdk_modules(homebrew)
    modules['setproctitle'].getproctitle = lambda: 'worker exit check'
    modules['decky_loader.plugin.worker'] = worker
    modules['decky_loader.plugin'].worker = worker
    # These OS/socket adapters stay installed until this child process ends.
    # The worker entrypoint, plugin importer and interpreter exit are real.
    mock.patch.dict(sys.modules, modules).start()
    mock.patch.object(worker, 'prepare_runtime').start()
    sandbox = _load_sandboxed_plugin()
    loop = _TestLoop()
    sandbox.new_event_loop = lambda: loop
    sandbox.set_event_loop = lambda _loop: None
    sandbox.get_event_loop = lambda: loop
    sys.argv = [str(WORKER_PATH.parents[2] / 'worker_main.py'), *config['arguments']]
    runpy.run_path(sys.argv[0], run_name='__main__')


class WorkerShutdownTests(unittest.TestCase):
    def _check_exit(self, *, replace, fail):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            plugin_root = base / 'plugins'
            public = plugin_root / 'example-plugin'
            replacement_root = base / 'replacement-plugins'
            replacement = replacement_root / public.name
            for directory, origin in ((public, 'approved'), (replacement, 'replacement')):
                modules = directory / 'py_modules'
                modules.mkdir(parents=True)
                (modules / 'exit_assets.py').write_text('VALUE = "resource module"\n')
                (modules / 'asset.txt').write_text(origin + ' resource')
                (modules / 'exit_late.py').write_text(f'VALUE = {origin!r}\n')
            result_path = base / 'exit-result.json'
            source = (
                'import atexit, importlib, json, os\nfrom pathlib import Path\n'
                'import exit_assets\n'
                'def finish():\n'
                '    result = {"name": os.environ["DECKY_PLUGIN_NAME"]}\n'
                '    try:\n'
                '        late = importlib.import_module("exit_late")\n'
                '        result.update(late=late.VALUE, late_path=late.__file__)\n'
                '    except BaseException as error:\n'
                '        result["late_error"] = repr(error)\n'
                '    try:\n'
                '        path = str(Path(exit_assets.__file__).with_name("asset.txt"))\n'
                '        result["resource"] = exit_assets.__loader__.get_data(path).decode()\n'
                '    except BaseException as error:\n'
                '        result["resource_error"] = repr(error)\n'
                f'    Path({str(result_path)!r}).write_text(json.dumps(result))\n'
                'atexit.register(finish)\n'
            )
            if replace:
                source += (f'Path({str(plugin_root)!r}).rename({str(base / "approved-plugins")!r})\n'
                           f'Path({str(replacement_root)!r}).rename({str(plugin_root)!r})\n')
            if fail:
                source += 'raise RuntimeError("requested startup failure")\n'
            source += 'class Plugin:\n    pass\n'
            (public / 'main.py').write_text(source)
            homebrew = base / 'homebrew'
            directory_fd = os.open(public, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                arguments = [
                    '--plugin-root', str(plugin_root), '--plugin-directory', public.name,
                    '--homebrew', str(homebrew), '--socket', str(base / 'plugin.sock'),
                    '--user', pwd.getpwuid(os.getuid()).pw_name, '--bootstrap', 'lean',
                    '--approved-launch', json.dumps(_approved_launch(directory_fd, flags=[])),
                ]
                config_path = base / 'child.json'
                config_path.write_text(json.dumps({'homebrew': str(homebrew), 'arguments': arguments}))
                child = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), '--exit-worker', str(config_path)],
                    capture_output=True, text=True, timeout=20,
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                )
            finally:
                os.close(directory_fd)
            self.assertEqual(child.returncode, 1 if fail else 0, child.stdout + child.stderr)
            self.assertTrue(result_path.is_file(), child.stdout + child.stderr)
            result = json.loads(result_path.read_text())
            self.assertEqual(result.get('late'), 'approved', result)
            self.assertEqual(result.get('resource'), 'approved resource', result)
            self.assertEqual(result['late_path'], str(public / 'py_modules/exit_late.py'))
            self.assertEqual(result['name'], 'approved-plugin')

    def test_normal_exit_keeps_late_imports_and_resource_reads(self):
        self._check_exit(replace=False, fail=False)

    def test_normal_exit_keeps_selected_modules_after_parent_move(self):
        self._check_exit(replace=True, fail=False)

    def test_failed_startup_keeps_late_imports_and_resource_reads(self):
        self._check_exit(replace=False, fail=True)

    def test_failed_startup_keeps_selected_modules_after_parent_move(self):
        self._check_exit(replace=True, fail=True)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--exit-worker']:
        _run_worker(sys.argv[2])
    else:
        unittest.main()
