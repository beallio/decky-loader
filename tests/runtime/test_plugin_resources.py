"""Per-plugin CPU and memory attribution for `install.py status`."""
import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER_PATH = REPO_ROOT / "backend-rust" / "install.py"
sys.path.insert(0, str(INSTALLER_PATH.parent))
_SPEC = importlib.util.spec_from_file_location("rust_runtime_resource_installer", INSTALLER_PATH)
installer = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = installer
_SPEC.loader.exec_module(installer)

# Real launchers show one space-joined argv entry after the worker sets its process title.
LAUNCHER = ("/opt/decky/worker --launcher --bootstrap abc123",)
EXEC_ARGS = (
    "/opt/decky/worker", "--plugin-root", "/home/deck/homebrew/plugins",
    "--plugin-directory", "decky-weather", "--socket", "/run/s", "--bootstrap", "abc123",
)


def stat_line(pid, comm, ppid, utime, stime, start, state="S"):
    fields = [state, str(ppid), str(pid), str(pid), "0", "-1", "4194560", "10", "0", "0", "0",
              str(utime), str(stime), "0", "0", "20", "0", "3", "0", str(start), "123456", "789"]
    return f"{pid} ({comm}) " + " ".join(fields) + "\n"


SMAPS_ROLLUP = """\
55d1c8a00000-7ffd1d9ff000 ---p 00000000 00:00 0                          [rollup]
Rss:               31204 kB
Pss:               12345 kB
Pss_Dirty:          9000 kB
Shared_Clean:       8000 kB
Shared_Dirty:          0 kB
Private_Clean:      4000 kB
Private_Dirty:      7777 kB
Private_Hugetlb:       0 kB
Referenced:        30000 kB
"""


class ParsingTests(unittest.TestCase):
    def test_stat_with_spaces_and_parentheses_in_comm(self):
        sample = installer._parse_stat_sample(stat_line(42, "we (ird) na me", 7, 250, 50, 9999))
        self.assertEqual(sample.comm, "we (ird) na me")
        self.assertEqual((sample.ppid, sample.cpu_ticks, sample.start_ticks), (7, 300, 9999))

    def test_stat_rejects_truncated_input(self):
        with self.assertRaises(ValueError):
            installer._parse_stat_sample("42 (short) S 1 2")
        with self.assertRaises(ValueError):
            installer._parse_stat_sample("no comm here")

    def test_smaps_rollup_reads_pss_and_private_dirty_only(self):
        self.assertEqual(
            installer._parse_smaps_rollup(SMAPS_ROLLUP),
            {"pss_kb": 12345, "private_dirty_kb": 7777},
        )

    def test_smaps_rollup_missing_fields_are_none(self):
        self.assertEqual(
            installer._parse_smaps_rollup("Rss: 5 kB\nPss: lots kB\n"),
            {"pss_kb": None, "private_dirty_kb": None},
        )
        self.assertEqual(installer._parse_smaps_rollup(""), {"pss_kb": None, "private_dirty_kb": None})

    def test_cmdline_drops_padding(self):
        self.assertEqual(installer._parse_cmdline(b"My Plugin (main.py)\0\0\0\0"), ("My Plugin (main.py)",))
        self.assertEqual(installer._parse_cmdline(b"a\0b\0"), ("a", "b"))
        self.assertEqual(installer._parse_cmdline(b""), ())


class IdentityTests(unittest.TestCase):
    def test_title_gives_plugin_name(self):
        self.assertEqual(installer._worker_identity(("Weather (main.py)",)), ("Weather", "title"))

    def test_title_with_parentheses_in_plugin_name(self):
        self.assertEqual(installer._worker_identity(("Foo (beta) (main.py)",)), ("Foo (beta)", "title"))

    def test_exec_arguments_give_plugin_directory_before_title_is_set(self):
        self.assertEqual(installer._worker_identity(EXEC_ARGS), ("decky-weather", "plugin-directory"))

    def test_other_command_lines_have_no_identity(self):
        self.assertIsNone(installer._worker_identity(LAUNCHER))
        self.assertIsNone(installer._worker_identity(("ffmpeg", "-i", "in.mp4")))
        self.assertIsNone(installer._worker_identity(()))


def info(pid, ppid, argv, comm="x"):
    return installer._ProcessInfo(pid, ppid, comm, tuple(argv))


