#!/usr/bin/env python3
"""Assemble a Decky runtime bundle from built components.

Use build.py for a complete build with pinned inputs. This lower-level command
checks the SDK baseline, fork overlays, worker, and payload checksums.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BACKEND_RUST = REPO / 'backend-rust'
BUILD_CONFIG = json.loads((BACKEND_RUST / 'build-config.json').read_text())
BASELINE_TAG = BUILD_CONFIG['sdk']['tag']
SDK_OVERLAYS = (
    'backend/decky_loader/plugin/worker.py',
    'backend/decky_loader/plugin/launcher.py',
    'backend/decky_loader/plugin/root_host.py',
    'backend/decky_loader/plugin/sandboxed_plugin.py',
    'backend/decky_loader/plugin/imports/decky.py',
    'backend/decky_loader/plugin/imports/decky.pyi',
    'backend/decky_loader/plugin/imports/native.py',
    'backend/decky_loader/plugin/imports/native.pyi',
    'frontend/src/components/DeckyErrorBoundary.tsx',
    'frontend/src/plugin-loader.tsx',
    'frontend/tsconfig.json',
    'frontend/src/components/settings/pages/general/RemoteDebugging.tsx',
    'backend/decky_loader/locales/en-US.json',
    'frontend/src/components/modals/filepicker/index.tsx',
    'frontend/src/components/DeckyState.tsx',
    'backend/decky_loader/settings.py',
    'frontend/src/store.tsx',
    'frontend/src/components/settings/pages/general/Updater.tsx',
    'frontend/src/components/modals/RootPluginConfirmModal.tsx',
    'frontend/src/wsrouter.ts',
    'frontend/src/toaster.tsx',
    'backend/decky_loader/localplatform/localplatform.py',
    'backend/decky_loader/localplatform/localplatformlinux.py',
    'backend/decky_loader/localplatform/localsocket.py',
    'backend/decky_loader/main.py',
    'backend/decky_loader/utilities.py',
    'backend/decky_loader/wsrouter.py',
    'frontend/src/storeSources.ts',
    'frontend/src/plugin.ts',
    'frontend/src/components/settings/pages/general/StoreSelect.tsx',
    'frontend/src/components/settings/pages/plugin_list/index.tsx',
    'frontend/src/components/store/PluginCard.tsx',
    'frontend/src/components/store/Store.tsx',
    'frontend/src/components/modals/RootInfoModal.tsx',
)
SDK_REMOVALS = (
    'frontend/src/steamfixes/README.md',
    'frontend/src/steamfixes/index.ts',
    'frontend/src/steamfixes/restart.ts',
    'frontend/src/steamfixes/socket.ts',
)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def prepare(worker: Path, rust: Path, assets: Path, locales: Path, output: Path, release_version: str,
            *, build_provenance: dict | None = None, source_root: Path = REPO) -> dict:
    if not release_version or any(ord(character) < 32 or ord(character) == 127 for character in release_version):
        raise ValueError('release version must be a non-empty printable identifier')
    for executable in (worker, rust):
        if not executable.is_file():
            raise ValueError(f'missing executable: {executable}')
    if not (assets / 'index.js').is_file() or not (locales / 'en-US.json').is_file():
        raise ValueError('build the existing frontend and supply its assets and locales')
    if output.exists():
        raise ValueError(f'output already exists; use a new isolated path: {output}')
    if any((REPO / path).exists() or (REPO / path).is_symlink() for path in SDK_REMOVALS):
        raise ValueError('obsolete privileged Steam workaround code is still present')
    subprocess.run(
        ['git', 'diff', '--quiet', BASELINE_TAG, '--', 'backend/decky_loader', 'frontend',
         *(f':(exclude){path}' for path in (*SDK_OVERLAYS, *SDK_REMOVALS))],
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
    runtime = json.loads(subprocess.check_output(
        [str(worker), '--describe'], text=True, timeout=20,
        env=dict(os.environ, DECKY_JSON_ENCODER='rust')))
    if not runtime['frozen'] or runtime['python'] != BUILD_CONFIG['python']['version']:
        raise ValueError('the runtime requires the pinned frozen CPython version')
    if runtime['decky_version'] != BASELINE_TAG:
        raise ValueError(f'worker version {runtime["decky_version"]} does not match {BASELINE_TAG}; rebuild its package metadata')
    helper = runtime.get('sdk_sources', {}).get('decky_loader.helpers', {})
    if helper.get('sha256') != helper_sha256 or helper.get('code_matches_source') is not True:
        raise ValueError('worker helper source/code does not match this checkout; rebuild the worker')
    encoder = runtime.get('json_encoder', {})
    native = encoder.get('native') if isinstance(encoder, dict) else None
    if (not isinstance(encoder, dict) or encoder.get('selected') != 'rust' or encoder.get('default') != 'rust'
            or not isinstance(native, dict) or native.get('module') != '_decky_native_json'
            or native.get('version') != '0.1.0' or not native.get('sha256')):
        raise ValueError('worker lacks the default compiled Rust JSON encoder')
    if build_provenance is not None and build_provenance.get('native_json', {}).get('sha256') != native['sha256']:
        raise ValueError('packaged native encoder differs from the compiled build input')
    if worker_packaging == 'onedir':
        native_files = list(onedir_runtime.glob('_decky_native_json*.so'))
        if len(native_files) != 1 or digest(native_files[0]) != native['sha256']:
            raise ValueError('worker native encoder inventory or checksum differs from its description')
        if any(path.name == 'libgcc_s.so.1' for path in onedir_runtime.rglob('*')):
            raise ValueError('worker must use the target system unwind library, not a bundled libgcc_s.so.1')
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
        shutil.copy2(source_root / 'backend-rust/install.py', bundle / 'install.py')
        shutil.copy2(source_root / 'backend-rust/process_observer.py', bundle / 'process_observer.py')
        shutil.copytree(assets, bundle / 'assets')
        shutil.copytree(locales, bundle / 'locales')
        manifest = {
            'kind': 'decky-loader-runtime',
            'release_version': release_version,
            'worker_packaging': worker_packaging,
            'rust_executable': 'decky-loader-runtime',
            'runtime': runtime,
            'source_baseline': source_baseline,
            'sdk_overlays': {path: digest(REPO / path) for path in SDK_OVERLAYS},
            'sdk_removals': list(SDK_REMOVALS),
            'cargo_lock_sha256': digest(BACKEND_RUST / 'Cargo.lock'),
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
            shutil.copy2(REPO / 'tests/fixtures/runtime/main.py', plugin / 'main.py')
            shutil.copy2(REPO / 'tests/fixtures/runtime' / frontend, plugin / 'dist/index.js')
            (plugin / 'plugin.json').write_text(json.dumps({'name': name, 'author': 'Runtime compatibility fixture', 'api_version': api_version, 'flags': []}))
            package = {'name': directory.lower(), 'version': '0.0.0'}
            if api_version:
                package['type'] = 'module'
            (plugin / 'package.json').write_text(json.dumps(package))
        for script in ('verify.py', 'validate_user.py'):
            shutil.copy2(HERE / 'validation' / script, staging / script)
        shutil.copy2(source_root / 'backend-rust/process_observer.py', staging / 'process_observer.py')
        staging.rename(output)
    return {'output': str(output), 'files': len(manifest['sha256']), 'runtime': runtime, 'source_baseline': source_baseline}


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
