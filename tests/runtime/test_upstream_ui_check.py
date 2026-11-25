import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "runtime"))
import upstream_ui_check


def make_lockfile(deps: dict[str, tuple[str, str]]) -> str:
    lines = [
        "lockfileVersion: '9.0'",
        "",
        "importers:",
        "",
        "  .:",
        "    dependencies:",
    ]
    for name, (specifier, version) in deps.items():
        key = f"'{name}'" if name.startswith("@") else name
        lines.append(f"      {key}:")
        lines.append(f"        specifier: {specifier}")
        lines.append(f"        version: {version}")
    lines += [
        "    devDependencies:",
        "      '@decky/api':",
        "        specifier: ^1.1.3",
        "        version: 1.1.3",
        "",
        "packages:",
        "",
    ]
    seen_versions = set()
    for _, (_, version) in deps.items():
        clean = version.strip("'\"")
        if clean.startswith("@decky/ui@"):
            clean = clean.removeprefix("@decky/ui@")
        clean = clean.split("(", 1)[0]
        if clean not in seen_versions:
            seen_versions.add(clean)
            lines.append(f"  '@decky/ui@{clean}':")
            lines.append("    resolution: {integrity: sha512-example}")
            lines.append("")
    return "\n".join(lines) + "\n"


DUAL_LOCK = make_lockfile({
    "@decky/ui": ("^5.0.0", "5.0.0"),
    "@decky/ui-legacy": ("npm:@decky/ui@4.16.4", "'@decky/ui@4.16.4'"),
    "@decky/ui-v5": ("npm:@decky/ui@5.0.0", "'@decky/ui@5.0.0'"),
})

UPSTREAM_MAIN_LOCK = make_lockfile({
    "@decky/ui": ("^5.0.0", "5.0.0"),
})

UPSTREAM_RELEASE_LOCK = make_lockfile({
    "@decky/ui": ("^4.16.4", "4.16.4"),
})


class ParseLockedUiVersionsTests(unittest.TestCase):
    def test_parses_dual_client_entries_and_strips_alias_prefixes(self):
        parsed = upstream_ui_check.parse_locked_ui_versions(DUAL_LOCK)
        self.assertEqual(parsed, {
            "@decky/ui": "5.0.0",
            "@decky/ui-legacy": "4.16.4",
            "@decky/ui-v5": "5.0.0",
        })

    def test_parses_single_client_entry_and_strips_peer_suffix(self):
        lock = make_lockfile({
            "@decky/ui": ("^5.0.2", "5.0.2(react@19.1.1)"),
        })
        self.assertEqual(upstream_ui_check.parse_locked_ui_versions(lock), {"@decky/ui": "5.0.2"})

    def test_raises_when_decky_ui_is_missing(self):
        lock = "lockfileVersion: '9.0'\n\nimporters:\n  .:\n    dependencies:\n      filesize:\n        specifier: ^10.1.2\n        version: 10.1.2\n"
        with self.assertRaises(upstream_ui_check.LockfileError):
            upstream_ui_check.parse_locked_ui_versions(lock)


