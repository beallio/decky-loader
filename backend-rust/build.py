#!/usr/bin/env python3
"""Build the complete Linux x86-64 runtime bundle without installing it.

Run from any directory with Node, pnpm, Rust, Git, gh and tar available.
Tool versions and the verified CPython archive are pinned in build-config.json.
Python tools, source snapshots and compiler output live in TMPDIR and are removed
when the build finishes. The selected output directory must not already exist.
This creates a private candidate; it does not publish or activate a release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CONFIG = json.loads((HERE / 'build-config.json').read_text())
RUNTIME_NAME = 'decky-loader-runtime'
ARTIFACT_NAME = f'{RUNTIME_NAME}-linux-x86_64'
SOURCE_ROOTS = ('backend', 'backend-rust', 'frontend')


def run(arguments: list[str], *, cwd: Path = REPO, env: dict[str, str] | None = None) -> None:
    print('+ ' + ' '.join(str(argument) for argument in arguments), flush=True)
    subprocess.run(arguments, cwd=cwd, env=env, check=True)


def capture(arguments: list[str], *, cwd: Path = REPO, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(arguments, cwd=cwd, env=env, text=True).strip()


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def snapshot_excluded(relative: Path) -> bool:
    parts = relative.parts
    if any(part in {'target', 'node_modules', '.venv', '__pycache__', 'build', 'dist', '.git'}
           or part.endswith(('.pyc', '.pyo')) for part in parts):
        return True
    return (parts[:2] == ('backend', 'plugins')
            or parts[:3] == ('backend', 'decky_loader', 'static')
            or parts[:2] == ('frontend', 'stats.html'))


def source_snapshot(destination: Path) -> dict[str, str]:
    def ignore(directory: str, names: list[str]) -> set[str]:
        relative = Path(directory).relative_to(REPO)
        return {name for name in names if snapshot_excluded(relative / name)}

    for name in SOURCE_ROOTS:
        shutil.copytree(REPO / name, destination / name, ignore=ignore)
    return {
        path.relative_to(destination).as_posix(): digest(path)
        for path in sorted(destination.rglob('*')) if path.is_file()
    }


def build(output: Path, release_version: str, python_archive: Path | None) -> dict[str, object]:
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('this build targets Linux x86-64 only')
    if output.exists():
        raise ValueError('output already exists; select a new directory')
    tool_environment = dict(os.environ, RUSTUP_TOOLCHAIN=CONFIG['rust'])
    rust_version = capture(['rustc', '--version'], env=tool_environment)
    cargo_version = capture(['cargo', '--version'], env=tool_environment)
    node_version = capture(['node', '--version']).removeprefix('v')
    pnpm_version = capture(['pnpm', '--version'])
    if (rust_version.split()[1] != CONFIG['rust'] or cargo_version.split()[1] != CONFIG['rust']
            or node_version != CONFIG['node'] or pnpm_version != CONFIG['pnpm']):
        raise RuntimeError(f'tool versions differ from build-config.json: {rust_version}; {cargo_version}; Node {node_version}; pnpm {pnpm_version}')
    temporary_root = Path(os.environ.get('TMPDIR') or '/var/tmp/omp-1000')
    temporary_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    base_commit = capture(['git', 'rev-parse', 'HEAD'])
    environment = dict(tool_environment, TMPDIR=str(temporary_root), PYTHONDONTWRITEBYTECODE='1', HUSKY='0', CI='true')
    with tempfile.TemporaryDirectory(prefix='decky-runtime-build-', dir=temporary_root) as temporary:
        work = Path(temporary)
        source = work / 'source'
        source_hashes = source_snapshot(source)
        python_config = CONFIG['python']
        archive = python_archive
        if archive is None:
            run(['gh', 'release', 'download', python_config['release'], '--repo', python_config['repository'],
                 '--pattern', python_config['asset'], '--dir', str(work)], env=environment)
            archive = work / python_config['asset']
        if digest(archive) != python_config['sha256']:
            raise ValueError('CPython archive checksum differs from the pinned build input')
        # This exact upstream archive is authenticated by the pinned SHA-256 above.
        run(['tar', '-xzf', str(archive), '-C', str(work)], env=environment)
        python = work / 'python/bin/python3'
        python_version = capture([str(python), '-c', 'import platform; print(platform.python_version())'], env=environment)
        if python_version != python_config['version']:
            raise ValueError(f'pinned archive reported unexpected Python version: {python_version}')
        for name in ('tools', 'worker-env'):
            run([str(python), '-m', 'venv', str(work / name)], env=environment)
        tools_python = work / 'tools/bin/python'
        worker_python = work / 'worker-env/bin/python'
        run([str(tools_python), '-m', 'pip', 'install', '--disable-pip-version-check',
             f'poetry=={CONFIG["poetry"]}', f'poetry-dynamic-versioning=={CONFIG["poetry_dynamic_versioning"]}'], env=environment)
        worker_environment = dict(environment, VIRTUAL_ENV=str(work / 'worker-env'),
                                  POETRY_VIRTUALENVS_CREATE='false',
                                  POETRY_DYNAMIC_VERSIONING_BYPASS=CONFIG['sdk']['tag'].removeprefix('v'))
        run([str(work / 'tools/bin/poetry'), 'sync', '--no-interaction'],
            cwd=source / 'backend', env=worker_environment)
        pyinstaller_version = capture([str(worker_python), '-m', 'PyInstaller', '--version'], env=worker_environment)
        if pyinstaller_version != CONFIG['pyinstaller']:
            raise ValueError(f'locked PyInstaller version differs from build configuration: {pyinstaller_version}')
        run(['pnpm', 'install', '--frozen-lockfile'], cwd=source / 'frontend', env=environment)
        run(['pnpm', 'run', 'build'], cwd=source / 'frontend', env=environment)
        run(['cargo', 'build', '--release', '--locked', '--package', RUNTIME_NAME, '--manifest-path',
             str(source / 'backend-rust/Cargo.toml'), '--target-dir', str(work / 'cargo-target')], env=environment)
        native_environment = dict(worker_environment, PYO3_PYTHON=str(worker_python))
        run(['cargo', 'build', '--locked', '--profile', 'native', '--package', 'decky-native-json',
             '--features', 'extension-module', '--manifest-path', str(source / 'backend-rust/Cargo.toml'),
             '--target-dir', str(work / 'cargo-target')], env=native_environment)
        native_modules = work / 'native-modules'
        native_modules.mkdir()
        extension_suffix = capture([str(worker_python), '-c',
                                    'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))'],
                                   env=worker_environment)
        native_module = native_modules / f'_decky_native_json{extension_suffix}'
        shutil.copy2(work / 'cargo-target/native/lib_decky_native_json.so', native_module)
        run([str(worker_python), '-m', 'unittest', 'discover', '-s',
             str(source / 'backend-rust/native-json'), '-p', 'test_*.py'],
            env=dict(native_environment, PYTHONPATH=str(native_modules)))
        worker_environment.update(DECKY_WORKER_ONEDIR='1', DECKY_NATIVE_JSON_DIR=str(native_modules))
        run([str(worker_python), '-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', str(work / 'worker-dist'),
             '--workpath', str(work / 'worker-build'), str(source / 'backend/worker.spec')], env=worker_environment)
        provenance = {
            'base_commit': base_commit,
            'source_sha256': source_hashes,
            'build_config': CONFIG,
            'tools': {'rustc': rust_version, 'cargo': cargo_version, 'node': node_version, 'pnpm': pnpm_version, 'pyinstaller': pyinstaller_version},
            'python_packages': json.loads(capture([str(worker_python), '-m', 'pip', 'list', '--format=json'], env=worker_environment)),
            'tool_packages': json.loads(capture([str(tools_python), '-m', 'pip', 'list', '--format=json'], env=worker_environment)),
            'native_json': {'module': '_decky_native_json', 'sha256': digest(native_module),
                            'abi_suffix': extension_suffix, 'system_unwind_library': 'libgcc_s.so.1'},
        }
        from prepare_bundle import prepare
        result = prepare(work / 'worker-dist/DeckyPluginWorker/DeckyPluginWorker',
                         work / f'cargo-target/release/{RUNTIME_NAME}',
                         source / 'backend/decky_loader/static', source / 'backend/decky_loader/locales',
                         output, release_version, build_provenance=provenance)
    artifact = output / f'{ARTIFACT_NAME}.zip'
    bundle = output / 'bundle'
    with zipfile.ZipFile(artifact, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(bundle).as_posix())
    checksum = digest(artifact)
    (output / 'SHA256SUMS').write_text(f'{checksum}  {artifact.name}\n')
    result.update(artifact=str(artifact), artifact_sha256=checksum)
    (output / 'build-result.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, required=True, help='new output directory for the complete bundle and ZIP')
    parser.add_argument('--release-version', default='dev', help='Rust release identity; Python SDK version remains pinned')
    parser.add_argument('--python-archive', type=Path, help='reuse the pinned CPython archive instead of downloading it')
    args = parser.parse_args()
    print(json.dumps(build(args.output.expanduser().resolve(), args.release_version,
                           args.python_archive.expanduser().resolve() if args.python_archive else None)))


if __name__ == '__main__':
    main()
