"""Run the upstream-loader main.py shim against a backend that speaks the native protocol."""
import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

SHIM = Path(__file__).resolve().parents[2] / 'backend-rust' / 'decky-plugin' / 'shim' / 'main.py'
# Same EOF handling as upstream's worker socket (SteamDeckHomebrew/decky-loader#975).
LOCALSOCKET = Path(__file__).resolve().parents[2] / 'backend' / 'decky_loader' / 'localplatform' / 'localsocket.py'

# Stands in for bin/backend: the worker protocol of the decky-plugin crate, plus
# marks in $MARKS for each lifecycle hook it runs.
BACKEND = '''#!/usr/bin/env python3
import json, os, signal, socket, sys
def mark(name):
    with open(os.environ["MARKS"], "a") as file:
        file.write(name + "\\n")
if os.environ.get("DECKY_PLUGIN_HOOK") == "uninstall":
    mark("uninstall")
    sys.exit(0)
mode = os.environ.get("FAKE_MODE")
if mode == "crash":
    sys.exit(3)
if mode == "stuck":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    signal.signal(signal.SIGTERM, lambda *_: (mark("unload"), os._exit(0)))
server = socket.socket(socket.AF_UNIX)
server.bind(os.environ["DECKY_PLUGIN_SOCKET"])
server.listen()
stream = server.accept()[0].makefile("rwb")
for line in stream:
    if mode == "garbage":
        stream.write(b"not json\\n")
        stream.flush()
        continue
    call = json.loads(line)
    reply = {"type": 1, "id": call["id"], "success": True, "res": call["args"]}
    if call["method"] == "fail":
        reply.update(success=False, res="nope")
    elif call["method"] == "notify":
        stream.write(json.dumps({"type": 2, "event": "ping", "args": call["args"]}).encode() + b"\\n")
    stream.write(json.dumps(reply).encode() + b"\\n")
    stream.flush()
while True:
    signal.pause()
'''


class NativeShimTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.plugin_dir = Path(temporary.name)
        (self.plugin_dir / 'plugin.json').write_text(json.dumps({'name': 'Dual', 'backend': 'bin/backend'}))
        (self.plugin_dir / 'bin').mkdir()
        backend = self.plugin_dir / 'bin' / 'backend'
        backend.write_text(BACKEND)
        backend.chmod(0o755)
        self.marks = self.plugin_dir / 'marks'
        self.marks.touch()
        self.events = []

        async def emit(event, *args):
            self.events.append((event, *args))

        decky = types.ModuleType('decky')
        decky.DECKY_PLUGIN_DIR = str(self.plugin_dir)
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(self.plugin_dir / 'data')
        decky.logger = logging.getLogger('native-shim-test')
        decky.emit = emit
        self.enterContext(mock.patch.dict(sys.modules, {'decky': decky}))
        self.enterContext(mock.patch.dict(os.environ, {'MARKS': str(self.marks)}))
        # Keep the shim's process-wide socket patch out of other worker tests.
        self.enterContext(mock.patch.dict(sys.modules, {'decky_loader.localplatform.localsocket': None}))
        spec = importlib.util.spec_from_file_location('native_shim', SHIM)
        self.shim = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.shim)

    def test_relays_calls_events_and_lifecycle_in_upstream_order(self):
        async def scenario():
            plugin = self.shim.Plugin()
            self.assertFalse(hasattr(plugin, '_migration'))
            main = asyncio.create_task(plugin._main())
            self.assertEqual(await asyncio.gather(plugin.echo(1, 'a'), plugin.echo(2)), [[1, 'a'], [2]])
            with self.assertRaisesRegex(RuntimeError, 'nope'):
                await plugin.fail()
            await plugin.notify(7)
            self.assertEqual(self.events, [('ping', 7)])
            # Upstream's socket listener spins without yielding once the loader
            # closes the socket, so the stop hooks must finish without yielding.
            loop_turns = []
            asyncio.get_running_loop().call_soon(loop_turns.append, 1)
            await plugin._unload()
            await plugin._uninstall()
            self.assertEqual(loop_turns, [])
            await asyncio.wait_for(main, 5)
            with self.assertRaisesRegex(RuntimeError, 'stopped'):
                await plugin.echo()
            self.assertFalse(Path(plugin._directory).exists())
            self.assertEqual(list((self.plugin_dir / 'data').iterdir()), [])

        asyncio.run(scenario())
        self.assertEqual(self.marks.read_text().split(), ['unload', 'uninstall'])

    def test_runs_a_copy_so_an_update_can_replace_the_packaged_backend(self):
        # Upstream extracts a ZIP update over the live folder; a running
        # packaged executable would make that fail with ETXTBSY.
        packaged = self.plugin_dir / 'bin' / 'backend'

        async def scenario():
            plugin = self.shim.Plugin()
            main = asyncio.create_task(plugin._main())
            self.assertEqual(await plugin.echo(), [])
            command = Path(f'/proc/{plugin._process.pid}/cmdline').read_bytes().split(b'\0')
            self.assertNotIn(str(packaged).encode(), command)
            self.assertEqual([p.name for p in (self.plugin_dir / 'data').iterdir()], ['.decky-native-backend'])
            packaged.write_text(BACKEND.replace('mark("unload")', 'mark("updated")'))
            await plugin._unload()
            await asyncio.wait_for(main, 5)
            # The next start runs the updated file.
            restarted = self.shim.Plugin()
            main = asyncio.create_task(restarted._main())
            self.assertEqual(await restarted.echo(), [])
            await restarted._unload()
            await asyncio.wait_for(main, 5)

        asyncio.run(scenario())
        self.assertEqual(self.marks.read_text().split(), ['unload', 'updated'])

    def test_unparsable_reply_fails_pending_calls_instead_of_hanging(self):
        async def scenario():
            plugin = self.shim.Plugin()
            main = asyncio.create_task(plugin._main())
            with self.assertRaisesRegex(RuntimeError, 'relay failed'):
                await asyncio.wait_for(plugin.echo(), 5)
            with self.assertRaisesRegex(RuntimeError, 'relay failed'):
                await plugin.echo()
            await plugin._unload()
            await asyncio.wait_for(main, 5)

        with mock.patch.dict(os.environ, {'FAKE_MODE': 'garbage'}):
            asyncio.run(scenario())

    def test_lone_surrogate_argument_rejects_and_later_calls_still_work(self):
        async def scenario():
            plugin = self.shim.Plugin()
            main = asyncio.create_task(plugin._main())
            with self.assertRaises(UnicodeEncodeError):
                await asyncio.wait_for(plugin.echo('\ud800'), 5)
            self.assertEqual(await plugin.echo('ok'), ['ok'])
            await plugin._unload()
            await asyncio.wait_for(main, 5)

        asyncio.run(scenario())

    def test_trim_waits_for_quiet_traffic_and_no_pending_calls(self):
        trims = []
        self.shim._malloc_trim = lambda pad: trims.append(pad)
        self.shim.TRIM_DELAY_SECONDS = 0.2

        async def scenario():
            plugin = self.shim.Plugin()
            main = asyncio.create_task(plugin._main())
            large = 'x' * (self.shim.TRIM_LINE_BYTES + 1)
            self.assertEqual(await plugin.echo(large), [large])
            # Small calls every 0.1 s for 0.8 s keep postponing the trim.
            for _ in range(8):
                self.assertEqual(await plugin.echo('small'), ['small'])
                await asyncio.sleep(0.1)
            self.assertEqual(trims, [])
            # A call still waiting for its reply also defers it.
            plugin._pending['held'] = asyncio.get_running_loop().create_future()
            await asyncio.sleep(0.5)
            self.assertEqual(trims, [])
            del plugin._pending['held']
            await asyncio.sleep(0.5)
            self.assertEqual(trims, [0])
            await plugin._unload()
            await asyncio.wait_for(main, 5)

        asyncio.run(scenario())

    def test_returns_freed_memory_once_after_a_burst_of_large_replies(self):
        trims = []
        self.shim._malloc_trim = lambda pad: trims.append(pad)
        self.shim.TRIM_DELAY_SECONDS = 0.2

        async def scenario():
            plugin = self.shim.Plugin()
            main = asyncio.create_task(plugin._main())
            self.assertEqual(await plugin.echo('small'), ['small'])
            await asyncio.sleep(0.4)
            self.assertEqual(trims, [])
            large = 'x' * (self.shim.TRIM_LINE_BYTES + 1)
            for _ in range(3):
                self.assertEqual(await plugin.echo(large), [large])
            self.assertEqual(trims, [])
            await asyncio.sleep(0.4)
            self.assertEqual(trims, [0])
            await plugin._unload()
            await asyncio.wait_for(main, 5)

        asyncio.run(scenario())

    def test_kills_a_backend_that_ignores_sigterm_at_the_deadline(self):
        async def scenario():
            plugin = self.shim.Plugin()
            main = asyncio.create_task(plugin._main())
            self.assertEqual(await plugin.echo(), [])
            started = time.monotonic()
            await plugin._unload()
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(plugin._process.returncode, -9)
            await asyncio.wait_for(main, 5)

        with mock.patch.dict(os.environ, {'FAKE_MODE': 'stuck'}), mock.patch.object(self.shim, 'STOP_SECONDS', 0.3):
            asyncio.run(scenario())

    def test_calls_fail_when_the_backend_exits_before_it_listens(self):
        async def scenario():
            plugin = self.shim.Plugin()
            await asyncio.wait_for(plugin._main(), 5)
            with self.assertRaisesRegex(RuntimeError, 'exited with 3'):
                await plugin.echo()
            await plugin._unload()

        with mock.patch.dict(os.environ, {'FAKE_MODE': 'crash'}):
            asyncio.run(scenario())

    def test_refuses_a_backend_outside_the_plugin(self):
        (self.plugin_dir / 'plugin.json').write_text(json.dumps({'backend': '../outside'}))

        async def scenario():
            plugin = self.shim.Plugin()
            await plugin._main()
            with self.assertRaisesRegex(RuntimeError, 'inside the plugin'):
                await plugin.echo()

        asyncio.run(scenario())

    def test_worker_socket_waits_at_eof_so_shutdown_can_run(self):
        spec = importlib.util.spec_from_file_location('decky_loader.localplatform.localsocket', LOCALSOCKET)
        localsocket = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(localsocket)
        with mock.patch.dict(sys.modules, {'decky_loader.localplatform.localsocket': localsocket}):
            self.shim._park_at_socket_eof()
        resumed = threading.Event()

        async def scenario():
            worker = localsocket.UnixSocket()
            worker.socket_addr = str(self.plugin_dir / 'loader.sock')

            async def on_new_message(line):
                return None

            await worker.setup_server(on_new_message)
            _, loader = await asyncio.open_unix_connection(worker.socket_addr)
            loader.close()  # the loader closes its end while it stops the plugin
            # A spinning listener never lets this sleep resume.
            await asyncio.sleep(0.2)
            resumed.set()

        # A daemon thread, so a spinning loop fails the test instead of hanging it.
        threading.Thread(target=asyncio.run, args=(scenario(),), daemon=True).start()
        self.assertTrue(resumed.wait(3))


if __name__ == '__main__':
    unittest.main()
