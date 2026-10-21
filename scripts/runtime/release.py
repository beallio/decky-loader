#!/usr/bin/env python3
"""Validate a complete Rust runtime release; publishing is opt-in."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import zipfile

from build import ARTIFACT_NAME, CONFIG, REPO, SOURCE_ROOTS, snapshot_excluded
from prepare_bundle import SDK_OVERLAYS, SDK_REMOVALS, digest

ASSET_NAME = ARTIFACT_NAME + '.zip'
ASSET_CHECKSUM_NAME = ASSET_NAME + '.sha256'
STABLE_TAG_PATTERN = r'v[0-9]+\.[0-9]+\.[0-9]+-rust\.[0-9]+'
PREVIEW_TAG_PATTERN = STABLE_TAG_PATTERN + r'-rc\.[0-9]+'
PYTHON_RELEASE_REPOSITORY = 'steamdeckhomebrew/decky-loader'
LIBPYTHON = '_internal/libpython{}.{}.so.1.0'.format(*CONFIG['python']['version'].split('.')[:2])


def release_channel(tag: str, requested: str | None = None) -> str:
    if re.fullmatch(STABLE_TAG_PATTERN, tag):
        inferred = 'stable'
    elif re.fullmatch(PREVIEW_TAG_PATTERN, tag):
        inferred = 'preview'
    else:
        raise ValueError('use a Rust stable tag vX.Y.Z-rust.N or preview tag vX.Y.Z-rust.N-rc.N')
    if requested is not None and requested != inferred:
        raise ValueError(f'{inferred} Rust tags cannot be published to the {requested} channel')
    return inferred


def publication_ref(ref: str, source_commit: str) -> None:
    if ref != 'refs/heads/main':
        raise ValueError('production publication requires refs/heads/main')
    result = subprocess.run(
        ['git', 'merge-base', '--is-ancestor', source_commit, ref], cwd=REPO,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise ValueError('production release source must belong to main')


def source_inventory(source_commit: str) -> set[str]:
    kind = subprocess.check_output(['git', 'cat-file', '-t', source_commit], cwd=REPO, text=True).strip()
    if kind != 'commit':
        raise ValueError('release source must identify a commit, not a tag or other Git object')
    names = subprocess.check_output(
        ['git', 'ls-tree', '--full-tree', '-r', '-z', '--name-only', source_commit, '--', *SOURCE_ROOTS],
        cwd=REPO).decode().split('\0')
    return {name for name in names if name and not snapshot_excluded(Path(name))}


def publication_repository(repository: str) -> None:
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('publication requires repository owner/repository')
    if repository.lower() == PYTHON_RELEASE_REPOSITORY:
        raise ValueError('Rust runtime releases must not target the official Python release feed')


def existing_tag_commit(repository: str, tag: str) -> str | None:
    refs = json.loads(subprocess.check_output(
        ['gh', 'api', f'repos/{repository}/git/matching-refs/tags/{tag}'], text=True))
    matches = [ref for ref in refs if ref.get('ref') == f'refs/tags/{tag}']
    if not matches:
        return None
    target = matches[0]['object']
    seen = set()
    while target['type'] == 'tag':
        identifier = target['sha']
        if identifier in seen:
            raise ValueError('release tag contains an annotation cycle')
        seen.add(identifier)
        target = json.loads(subprocess.check_output(
            ['gh', 'api', f'repos/{repository}/git/tags/{identifier}'], text=True))['object']
    if target['type'] != 'commit':
        raise ValueError('release tag does not resolve to a commit')
    return target['sha']


def ensure_release_tag(repository: str, tag: str, source_commit: str) -> None:
    current = existing_tag_commit(repository, tag)
    if current is not None:
        if current != source_commit:
            raise ValueError('existing release tag points to a different source commit')
        return
    subprocess.run(['gh', 'api', '--method', 'POST', f'repos/{repository}/git/refs',
                    '-f', f'ref=refs/tags/{tag}', '-f', f'sha={source_commit}'], check=True)


def validate(directory: Path, tag: str, source_commit: str, channel: str | None = None) -> dict[str, object]:
    channel = release_channel(tag, channel)
    if not re.fullmatch(r'[0-9a-f]{40}', source_commit):
        raise ValueError('source commit must be a complete lowercase Git commit SHA')
    artifact = directory / ASSET_NAME
    checksum = digest(artifact)
    checksum_line = f'{checksum}  {ASSET_NAME}'
    if (directory / 'SHA256SUMS').read_text().splitlines() != [checksum_line]:
        raise ValueError('SHA256SUMS does not identify exactly this runtime ZIP')
    if (directory / ASSET_CHECKSUM_NAME).read_text().splitlines() != [checksum_line]:
        raise ValueError(f'{ASSET_CHECKSUM_NAME} does not identify exactly this runtime ZIP')
    record = json.loads((directory / 'build-result.json').read_text())
    if not isinstance(record, dict) or record.get('artifact_sha256') != checksum:
        raise ValueError('build record does not match the runtime ZIP')
    with zipfile.ZipFile(artifact) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ValueError('duplicate runtime ZIP member')
        manifest = json.loads(archive.read('bundle.json'))
        if not isinstance(manifest, dict) or manifest.get('kind') != 'decky-loader-runtime':
            raise ValueError('not a Decky Rust runtime bundle')
        if manifest.get('release_version') != tag:
            raise ValueError('release tag differs from the built runtime version')
        provenance = manifest.get('build')
        if not isinstance(provenance, dict) or provenance.get('base_commit') != source_commit:
            raise ValueError('bundle source commit differs from the requested release source')
        if manifest.get('source_baseline') != CONFIG['sdk']:
            raise ValueError('bundle SDK baseline differs from the pinned build configuration')
        overlays = manifest.get('sdk_overlays')
        if not isinstance(overlays, dict) or set(overlays) != set(SDK_OVERLAYS):
            raise ValueError('bundle does not identify the complete explicit fork SDK overlay')
        if manifest.get('sdk_removals') != list(SDK_REMOVALS):
            raise ValueError('bundle does not identify the explicit fork SDK removals')
        runtime = manifest.get('runtime')
        if (not isinstance(runtime, dict) or runtime.get('python') != CONFIG['python']['version']
                or runtime.get('decky_version') != CONFIG['sdk']['tag']):
            raise ValueError('bundle Python or SDK runtime differs from the pinned configuration')
        if record.get('runtime') != runtime or record.get('source_baseline') != manifest['source_baseline']:
            raise ValueError('build record runtime or SDK baseline differs from the bundle')
        hashes = manifest.get('sha256')
        if not isinstance(hashes, dict) or not hashes or set(names) != set(hashes) | {'bundle.json'}:
            raise ValueError('runtime ZIP inventory differs from its manifest')
        if record.get('files') != len(hashes):
            raise ValueError('build record file count differs from the manifest inventory')
        executable_names = {'decky-loader-runtime', 'DeckyPluginWorker'}
        required = executable_names | {'install.py', 'process_observer.py', 'assets/index.js', 'locales/en-US.json'}
        packaging = manifest.get('worker_packaging')
        if packaging == 'onedir':
            required |= {LIBPYTHON, '_internal/base_library.zip'}
        elif packaging != 'onefile':
            raise ValueError('runtime bundle must declare its worker packaging')
        if not required.issubset(hashes):
            raise ValueError('runtime bundle lacks required installer, frontend, locale or worker payload')
        for info in infos:
            path = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            if (path.is_absolute() or '..' in path.parts or path.as_posix() != info.filename
                    or '\\' in info.filename or not stat.S_ISREG(mode) or mode & 0o6022):
                raise ValueError('unsupported ZIP path or mode: ' + info.filename)
            if info.filename in executable_names and not mode & 0o111:
                raise ValueError('executable mode missing: ' + info.filename)
            if info.filename in hashes:
                with archive.open(info) as stream:
                    actual = hashlib.sha256()
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        actual.update(block)
                if actual.hexdigest() != hashes[info.filename]:
                    raise ValueError('payload checksum mismatch: ' + info.filename)
    source_hashes = provenance.get('source_sha256')
    if not isinstance(source_hashes, dict) or not source_hashes:
        raise ValueError('bundle lacks exact build-source hashes')
    if set(source_hashes) != source_inventory(source_commit):
        raise ValueError('build-source inventory does not cover exactly the release commit snapshot')
    if any(source_hashes.get(name) != expected for name, expected in overlays.items()):
        raise ValueError('fork SDK overlay hashes differ from the pinned-source release snapshot')
    if any(name in source_hashes for name in SDK_REMOVALS):
        raise ValueError('release source reintroduces a removed SDK file')
    for name, expected in source_hashes.items():
        if not isinstance(name, str) or any(ord(char) < 32 for char in name):
            raise ValueError('invalid build-source path')
        path = PurePosixPath(name)
        if (not path.parts or not any(name.startswith(f'{root}/') for root in SOURCE_ROOTS)
                or '..' in path.parts or path.as_posix() != name):
            raise ValueError('unsupported build-source path: ' + name)
        original = subprocess.check_output(['git', 'show', f'{source_commit}:{name}'], cwd=REPO)
        if hashlib.sha256(original).hexdigest() != expected:
            raise ValueError('build-source hash differs from the release commit: ' + name)
    return {'validated': True, 'tag': tag, 'channel': channel, 'source_commit': source_commit,
            'artifact': str(artifact), 'artifact_sha256': checksum, 'files': len(hashes),
            'verified_source_files': len(source_hashes)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact-dir', type=Path, required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--channel', choices=('stable', 'preview'), required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--repository', help='GitHub owner/repository; required only for publication')
    parser.add_argument('--ref', help='Git ref; publication is permitted only from refs/heads/main')
    parser.add_argument('--publish', action='store_true', help='explicitly publish the validated Rust runtime release')
    args = parser.parse_args()
    if args.publish and (not args.repository or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repository)):
        parser.error('--publish requires --repository owner/repository')
    if args.publish and not args.ref:
        parser.error('--publish requires --ref refs/heads/main')
    try:
        if args.publish:
            publication_repository(args.repository)
            publication_ref(args.ref, args.source_commit)
        result = validate(args.artifact_dir, args.tag, args.source_commit, args.channel)
        if args.publish:
            assets = [args.artifact_dir / name for name in
                      (ASSET_NAME, ASSET_CHECKSUM_NAME, 'SHA256SUMS', 'build-result.json')]
            ensure_release_tag(args.repository, args.tag, args.source_commit)
            notes = (f'{args.channel.title()} Rust runtime release. '
                     f'SDK baseline {CONFIG["sdk"]["tag"]}; source {args.source_commit}.')
            release_args = ['gh', 'release', 'create', args.tag, '--repo', args.repository,
                            '--verify-tag', '--title', args.tag, '--notes', notes]
            if args.channel == 'preview':
                release_args.extend(('--prerelease', '--latest=false'))
            else:
                release_args.append('--latest')
            subprocess.run([*release_args, *(str(path) for path in assets)], check=True)
        result['published'] = args.publish
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        print(f'{parser.prog}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

