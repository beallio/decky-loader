"""Ownership boundaries for the Linux process observer."""
import sys
import tempfile
from pathlib import Path
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend-rust"))

from process_observer import ProcessObserver


class ProcessObserverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.proc = Path(self.temporary.name)
        self.group = '/system.slice/decky-probe-unique.service'
        self.observer = ProcessObserver(self.group, self.proc)

    def process(self, pid, *, group=None, ticks=100, state='S', ppid=1, pgrp=None,
                session=None, comm='worker'):
        directory = self.proc / str(pid)
        directory.mkdir(exist_ok=True)
        fields = [state, str(ppid), str(pgrp or pid), str(session or pid)] + ['0'] * 16
        fields[19] = str(ticks)
        (directory / 'stat').write_text(f'{pid} ({comm}) ' + ' '.join(fields) + '\n')
        (directory / 'cgroup').write_text('0::' + (group or self.group) + '\n')

    def test_zombies_survive_deleted_cgroup_and_prefix_neighbor_is_excluded(self):
        self.process(10, state='Z', group=self.group + ' (deleted)')
        self.process(11, group=self.group + '/child')
        self.process(12, group=self.group + '-neighbor')
        snapshot = self.observer.snapshot()
        self.assertEqual(set(snapshot), {10, 11})
        self.assertEqual(snapshot[10].state, 'Z')
        self.assertEqual(snapshot[10].cgroups, (self.group,))

    def test_same_identity_remains_owned_after_migration_and_reparenting(self):
        self.process(10)
        self.process(11, group='/other', ppid=10)
        self.assertEqual(set(self.observer.snapshot()), {10, 11})
        (self.proc / '10' / 'stat').unlink()
        self.process(11, group='/other', ppid=1, state='Z', pgrp=50, session=50)
        snapshot = self.observer.snapshot()
        self.assertEqual(set(snapshot), {11})
        self.assertEqual((snapshot[11].start_ticks, snapshot[11].ppid, snapshot[11].state),
                         (100, 1, 'Z'))

    def test_pid_reuse_and_unanchored_old_process_group_are_not_owned(self):
        self.process(10, pgrp=10, session=10)
        self.assertEqual(set(self.observer.snapshot()), {10})
        self.process(10, group='/other', ticks=200, pgrp=20, session=20)
        self.process(11, group='/other', pgrp=10, session=10)
        self.assertEqual(self.observer.snapshot(), {})

    def test_process_group_requires_matching_owned_session_anchor(self):
        self.process(10, pgrp=10, session=10)
        self.process(11, group='/other', pgrp=10, session=10)
        self.process(12, group='/other', pgrp=10, session=20)
        snapshot = self.observer.snapshot()
        self.assertEqual(set(snapshot), {10, 11})
        self.assertEqual((snapshot[11].pgrp, snapshot[11].session), (10, 10))

    def test_stat_comm_with_spaces_parentheses_and_newline_preserves_identity(self):
        self.process(10, comm='plugin ) name\n(', ticks=999, ppid=42)
        record = self.observer.snapshot()[10]
        self.assertEqual((record.start_ticks, record.ppid, record.comm),
                         (999, 42, 'plugin ) name\n('))


    def test_restored_identity_rediscovers_escaped_descendants(self):
        self.process(10, group='/escaped', ticks=100)
        self.process(11, group='/escaped-child', ppid=10)
        self.process(12, group='/unrelated')
        snapshot = self.observer.snapshot(retained={10: 100})
        self.assertEqual(set(snapshot), {10, 11})
        self.assertEqual((snapshot[10].start_ticks, snapshot[11].ppid), (100, 10))

    def test_restored_pid_epoch_does_not_claim_reused_pid_or_its_children(self):
        self.process(10, group='/unrelated', ticks=200)
        self.process(11, group='/unrelated', ppid=10)
        self.assertEqual(self.observer.snapshot(retained={10: 100}), {})


if __name__ == '__main__':
    unittest.main()
