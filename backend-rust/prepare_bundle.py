#!/usr/bin/env python3
"""Assemble a complete private Decky runtime candidate from built components.

Use build.py for the pinned, end-to-end build. This lower-level command accepts
prebuilt components and verifies their Python SDK/frontend source baseline,
packaged helper code, runtime description and complete payload checksums.
Both one-file and shared-directory workers are supported; the automated build
uses the shared-directory worker with separate forked Python plugin processes.
Distribution approval remains disabled until the deferred hardware/UI checks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BUILD_CONFIG = json.loads((HERE / 'build-config.json').read_text())
BASELINE_TAG = BUILD_CONFIG['sdk']['tag']


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def prepare(worker: Path, rust: Path, assets: Path, locales: Path, output: Path, release_version: str,
            *, build_provenance: dict | None = None) -> dict:
    if not release_version or any(ord(character) < 32 or ord(character) == 127 for character in release_version):
        raise ValueError('release version must be a non-empty printable identifier')
    for executable in (worker, rust):
        if not executable.is_file():
            raise ValueError(f'missing executable: {executable}')
    if not (assets / 'index.js').is_file() or not (locales / 'en-US.json').is_file():
        raise ValueError('build the existing frontend and supply its assets and locales')
    if output.exists():
        raise ValueError(f'output already exists; use a new isolated path: {output}')
    subprocess.run(
        ['git', 'diff', '--quiet', BASELINE_TAG, '--', 'backend/decky_loader', 'frontend',
         ':(exclude)backend/decky_loader/plugin/worker.py',
         ':(exclude)backend/decky_loader/plugin/launcher.py'],
        cwd=REPO, check=True,
    )
    commit, backend_tree, frontend_tree = subprocess.check_output(
        ['git', 'rev-parse', f'{BASELINE_TAG}^{{commit}}',
         f'{BASELINE_TAG}:backend/decky_loader', f'{BASELINE_TAG}:frontend'],
        cwd=REPO, text=True,
    ).splitlines()
    source_baseline = {
        'tag': BASELINE_TAG, 'commit': commit,
        'backend_tree': backend_tree, 'frontend_tree': frontend_tree,
    }
    if source_baseline != BUILD_CONFIG['sdk']:
        raise ValueError('SDK/frontend tag identities differ from the pinned source baseline')
    helper_path = REPO / 'backend/decky_loader/helpers.py'
    helper_sha256 = digest(helper_path)
    onedir_runtime = worker.parent / '_internal'
    worker_packaging = 'onedir' if onedir_runtime.is_dir() else 'onefile'
    if worker_packaging == 'onedir' and not any(onedir_runtime.rglob('*')):
        raise ValueError(f'worker onedir runtime directory is empty: {onedir_runtime}')
    runtime = json.loads(subprocess.check_output([str(worker), '--describe'], text=True, timeout=20))
    if not runtime['frozen'] or runtime['python'] != BUILD_CONFIG['python']['version']:
        raise ValueError('the runtime requires the pinned frozen CPython version')
    if runtime['decky_version'] != BASELINE_TAG:
        raise ValueError(f'worker version {runtime["decky_version"]} does not match {BASELINE_TAG}; rebuild its package metadata')
    helper = runtime.get('sdk_sources', {}).get('decky_loader.helpers', {})
    if helper.get('sha256') != helper_sha256 or helper.get('code_matches_source') is not True:
        raise ValueError('worker helper source/code does not match this checkout; rebuild the worker')
    subprocess.run([str(rust), '--help'], check=True, stdout=subprocess.DEVNULL, timeout=10)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.decky-private-bundle-', dir=output.parent) as temporary:
        staging = Path(temporary) / 'payload'
        bundle = staging / 'bundle'
        bundle.mkdir(parents=True)
        shutil.copy2(worker, bundle / 'DeckyPluginWorker')
        if worker_packaging == 'onedir':
            shutil.copytree(onedir_runtime, bundle / '_internal')
            # Match the frozen SDK bootstrap before recording the immutable payload.
            (bundle / '_internal').chmod(0o755)
            for path in (bundle / '_internal').rglob('*'):
                path.chmod(0o755)
        shutil.copy2(rust, bundle / 'decky-loader-runtime')
        shutil.copy2(HERE / 'install.py', bundle / 'install.py')
        shutil.copy2(HERE / 'process_observer.py', bundle / 'process_observer.py')
        shutil.copytree(assets, bundle / 'assets')
        shutil.copytree(locales, bundle / 'locales')
        manifest = {
            'kind': 'decky-loader-runtime',
            'release_version': release_version,
            'private_only': True,
            'distribution_approved': False,
            'worker_packaging': worker_packaging,
            'rust_executable': 'decky-loader-runtime',
            'runtime': runtime,
            'source_baseline': source_baseline,
            'cargo_lock_sha256': digest(HERE / 'Cargo.lock'),
            'build': build_provenance,
            'sha256': {path.relative_to(bundle).as_posix(): digest(path) for path in sorted(bundle.rglob('*')) if path.is_file()},
        }
        (bundle / 'bundle.json').write_text(json.dumps(manifest, indent=2) + '\n')
        fixtures = staging / 'fixtures/plugins'
        for api_version, directory, name, frontend in (
            (1, 'ProbeModern', 'Rust Probe Modern', 'modern.js'),
            (0, 'ProbeLegacy', 'Rust Probe Legacy', 'legacy.js'),
        ):
            plugin = fixtures / directory
            (plugin / 'dist').mkdir(parents=True)
            shutil.copy2(HERE / 'fixtures/main.py', plugin / 'main.py')
            shutil.copy2(HERE / 'fixtures' / frontend, plugin / 'dist/index.js')
            (plugin / 'plugin.json').write_text(json.dumps({'name': name, 'author': 'Private compatibility proof', 'api_version': api_version, 'flags': []}))
            package = {'name': directory.lower(), 'version': '0.0.0'}
            if api_version:
                package['type'] = 'module'
            (plugin / 'package.json').write_text(json.dumps(package))
        for script in ('verify.py', 'validate_root.py'):
            shutil.copy2(HERE / script, staging / script)
        shutil.copy2(HERE / 'process_observer.py', staging / 'process_observer.py')
        staging.rename(output)
    return {'output': str(output), 'files': len(manifest['sha256']), 'runtime': runtime, 'source_baseline': source_baseline, 'private_only': True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--worker', type=Path, required=True)
    parser.add_argument('--rust', type=Path, required=True)
    parser.add_argument('--assets', type=Path, default=REPO / 'backend/decky_loader/static')
    parser.add_argument('--locales', type=Path, default=REPO / 'backend/decky_loader/locales')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--release-version', default='dev', help='Rust release identity, separate from the packaged SDK baseline')
    args = parser.parse_args()
    print(json.dumps(prepare(args.worker.resolve(), args.rust.resolve(), args.assets.resolve(), args.locales.resolve(), args.output.resolve(), args.release_version)))


if __name__ == '__main__':
    main()
