import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "runtime"))
import upstream_check

OVERLAY = "backend/decky_loader/overlay.py"
UNTOUCHED_OVERLAY = "backend/decky_loader/untouched.py"
REMOVAL = "frontend/src/removed.ts"
UNCHANGED_REMOVAL = "frontend/src/kept-removed.ts"
OVERLAYS = (OVERLAY, UNTOUCHED_OVERLAY)
REMOVALS = (REMOVAL, UNCHANGED_REMOVAL)
BASE_LINES = [f"line {number}" for number in range(1, 21)]


class UpstreamCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or "/var/tmp/omp-1000")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        self.upstream = self.root / "upstream"
        self.upstream.mkdir()
        self.git(self.upstream, "-c", "init.defaultBranch=main", "init", "--quiet")
        self.write(self.upstream, OVERLAY, "\n".join(BASE_LINES) + "\n")
        self.write(self.upstream, UNTOUCHED_OVERLAY, "untouched\n")
        self.write(self.upstream, REMOVAL, "removed\n")
        self.write(self.upstream, UNCHANGED_REMOVAL, "removed too\n")
        self.write(self.upstream, "frontend/src/other.ts", "other\n")
        self.release(self.upstream, "v1.0.0")
        self.pinned_commit = self.git(self.upstream, "rev-parse", "v1.0.0^{commit}").strip()
        self.fork = self.root / "fork"
        self.git(self.root, "clone", "--quiet", str(self.upstream), str(self.fork))

    def git(self, repo, *arguments):
        return subprocess.check_output(["git", *arguments], cwd=repo, env=self.environment, text=True)

    def write(self, repo, name, text):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def release(self, repo, tag, message="change"):
        self.git(repo, "add", "--all")
        self.git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", message)
        self.git(repo, "tag", tag)

    def edit_line(self, repo, number, text):
        lines = list(BASE_LINES)
        lines[number - 1] = text
        self.write(repo, OVERLAY, "\n".join(lines) + "\n")

    def check(self, pinned="v1.0.0"):
        return upstream_check.check(self.fork, str(self.upstream), pinned, self.pinned_commit, OVERLAYS, REMOVALS)

    def statuses(self, report, key):
        return {item["path"]: item["status"] for item in report[key]}

    def test_independent_changes_merge_cleanly(self):
        self.edit_line(self.fork, 2, "fork change")
        self.release(self.fork, "fork-head")
        self.edit_line(self.upstream, 19, "upstream change")
        self.write(self.upstream, REMOVAL, "removed, then edited\n")
        self.write(self.upstream, "frontend/src/other.ts", "other, edited\n")
        self.write(self.upstream, "backend/decky_loader/added.py", "new\n")
        self.release(self.upstream, "v1.1.0")

        report = self.check()

        self.assertTrue(report["update_available"])
        self.assertEqual(report["latest"], "v1.1.0")
        self.assertEqual(self.statuses(report, "overlays"),
                         {OVERLAY: "clean", UNTOUCHED_OVERLAY: "unchanged-upstream"})
        self.assertEqual(self.statuses(report, "removals"),
                         {REMOVAL: "changed-upstream", UNCHANGED_REMOVAL: "unchanged-upstream"})
        self.assertEqual(self.statuses(report, "other_changes"),
                         {"frontend/src/other.ts": "M", "backend/decky_loader/added.py": "A"})

    def test_overlapping_changes_are_reported_as_conflict(self):
        self.edit_line(self.fork, 5, "fork change")
        self.release(self.fork, "fork-head")
        self.edit_line(self.upstream, 5, "upstream change")
        self.release(self.upstream, "v1.1.0")

        report = self.check()

        self.assertEqual(self.statuses(report, "overlays")[OVERLAY], "conflict")
        self.assertEqual(report["overlays"][0]["conflicts"], 1)

    def test_up_to_date_fork_reports_no_update(self):
        self.edit_line(self.upstream, 3, "upstream change")
        self.release(self.upstream, "v1.1.0")
        self.pinned_commit = self.git(self.upstream, "rev-parse", "v1.1.0^{commit}").strip()

        report = self.check(pinned="v1.1.0")

        self.assertFalse(report["update_available"])
        self.assertNotIn("overlays", report)

    def test_pre_release_tags_are_ignored(self):
        self.edit_line(self.upstream, 3, "pre-release change")
        self.release(self.upstream, "v1.1.0-pre")
        self.release(self.upstream, "v2.0.0-rc.1")
        self.release(self.upstream, "v1.0.1-beta.1")

        report = self.check()

        self.assertFalse(report["update_available"])
        self.assertEqual(report["latest"], "v1.0.0")

    def test_versions_compare_numerically(self):
        self.release(self.upstream, "v1.9.0")
        self.release(self.upstream, "v1.10.0")

        self.assertEqual(self.check()["latest"], "v1.10.0")

    def pin(self, tag):
        self.pinned_commit = self.git(self.upstream, "rev-parse", f"{tag}^{{commit}}").strip()

    def test_prerelease_pin_follows_newer_prereleases_then_stable(self):
        self.release(self.upstream, "v1.1.0-pre1")
        self.pin("v1.1.0-pre1")
        self.assertFalse(self.check(pinned="v1.1.0-pre1")["update_available"])

        self.edit_line(self.upstream, 3, "second prerelease")
        self.release(self.upstream, "v1.1.0-pre2")
        report = self.check(pinned="v1.1.0-pre1")
        self.assertEqual((report["latest"], report["update_available"]), ("v1.1.0-pre2", True))
        self.assertTrue(report["latest_is_prerelease"])
        self.assertEqual(self.statuses(report, "overlays")[OVERLAY], "clean")

        self.release(self.upstream, "v1.1.0")
        report = self.check(pinned="v1.1.0-pre1")
        self.assertEqual((report["latest"], report["latest_is_prerelease"]), ("v1.1.0", False))

    def test_stable_pin_does_not_move_to_prereleases(self):
        self.release(self.upstream, "v1.1.0")
        self.pin("v1.1.0")
        self.release(self.upstream, "v1.2.0-pre1")

        self.assertFalse(self.check(pinned="v1.1.0")["update_available"])

    def test_pin_that_does_not_match_upstream_is_an_error(self):
        self.release(self.upstream, "v1.1.0")
        self.pinned_commit = "0" * 40

        with self.assertRaisesRegex(upstream_check.UpstreamError, "pin records"):
            self.check()

    def test_unreachable_remote_is_an_error(self):
        with self.assertRaises(upstream_check.UpstreamError):
            upstream_check.check(self.fork, str(self.root / "missing"), "v1.0.0", self.pinned_commit,
                                 OVERLAYS, REMOVALS)


if __name__ == "__main__":
    unittest.main()
