#!/usr/bin/env python3
"""Compare locked @decky/ui versions between this fork and upstream Decky Loader.

Supports both the fork's dual-client setup (@decky/ui types, @decky/ui-v5 for
Steam Beta / ESM, and @decky/ui-legacy for Steam Stable / Webpack) and the
single-client setup (@decky/ui only). Every active track is checked with strict
exact equality so any upstream or local version drift opens or updates the
tracking issue.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

UI_PACKAGES = ('@decky/ui', '@decky/ui-v5', '@decky/ui-legacy')
PACKAGE_LABELS = {
    '@decky/ui': 'This fork `@decky/ui` (types)',
    '@decky/ui-v5': 'This fork `@decky/ui-v5` (Beta / ESM)',
    '@decky/ui-legacy': 'This fork `@decky/ui-legacy` (Stable / Webpack)',
}


class LockfileError(Exception):
    pass


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _normalize_ui_version(raw_version: str) -> str:
    cleaned = _strip_quotes(raw_version)
    if cleaned.startswith('npm:'):
        cleaned = cleaned[4:]
    if cleaned.startswith('@decky/ui@'):
        cleaned = cleaned[len('@decky/ui@'):]
    cleaned = cleaned.split('(', 1)[0].strip()
    if not cleaned:
        raise LockfileError(f'empty @decky/ui version in lockfile entry: {raw_version!r}')
    return cleaned


def parse_locked_ui_versions(lockfile_text: str) -> dict[str, str]:
    """Extract resolved @decky/ui dependency versions from importers['.'].dependencies."""
    in_importers = False
    in_root_importer = False
    in_dependencies = False
    current_package: str | None = None
    versions: dict[str, str] = {}

    for raw_line in lockfile_text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith('#'):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(' '))
        stripped = raw_line.strip()

        if indent == 0:
            in_importers = stripped == 'importers:'
            in_root_importer = False
            in_dependencies = False
            current_package = None
            continue

        if not in_importers:
            continue

        if indent == 2:
            key = _strip_quotes(stripped[:-1]) if stripped.endswith(':') else ''
            in_root_importer = key == '.'
            in_dependencies = False
            current_package = None
            continue

        if not in_root_importer:
            continue

        if indent == 4:
            in_dependencies = stripped == 'dependencies:'
            current_package = None
            continue

        if not in_dependencies:
            continue

        if indent == 6 and stripped.endswith(':'):
            dep_name = _strip_quotes(stripped[:-1])
            current_package = dep_name if dep_name in UI_PACKAGES else None
            continue

        if indent == 8 and current_package is not None and stripped.startswith('version:'):
            raw_version = stripped.split(':', 1)[1].strip()
            versions[current_package] = _normalize_ui_version(raw_version)
            current_package = None

    if '@decky/ui' not in versions:
        raise LockfileError("could not find '@decky/ui' under importers['.'].dependencies")
    return versions


def compare_ui_versions(
    ours: dict[str, str],
    upstream_main: str,
    upstream_release_tag: str,
    upstream_release: str,
    library_release: str,
) -> dict:
    """Strictly compare our locked @decky/ui version(s) against upstream main and latest release."""
    dual_client = '@decky/ui-v5' in ours or '@decky/ui-legacy' in ours
    mismatches: list[str] = []
    main_mismatch = False

    if dual_client:
        mode = 'dual-client'
        if ours.get('@decky/ui') != upstream_main:
            main_mismatch = True
            mismatches.append(
                f"`@decky/ui` (types) locks `{ours.get('@decky/ui', 'missing')}`, expected upstream `main` `{upstream_main}`."
            )
        if '@decky/ui-v5' not in ours:
            main_mismatch = True
            mismatches.append(f"`@decky/ui-v5` (Beta / ESM) is missing; expected upstream `main` `{upstream_main}`.")
        elif ours['@decky/ui-v5'] != upstream_main:
            main_mismatch = True
            mismatches.append(
                f"`@decky/ui-v5` (Beta / ESM) locks `{ours['@decky/ui-v5']}`, expected upstream `main` `{upstream_main}`."
            )
        if '@decky/ui-legacy' not in ours:
            mismatches.append(
                f"`@decky/ui-legacy` (Stable / Webpack) is missing; expected upstream release `{upstream_release_tag}` `{upstream_release}`."
            )
        elif ours['@decky/ui-legacy'] != upstream_release:
            mismatches.append(
                f"`@decky/ui-legacy` (Stable / Webpack) locks `{ours['@decky/ui-legacy']}`, "
                f"expected upstream release `{upstream_release_tag}` `{upstream_release}`."
            )
        close_comment = (
            f"All tracks now strictly match upstream: `@decky/ui` and `@decky/ui-v5` lock `{upstream_main}` "
            f"(upstream `main`), and `@decky/ui-legacy` locks `{upstream_release}` (upstream `{upstream_release_tag}`)."
        )
    else:
        mode = 'single-client'
        fork_ui = ours.get('@decky/ui', 'missing')
        if fork_ui != upstream_main:
            main_mismatch = True
            mismatches.append(f"`@decky/ui` locks `{fork_ui}`, expected upstream `main` `{upstream_main}`.")
        if fork_ui != upstream_release:
            mismatches.append(
                f"`@decky/ui` locks `{fork_ui}`, expected upstream release `{upstream_release_tag}` `{upstream_release}`."
            )
        close_comment = f"Upstream `main` and `{upstream_release_tag}` now lock `@decky/ui` `{upstream_main}`, the same as this fork."

    if main_mismatch or not mismatches:
        issue_title = f"Upstream decky-loader locks @decky/ui {upstream_main}"
    else:
        issue_title = f"Upstream decky-loader {upstream_release_tag} locks @decky/ui {upstream_release}"

    return {
        'mode': mode,
        'ours': ours,
        'upstream_main': upstream_main,
        'upstream_release_tag': upstream_release_tag,
        'upstream_release': upstream_release,
        'library_release': library_release,
        'in_sync': not mismatches,
        'mismatches': mismatches,
        'issue_title': issue_title,
        'close_comment': close_comment,
    }


def render_issue_markdown(report: dict, repo: str, recent_commits: list[dict] | None = None) -> str:
    lines = [
        'Upstream Decky Loader locks a different `@decky/ui` version than this fork.',
        '',
        '| Where | `@decky/ui` |',
        '|---|---|',
    ]
    ours = report['ours']
    if report['mode'] == 'dual-client':
        for package in UI_PACKAGES:
            if package in ours:
                lines.append(f"| {PACKAGE_LABELS[package]} | {ours[package]} |")
    else:
        lines.append(f"| This fork (`frontend/pnpm-lock.yaml`) | {ours['@decky/ui']} |")

    lines += [
        f"| Upstream `main` | {report['upstream_main']} |",
        f"| Upstream release {report['upstream_release_tag']} | {report['upstream_release']} |",
        f"| Newest decky-frontend-lib release | {report['library_release']} |",
    ]

    if report['mismatches']:
        lines += ['', 'Mismatches:', '']
        lines += [f'- {item}' for item in report['mismatches']]

    if recent_commits:
        lines += ['', 'Recent upstream commits that change `frontend/pnpm-lock.yaml`:', '']
        for commit in recent_commits:
            sha = str(commit.get('sha', ''))[:7]
            url = commit.get('html_url', '')
            info = commit.get('commit', {})
            date = str(info.get('committer', {}).get('date', ''))[:10]
            subject = str(info.get('message', '')).splitlines()[0] if info.get('message') else ''
            lines.append(f'- [`{sha}`]({url}) {date} {subject}')

    lines += [
        '',
        'Plugins use the loader\'s copy of this library at runtime. Before the fork moves to a new version, '
        f'check the [UI library section](https://github.com/{repo}/blob/main/docs/development.md#ui-library) '
        'of the development guide.',
    ]
    return '\n'.join(lines) + '\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ours-lock', type=Path, required=True, help='path to this fork frontend/pnpm-lock.yaml')
    parser.add_argument('--upstream-main-lock', type=Path, required=True, help='path to upstream main pnpm-lock.yaml')
    parser.add_argument('--upstream-release-lock', type=Path, required=True, help='path to upstream release pnpm-lock.yaml')
    parser.add_argument('--upstream-release-tag', required=True, help='upstream latest release tag name')
    parser.add_argument('--library-release', required=True, help='newest decky-frontend-lib release tag name')
    parser.add_argument('--repo', default='beallio/decky-loader', help='GitHub owner/repo for documentation link')
    parser.add_argument('--commits-json', type=Path, help='optional JSON array of recent upstream commits')
    parser.add_argument('--markdown', type=Path, help='optional output path for the issue Markdown body')
    args = parser.parse_args(argv)

    try:
        ours = parse_locked_ui_versions(args.ours_lock.read_text(encoding='utf-8'))
        upstream_main = parse_locked_ui_versions(args.upstream_main_lock.read_text(encoding='utf-8'))['@decky/ui']
        upstream_release = parse_locked_ui_versions(args.upstream_release_lock.read_text(encoding='utf-8'))['@decky/ui']
    except (LockfileError, OSError) as error:
        print(f'upstream_ui_check: {error}', file=sys.stderr)
        return 1

    report = compare_ui_versions(
        ours=ours,
        upstream_main=upstream_main,
        upstream_release_tag=args.upstream_release_tag,
        upstream_release=upstream_release,
        library_release=args.library_release,
    )

    commits = json.loads(args.commits_json.read_text(encoding='utf-8')) if args.commits_json else None
    if args.markdown:
        args.markdown.write_text(render_issue_markdown(report, args.repo, commits), encoding='utf-8')

    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
