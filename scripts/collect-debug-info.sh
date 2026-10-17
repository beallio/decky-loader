#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
exec python3 - "$@" <<'PY'
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import zipfile

parser = argparse.ArgumentParser(description='Save user-service logs for a bug report.')
parser.add_argument('--homebrew', type=Path, default=Path.home() / 'homebrew')
parser.add_argument('--service', default='plugin_loader.service')
parser.add_argument('--output', type=Path, default=Path.home() / 'decky-debug-info.zip')
args = parser.parse_args()
if args.output.exists():
    parser.error('--output already exists; choose a new file')
if not args.service or args.service.startswith('-') or '/' in args.service:
    parser.error('--service must be a service name')

base = Path(os.environ.get('TMPDIR') or '/var/tmp')
base.mkdir(mode=0o700, parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='decky-debug-', dir=base) as temporary:
    work = Path(temporary)
    system = {'kernel': platform.release(), 'machine': platform.machine()}
    os_release = Path('/etc/os-release')
    if os_release.is_file():
        system['os_release'] = os_release.read_text()
    (work / 'system.json').write_text(json.dumps(system, indent=2))
    for name, command in (
        ('service-status.txt', ['systemctl', '--user', 'status', args.service, '--no-pager']),
        ('backend-log.txt', ['journalctl', '--user', '-b', '-u', args.service, '--no-pager']),
    ):
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        (work / name).write_text(result.stdout)
    plugins = []
    directory = args.homebrew / 'plugins'
    if directory.is_dir():
        for plugin in sorted(directory.iterdir()):
            if plugin.is_symlink() or not plugin.is_dir():
                continue
            item = {'directory': plugin.name}
            try:
                metadata = json.loads((plugin / 'plugin.json').read_text())
                package = json.loads((plugin / 'package.json').read_text())
                item.update(name=metadata.get('name'), version=package.get('version'), flags=metadata.get('flags', []))
            except (OSError, ValueError, TypeError, AttributeError) as error:
                item['error'] = str(error)
            plugins.append(item)
    (work / 'plugins.json').write_text(json.dumps(plugins, indent=2))
    with zipfile.ZipFile(args.output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(work.iterdir()):
            archive.write(path, path.name)
        for name in ('cef_log.txt', 'cef_log.previous.txt'):
            path = Path.home() / '.steam/steam/logs' / name
            if path.is_file():
                archive.write(path, name)
print(f'Saved {args.output}. Check the logs for personal details before sharing them.')
print('Report fork issues at https://github.com/beallio/decky-loader/issues.')
PY
