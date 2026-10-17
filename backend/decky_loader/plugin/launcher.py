"""Single-threaded parent for forked plugin workers.

The control protocol is newline-delimited JSON on stdin/stdout. A start request
first forks a child held behind a private pipe, then reports its PID; Rust records
that PID before sending release. Closing control stdin or handling a termination
signal closes unreleased gates and terminates every owned process group.
Rust kills registered process groups if the launcher exits abruptly.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
from io import TextIOWrapper
import json
import os
import select
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any, cast

from decky_loader.plugin import worker


_CHILD_SHUTDOWN_TIMEOUT = 5.0
_POLL_INTERVAL = 0.05
_stopping = False
_children_changed = False
_wake_read = -1
_wake_write = -1
_system_pythonpaths: dict[str, list[str]] = {}


@dataclass
class _Child:
    identifier: str
    pid: int
    gate_fd: int | None
    wait_status: int | None = None


def _shared_system_pythonpaths(user: str) -> list[str]:
    # Every plugin asks the system Python for the same module paths. Ask once
    # before forking so that children do not each start python3.
    paths = _system_pythonpaths.get(user)
    if paths is None:
        from decky_loader.helpers import get_system_pythonpaths

        previous = os.environ.get("UNPRIVILEGED_USER")
        os.environ["UNPRIVILEGED_USER"] = user
        try:
            paths = get_system_pythonpaths()
        finally:
            if previous is None:
                del os.environ["UNPRIVILEGED_USER"]
            else:
                os.environ["UNPRIVILEGED_USER"] = previous
        _system_pythonpaths[user] = paths
    return paths


def _write_message(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _set_parent_death_signal(expected_parent: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() != expected_parent:
        os._exit(1)


def _worker_child(
    gate_read: int, gate_write: int, arguments: dict[str, Any],
    parent_pid: int, inherited_fds: tuple[int, ...],
) -> None:
    try:
        gc.enable()
        signal.set_wakeup_fd(-1)
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        for descriptor in inherited_fds:
            os.close(descriptor)
        os.close(gate_write)
        os.close(0)
        stdin_fd = os.open(os.devnull, os.O_RDONLY)
        if stdin_fd != 0:
            os.dup2(stdin_fd, 0)
            os.close(stdin_fd)
        sys.stdout.flush()
        os.dup2(2, 1)
        cast(TextIOWrapper, sys.stdout).reconfigure(line_buffering=True, write_through=True)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        _set_parent_death_signal(parent_pid)
        while True:
            try:
                released = os.read(gate_read, 1)
                break
            except InterruptedError:
                continue
        os.close(gate_read)
        if not released:
            os._exit(1)

        try:
            worker.launch(argparse.Namespace(**arguments))
        except BaseException as error:
            print(f"DeckyPluginWorker: {error}", file=sys.stderr, flush=True)
            code = error.code if isinstance(error, SystemExit) else 1
            if not isinstance(code, int):
                code = 1
        else:
            code = 0
        try:
            sys.stderr.flush()
        finally:
            os._exit(code)
    except BaseException as error:
        try:
            print(f"DeckyPluginLauncher child setup failed: {error}", file=sys.stderr, flush=True)
        finally:
            os._exit(1)


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def _poll_children(children: dict[str, _Child]) -> None:
    for child in children.values():
        if child.wait_status is not None:
            continue
        try:
            pid, status = os.waitpid(child.pid, os.WNOHANG)
        except ChildProcessError as error:
            raise RuntimeError(f"lost the wait status for owned worker {child.pid}") from error
        except InterruptedError:
            continue
        else:
            if pid:
                child.wait_status = status
        if child.wait_status is not None:
            _signal_group(child.pid, signal.SIGKILL)
            if child.gate_fd is not None:
                os.close(child.gate_fd)
                child.gate_fd = None


def _request(arguments: dict[str, Any], key: str, expected: type) -> Any:
    value = arguments.get(key)
    if not isinstance(value, expected) or (expected is str and not value):
        raise ValueError(f"{key} must be a non-empty {expected.__name__}")
    return value


def _assert_fork_safe() -> None:
    with os.scandir("/proc/self/task") as tasks:
        if sum(1 for _ in tasks) != 1:
            raise RuntimeError("the shared Python launcher must be single-threaded before fork")


def _start_child(
    request: dict[str, Any], bootstrap: str, children: dict[str, _Child]
) -> dict[str, Any]:
    identifier = _request(request, "id", str)
    if identifier in children:
        raise ValueError(f"worker id is already active: {identifier}")
    requested_bootstrap = request.get("bootstrap", bootstrap)
    if requested_bootstrap != bootstrap:
        raise ValueError("worker bootstrap does not match launcher bootstrap")
    arguments: dict[str, Any] = {}
    for key in ("plugin_root", "plugin_directory", "socket", "homebrew", "user"):
        arguments[key] = _request(request, key, str)
    arguments["approved_launch"] = _request(request, "approved_launch", dict)
    arguments["bootstrap"] = bootstrap
    arguments["system_pythonpaths"] = _shared_system_pythonpaths(arguments["user"])

    _assert_fork_safe()
    gc.freeze()
    gate_read, gate_write = os.pipe2(os.O_CLOEXEC)
    parent_pid = os.getpid()
    try:
        pid = os.fork()
    except BaseException:
        os.close(gate_read)
        os.close(gate_write)
        raise
    if pid == 0:
        inherited_fds = (_wake_read, _wake_write) + tuple(
            child.gate_fd for child in children.values() if child.gate_fd is not None
        )
        _worker_child(gate_read, gate_write, arguments, parent_pid, inherited_fds)
        os._exit(1)

    os.close(gate_read)
    try:
        os.setpgid(pid, pid)
    except OSError:
        os.close(gate_write)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        raise
    children[identifier] = _Child(identifier, pid, gate_write)
    return {"ok": True, "op": "forked", "id": identifier, "pid": pid}


def _handle_request(
    request: Any, bootstrap: str, children: dict[str, _Child]
) -> tuple[dict[str, Any], bool]:
    if not isinstance(request, dict):
        raise ValueError("control request must be a JSON object")
    request = cast(dict[str, Any], request)
    operation = request.get("op")
    if operation == "start":
        return _start_child(request, bootstrap, children), False

    if operation == "shutdown":
        return {"ok": True, "op": "shutdown"}, True

    identifier = _request(request, "id", str)
    child = children.get(identifier)
    if child is None:
        raise ValueError(f"unknown worker id: {identifier}")

    if operation == "release":
        if child.gate_fd is None or child.wait_status is not None:
            raise ValueError(f"worker {identifier} cannot be released")
        os.write(child.gate_fd, b"\x01")
        os.close(child.gate_fd)
        child.gate_fd = None
        return {"ok": True, "op": "released", "id": identifier}, False

    if operation == "status":
        _poll_children(children)
        if child.wait_status is None:
            return {"ok": True, "op": "status", "id": identifier, "running": True}, False
        return {
            "ok": True,
            "op": "status",
            "id": identifier,
            "running": False,
            "wait_status": child.wait_status,
        }, False

    if operation == "forget":
        _poll_children(children)
        if child.wait_status is None:
            raise ValueError(f"worker {identifier} is still running")
        del children[identifier]
        return {"ok": True, "op": "forgotten", "id": identifier}, False


    raise ValueError(f"unknown control operation: {operation!r}")


def _stop_children(children: dict[str, _Child]) -> None:
    for child in children.values():
        _signal_group(child.pid, signal.SIGTERM)
    deadline = time.monotonic() + _CHILD_SHUTDOWN_TIMEOUT
    while time.monotonic() < deadline:
        _poll_children(children)
        if all(child.wait_status is not None for child in children.values()):
            break
        time.sleep(_POLL_INTERVAL)
    for child in children.values():
        _signal_group(child.pid, signal.SIGKILL)
        if child.gate_fd is not None:
            os.close(child.gate_fd)
            child.gate_fd = None
    deadline = time.monotonic() + _CHILD_SHUTDOWN_TIMEOUT
    while time.monotonic() < deadline:
        _poll_children(children)
        if all(child.wait_status is not None for child in children.values()):
            break
        time.sleep(_POLL_INTERVAL)


def _request_stop(_signum: int, _frame: Any) -> None:
    global _stopping
    _stopping = True


def _request_child_poll(_signum: int, _frame: Any) -> None:
    global _children_changed
    _children_changed = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the shared Decky plugin fork launcher")
    parser.add_argument("--bootstrap", choices=("full", "lean"), default="lean")
    args = parser.parse_args(argv)
    global _wake_read, _wake_write, _children_changed, _stopping
    _stopping = False
    _children_changed = False
    _wake_read, _wake_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    signal.set_wakeup_fd(_wake_write)
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGCHLD, _request_child_poll)
    children: dict[str, _Child] = {}
    try:
        worker.prepare_runtime(args.bootstrap)
        _assert_fork_safe()
        _write_message({"ok": True, "op": "ready", "pid": os.getpid()})

        while not _stopping:
            if _children_changed:
                _poll_children(children)
                _children_changed = False
            readable, _, _ = select.select([sys.stdin, _wake_read], [], [])
            if _wake_read in readable:
                os.read(_wake_read, 4096)
            if _stopping:
                break
            if sys.stdin not in readable:
                continue
            line = sys.stdin.buffer.readline()
            if not line:
                break
            try:
                request = json.loads(line)
                response, shutdown = _handle_request(request, args.bootstrap, children)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                _write_message({"ok": False, "error": str(error)})
                continue
            _write_message(response)
            if shutdown:
                break
        return 0
    except BrokenPipeError:
        return 1
    except BaseException as error:
        print(f"DeckyPluginLauncher: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        signal.set_wakeup_fd(-1)
        os.close(_wake_read)
        os.close(_wake_write)
        _stop_children(children)


if __name__ == "__main__":
    raise SystemExit(main())
