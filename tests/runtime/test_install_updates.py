import base64
import hashlib
import importlib.util
import json
import os
import tempfile
import shutil
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER_PATH = REPO_ROOT / "backend-rust" / "install.py"
sys.path.insert(0, str(INSTALLER_PATH.parent))
_INSTALLER_SPEC = importlib.util.spec_from_file_location("rust_runtime_installer", INSTALLER_PATH)
installer = importlib.util.module_from_spec(_INSTALLER_SPEC)
sys.modules[_INSTALLER_SPEC.name] = installer
_INSTALLER_SPEC.loader.exec_module(installer)



class RustBundleUpdateTests(unittest.TestCase):
    def test_running_installer_keeps_the_bundle_file_set_unchanged(self):
        temporary_root = Path(os.environ.get("TMPDIR", "/var/tmp/omp-1000"))
        with tempfile.TemporaryDirectory(dir=temporary_root) as temporary:
            bundle = Path(temporary)
            for name in ("install.py", "process_observer.py"):
                shutil.copy2(INSTALLER_PATH.with_name(name), bundle / name)
            before = {path.relative_to(bundle) for path in bundle.rglob("*")}
            environment = dict(os.environ)
            environment.pop("PYTHONDONTWRITEBYTECODE", None)
            subprocess.run(
                [sys.executable, str(bundle / "install.py"), "--help"],
                env=environment, capture_output=True, text=True, check=True,
            )
            self.assertEqual({path.relative_to(bundle) for path in bundle.rglob("*")}, before)


    def test_activation_health_preserves_frontend_cache_and_acknowledges_health_replies(self):
        frontend_reply = {"type": 1, "id": 1, "result": {"setting": "frontend value"}}
        cached = {1: frontend_reply}
        errors = []
        responses = {
            "utilities/ping": "pong",
            "loader/get_plugins": [],
            "updater/get_version_info": {"current": "v3.2.10"},
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path.startswith("/auth/token"):
                    denied = self.headers.get("X-Decky-Auth") == "invalid-installer-token"
                    body = b"denied" if denied else b"health-test-token"
                    self.send_response(403 if denied else 200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                key = self.headers["Sec-WebSocket-Key"]
                accept = base64.b64encode(hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                self.connection.settimeout(2)
                try:
                    while True:
                        opcode, _, payload = installer._websocket_receive(self.connection)
                        if opcode == 8:
                            break
                        message = json.loads(payload)
                        if message["type"] == 3:
                            cached.pop(message["id"], None)
                            continue
                        call_id = message["id"]
                        if call_id not in cached:
                            cached[call_id] = {"type": 1, "id": call_id,
                                               "result": responses[message["route"]]}
                        payload = json.dumps(cached[call_id]).encode()
                        self.connection.sendall(bytes((0x81, len(payload))) + payload)
                except BaseException as error:
                    errors.append(error)
                finally:
                    self.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            temporary_root = Path(os.environ.get("TMPDIR") or "/var/tmp/omp-1000")
            with tempfile.TemporaryDirectory(dir=temporary_root) as temporary:
                installer._api_health(
                    {"active": Path(temporary) / "active.json"},
                    f"127.0.0.1:{server.server_port}",
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(cached, {1: frontend_reply})



if __name__ == "__main__":
    unittest.main()
