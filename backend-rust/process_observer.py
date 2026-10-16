"""Observe owned Linux processes, including zombies omitted by cgroup.procs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Process:
    pid: int
    start_ticks: int
    state: str
    ppid: int
    pgrp: int
    session: int
    comm: str
    cgroups: tuple[str, ...]


def _stat(path: Path) -> tuple[int, str, int, int, int, str]:
    head, separator, tail = path.read_text().rpartition(')')
    if not separator or ' (' not in head:
        raise ValueError(f'invalid process stat: {path}')
    fields = tail.split()
    return (int(fields[19]), fields[0], int(fields[1]), int(fields[2]),
            int(fields[3]), head.partition(' (')[2])


def read_process(pid: int, proc_root: Path = Path('/proc')) -> Process | None:
    directory = proc_root / str(pid)
    try:
        before = _stat(directory / 'stat')
        groups = []
        for line in (directory / 'cgroup').read_text().splitlines():
            hierarchy, separator, path = line.partition('::')
            if separator and hierarchy == '0':
                # A removed cgroup remains named here until its zombies are reaped.
                groups.append(path.removesuffix(' (deleted)'))
        after = _stat(directory / 'stat')
    except (FileNotFoundError, ProcessLookupError):
        return None
    if before[0] != after[0]:
        return None
    return Process(pid, after[0], after[1], after[2], after[3], after[4],
                   after[5], tuple(groups))


class ProcessObserver:
    """Retain PID identities while discovering cgroup members and escaped children.

    Process-group expansion requires a currently owned identity as its anchor.
    A stale PGID by itself must not claim an unrelated, later process group.
    This is a point-in-time observation, not proof about unsampled lifetimes.
    """

    def __init__(self, control_group: str, proc_root: Path = Path('/proc')):
        if not control_group.startswith('/') or control_group == '/' or any(
            part in ('', '.', '..') for part in control_group.split('/')[1:]
        ):
            raise ValueError(f'unsafe owned control group: {control_group!r}')
        self.control_group = control_group
        self.proc_root = proc_root
        self._tracked: dict[int, int] = {}

    def snapshot(self, *, retained: dict[int, int] | None = None) -> dict[int, Process]:
        if retained:
            self._tracked.update(retained)
        records = {}
        for directory in self.proc_root.iterdir():
            if not directory.name.isdecimal():
                continue
            pid = int(directory.name)
            record = read_process(pid, self.proc_root)
            if record is not None:
                records[pid] = record
        owned = {
            pid: record for pid, record in records.items()
            if self._tracked.get(pid) == record.start_ticks or any(
                group == self.control_group or group.startswith(self.control_group + '/')
                for group in record.cgroups
            )
        }
        while True:
            groups = {(record.session, record.pgrp) for record in owned.values()}
            additions = {
                pid: record for pid, record in records.items()
                if pid not in owned and (
                    record.ppid in owned or (record.session, record.pgrp) in groups
                )
            }
            if not additions:
                break
            owned.update(additions)
        self._tracked = {pid: record.start_ticks for pid, record in owned.items()}
        return dict(sorted(owned.items()))


_observers: dict[str, ProcessObserver] = {}


def cgroup_processes(control_group: str | None, *, retained: dict[int, int] | None = None) -> dict[int, Process]:
    if not control_group:
        return {}
    observer = _observers.get(control_group)
    if observer is None:
        observer = ProcessObserver(control_group)
        _observers[control_group] = observer
    return observer.snapshot(retained=retained)
