"""The worker's plugin socket when the backend closes its connection."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from test_worker_imports import REPO_ROOT, worker

sys.path.insert(0, str(REPO_ROOT / "backend"))

from decky_loader.localplatform.localsocket import LocalSocket  # noqa: E402


class WorkerSocket(worker.BackendLossListener, LocalSocket):
    pass


class BackendLossTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_backend_ends_the_plugin_instead_of_spinning(self) -> None:
        received: list[str] = []
        closed = asyncio.Event()

        async def on_new_message(message: str) -> str:
            received.append(message)
            return '{"ok": true}'

        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temporary:
            socket = WorkerSocket()
            socket.socket_addr = str(Path(temporary) / "plugin.sock")
            socket.on_backend_closed = closed.set
            await socket.setup_server(on_new_message)
            try:
                reader, writer = await asyncio.open_unix_connection(socket.socket_addr)
                writer.write(b'{"id": 1}\n')
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.readline(), 5), b'{"ok": true}\n')
                writer.close()
                await writer.wait_closed()

                await asyncio.wait_for(closed.wait(), 5)
                # Give a spinning listener time to show itself.
                await asyncio.sleep(0.2)
                self.assertEqual(received, ['{"id": 1}\n'])
            finally:
                assert socket.socket is not None
                socket.socket.close()


if __name__ == "__main__":
    unittest.main()