class AttributionTests(unittest.TestCase):
    def test_fork_mode_workers_and_their_children(self):
        infos = {
            10: info(10, 1, ("/opt/decky/backend",)),
            11: info(11, 10, LAUNCHER),
            12: info(12, 11, ("Weather (main.py)",)),
            13: info(13, 11, ("Music Player (main.py)",)),
            14: info(14, 12, ("curl", "https://example.invalid")),
            15: info(15, 14, ("sh", "-c", "sleep 9")),
            16: info(16, 11, LAUNCHER),
        }
        got = installer._attribute_processes(infos, 10)
        self.assertEqual(got[10], ("backend", None, None))
        self.assertEqual(got[11], ("shared launcher", None, None))
        self.assertEqual(got[12], ("plugin worker", "Weather", "title"))
        self.assertEqual(got[13], ("plugin worker", "Music Player", "title"))
        self.assertEqual(got[14], ("plugin child", "Weather", "title"))
        self.assertEqual(got[15], ("plugin child", "Weather", "title"))
        self.assertEqual(got[16], ("plugin worker", installer._UNKNOWN_PLUGIN, "unknown"))

    def test_exec_mode_workers_are_children_of_the_backend(self):
        infos = {
            10: info(10, 1, ("/opt/decky/backend",)),
            20: info(20, 10, ("Weather (main.py)",)),
            21: info(21, 10, EXEC_ARGS),
            22: info(22, 21, ("node", "server.js")),
            23: info(23, 10, ("git", "fetch")),
        }
        got = installer._attribute_processes(infos, 10)
        self.assertEqual(got[20], ("plugin worker", "Weather", "title"))
        self.assertEqual(got[21], ("plugin worker", "decky-weather", "plugin-directory"))
        self.assertEqual(got[22], ("plugin child", "decky-weather", "plugin-directory"))
        self.assertEqual(got[23], ("backend child", None, None))

    def test_orphan_with_parent_outside_the_service_is_unattributed(self):
        infos = {
            10: info(10, 1, ("/opt/decky/backend",)),
            30: info(30, 1, ("Weather (main.py)",)),
            31: info(31, 30, ("helper",)),
        }
        got = installer._attribute_processes(infos, 10)
        self.assertEqual(got[30], ("unattributed", None, None))
        self.assertEqual(got[31], ("unattributed", None, None))

    def test_parent_cycle_does_not_loop(self):
        infos = {5: info(5, 6, ("a",)), 6: info(6, 5, ("b",))}
        got = installer._attribute_processes(infos, 10)
        self.assertEqual({role for role, _, _ in got.values()}, {"unattributed"})


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.infos = {
            10: info(10, 1, ("/opt/decky/backend",), "backend"),
            11: info(11, 10, LAUNCHER, "worker"),
            12: info(12, 11, ("Weather (main.py)",), "Weather"),
            13: info(13, 12, ("curl",), "curl"),
            14: info(14, 11, ("Music (main.py)",), "Music"),
        }
        self.samples = {
            10: installer._StatSample("backend", 1, 1000, 1),
            11: installer._StatSample("worker", 10, 100, 2),
            12: installer._StatSample("Weather", 11, 600, 3),
            13: installer._StatSample("curl", 12, 150, 4),
            14: installer._StatSample("Music", 11, 50, 5),
        }
        self.memory = {
            10: {"pss_kb": 1000, "private_dirty_kb": 500},
            11: {"pss_kb": 2000, "private_dirty_kb": 100},
            12: {"pss_kb": 3000, "private_dirty_kb": 1000},
            13: {"pss_kb": 400, "private_dirty_kb": 40},
            # 14 is unreadable
        }

    def report(self, previous):
        return installer._resource_report(self.infos, self.samples, previous, self.memory, 10, 100, 2.0)

    def test_totals_percent_and_unreadable_memory(self):
        previous = {
            12: installer._StatSample("Weather", 11, 400, 3),   # +200 ticks / 100 Hz / 2 s = 100 %
            13: installer._StatSample("curl", 12, 130, 4),      # +20 ticks = 10 %
            14: installer._StatSample("Music", 11, 50, 5),      # idle
        }
        report = self.report(previous)
        by_pid = {row["pid"]: row for row in report["processes"]}
        self.assertEqual(by_pid[12]["cpu_seconds_total"], 6.0)
        self.assertEqual(by_pid[12]["cpu_percent"], 100.0)
        self.assertIsNone(by_pid[10]["cpu_percent"])
        plugins = {entry["name"]: entry for entry in report["plugins"]}
        self.assertEqual(set(plugins), {"Weather", "Music"})
        self.assertEqual(plugins["Weather"]["pids"], [12, 13])
        self.assertEqual(plugins["Weather"]["cpu_percent"], 110.0)
        self.assertEqual(plugins["Weather"]["cpu_seconds_total"], 7.5)
        self.assertEqual((plugins["Weather"]["pss_kb"], plugins["Weather"]["private_dirty_kb"]), (3400, 1040))
        self.assertEqual(plugins["Music"]["pss_kb"], None)
        self.assertEqual(plugins["Music"]["memory_unreadable_processes"], 1)
        service = report["service"]
        self.assertEqual(service["process_count"], 5)
        self.assertEqual(service["pss_kb"], 6400)
        self.assertEqual(service["cpu_seconds_total"], 19.0)
        self.assertEqual(service["memory_unreadable_processes"], 1)
        self.assertEqual(report["by_role"]["backend"]["pids"], [10])
        self.assertEqual(report["by_role"]["shared launcher"]["pids"], [11])

    def test_reused_pid_has_no_percent(self):
        reused = {12: installer._StatSample("Weather", 11, 10, 999)}
        self.assertIsNone({row["pid"]: row for row in self.report(reused)["processes"]}[12]["cpu_percent"])


if __name__ == "__main__":
    unittest.main()
