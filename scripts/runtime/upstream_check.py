#!/usr/bin/env python3
"""Compare the pinned SDK tag with the newest stable upstream release.

The check uses git only. For each fork overlay it runs a three-way merge of the
pinned tag (base), fork HEAD (ours), and the new tag (theirs). It prints a JSON
report on stdout and appends a Markdown summary to $GITHUB_STEP_SUMMARY.
It exits non-zero only on errors; a newer tag is a normal result.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from prepare_bundle import BUILD_CONFIG, REPO, SDK_OVERLAYS, SDK_REMOVALS

DEFAULT_REMOTE = 'https://github.com/SteamDeckHomebrew/decky-loader.git'
STABLE_TAG = re.compile(r'v(\d+)\.(\d+)\.(\d+)')
TAKEN_AS_IS = ('backend/decky_loader', 'frontend')
MARKDOWN_LIST_LIMIT = 100


class UpstreamError(Exception):
    pass


def git(repo: Path, *arguments: str, ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    result = subprocess.run(['git', *arguments], cwd=repo, capture_output=True)
    if result.returncode not in ok:
        detail = result.stderr.decode(errors='replace').strip()
        raise UpstreamError(f'git {arguments[0]} failed ({result.returncode}): {detail}')
    return result


def version_key(tag: str) -> tuple[int, int, int] | None:
    match = STABLE_TAG.fullmatch(tag)
    return tuple(int(part) for part in match.groups()) if match else None


def newest_stable_tag(remote: str) -> str:
    listing = git(Path.cwd(), 'ls-remote', '--tags', '--refs', remote).stdout.decode()
    tags = [line.split('refs/tags/', 1)[1] for line in listing.splitlines() if 'refs/tags/' in line]
    stable = [tag for tag in tags if version_key(tag)]
    if not stable:
        raise UpstreamError(f'no stable vX.Y.Z tag found at {remote}')
    return max(stable, key=version_key)


def blob_id(repo: Path, revision: str, path: str) -> str | None:
    result = git(repo, 'rev-parse', '--verify', '--quiet', f'{revision}:{path}', ok=(0, 1))
    return result.stdout.decode().strip() if result.returncode == 0 else None


def blob(repo: Path, revision: str, path: str) -> bytes | None:
    if blob_id(repo, revision, path) is None:
        return None
    return git(repo, 'cat-file', 'blob', f'{revision}:{path}').stdout


def merge_overlay(fork: Path, upstream: Path, scratch: Path, base_tag: str, new_tag: str, path: str) -> dict:
    base_id = blob_id(upstream, base_tag, path)
    theirs_id = blob_id(upstream, new_tag, path)
    if base_id == theirs_id:
        return {'path': path, 'status': 'unchanged-upstream'}
    if theirs_id is None:
        return {'path': path, 'status': 'deleted-upstream'}
    ours = blob(fork, 'HEAD', path)
    if ours is None:
        raise UpstreamError(f'fork HEAD does not contain overlay file {path}')
    (scratch / 'ours').write_bytes(ours)
    (scratch / 'base').write_bytes(blob(upstream, base_tag, path) or b'')
    (scratch / 'theirs').write_bytes(blob(upstream, new_tag, path) or b'')
    # Exit status is the number of conflicts (at most 127); a larger value is an error.
    result = git(scratch, 'merge-file', '-p', '-L', 'fork', '-L', base_tag, '-L', new_tag,
                 'ours', 'base', 'theirs', ok=tuple(range(128)))
    if result.returncode == 0:
        return {'path': path, 'status': 'clean'}
    return {'path': path, 'status': 'conflict', 'conflicts': result.returncode}


def other_changes(upstream: Path, base_tag: str, new_tag: str, excluded: tuple[str, ...]) -> list[dict]:
    output = git(upstream, 'diff', '--name-status', '--no-renames', '-z', base_tag, new_tag, '--',
                 *TAKEN_AS_IS, *(f':(exclude){path}' for path in excluded)).stdout.decode()
    fields = output.split('\0')[:-1]
    return [{'status': status, 'path': path} for status, path in zip(fields[::2], fields[1::2])]


def check(fork: Path, remote: str, pinned_tag: str, pinned_commit: str,
          overlays: tuple[str, ...], removals: tuple[str, ...]) -> dict:
    if version_key(pinned_tag) is None:
        raise UpstreamError(f'pinned SDK tag is not a stable vX.Y.Z tag: {pinned_tag}')
    if Path(remote).exists():
        remote = str(Path(remote).resolve())
    latest = newest_stable_tag(remote)
    report = {'remote': remote, 'pinned': pinned_tag, 'latest': latest,
              'update_available': version_key(latest) > version_key(pinned_tag)}
    if not report['update_available']:
        return report
    with tempfile.TemporaryDirectory(prefix='upstream-check-') as scratch_name:
        scratch = Path(scratch_name)
        upstream = scratch / 'upstream.git'
        git(scratch, 'init', '--quiet', '--bare', str(upstream))
        git(upstream, 'fetch', '--quiet', '--no-tags', remote,
            f'+refs/tags/{pinned_tag}:refs/tags/{pinned_tag}', f'+refs/tags/{latest}:refs/tags/{latest}')
        actual = git(upstream, 'rev-parse', f'{pinned_tag}^{{commit}}').stdout.decode().strip()
        if actual != pinned_commit:
            raise UpstreamError(f'{pinned_tag} at {remote} is {actual}, but the pin records {pinned_commit}')
        workdir = scratch / 'merge'
        workdir.mkdir()
        git(workdir, 'init', '--quiet')
        report['overlays'] = [merge_overlay(fork, upstream, workdir, pinned_tag, latest, path) for path in overlays]
        report['removals'] = [
            {'path': path, 'status': 'unchanged-upstream'
             if blob_id(upstream, pinned_tag, path) == blob_id(upstream, latest, path) else 'changed-upstream'}
            for path in removals]
        report['other_changes'] = other_changes(upstream, pinned_tag, latest, (*overlays, *removals))
    return report


def markdown(report: dict) -> str:
    if not report['update_available']:
        return f"## Upstream check\n\nThe fork is up to date. Pinned SDK: `{report['pinned']}`.\n"
    lines = [f"## Upstream {report['latest']} is available", '',
             f"The fork pins `{report['pinned']}`. Newest stable upstream release: `{report['latest']}` "
             f"({report['remote']}).", '', '### Fork overlays (3-way merge)', '',
             '| File | Result |', '| --- | --- |']
    for item in report['overlays']:
        result = item['status']
        if result == 'conflict':
            result += f" ({item['conflicts']})"
        lines.append(f"| `{item['path']}` | {result} |")
    lines += ['', '### Fork removals', '', '| File | Result |', '| --- | --- |']
    lines += [f"| `{item['path']}` | {item['status']} |" for item in report['removals']]
    changes = report['other_changes']
    lines += ['', f'### Other upstream changes taken as-is ({len(changes)})', '']
    if not changes:
        lines.append('None.')
    lines += [f"- `{item['status']}` `{item['path']}`" for item in changes[:MARKDOWN_LIST_LIMIT]]
    if len(changes) > MARKDOWN_LIST_LIMIT:
        lines.append(f'- and {len(changes) - MARKDOWN_LIST_LIMIT} more')
    return '\n'.join(lines) + '\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('remote', nargs='?', default=DEFAULT_REMOTE, help='git remote or URL (default: %(default)s)')
    parser.add_argument('--repo', type=Path, default=REPO, help='fork checkout to read overlays from')
    parser.add_argument('--markdown', type=Path, help='also write the Markdown summary to this file')
    arguments = parser.parse_args(argv)
    sdk = BUILD_CONFIG['sdk']
    try:
        report = check(arguments.repo, arguments.remote, sdk['tag'], sdk['commit'], SDK_OVERLAYS, SDK_REMOVALS)
    except (UpstreamError, OSError) as error:
        print(f'upstream_check: {error}', file=sys.stderr)
        return 1
    summary = markdown(report)
    if arguments.markdown:
        arguments.markdown.write_text(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write(summary)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