class CompareUiVersionsTests(unittest.TestCase):
    def test_dual_client_exact_match_is_in_sync(self):
        report = upstream_ui_check.compare_ui_versions(
            ours={"@decky/ui": "5.0.0", "@decky/ui-v5": "5.0.0", "@decky/ui-legacy": "4.16.4"},
            upstream_main="5.0.0",
            upstream_release_tag="v3.3.1",
            upstream_release="4.16.4",
            library_release="v5.0.2",
        )
        self.assertTrue(report["in_sync"])
        self.assertEqual(report["mode"], "dual-client")
        self.assertEqual(report["mismatches"], [])

    def test_dual_client_alerts_strictly_when_ui_v5_is_ahead_of_upstream_main(self):
        report = upstream_ui_check.compare_ui_versions(
            ours={"@decky/ui": "5.0.0", "@decky/ui-v5": "5.0.1", "@decky/ui-legacy": "4.16.4"},
            upstream_main="5.0.0",
            upstream_release_tag="v3.3.1",
            upstream_release="4.16.4",
            library_release="v5.0.2",
        )
        self.assertFalse(report["in_sync"])
        self.assertEqual(len(report["mismatches"]), 1)
        self.assertIn("@decky/ui-v5", report["mismatches"][0])

    def test_dual_client_alerts_when_upstream_main_bumps(self):
        report = upstream_ui_check.compare_ui_versions(
            ours={"@decky/ui": "5.0.0", "@decky/ui-v5": "5.0.0", "@decky/ui-legacy": "4.16.4"},
            upstream_main="5.0.2",
            upstream_release_tag="v3.3.1",
            upstream_release="4.16.4",
            library_release="v5.0.2",
        )
        self.assertFalse(report["in_sync"])
        self.assertEqual(report["issue_title"], "Upstream decky-loader locks @decky/ui 5.0.2")
        self.assertEqual(len(report["mismatches"]), 2)

    def test_dual_client_alerts_when_upstream_stable_release_changes(self):
        report = upstream_ui_check.compare_ui_versions(
            ours={"@decky/ui": "5.0.0", "@decky/ui-v5": "5.0.0", "@decky/ui-legacy": "4.16.4"},
            upstream_main="5.0.0",
            upstream_release_tag="v4.0.0",
            upstream_release="5.0.0",
            library_release="v5.0.2",
        )
        self.assertFalse(report["in_sync"])
        self.assertEqual(report["issue_title"], "Upstream decky-loader v4.0.0 locks @decky/ui 5.0.0")
        self.assertEqual(len(report["mismatches"]), 1)
        self.assertIn("@decky/ui-legacy", report["mismatches"][0])

    def test_dual_client_alerts_when_only_one_alias_is_present(self):
        report = upstream_ui_check.compare_ui_versions(
            ours={"@decky/ui": "5.0.0", "@decky/ui-v5": "5.0.0"},
            upstream_main="5.0.0",
            upstream_release_tag="v3.3.1",
            upstream_release="4.16.4",
            library_release="v5.0.2",
        )
        self.assertFalse(report["in_sync"])
        self.assertTrue(any("@decky/ui-legacy" in item for item in report["mismatches"]))

    def test_single_client_exact_match_and_mismatch(self):
        in_sync = upstream_ui_check.compare_ui_versions(
            ours={"@decky/ui": "5.0.0"},
            upstream_main="5.0.0",
            upstream_release_tag="v4.0.0",
            upstream_release="5.0.0",
            library_release="v5.0.2",
        )
        self.assertTrue(in_sync["in_sync"])
        self.assertEqual(in_sync["mode"], "single-client")

        drifted = upstream_ui_check.compare_ui_versions(
            ours={"@decky/ui": "5.0.0"},
            upstream_main="5.0.2",
            upstream_release_tag="v4.0.0",
            upstream_release="5.0.0",
            library_release="v5.0.2",
        )
        self.assertFalse(drifted["in_sync"])
        self.assertEqual(drifted["issue_title"], "Upstream decky-loader locks @decky/ui 5.0.2")


class RenderAndCliTests(unittest.TestCase):
    def test_markdown_renders_valid_single_line_table_rows_and_mismatches(self):
        report = upstream_ui_check.compare_ui_versions(
            ours={"@decky/ui": "5.0.0", "@decky/ui-v5": "5.0.0", "@decky/ui-legacy": "4.16.4"},
            upstream_main="5.0.2",
            upstream_release_tag="v3.3.1",
            upstream_release="4.16.4",
            library_release="v5.0.2",
        )
        commits = [
            {
                "sha": "24082fe63cdcdf41993f9e1430d1405152a2c930",
                "html_url": "https://github.com/SteamDeckHomebrew/decky-loader/commit/24082fe",
                "commit": {"committer": {"date": "2026-11-22T03:00:00Z"}, "message": "bump ui\n\nmore details"},
            }
        ]
        body = upstream_ui_check.render_issue_markdown(report, "beallio/decky-loader", commits)
        table_lines = [line for line in body.splitlines() if line.startswith("|")]
        self.assertTrue(all(line.endswith("|") for line in table_lines))
        self.assertIn("| This fork `@decky/ui` (types) | 5.0.0 |", body)
        self.assertIn("| This fork `@decky/ui-v5` (Beta / ESM) | 5.0.0 |", body)
        self.assertIn("| This fork `@decky/ui-legacy` (Stable / Webpack) | 4.16.4 |", body)
        self.assertIn("| Upstream `main` | 5.0.2 |", body)
        self.assertIn("| Upstream release v3.3.1 | 4.16.4 |", body)
        self.assertIn("`24082fe`", body)
        self.assertIn("docs/development.md#ui-library", body)

    def test_cli_reads_lockfiles_and_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or "/tmp") as tmp:
            root = Path(tmp)
            ours = root / "ours.yaml"
            main = root / "main.yaml"
            release = root / "release.yaml"
            markdown = root / "out.md"
            ours.write_text(DUAL_LOCK)
            main.write_text(UPSTREAM_MAIN_LOCK)
            release.write_text(UPSTREAM_RELEASE_LOCK)

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "runtime" / "upstream_ui_check.py"),
                    "--ours-lock", str(ours),
                    "--upstream-main-lock", str(main),
                    "--upstream-release-lock", str(release),
                    "--upstream-release-tag", "v3.3.1",
                    "--library-release", "v5.0.2",
                    "--repo", "beallio/decky-loader",
                    "--markdown", str(markdown),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(result.stdout)
            self.assertTrue(payload["in_sync"])
            self.assertIn("5.0.0", payload["close_comment"])
            self.assertIn("4.16.4", payload["close_comment"])
            self.assertTrue(markdown.exists())


if __name__ == "__main__":
    unittest.main()
