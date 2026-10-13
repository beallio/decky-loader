#!/usr/bin/env python3
"""Prepare an isolated private-test bundle, not a distributable Decky release.

The tracked SDK and frontend must match the v3.2.10 tag. Use a CPython 3.11
environment with the backend dependencies and PyInstaller 6.22.3 installed.
Install the backend with POETRY_DYNAMIC_VERSIONING_BYPASS=3.2.10 so that its
package metadata reports the verified SDK baseline, not the probe branch's
development commit count.

From the repository root, build the default one-file worker with:
    python -m PyInstaller --distpath /var/tmp/worker-dist --workpath /var/tmp/worker-build backend/worker.spec
For an onedir worker, set DECKY_WORKER_ONEDIR=1 on the same command:
    DECKY_WORKER_ONEDIR=1 python -m PyInstaller --distpath /var/tmp/worker-dist --workpath /var/tmp/worker-build backend/worker.spec
Both builds produce DeckyPluginWorker under the supplied dist path; the onedir build also produces its
sibling _internal directory. Then supply the worker executable, Rust executable
and frontend assets/locales to this command. It records the release commit,
SDK/frontend Git tree hashes, runtime description, packaging variant, and file
checksums, including every onedir runtime file.
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
REPO = HERE.parents[1]
BASELINE_TAG = 'v3.2.10'


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def prepare(worker: Path, rust: Path, assets: Path, locales: Path, output: Path) -> dict:
    for executable in (worker, rust):
        if not executable.is_file():
            raise ValueError(f'missing executable: {executable}')
    if not (assets / 'index.js').is_file() or not (locales / 'en-US.json').is_file():
        raise ValueError('build the existing frontend and supply its assets and locales')
    if output.exists():
        raise ValueError(f'output already exists; use a new isolated path: {output}')
    subprocess.run(
        ['git', 'diff', '--quiet', BASELINE_TAG, '--', 'backend/decky_loader', 'frontend',
         ':(exclude)backend/decky_loader/plugin/worker.py'],
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
    onedir_runtime = worker.parent / '_internal'
    worker_packaging = 'onedir' if onedir_runtime.is_dir() else 'onefile'
    if worker_packaging == 'onedir' and not any(onedir_runtime.rglob('*')):
        raise ValueError(f'worker onedir runtime directory is empty: {onedir_runtime}')
    runtime = json.loads(subprocess.check_output([str(worker), '--describe'], text=True, timeout=20))
    if not runtime['frozen'] or not runtime['python'].startswith('3.11.'):
        raise ValueError('the private probe requires a frozen CPython 3.11 worker')
    if runtime['decky_version'] != BASELINE_TAG:
        raise ValueError(f'worker version {runtime["decky_version"]} does not match {BASELINE_TAG}; rebuild its package metadata')
    subprocess.run([str(rust), '--help'], check=True, stdout=subprocess.DEVNULL, timeout=10)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.decky-private-bundle-', dir=output.parent) as temporary:
        staging = Path(temporary) / 'payload'
        bundle = staging / 'bundle'
        bundle.mkdir(parents=True)
        shutil.copy2(worker, bundle / 'DeckyPluginWorker')
        if worker_packaging == 'onedir':
            shutil.copytree(onedir_runtime, bundle / '_internal')
        shutil.copy2(rust, bundle / 'decky-rust-runtime-probe')
        shutil.copytree(assets, bundle / 'assets')
        shutil.copytree(locales, bundle / 'locales')
        manifest = {
            'kind': 'decky-rust-runtime-probe',
            'private_only': True,
            'distribution_approved': False,
            'worker_packaging': worker_packaging,
            'runtime': runtime,
            'source_baseline': source_baseline,
            'cargo_lock_sha256': digest(HERE / 'Cargo.lock'),
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
        staging.rename(output)
    return {'output': str(output), 'files': len(manifest['sha256']), 'runtime': runtime, 'source_baseline': source_baseline, 'private_only': True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--worker', type=Path, required=True)
    parser.add_argument('--rust', type=Path, required=True)
    parser.add_argument('--assets', type=Path, default=REPO / 'backend/decky_loader/static')
    parser.add_argument('--locales', type=Path, default=REPO / 'backend/decky_loader/locales')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.worker.resolve(), args.rust.resolve(), args.assets.resolve(), args.locales.resolve(), args.output.resolve())))


if __name__ == '__main__':
    main()
