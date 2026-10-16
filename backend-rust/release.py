#!/usr/bin/env python3
"""Validate a complete Rust candidate; publish a prerelease only with --publish."""
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

from build import ARTIFACT_NAME, CONFIG, REPO, digest

ASSET_NAME = ARTIFACT_NAME + '.zip'
TAG_PATTERN = r'v[0-9]+\.[0-9]+\.[0-9]+-rust\.[0-9]+(?:-rc\.[0-9]+)?'


def validate(directory: Path, tag: str, source_commit: str) -> dict[str, object]:
    if not re.fullmatch(TAG_PATTERN, tag):
        raise ValueError('use a Rust candidate tag such as v3.2.10-rust.7-rc.1')
    if not re.fullmatch(r'[0-9a-f]{40}', source_commit):
        raise ValueError('source commit must be a complete lowercase Git commit SHA')
    artifact = directory / ASSET_NAME
    checksum = digest(artifact)
    if (directory / 'SHA256SUMS').read_text().splitlines() != [f'{checksum}  {ASSET_NAME}']:
        raise ValueError('SHA256SUMS does not identify exactly this runtime ZIP')
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
        if not executable_names.issubset(hashes):
            raise ValueError('runtime bundle lacks a backend or worker executable')
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
    for name, expected in source_hashes.items():
        if not isinstance(name, str) or any(ord(char) < 32 for char in name):
            raise ValueError('invalid build-source path')
        path = PurePosixPath(name)
        if (not path.parts or path.parts[0] not in ('backend', 'backend-rust', 'frontend')
                or '..' in path.parts or path.as_posix() != name):
            raise ValueError('unsupported build-source path: ' + name)
        original = subprocess.check_output(['git', 'show', f'{source_commit}:{name}'], cwd=REPO)
        if hashlib.sha256(original).hexdigest() != expected:
            raise ValueError('build-source hash differs from the release commit: ' + name)
    return {'validated': True, 'tag': tag, 'source_commit': source_commit,
            'artifact': str(artifact), 'artifact_sha256': checksum, 'files': len(hashes),
            'verified_source_files': len(source_hashes)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact-dir', type=Path, required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--repository', help='GitHub owner/repository; required only for publication')
    parser.add_argument('--publish', action='store_true', help='explicitly publish an experimental prerelease, never latest/stable')
    args = parser.parse_args()
    if args.publish and (not args.repository or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repository)):
        parser.error('--publish requires --repository owner/repository')
    try:
        result = validate(args.artifact_dir, args.tag, args.source_commit)
        if args.publish:
            assets = [args.artifact_dir / name for name in (ASSET_NAME, 'SHA256SUMS', 'build-result.json')]
            subprocess.run(['gh', 'release', 'create', args.tag, '--repo', args.repository,
                            '--target', args.source_commit, '--prerelease', '--latest=false',
                            '--title', args.tag, '--notes',
                            'Experimental Rust runtime candidate, not the default Decky release. '
                            f'SDK baseline {CONFIG["sdk"]["tag"]}; source {args.source_commit}. '
                            'Physical Deck and plugin checks are required before wider rollout.',
                            *(str(path) for path in assets)], check=True)
        result['published'] = args.publish
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        print(f'{parser.prog}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
