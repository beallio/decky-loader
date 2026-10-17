"""Installer shutdown ownership, retry, and real-process signaling coverage."""
import importlib.util
import os
import select
import signal
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER_PATH = REPO_ROOT / "backend-rust" / "install.py"
sys.path.insert(0, str(INSTALLER_PATH.parent))
_INSTALLER_SPEC = importlib.util.spec_from_file_location("rust_runtime_installer", INSTALLER_PATH)
installer = importlib.util.module_from_spec(_INSTALLER_SPEC)
sys.modules[_INSTALLER_SPEC.name] = installer
_INSTALLER_SPEC.loader.exec_module(installer)

from process_observer import ProcessObserver, read_process


class ServiceShutdownSignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != "linux" or not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise unittest.SkipTest("service shutdown signaling requires Linux pidfds")

    def _spawn_ready_child(self, body, *, interactive=False):
        child = subprocess.Popen(
            [sys.executable, "-c", body],
            stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.addCleanup(self._cleanup_child, child)
        ready, _, _ = select.select([child.stdout], [], [], 5)
        if not ready:
            self.fail(f"child {child.pid} did not become ready")
        line = child.stdout.readline()
        if line != "ready\n":
            self.fail(f"child {child.pid} failed before readiness: {line!r}")
        return child

    @staticmethod
    def _cleanup_child(child):
        if child.poll() is None:
            try:
                child.kill()
            except ProcessLookupError:
                pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()

    def _process_record(self, child):
        record = read_process(child.pid)
        self.assertIsNotNone(record, f"child {child.pid} disappeared after readiness")
        return record

    def _assert_ping(self, child):
        child.stdin.write("ping\n")
        child.stdin.flush()
        ready, _, _ = select.select([child.stdout], [], [], 5)
        self.assertTrue(ready, f"child {child.pid} did not answer while expected to be alive")
        self.assertEqual(child.stdout.readline(), "pong\n")

    def test_term_exits_only_owned_child_with_handler_status(self):
        child = self._spawn_ready_child(
            "import os, signal, sys\n"
            "signal.signal(signal.SIGTERM, lambda *_: os._exit(37))\n"
            "print('ready', flush=True)\n"
            "while True: signal.pause()\n"
        )
        processes = {child.pid: self._process_record(child)}

        installer._signal_service_processes(processes, signal.SIGTERM)

        self.assertEqual(child.wait(timeout=5), 37)

    def test_term_resistant_owned_child_is_killed_without_touching_sibling(self):
        target = self._spawn_ready_child(
            "import signal, sys\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True)\n"
            "while True: signal.pause()\n"
        )
        sibling = self._spawn_ready_child(
            "import signal, sys\n"
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(38))\n"
            "print('ready', flush=True)\n"
            "for line in sys.stdin:\n"
            "    if line == 'ping\\n': print('pong', flush=True)\n",
            interactive=True,
        )
        processes = {target.pid: self._process_record(target)}

        installer._signal_service_processes(processes, signal.SIGTERM)
        self.assertIsNone(target.poll(), "SIGTERM-ignoring owned process must survive TERM")
        self._assert_ping(sibling)

        installer._signal_service_processes(processes, signal.SIGKILL)
        self.assertEqual(target.wait(timeout=5), -signal.SIGKILL)
        self.assertIsNone(sibling.poll(), "unrelated sibling must remain alive after escalation")
        self._assert_ping(sibling)

    def test_mismatched_start_ticks_do_not_signal_live_pid(self):
        child = self._spawn_ready_child(
            "import os, signal, sys\n"
            "signal.signal(signal.SIGTERM, lambda *_: os._exit(39))\n"
            "print('ready', flush=True)\n"
            "for line in sys.stdin:\n"
            "    if line == 'ping\\n': print('pong', flush=True)\n",
            interactive=True,
        )
        actual = self._process_record(child)
        processes = {child.pid: replace(actual, start_ticks=actual.start_ticks + 1)}

        installer._signal_service_processes(processes, signal.SIGTERM)
        self._assert_ping(child)

        self.assertIsNone(child.poll(), "a PID with a different start_ticks identity must not be signalled")


class ServiceShutdownRetryTests(unittest.TestCase):
    def test_retry_keeps_zombie_ownership_after_systemd_forgets_group(self):
        for load_state, validation_failure in (
            ("loaded", False), ("not-found", False), ("loaded", True), ("not-found", True),
        ):
            with self.subTest(load_state=load_state, validation_failure=validation_failure), tempfile.TemporaryDirectory() as temporary:
                proc = Path(temporary)
                unit = f"decky-stop-retry-{load_state}.service"
                group = f"/system.slice/{unit}"
                child = proc / "42"
                child.mkdir()
                fields = ["Z", "20", "42", "42"] + ["0"] * 16
                fields[19] = "100"
                (child / "stat").write_text("42 (held worker) " + " ".join(fields) + "\n")
                (child / "cgroup").write_text(f"0::{group}\n")
                observer = ProcessObserver(group, proc)
                properties = {
                    "LoadState": "loaded", "ActiveState": "active" if validation_failure else "inactive",
                    "MainPID": "99" if validation_failure else "0", "ControlGroup": group,
                }
                clock = [0.0]

                def snapshot(control_group, *, retained=None):
                    if control_group is None:
                        return {}
                    processes = observer.snapshot(retained=retained)
                    # systemd can drop the unit/group while its zombie is unreaped.
                    properties.update(LoadState=load_state, ActiveState="inactive", MainPID="0", ControlGroup="")
                    (child / "cgroup").write_text("0::/escaped\n")
                    return processes

                def sleep(seconds):
                    clock[0] += seconds

                def main_disappeared(*_):
                    snapshot(group)
                    raise installer.InstallerError("selected main disappeared during validation")

                with (
                    patch.object(installer, "SERVICE_STOP_DIRECTORY", proc / "stop-state"),
                    patch.object(
                        installer, "_ensure_directory",
                        side_effect=lambda path, *_, **__: path.mkdir(mode=0o700, exist_ok=True),
                    ),
                    patch.object(installer, "_unit_properties", side_effect=lambda _: dict(properties)),
                    patch.object(installer, "_systemctl", side_effect=AssertionError("unit is already stopped")),
                    patch.object(installer, "cgroup_processes", side_effect=snapshot),
                    patch.object(installer, "_service_cgroup_pids", side_effect=main_disappeared),
                    patch.object(installer.time, "monotonic", side_effect=lambda: clock[0]),
                    patch.object(installer.time, "sleep", side_effect=sleep),
                ):
                    for attempt in range(2):
                        observer = ProcessObserver(group, proc)
                        with self.subTest(attempt=attempt):
                            with self.assertRaises(installer.InstallerError) as refused:
                                installer.stop_service(unit, scope="system")
                            if attempt or not validation_failure:
                                self.assertRegex(str(refused.exception), r"PID 42 .*state Z")
                    # Reaping, not another retry, makes shutdown complete.
                    (child / "stat").unlink()
                    installer.stop_service(unit, scope="system")


if __name__ == "__main__":
    unittest.main()
