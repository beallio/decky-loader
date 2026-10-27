"""Exercise the relay through official upstream PluginWrapper and SandboxedPlugin.

Set DECKY_UPSTREAM_ROOT, DECKY_EXAMPLE_BACKEND and DECKY_EXAMPLE_UPDATED.
The example builds must emit example_event and updated_event and have equal size.
"""
import asyncio
from contextlib import ExitStack
import json
import multiprocessing
import os
from pathlib import Path
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
SHIM = ROOT / 'backend-rust' / 'decky-plugin' / 'shim' / 'main.py'
UPSTREAM = os.environ.get('DECKY_UPSTREAM_ROOT')
EXAMPLE = os.environ.get('DECKY_EXAMPLE_BACKEND')
UPDATED = os.environ.get('DECKY_EXAMPLE_UPDATED')


def children(pid):
    return [int(child) for child in Path(f'/proc/{pid}/task/{pid}/children').read_text().split()]


def pss(pid):
    for line in Path(f'/proc/{pid}/smaps_rollup').read_text().splitlines():
        if line.startswith('Pss:'):
            return int(line.split()[1])
    raise AssertionError('PSS missing')


async def drive(tag, method):
    multiprocessing.set_start_method(method)
    sys.path.insert(0, str(Path(UPSTREAM) / tag / 'backend'))
    from decky_loader.plugin.plugin import PluginWrapper

    active = []

    def kill_active():
        for item in active:
            if item.proc.is_alive():
                item.proc.kill()
                item.proc.join(1)

    with tempfile.TemporaryDirectory(prefix='upstream-relay-') as temporary, ExitStack() as cleanup:
        cleanup.callback(kill_active)
        base = Path(temporary)
        home = base / 'homebrew'
        plugins = base / 'plugins'
        plugin = plugins / 'Example'
        (plugin / 'bin').mkdir(parents=True)
        (plugin / 'plugin.json').write_text(json.dumps({
            'name': 'Native Example', 'author': 'Test', 'flags': [],
            'api_version': 1, 'backend': 'bin/backend',
        }))
        shim = plugin / 'main.py'
        shutil.copyfile(SHIM, shim)
        packaged = plugin / 'bin/backend'
        shutil.copyfile(EXAMPLE, packaged)
        packaged.chmod(0o755)
        os.environ.update(UNPRIVILEGED_PATH=str(home), UNPRIVILEGED_USER=pwd.getpwuid(os.getuid()).pw_name,
                          CHOWN_PLUGIN_PATH='0', TMPDIR=str(base))
        events = []
        sequence = 0

        async def emitted(event, args):
            events.append((event, args))

        async def start():
            nonlocal sequence
            sequence += 1
            item = PluginWrapper(str(shim), 'Example', str(plugins), emitted)
            item._socket.socket_addr = str(base / f'socket-{sequence}')
            begin = time.perf_counter()
            item.start()
            active.append(item)
            result = await asyncio.wait_for(item.execute_method('echo', 'ready', 4), 12)
            assert result == ['ready', 4], result
            start_ms = (time.perf_counter() - begin) * 1000
            worker = item.proc.pid
            backend = children(worker)
            assert len(backend) == 1, backend
            stage = home / 'data/Example/.decky-native-backend'
            assert os.path.samefile(f'/proc/{backend[0]}/exe', stage)
            assert not os.path.samefile(f'/proc/{worker}/exe', stage)
            return item, backend[0], start_ms

        async def stop(item, uninstall=False):
            begin = time.perf_counter()
            await asyncio.wait_for(item.stop(uninstall=uninstall), 7)
            elapsed = time.perf_counter() - begin
            assert elapsed < 2, elapsed
            assert not item.proc.is_alive()
            assert item.proc.exitcode == 0, item.proc.exitcode
            item.proc.join(0.2)
            active.remove(item)
            return elapsed

        async def notify(item, expected):
            events.clear()
            await asyncio.wait_for(item.execute_method('notify', 'hello', 7), 5)
            deadline = time.monotonic() + 2
            while not events and time.monotonic() < deadline:
                await asyncio.sleep(0.001)
            assert events == [(expected, ['hello', 7])], events

        first, backend, startup_ms = await start()
        stage = home / 'data/Example/.decky-native-backend'
        original_inode = stage.stat().st_ino
        await notify(first, 'example_event')
        try:
            await asyncio.wait_for(first.execute_legacy_method('echo', {'x': 1}), 5)
        except Exception as error:
            assert 'Legacy methods may not be used' in str(error), error
        else:
            raise AssertionError('legacy method was accepted')
        size_mib = (pss(first.proc.pid) + pss(backend)) / 1024
        begin = time.perf_counter()
        for _ in range(100):
            assert await first.execute_method('echo', 1) == [1]
        call_us = (time.perf_counter() - begin) * 10000

        old = packaged.stat()
        updated = Path(UPDATED).read_bytes()
        assert len(updated) == old.st_size and updated != packaged.read_bytes()
        packaged.write_bytes(updated)
        os.utime(packaged, ns=(old.st_atime_ns, old.st_mtime_ns))
        new = packaged.stat()
        assert (old.st_ino, old.st_size, old.st_mtime_ns) == (new.st_ino, new.st_size, new.st_mtime_ns)
        await notify(first, 'example_event')
        await stop(first)

        second, _, _ = await start()
        assert stage.stat().st_ino != original_inode
        await notify(second, 'updated_event')
        uninstall_seconds = await stop(second, uninstall=True)
        assert (home / 'settings/Example/uninstalled').exists()
        logs = ''.join(file.read_text() for file in (home / 'logs/Example').glob('*.log'))
        assert 'unloading' in logs and 'uninstalling' in logs and logs.index('unloading') < logs.index('uninstalling')
        assert not stage.exists()

        third, _, _ = await start()
        reused_inode = stage.stat().st_ino
        await stop(third)
        fourth, backend, _ = await start()
        assert stage.stat().st_ino == reused_inode
        os.kill(backend, signal.SIGKILL)
        try:
            await asyncio.wait_for(fourth.execute_method('echo', 'after crash'), 5)
        except Exception as error:
            assert 'native backend stopped' in str(error) or 'native backend relay failed' in str(error), error
        else:
            raise AssertionError('backend crash did not fail the call')
        assert fourth.proc.is_alive(), 'relay worker died with backend'
        await stop(fourth)
        print(json.dumps({'tag': tag, 'method': method, 'startup_ms': round(startup_ms, 3),
                          'pss_mib': round(size_mib, 3), 'call_us_mean': round(call_us, 3),
                          'calls': 100, 'uninstall_seconds': round(uninstall_seconds, 4)}))


@unittest.skipUnless(UPSTREAM and EXAMPLE and UPDATED, 'set DECKY_UPSTREAM_ROOT and the two SDK example builds')
class OfficialUpstreamTests(unittest.TestCase):
    def run_tag(self, tag):
        for method in ('fork', 'forkserver'):
            with self.subTest(method=method):
                process = subprocess.Popen([sys.executable, __file__, '--drive', tag, method], text=True,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                           env=os.environ)
                try:
                    stdout, stderr = process.communicate(timeout=90)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    stdout, stderr = process.communicate()
                    self.fail(f'{tag} {method} timed out:\n{stdout}\n{stderr}')
                self.assertEqual(process.returncode, 0, stdout + '\n' + stderr)
                evidence = json.loads(stdout.splitlines()[-1])
                self.assertEqual((evidence['tag'], evidence['method']), (tag, method))
                print(json.dumps(evidence), flush=True)

    def test_v3_2_10(self):
        self.run_tag('v3.2.10')

    def test_v3_3_1(self):
        self.run_tag('v3.3.1')


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--drive':
        asyncio.run(drive(sys.argv[2], sys.argv[3]))
    else:
        unittest.main()
