#!/usr/bin/env bash
# Verify a published runtime bundle, then let install.py own user-service activation.
set -Eeuo pipefail
umask 077

fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || fail "Required command is missing: $1"; }

show_help() {
    cat <<'HELP'
Usage: bash install-to-deck.sh [options]

Install locally by default, or use --remote HOST to deploy over SSH. Stable
fork releases are downloaded by default. Use --channel preview for previews,
or --version TAG for one exact release. No GitHub account, gh, or build tools
are needed. --bundle is for a saved ZIP and requires --sha256 and --version.

  --remote HOST            Install on this host over SSH
  --bundle ZIP             Use a saved runtime ZIP
  --sha256 HASH            Required with --bundle; expected ZIP checksum
  --version TAG            Exact release (or expected offline bundle version)
  --channel stable|preview Release channel (default: stable)
  --check                  Verify the bundle and target without installing
  --status                 Report current user-service state
  --undo                   Roll back one user-service transaction
  --migrate-legacy         Explicitly park a legacy system service first
  --restore-legacy         Restore the saved legacy system service
  --homebrew DIR           Homebrew directory (default: /home/DECK_USER/homebrew)
  --backup-dir DIR         Homebrew archive directory (default: sibling decky-user-backups)
  --service NAME           Service name (default: plugin_loader)
  --listen ADDRESS         Backend listener (default: 127.0.0.1:1337)
  --cef-url URL            Steam debugger endpoint (default: http://127.0.0.1:8080)
  --inject-frontend BOOL   true or false (default: true)
  --non-interactive        Do not prompt for SSH or migration sudo
  --help                   Show this help

Install, rollback, and legacy restoration archive homebrew outside the homebrew
tree before service activation. Only with an explicit migration selection or a
matching protected root snapshot, the user archive excludes root-owned
services/.decky-installer, services/.decky-deploy, and
services/.decky-legacy-system-migration.

DECK_USER defaults to deck. Normal install, update, status, rollback, and
recovery run as that account with systemd --user; they do not use sudo.
HELP
}


unpack_bundle() {
    python3 - "$@" <<'PY'
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import zipfile

MARGIN = 64 * 1024 * 1024
EXCLUDED = {
    Path('services/.decky-installer'),
    Path('services/.decky-deploy'),
    Path('services/.decky-legacy-system-migration'),
}
archive, expected_version, destination, homebrew, backup_directory, services, mode, root_controls, migrate_legacy = sys.argv[1:]
allow_root_exclusions = root_controls == 'true'
LEGACY_STORE = Path('/var/lib/decky-loader-runtime/legacy-migrations')

def inspect_bundle(source):
    infos = source.infolist()
    names = [item.filename for item in infos]
    if len(names) != len(set(names)):
        raise SystemExit('Runtime ZIP contains a duplicate member')
    try:
        manifest_info = next(item for item in infos if item.filename == 'bundle.json')
        if manifest_info.file_size > 16 * 1024 * 1024:
            raise SystemExit('Runtime ZIP bundle manifest is too large')
        manifest = json.loads(source.read(manifest_info))
    except (StopIteration, KeyError, json.JSONDecodeError) as error:
        raise SystemExit(f'Runtime ZIP has no valid bundle.json: {error}') from error
    if not isinstance(manifest, dict) or manifest.get('kind') != 'decky-loader-runtime' or manifest.get('release_version') != expected_version:
        raise SystemExit('Bundle kind or expected release version does not match')
    hashes = manifest.get('sha256')
    if not isinstance(hashes, dict) or not hashes or any(
        not isinstance(name, str) or not isinstance(value, str)
        or len(value) != 64 or any(character not in '0123456789abcdef' for character in value)
        for name, value in hashes.items()
    ):
        raise SystemExit('Bundle manifest has invalid payload checksums')
    file_infos = {}
    kinds = {}
    unpacked = 0
    for info in infos:
        path = PurePosixPath(info.filename)
        mode_bits = info.external_attr >> 16
        name = info.filename.rstrip('/') if info.is_dir() else info.filename
        if (not name or path.is_absolute() or '..' in path.parts
                or path.as_posix() != name or '\\' in name or '\x00' in name):
            raise SystemExit('Runtime ZIP contains an unsafe member path')
        current_kind = 'directory' if info.is_dir() else 'file'
        if name in kinds and kinds[name] != current_kind:
            raise SystemExit(f'Runtime ZIP contains a conflicting member path: {name}')
        kinds[name] = current_kind
        if info.is_dir():
            if mode_bits and not stat.S_ISDIR(mode_bits):
                raise SystemExit('Runtime ZIP contains an unsupported directory mode')
            continue
        if not stat.S_ISREG(mode_bits) or mode_bits & 0o6022:
            raise SystemExit('Runtime ZIP contains a non-regular or unsafe file mode: ' + name)
        file_infos[name] = info
        unpacked += info.file_size
    for name in kinds:
        parts = PurePosixPath(name).parts
        for depth in range(1, len(parts)):
            parent_path = '/'.join(parts[:depth])
            if kinds.get(parent_path) == 'file':
                raise SystemExit(f'Runtime ZIP file is also used as a directory: {parent_path}')
    if set(file_infos) != set(hashes) | {'bundle.json'}:
        raise SystemExit('Bundle inventory does not match its manifest')
    return infos, hashes, unpacked

def existing_directory(path):
    current = Path(path)
    while not current.exists():
        current = current.parent
    if not current.is_dir():
        current = current.parent
    return current.resolve(strict=True)

def inspect_homebrew(root, allow_root_exclusions):
    root = Path(root)
    if not root.exists():
        return 0, 0
    if not root.is_dir():
        raise SystemExit(f'Homebrew destination is not a directory: {root}')
    total = 0
    archive_overhead = 0
    def visit(directory, relative):
        nonlocal total, archive_overhead
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise SystemExit(f'Cannot read homebrew directory {directory}: {error}') from error
        for entry in entries:
            rel = relative / entry.name
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise SystemExit(f'Cannot inspect homebrew item {path}: {error}') from error
            if rel in EXCLUDED and allow_root_exclusions:
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise SystemExit(f'Refusing to exclude non-root legacy control data: {path}')
                continue
            archive_name_bytes = len(os.fsencode('homebrew/' + rel.as_posix()))
            link_name_bytes = 0
            if stat.S_ISLNK(info.st_mode):
                try:
                    link_name_bytes = len(os.fsencode(os.readlink(path)))
                except OSError as error:
                    raise SystemExit(f'Cannot read homebrew symlink {path}: {error}') from error
            archive_overhead += 1024 + 2 * (archive_name_bytes + link_name_bytes)
            if stat.S_ISDIR(info.st_mode):
                visit(path, rel)
            elif stat.S_ISREG(info.st_mode):
                try:
                    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, 'O_NOFOLLOW', 0))
                    try:
                        current = os.fstat(descriptor)
                        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                            raise SystemExit(f'Homebrew item changed during preflight: {path}')
                    finally:
                        os.close(descriptor)
                except OSError as error:
                    raise SystemExit(f'Cannot read homebrew file {path}: {error}') from error
                total += info.st_size
            elif stat.S_ISLNK(info.st_mode):
                total += info.st_size
            else:
                raise SystemExit(f'Unsupported homebrew file type; refusing an incomplete archive: {path}')
    visit(root, Path())
    return total, archive_overhead

def backup_destination(path, homebrew_path):
    raw = Path(path)
    if not raw.is_absolute():
        raise SystemExit('--backup-dir must be an absolute path')
    if raw.is_symlink():
        raise SystemExit('--backup-dir must not be a symlink')
    resolved = raw.resolve(strict=False)
    hb = Path(homebrew_path).resolve(strict=False)
    if resolved == hb or hb in resolved.parents:
        raise SystemExit('--backup-dir must be outside the homebrew directory')
    if resolved.exists():
        info = resolved.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022 or not os.access(resolved, os.W_OK | os.X_OK)):
            raise SystemExit(f'Backup directory must be a safe writable directory owned by this account: {resolved}')
    else:
        parent = existing_directory(resolved.parent)
        info = parent.stat()
        if not os.access(parent, os.W_OK | os.X_OK):
            raise SystemExit(f'Backup directory parent is not writable: {parent}')
        if info.st_uid != os.geteuid() and not (info.st_mode & stat.S_ISVTX):
            raise SystemExit(f'Backup directory parent is not privately owned: {parent}')
    return resolved

def check_space(source, unpacked, homebrew_bytes, homebrew_archive_overhead, hb, backup, services_path, extracted):
    archive_size = os.stat(source.filename).st_size
    needs = {}
    labels = {}
    availability = {}
    def add(path, amount, label):
        directory = existing_directory(path)
        info = directory.stat()
        device = info.st_dev
        needs[device] = needs.get(device, 0) + amount
        labels.setdefault(device, []).append(label)
        availability[device] = shutil.disk_usage(directory).free
    stage_directory = Path(destination).parent
    add(stage_directory, archive_size + unpacked + unpacked // 10 + MARGIN,
        'runtime ZIP and extracted bundle')
    try:
        binary = Path(hb) / 'services' / 'PluginLoader'
        binary_info = binary.lstat()
        if not (stat.S_ISREG(binary_info.st_mode) or stat.S_ISLNK(binary_info.st_mode)):
            raise SystemExit(f'Existing backend binary has an unsupported type: {binary}')
        binary_size = binary_info.st_size
    except FileNotFoundError:
        binary_size = 0
    add(services_path, unpacked + unpacked // 10 + binary_size + MARGIN,
        'installed runtime and old-binary snapshot')
    if migrate_legacy == 'true':
        helpers = sum(source.getinfo(name).file_size for name in ('install.py', 'process_observer.py'))
        add(LEGACY_STORE, binary_size + helpers + MARGIN,
            'protected legacy snapshot and restore helper')
    if not extracted and Path(hb).exists():
        add(backup, homebrew_bytes + homebrew_archive_overhead + homebrew_bytes // 10 + MARGIN,
            'homebrew archive')
    archive_device = os.stat(source.filename).st_dev
    stage_device = existing_directory(stage_directory).stat().st_dev
    if archive_device == stage_device:
        availability[stage_device] += archive_size
    for device, needed in needs.items():
        available = availability[device]
        if available < needed:
            detail = ', '.join(labels[device])
            raise SystemExit(
                f'Insufficient free space on filesystem {device} for {detail}: '
                f'{available} bytes available, {needed} bytes required'
            )
    return archive_size

with zipfile.ZipFile(archive) as source:
    infos, hashes, unpacked = inspect_bundle(source)
    homebrew_path = Path(homebrew).resolve(strict=False)
    backup_path = backup_destination(backup_directory, homebrew_path)
    homebrew_bytes, homebrew_archive_overhead = inspect_homebrew(homebrew_path, allow_root_exclusions)
    if mode == 'inspect':
        check_space(source, unpacked, homebrew_bytes, homebrew_archive_overhead, homebrew_path, backup_path, services, False)
        if homebrew_path.exists() and allow_root_exclusions:
            print('Homebrew archive preflight excludes root-owned legacy controls only for an explicit migration or matching protected root snapshot: services/.decky-installer, services/.decky-deploy, services/.decky-legacy-system-migration.')
        print(json.dumps({'archive_bytes': os.stat(archive).st_size, 'unpacked_bytes': unpacked, 'homebrew_bytes': homebrew_bytes}))
    else:
        if mode == 'extract':
            check_space(source, unpacked, homebrew_bytes, homebrew_archive_overhead, homebrew_path, backup_path, services, False)
        elif mode == 'extract-after-backup':
            check_space(source, unpacked, homebrew_bytes, homebrew_archive_overhead, homebrew_path, backup_path, services, True)
        else:
            raise SystemExit('Unknown runtime bundle operation')
        target = Path(destination)
        if target.exists():
            raise SystemExit('Bundle extraction directory already exists')
        target.mkdir(mode=0o700)
        for info in infos:
            path = PurePosixPath(info.filename)
            output = target.joinpath(*path.parts)
            if info.is_dir():
                output.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with source.open(info) as input_stream, output.open('xb') as output_stream:
                while True:
                    block = input_stream.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    output_stream.write(block)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            if info.filename in hashes and digest.hexdigest() != hashes[info.filename]:
                raise SystemExit('Payload checksum mismatch: ' + info.filename)
            output.chmod(stat.S_IMODE(info.external_attr >> 16))
        for directory in sorted((item for item in target.rglob('*') if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
            directory.chmod(0o755)
        target.chmod(0o755)
        print(unpacked)
PY
}


release_api='https://api.github.com/repos/beallio/decky-loader'
asset_name='decky-loader-runtime-linux-x86_64.zip'

fetch_release_metadata() {
    local requested=$1 channel=$2
    if [[ -n $requested ]]; then
        curl -q --fail --location --proto '=https' --proto-redir '=https' \
            --header 'Accept: application/vnd.github+json' \
            --header 'X-GitHub-Api-Version: 2022-11-28' \
            --output "$work/release.json" "$release_api/releases/tags/$requested" \
            || fail "Published release $requested could not be found in beallio/decky-loader"
    elif [[ $channel == stable ]]; then
        curl -q --fail --location --proto '=https' --proto-redir '=https' \
            --header 'Accept: application/vnd.github+json' \
            --header 'X-GitHub-Api-Version: 2022-11-28' \
            --output "$work/release.json" "$release_api/releases/latest" \
            || fail 'The fork has no published stable release yet; use --bundle with a verified ZIP or publish a stable release first'
    else
        local page count
        page=1
        while :; do
            curl -q --fail --location --proto '=https' --proto-redir '=https' \
                --header 'Accept: application/vnd.github+json' \
                --header 'X-GitHub-Api-Version: 2022-11-28' \
                --output "$work/releases-$page.json" \
                "$release_api/releases?per_page=100&page=$page" \
                || fail 'The public preview release list could not be downloaded'
            count=$(python3 -c 'import json,sys; value=json.load(open(sys.argv[1])); print(len(value) if isinstance(value,list) else 0)' "$work/releases-$page.json")
            (( count < 100 )) && break
            (( page += 1 ))
        done
    fi
    python3 - "$work" "$channel" "$requested" "$asset_name" <<'PY'
import glob
import json
import os
import re
import sys
from urllib.parse import urlsplit

work, channel, requested, asset_name = sys.argv[1:]
stable = re.compile(r'v[0-9]+\.[0-9]+\.[0-9]+-rust\.[0-9]+\Z')
preview = re.compile(r'v[0-9]+\.[0-9]+\.[0-9]+-rust\.[0-9]+-rc\.[0-9]+\Z')
if requested:
    with open(os.path.join(work, 'release.json'), encoding='utf-8') as source:
        release = json.load(source)
    releases = [release] if isinstance(release, dict) else []
elif channel == 'stable':
    with open(os.path.join(work, 'release.json'), encoding='utf-8') as source:
        release = json.load(source)
    releases = [release] if isinstance(release, dict) else []
else:
    releases = []
    for filename in sorted(glob.glob(os.path.join(work, 'releases-*.json'))):
        with open(filename, encoding='utf-8') as source:
            page = json.load(source)
        if not isinstance(page, list):
            raise SystemExit('GitHub returned an invalid release list')
        releases.extend(page)

if requested:
    matches = [item for item in releases if item.get('tag_name') == requested]
    if len(matches) != 1:
        raise SystemExit('The requested tag did not identify exactly one published release')
    release = matches[0]
elif channel == 'preview':
    matches = [item for item in releases if not item.get('draft') and item.get('prerelease')
               and isinstance(item.get('tag_name'), str) and preview.fullmatch(item['tag_name'])]
    if not matches:
        raise SystemExit('The fork has no published preview release yet; use --bundle with a verified ZIP or publish a preview release first')
    release = max(matches, key=lambda item: item.get('published_at') or item.get('created_at') or '')
else:
    release = releases[0] if releases else None
    if release is None:
        raise SystemExit('The fork has no published stable release yet; use --bundle with a verified ZIP or publish a stable release first')

tag = release.get('tag_name')
if not isinstance(tag, str) or release.get('draft'):
    raise SystemExit('The selected GitHub release is not published')
if requested and tag != requested:
    raise SystemExit('The release API returned a different tag than requested')
if channel == 'stable':
    if release.get('prerelease') or not stable.fullmatch(tag):
        raise SystemExit('Stable releases must use a non-prerelease vX.Y.Z-rust.N tag')
else:
    if not release.get('prerelease') or not preview.fullmatch(tag):
        raise SystemExit('Preview releases must use a prerelease vX.Y.Z-rust.N-rc.N tag')
assets = release.get('assets')
if not isinstance(assets, list):
    raise SystemExit('Selected GitHub release has no asset list')
by_name = {}
for asset in assets:
    if isinstance(asset, dict) and isinstance(asset.get('name'), str):
        by_name.setdefault(asset['name'], []).append(asset)
zip_name = asset_name
checksum_name = asset_name + '.sha256'
if len(by_name.get(zip_name, [])) != 1 or len(by_name.get(checksum_name, [])) != 1:
    raise SystemExit('Selected release must contain one runtime ZIP and its .sha256 checksum asset')
urls = []
for name in (zip_name, checksum_name):
    url = by_name[name][0].get('browser_download_url')
    parsed = urlsplit(url or '')
    if parsed.scheme != 'https' or parsed.netloc != 'github.com' or not parsed.path.startswith('/beallio/decky-loader/releases/download/'):
        raise SystemExit('Selected release contains an unexpected asset download URL')
    urls.append(url)
print(json.dumps({'tag': tag, 'zip_url': urls[0], 'checksum_url': urls[1]}))
PY
}


target_installer() {
    python3 -B "$@"
}

run_target_installer() {
    if [[ -n ${failure_log:-} ]]; then
        target_installer "$@" 2>&1 | tee -a "$failure_log"
    else
        target_installer "$@"
    fi
}

# Prints "persisted<TAB>DIR" for an existing installation's saved unit directory,
# otherwise "manager<TAB>DIR" for the first safe manager UnitPath entry.
select_unit_directory() {
    python3 - "$1" "$2" "$homebrew" "$unit" <<'PY'
import pathlib
import pwd
import sys

script, username, homebrew, service = sys.argv[1:]
sys.path.insert(0, str(pathlib.Path(script).resolve().parent))
import install
account = pwd.getpwnam(username)
state = pathlib.Path(homebrew) / 'services' / '.decky-user-installer' / service
persisted = None
if (state / 'unit-directory.json').exists():
    persisted = install._load_persisted_unit_directory(state, account)
if persisted is not None:
    print('persisted\t' + persisted.as_posix())
else:
    print('manager\t' + install._user_unit_directory(account, create=False).as_posix())
PY
}

use_unit_directory() {
    local selection
    selection=$(select_unit_directory "$1" "$user") || return 1
    unit_directory_source=${selection%%$'\t'*}
    unit_directory=${selection#*$'\t'}
    [[ $unit_directory_source == persisted || $unit_directory_source == manager ]] || return 1
    [[ $unit_directory == /* ]] || return 1
}

capture_and_stop_user() {
    local installer_script=$1 state_file
    state_file=$(mktemp -- "$stage/stop-state.XXXXXXXXXX")
    if ! python3 - "$installer_script" "$user" "$unit" "$homebrew" "$unit_directory" "$state_file" <<'PY' 2>&1 | tee -a "${failure_log:-/dev/null}"
import json
import pathlib
import pwd
import sys

script, username, service, homebrew, expected_unit_directory, state_path = sys.argv[1:]
sys.path.insert(0, str(pathlib.Path(script).resolve().parent))
import install
account = pwd.getpwnam(username)
unit_directory = install._user_unit_directory(account, create=False, value=expected_unit_directory)
paths = {
    'unit': unit_directory / service,
    'unit_dir': unit_directory,
    'scope': 'user',
    'user': account,
}
properties = install._unit_properties(paths)
install._assert_user_unit_source(paths)
if properties.get('LoadState') == 'loaded':
    working = install._systemctl(
        paths, 'show', service, '--property=WorkingDirectory', check=False, timeout=15
    ).stdout
    value = next((line.partition('=')[2] for line in working.splitlines()
                  if line.startswith('WorkingDirectory=')), '')
    expected = (pathlib.Path(homebrew).resolve(strict=False) / 'services').as_posix()
    if value != expected:
        raise SystemExit(
            f'refusing to stop {service}: WorkingDirectory {value!r} is not {expected!r}'
        )
was_running = properties.get('ActiveState') in ('active', 'activating', 'reloading')
install.stop_service(service, scope='user', user=account)
# Only this file is machine-readable; shutdown diagnostics stay in the log.
pathlib.Path(state_path).write_text(json.dumps({
    'running': was_running,
    'unit_directory': unit_directory.as_posix(),
}), encoding='utf-8')
PY
    then
        rm -f -- "$state_file"
        return 1
    fi
    prior_user_running=$(python3 -c 'import json,sys; from pathlib import Path; print("1" if json.loads(Path(sys.argv[1]).read_text())["running"] else "0")' "$state_file") || return 1
    rm -f -- "$state_file"
    user_stop_confirmed=1
}

restart_prior_user() {
    local installer_script=$1
    python3 - "$installer_script" "$user" "$unit" "$unit_directory" <<'PY'
import pathlib
import pwd
import sys

script, username, service, unit_directory = sys.argv[1:]
sys.path.insert(0, str(pathlib.Path(script).resolve().parent))
import install
account = pwd.getpwnam(username)
directory = install._user_unit_directory(account, create=False, value=unit_directory)
paths = {
    'unit': directory / service,
    'unit_dir': directory,
    'scope': 'user',
    'user': account,
}
install._systemctl(paths, 'start', service, timeout=45)
install._wait_running(paths)
PY
}

backup_preflight() {
    python3 - "$homebrew" "$backup_directory" "$root_archive_exclusions" <<'PY'
import os
from pathlib import Path
import shutil
import stat
import sys

MARGIN = 64 * 1024 * 1024
EXCLUDED = {
    Path('services/.decky-installer'),
    Path('services/.decky-deploy'),
    Path('services/.decky-legacy-system-migration'),
}
homebrew, backup = map(Path, sys.argv[1:3])
allow_root_exclusions = sys.argv[3] == 'true'
if not homebrew.is_absolute() or not backup.is_absolute():
    raise SystemExit('Homebrew and backup directories must be absolute paths')
homebrew = homebrew.resolve(strict=False)
if backup.is_symlink():
    raise SystemExit('--backup-dir must not be a symlink')
backup = backup.resolve(strict=False)
if backup == homebrew or homebrew in backup.parents:
    raise SystemExit('--backup-dir must be outside the homebrew directory')
if backup.exists():
    info = backup.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022 or not os.access(backup, os.W_OK | os.X_OK)):
        raise SystemExit(f'Backup directory must be a safe writable directory owned by this account: {backup}')

def existing_directory(path):
    current = Path(path)
    while not current.exists():
        current = current.parent
    if not current.is_dir():
        current = current.parent
    return current.resolve(strict=True)
if not backup.exists():
    parent = existing_directory(backup.parent)
    info = parent.stat()
    if not os.access(parent, os.W_OK | os.X_OK):
        raise SystemExit(f'Backup directory parent is not writable: {parent}')
    if info.st_uid != os.geteuid() and not (info.st_mode & stat.S_ISVTX):
        raise SystemExit(f'Backup directory parent is not privately owned: {parent}')

total = 0
archive_overhead = 0
present = homebrew.exists()
if present:
    if not homebrew.is_dir():
        raise SystemExit(f'Homebrew destination is not a directory: {homebrew}')
    def visit(directory, relative):
        global total, archive_overhead
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as error:
            raise SystemExit(f'Cannot read homebrew directory {directory}: {error}') from error
        for entry in entries:
            rel = relative / entry.name
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise SystemExit(f'Cannot inspect homebrew item {path}: {error}') from error
            if rel in EXCLUDED and allow_root_exclusions:
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise SystemExit(f'Refusing to exclude non-root legacy control data: {path}')
                continue
            archive_name_bytes = len(os.fsencode('homebrew/' + rel.as_posix()))
            link_name_bytes = 0
            if stat.S_ISLNK(info.st_mode):
                try:
                    link_name_bytes = len(os.fsencode(os.readlink(path)))
                except OSError as error:
                    raise SystemExit(f'Cannot read homebrew symlink {path}: {error}') from error
            archive_overhead += 1024 + 2 * (archive_name_bytes + link_name_bytes)
            if stat.S_ISDIR(info.st_mode):
                visit(path, rel)
            elif stat.S_ISREG(info.st_mode):
                try:
                    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, 'O_NOFOLLOW', 0))
                    try:
                        current = os.fstat(descriptor)
                        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                            raise SystemExit(f'Homebrew item changed during preflight: {path}')
                    finally:
                        os.close(descriptor)
                except OSError as error:
                    raise SystemExit(f'Cannot read homebrew file {path}: {error}') from error
                total += info.st_size
            elif stat.S_ISLNK(info.st_mode):
                total += info.st_size
            else:
                raise SystemExit(f'Unsupported homebrew file type; refusing an incomplete archive: {path}')
    visit(homebrew, Path())

def device(path):
    directory = existing_directory(path)
    return directory.stat().st_dev, shutil.disk_usage(directory).free

needs = {}
available = {}
labels = {}
def add(path, size, label):
    dev, free = device(path)
    needs[dev] = needs.get(dev, 0) + size
    available[dev] = free
    labels.setdefault(dev, []).append(label)

if present:
    add(backup, total + archive_overhead + total // 10 + MARGIN, 'homebrew archive')
    binary = homebrew / 'services' / 'PluginLoader'
    try:
        info = binary.lstat()
    except FileNotFoundError:
        binary_size = 0
    else:
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            raise SystemExit(f'Existing backend binary has an unsupported type: {binary}')
        binary_size = info.st_size
    add(homebrew / 'services', binary_size + MARGIN, 'old-binary snapshot')
for dev, needed in needs.items():
    if available[dev] < needed:
        raise SystemExit(
            f'Insufficient free space on filesystem {dev} for {", ".join(labels[dev])}: '
            f'{available[dev]} bytes available, {needed} bytes required'
        )
print('Homebrew storage preflight complete.')
PY
}

create_homebrew_archive() {
    [[ -d $homebrew ]] || return 0
    mkdir -p -- "$backup_directory"
    partial_archive=$(mktemp -- "$backup_directory/.decky-homebrew-backup.XXXXXXXX")
    local result
    result=$(python3 - "$homebrew" "$partial_archive" "$unit" "$root_archive_exclusions" <<'PY'
import errno
import os
from pathlib import Path
import stat
import sys
import tarfile
import time

homebrew, temporary = (Path(value) for value in sys.argv[1:3])
service = sys.argv[3]
root_controls = sys.argv[4] == 'true'
homebrew = homebrew.resolve(strict=True)
temporary = temporary.absolute()
backup_directory = temporary.parent
backup_real = backup_directory.resolve(strict=True)
if backup_real == homebrew or homebrew in backup_real.parents:
    raise SystemExit('--backup-dir must remain outside the homebrew directory')
EXCLUDED = {
    Path('services/.decky-installer'),
    Path('services/.decky-deploy'),
    Path('services/.decky-legacy-system-migration'),
}
backup_info = backup_directory.lstat()
if (not stat.S_ISDIR(backup_info.st_mode) or backup_info.st_uid != os.geteuid()
        or backup_info.st_mode & 0o022):
    raise SystemExit(f'Unsafe backup directory: {backup_directory}')
temporary_info = temporary.lstat()
if (not stat.S_ISREG(temporary_info.st_mode) or temporary_info.st_uid != os.geteuid()
        or temporary_info.st_nlink != 1):
    raise SystemExit(f'Unsafe partial archive: {temporary}')

ACL_TAGS = {0x01: 'user', 0x02: 'user', 0x04: 'group', 0x08: 'group', 0x10: 'mask', 0x20: 'other'}

def acl_text(raw, path):
    # Convert the kernel's POSIX ACL xattr to the text form GNU tar --acls restores.
    if len(raw) < 4 or (len(raw) - 4) % 8 or int.from_bytes(raw[:4], 'little') != 2:
        raise SystemExit(f'Unsupported POSIX ACL on homebrew item: {path}')
    entries = []
    for offset in range(4, len(raw), 8):
        tag = int.from_bytes(raw[offset:offset + 2], 'little')
        perm = int.from_bytes(raw[offset + 2:offset + 4], 'little')
        ident = int.from_bytes(raw[offset + 4:offset + 8], 'little')
        if tag not in ACL_TAGS:
            raise SystemExit(f'Unsupported POSIX ACL entry on homebrew item: {path}')
        qualifier = str(ident) if tag in (0x02, 0x08) else ''
        bits = ('r' if perm & 4 else '-') + ('w' if perm & 2 else '-') + ('x' if perm & 1 else '-')
        entries.append(f'{ACL_TAGS[tag]}:{qualifier}:{bits}')
    return '\n'.join(entries)

def metadata_headers(target, path):
    # target is a descriptor for regular files and a path otherwise.
    follow = {} if isinstance(target, int) else {'follow_symlinks': False}
    try:
        names = os.listxattr(target, **follow)
    except OSError as error:
        if error.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            return {}
        raise SystemExit(f'Cannot read extended attributes of homebrew item {path}: {error}') from error
    headers = {}
    for name in sorted(names):
        try:
            value = os.getxattr(target, name, **follow)
        except OSError as error:
            raise SystemExit(f'Cannot read extended attribute {name} of homebrew item {path}: {error}') from error
        if name == 'system.posix_acl_access':
            headers['SCHILY.acl.access'] = acl_text(value, path)
        elif name == 'system.posix_acl_default':
            headers['SCHILY.acl.default'] = acl_text(value, path)
        else:
            headers['SCHILY.xattr.' + name] = value.decode('utf-8', 'surrogateescape')
    return headers

def tar_info(name, info, kind, headers):
    item = tarfile.TarInfo(name)
    item.mode = stat.S_IMODE(info.st_mode)
    item.uid = info.st_uid
    item.gid = info.st_gid
    item.mtime = info.st_mtime
    item.uname = ''
    item.gname = ''
    item.type = kind
    item.pax_headers = headers
    if kind == tarfile.REGTYPE:
        item.size = info.st_size
    return item

def add_tree(archive, path, relative, info):
    if relative in EXCLUDED and root_controls:
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise SystemExit(f'Refusing to exclude non-root legacy control data: {path}')
        return
    name = 'homebrew' if not relative.parts else 'homebrew/' + relative.as_posix()
    if stat.S_ISDIR(info.st_mode):
        archive.addfile(tar_info(name, info, tarfile.DIRTYPE, metadata_headers(path, path)))
        try:
            entries = sorted(os.scandir(path), key=lambda entry: entry.name)
        except OSError as error:
            raise SystemExit(f'Cannot read homebrew directory {path}: {error}') from error
        for entry in entries:
            child_path = path / entry.name
            child_relative = relative / entry.name
            try:
                child_info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise SystemExit(f'Cannot inspect homebrew item {child_path}: {error}') from error
            add_tree(archive, child_path, child_relative, child_info)
    elif stat.S_ISREG(info.st_mode):
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as source:
            current = os.fstat(source.fileno())
            if (current.st_dev, current.st_ino, current.st_size) != (info.st_dev, info.st_ino, info.st_size):
                raise SystemExit(f'Homebrew item changed while creating its archive: {path}')
            archive.addfile(tar_info(name, info, tarfile.REGTYPE, metadata_headers(source.fileno(), path)), source)
            after = os.fstat(source.fileno())
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
            ):
                raise SystemExit(f'Homebrew item changed while creating its archive: {path}')
    elif stat.S_ISLNK(info.st_mode):
        item = tar_info(name, info, tarfile.SYMTYPE, metadata_headers(path, path))
        item.linkname = os.readlink(path)
        archive.addfile(item)
    else:
        raise SystemExit(f'Unsupported homebrew file type; refusing an incomplete archive: {path}')

descriptor = os.open(temporary, os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC | getattr(os, 'O_NOFOLLOW', 0))
with os.fdopen(descriptor, 'wb') as output:
    with tarfile.open(fileobj=output, mode='w', format=tarfile.PAX_FORMAT) as archive:
        root_info = homebrew.lstat()
        if not stat.S_ISDIR(root_info.st_mode):
            raise SystemExit(f'Homebrew destination is not a real directory: {homebrew}')
        add_tree(archive, homebrew, Path(), root_info)
    output.flush()
    os.fsync(output.fileno())

stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
stem = f'homebrew-{service}-{stamp}-{os.getpid()}'
for suffix in range(1000):
    final = backup_directory / (stem if suffix == 0 else f'{stem}-{suffix}')
    final = final.with_name(final.name + '.tar')
    try:
        os.link(temporary, final, follow_symlinks=False)
    except FileExistsError:
        continue
    os.chmod(final, 0o600, follow_symlinks=False)
    os.unlink(temporary)
    print(final)
    break
else:
    raise SystemExit('Could not allocate a unique homebrew archive name')
PY
    ) || return 1
    partial_archive=''
    printf 'Homebrew archive created: %s\n' "$result"
}

protected_legacy_helper() {
    python3 - "$target_uid" "$unit" "$homebrew" <<'PY'
import json
import os
from pathlib import Path
import stat
import sys

uid, unit, homebrew = sys.argv[1:]
base = Path('/var/lib/decky-loader-runtime/legacy-migrations')
service_directory = base / uid / unit
helper_directory = service_directory / 'helper'
required_directories = (
    Path('/var'), Path('/var/lib'), Path('/var/lib/decky-loader-runtime'),
    base, base / uid, service_directory, helper_directory,
)
for directory in required_directories:
    try:
        info = directory.lstat()
    except OSError as error:
        raise SystemExit(f'Protected legacy helper directory is unavailable: {directory}: {error}') from error
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022):
        raise SystemExit(f'Unsafe protected legacy helper directory: {directory}')
helper = helper_directory / 'install.py'
observer = helper_directory / 'process_observer.py'
for path in (helper, observer):
    try:
        info = path.lstat()
    except OSError as error:
        raise SystemExit(f'Protected legacy helper is unavailable: {path}: {error}') from error
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit(f'Unsafe protected legacy helper file: {path}')
status_path = service_directory / 'status.json'
try:
    status_info = status_path.lstat()
    if (not stat.S_ISREG(status_info.st_mode) or status_info.st_uid != 0
            or status_info.st_mode & 0o022):
        raise SystemExit(f'Unsafe protected legacy status file: {status_path}')
    status = json.loads(status_path.read_text(encoding='utf-8'))
except OSError as error:
    raise SystemExit(f'Protected legacy status is unavailable: {error}') from error
expected_homebrew = Path(homebrew).resolve(strict=False).as_posix()
if not isinstance(status, dict):
    raise SystemExit('Protected legacy status is invalid')
schema = status.get('schema')
if (type(schema) not in (int, str) or not schema
        or status.get('user_uid') != int(uid) or status.get('service') != unit
        or status.get('homebrew') != expected_homebrew
        or not isinstance(status.get('phase'), str) or status['phase'] == 'restored'):
    raise SystemExit('Protected legacy status does not match the selected user, service, and homebrew')
print(helper)
PY
}

# Values referenced by EXIT handling are deliberately global: the trap can run
# after target_action has unwound and must still know whether legacy restore is due.
target_cleanup() {
    local result=$?
    trap - EXIT
    trap '' INT TERM HUP
    if (( result != 0 && installer_transaction_started )); then
        if [[ -n ${transaction_installer:-} && -f $transaction_installer ]]; then
            if ! run_target_installer "$transaction_installer" status \
                --homebrew "$homebrew" --service-name "$unit"; then
                printf 'Error: installer transaction recovery is pending; retained installer: %s\n' \
                    "$transaction_installer" >&2
                result=1
            fi
        else
            printf 'Error: installer transaction recovery is pending, but its retained installer is unavailable.\n' >&2
            result=1
        fi
    fi
    if (( result != 0 && legacy_migration_attempted )); then
        if [[ -r ${legacy_record:-} ]]; then
            local helper_path
            if helper_path=$(protected_legacy_helper); then
                legacy_restore_command=("${sudo_args[@]}" python3 -B "$helper_path" restore-legacy-system \
                    --homebrew "$homebrew" --service-name "$unit" --user "$user" \
                    --user-unit-directory "$unit_directory")
                if [[ -n ${failure_log:-} ]]; then
                    if ! "${legacy_restore_command[@]}" 2>&1 | tee -a "$failure_log"; then
                        printf 'Error: automatic legacy system-service restore failed. Retry explicitly with --restore-legacy.\n' >&2
                        result=1
                    else
                        legacy_migration_attempted=0
                    fi
                elif ! "${legacy_restore_command[@]}"; then
                    printf 'Error: automatic legacy system-service restore failed. Retry explicitly with --restore-legacy.\n' >&2
                    result=1
                else
                    legacy_migration_attempted=0
                fi
            else
                printf 'Error: protected legacy restore helper is unavailable; retry recovery explicitly with --restore-legacy.\n' >&2
                result=1
            fi
        else
            printf 'Error: migration recovery status is unavailable; the system service may require explicit recovery.\n' >&2
            result=1
        fi
    fi
    if (( result != 0 && user_stop_confirmed && prior_user_running
            && !installer_transaction_started && !legacy_migration_attempted
            && !restore_helper_started )); then
        if [[ -n ${restart_installer:-} ]] && restart_prior_user "$restart_installer" \
            2>&1 | tee -a "${failure_log:-/dev/null}"; then
            user_stop_confirmed=0
        else
            printf 'Error: previously active user backend could not be restarted after backup failure.\n' >&2
            result=1
        fi
    fi
    if [[ -n ${partial_archive:-} ]]; then
        if python3 - "$partial_archive" "$EUID" <<'PY'
import os
from pathlib import Path
import stat
import sys
path = Path(sys.argv[1])
try:
    info = path.lstat()
except FileNotFoundError:
    raise SystemExit(0)
if not stat.S_ISREG(info.st_mode) or info.st_uid != int(sys.argv[2]) or info.st_nlink != 1:
    raise SystemExit(f'refusing to remove an unowned partial archive: {path}')
PY
        then
            rm -f -- "$partial_archive"
        else
            printf 'Warning: partial archive was not removed because ownership could not be verified: %s\n' "$partial_archive" >&2
            result=1
        fi
    fi
    if [[ -n ${kit_partial:-} ]]; then
        if python3 - "$kit_partial" "$EUID" <<'PY'
import os
from pathlib import Path
import stat
import sys
path = Path(sys.argv[1])
try:
    info = path.lstat()
except FileNotFoundError:
    raise SystemExit(0)
if not stat.S_ISREG(info.st_mode) or info.st_uid != int(sys.argv[2]) or info.st_nlink != 1:
    raise SystemExit(f'refusing to remove an unowned installer-kit temporary: {path}')
PY
        then
            rm -f -- "$kit_partial"
        else
            printf 'Warning: installer-kit temporary was not removed because ownership could not be verified: %s\n' "$kit_partial" >&2
            result=1
        fi
    fi
    if (( result == 0 )) && [[ -n ${failure_log:-} ]]; then
        if python3 - "$failure_log" "$EUID" <<'PY'
import stat
import sys
from pathlib import Path
path = Path(sys.argv[1])
try:
    info = path.lstat()
except FileNotFoundError:
    raise SystemExit(0)
if not stat.S_ISREG(info.st_mode) or info.st_uid != int(sys.argv[2]) or info.st_nlink != 1:
    raise SystemExit(f'refusing to remove an unowned successful-run log: {path}')
PY
        then
            rm -f -- "$failure_log"
        else
            printf 'Warning: successful-run log was not removed because ownership could not be verified: %s\n' "$failure_log" >&2
            result=1
        fi
    fi
    if (( stage_owned )) && [[ -n ${stage:-} && -d $stage && ! -L $stage ]]; then
        if [[ $(stat -c '%u' -- "$stage") == "$EUID" ]]; then
            if ! rm -rf -- "$stage"; then
                printf 'Warning: deployment staging may remain at %s\n' "$stage" >&2
                result=1
            fi
        else
            printf 'Warning: deployment staging was not removed because ownership changed: %s\n' "$stage" >&2
            result=1
        fi
    fi
    if (( result != 0 )) && [[ -n ${failure_log:-} && -f $failure_log ]]; then
        printf 'Installer log: %s\n' "$failure_log" >&2
    fi
    exit "$result"
}

target_action() {
    action=$1
    homebrew=$2
    user=$3
    service=$4
    expected=$5
    version=$6
    listen=$7
    cef=$8
    inject=$9
    migrate_legacy=${10}
    non_interactive=${11}
    backup_directory=${12}
    need realpath
    stage=$(realpath -- "$(dirname -- "${BASH_SOURCE[0]}")")
    need id
    need rm
    need stat
    [[ $(basename -- "$stage") == decky-user-deploy.???????? ]] || fail 'Invalid target staging directory'
    [[ $EUID != 0 ]] || fail 'Run normal installation as the non-root plugin account; sudo is used only for explicit legacy migration/restore'
    target_uid=$(id -u "$user") || fail "Unix account not found: $user"
    [[ $target_uid == "$EUID" && $target_uid != 0 ]] || fail "Run this operation as the selected non-root account: $user"
    [[ $homebrew == /* && $homebrew != / && $homebrew != /home ]] || fail 'Use an absolute homebrew directory, not / or /home'
    [[ -n $backup_directory ]] || backup_directory="$(dirname -- "$homebrew")/decky-user-backups"
    [[ $backup_directory == /* ]] || fail '--backup-dir must be an absolute path'
    unit="${service%.service}.service"
    [[ $unit =~ ^[a-zA-Z0-9][a-zA-Z0-9_.@-]*\.service$ ]] || fail 'Invalid service name'
    [[ $inject == true || $inject == false ]] || fail 'inject-frontend must be true or false'
    [[ $migrate_legacy == true || $migrate_legacy == false ]] || fail 'Invalid legacy migration selection'
    export PYTHONDONTWRITEBYTECODE=1 TMPDIR="${TMPDIR:-/var/tmp/omp-1000}"
    [[ $TMPDIR == /* ]] || fail 'TMPDIR must be absolute'
    [[ -d $TMPDIR ]] || mkdir -p -- "$TMPDIR"
    [[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || fail 'This bundle requires Linux x86-64'
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python 3.10 or newer is required")'
    kit="$homebrew/services/.decky-user-deploy/$unit"
    legacy_root="/var/lib/decky-loader-runtime/legacy-migrations/$target_uid/$unit"
    legacy_record="$legacy_root/status.json"
    legacy_helper="$legacy_root/helper/install.py"
    sudo_args=(sudo -n)
    if [[ $non_interactive != true && -t 0 && -t 1 ]]; then sudo_args=(sudo); fi
    stage_owned=0
    partial_archive=''
    kit_partial=''
    failure_log=''
    prior_user_running=0
    user_stop_confirmed=0
    installer_transaction_started=0
    restore_helper_started=0
    legacy_migration_attempted=0
    transaction_installer=''
    restart_installer=''
    legacy_restore_command=()
    unit_directory=''
    unit_directory_source=''
    root_archive_exclusions=false
    if [[ $migrate_legacy == true ]]; then
        root_archive_exclusions=true
    elif [[ -r $legacy_record ]] && protected_legacy_helper >/dev/null 2>&1; then
        root_archive_exclusions=true
    fi
    if ! python3 - "$stage" "$EUID" "$TMPDIR" <<'PY'
import os
from pathlib import Path
import stat
import sys
path = Path(sys.argv[1])
info = path.lstat()
expected_parent = Path(sys.argv[3]).resolve(strict=True)
if (not stat.S_ISDIR(info.st_mode) or info.st_uid != int(sys.argv[2])
        or info.st_mode & 0o077 or path.parent != expected_parent):
    raise SystemExit(f'refusing to use or remove an unowned or untrusted staging directory: {path}')
PY
    then
        fail 'Target staging directory is not an owned private directory under TMPDIR'
    fi
    stage_owned=1
    trap target_cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM HUP

    if [[ $action == status ]]; then
        [[ -f $kit/install.py && -f $kit/process_observer.py ]] || fail "No retained user installer kit at $kit"
        run_target_installer "$kit/install.py" status --homebrew "$homebrew" --service-name "$unit"
        exit 0
    fi

    if [[ $action == undo ]]; then
        [[ -f $kit/install.py && -f $kit/process_observer.py ]] || fail "No retained user installer kit at $kit"
        use_unit_directory "$kit/install.py" || fail 'Could not select the user unit directory'
        restart_installer=$kit/install.py
        backup_preflight
        capture_and_stop_user "$restart_installer" || fail 'Selected user service did not drain; rollback cancelled'
        backup_preflight
        create_homebrew_archive
        if (( prior_user_running )); then
            restart_prior_user "$restart_installer" || fail 'Previously active user backend could not be restarted after its backup'
            user_stop_confirmed=0
        fi
        failure_log=$(mktemp -- "$kit/failure.XXXXXXXXXX")
        chmod 0600 -- "$failure_log"
        installer_transaction_started=1
        transaction_installer=$kit/install.py
        run_target_installer "$transaction_installer" rollback --homebrew "$homebrew" --service-name "$unit"
        installer_transaction_started=0
        exit 0
    fi

    if [[ $action == restore-legacy ]]; then
        need sudo
        legacy_helper=$(protected_legacy_helper) || fail 'No safe protected legacy restore helper is available'
        root_archive_exclusions=true
        use_unit_directory "$legacy_helper" || fail 'Could not select the user unit directory'
        restart_installer=$legacy_helper
        backup_preflight
        capture_and_stop_user "$restart_installer" || fail 'Selected user service did not drain; legacy restore cancelled'
        backup_preflight
        create_homebrew_archive
        if [[ -f $kit/install.py && -f $kit/process_observer.py ]]; then
            run_target_installer "$kit/install.py" status --homebrew "$homebrew" --service-name "$unit"
        fi
        legacy_restore_args=(--homebrew "$homebrew" --service-name "$unit" --user "$user")
        # The protected migration record owns its unit directory unless the user install saved one.
        if [[ $unit_directory_source == persisted ]]; then
            legacy_restore_args+=(--user-unit-directory "$unit_directory")
        fi
        restore_helper_started=1
        "${sudo_args[@]}" python3 -B "$legacy_helper" restore-legacy-system "${legacy_restore_args[@]}" \
            2>&1 | tee -a "${failure_log:-/dev/null}"
        exit 0
    fi

    [[ $action == install || $action == check ]] || fail "Unsupported target operation: $action"
    [[ -f $stage/runtime.zip ]] || fail 'Runtime ZIP was not staged on the target'
    need sha256sum
    local actual inspection unpacked
    read -r actual _ < <(sha256sum -- "$stage/runtime.zip")
    [[ $actual == "$expected" ]] || fail 'Runtime ZIP checksum mismatch'
    inspection=$(unpack_bundle "$stage/runtime.zip" "$version" "$stage/bundle" \
        "$homebrew" "$backup_directory" "$homebrew/services" inspect "$root_archive_exclusions" "$migrate_legacy")
    unpacked=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1].splitlines()[-1])["unpacked_bytes"])' "$inspection")
    printf 'Verified ZIP metadata for %s (%s extracted bytes).\n' "$version" "$unpacked"
    unpack_bundle "$stage/runtime.zip" "$version" "$stage/bundle" \
        "$homebrew" "$backup_directory" "$homebrew/services" extract "$root_archive_exclusions" "$migrate_legacy" >/dev/null
    printf 'Runtime bundle extracted and payload checksums verified.\n'
    run_target_installer "$stage/bundle/install.py" verify-bundle --bundle "$stage/bundle" \
        --version "$version" --homebrew "$homebrew" --service-name "$unit" --user "$user"
    use_unit_directory "$stage/bundle/install.py" || fail 'Could not select the user unit directory'
    local migration_args=()
    if [[ $migrate_legacy == true ]]; then migration_args+=(--migrate-legacy); fi
    run_target_installer "$stage/bundle/install.py" check-bundle --bundle "$stage/bundle" \
        --version "$version" --homebrew "$homebrew" --service-name "$unit" --user "$user" \
        "${migration_args[@]}"
    if [[ $action == check ]]; then
        printf 'Preflight complete. No service or installation state was changed.\n'
        exit 0
    fi

    if [[ $migrate_legacy == true ]]; then
        need sudo
        legacy_migration_attempted=1
        legacy_restore_command=("${sudo_args[@]}" python3 -B "$legacy_helper" restore-legacy-system \
            --homebrew "$homebrew" --service-name "$unit" --user "$user" \
            --user-unit-directory "$unit_directory")
        "${sudo_args[@]}" python3 -B "$stage/bundle/install.py" migrate-legacy-system \
            --homebrew "$homebrew" --service-name "$unit" --user "$user" \
            --user-unit-directory "$unit_directory" \
            2>&1 | tee -a "${failure_log:-/dev/null}"
        legacy_helper=$(protected_legacy_helper) || fail 'Legacy migration did not preserve a safe protected restore helper'
    fi

    restart_installer=$stage/bundle/install.py
    backup_preflight
    capture_and_stop_user "$restart_installer" || fail 'Selected user service did not drain; installation cancelled'
    backup_preflight
    create_homebrew_archive
    if (( prior_user_running )); then
        restart_prior_user "$restart_installer" || fail 'Previously active user backend could not be restarted after its backup'
        user_stop_confirmed=0
    fi
    run_target_installer "$stage/bundle/install.py" check-bundle --bundle "$stage/bundle" \
        --version "$version" --homebrew "$homebrew" --service-name "$unit" --user "$user"

    need install
    need mv
    python3 - "$homebrew" "$unit" <<'PY'
import os
from pathlib import Path
import stat
import sys
homebrew, unit = Path(sys.argv[1]), sys.argv[2]
services = homebrew / 'services'
services.mkdir(mode=0o755, parents=True, exist_ok=True)
info = services.lstat()
if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022 or not os.access(services, os.W_OK | os.X_OK):
    raise SystemExit(f'User cannot safely manage service files in {services}')
root = services / '.decky-user-deploy'
root.mkdir(mode=0o700, exist_ok=True)
for path in (root, root / unit):
    if path == root / unit:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise SystemExit(f'Unsafe or unowned installer kit directory: {path}')
PY
    failure_log=$(mktemp -- "$kit/failure.XXXXXXXXXX")
    chmod 0600 -- "$failure_log"
    kit_partial=$(mktemp -- "$kit/.install.py.XXXXXXXXXX")
    install -m 0755 -- "$stage/bundle/install.py" "$kit_partial"
    mv -f -- "$kit_partial" "$kit/install.py"
    kit_partial=''
    kit_partial=$(mktemp -- "$kit/.process_observer.py.XXXXXXXXXX")
    install -m 0644 -- "$stage/bundle/process_observer.py" "$kit_partial"
    mv -f -- "$kit_partial" "$kit/process_observer.py"
    kit_partial=''
    installer_transaction_started=1
    transaction_installer=$kit/install.py
    run_target_installer "$transaction_installer" install --bundle "$stage/bundle" --homebrew "$homebrew" \
        --user "$user" --service-name "$unit" --version "$version" --worker-mode fork \
        --listen "$listen" --cef-url "$cef" --inject-frontend "$inject"
    installer_transaction_started=0
    legacy_migration_attempted=0
    printf 'Installation complete for %s as %s.\n' "$unit" "$user"
    exit 0
}

if [[ ${1:-} == --_target ]]; then
    shift
    [[ $# == 12 ]] || fail 'Invalid internal deployment arguments'
    stage='' legacy_migration_attempted=0
    legacy_restore_command=()
    target_action "$@"
    exit 0
fi

action=install
remote_host=''
bundle_archive=''
expected=''
version=''
channel=stable
channel_set=false
user=${DECK_USER:-deck}
homebrew=''
backup_directory=''
service=plugin_loader
listen=127.0.0.1:1337
cef=http://127.0.0.1:8080
inject=true
non_interactive=false
migrate_legacy=false
restore_legacy=false
while (( $# )); do
    case $1 in
        --help|-h) show_help; exit 0 ;;
        --undo|--status|--check)
            [[ $action == install ]] || fail 'Choose only one of --undo, --status, or --check'
            action=${1#--}; shift ;;
        --restore-legacy)
            [[ $action == install && $restore_legacy == false ]] || fail 'Choose only one operation'
            action=restore-legacy; restore_legacy=true; shift ;;
        --migrate-legacy) migrate_legacy=true; shift ;;
        --non-interactive) non_interactive=true; shift ;;
        --remote|--bundle|--sha256|--version|--channel|--homebrew|--backup-dir|--service|--listen|--cef-url|--inject-frontend)
            (( $# >= 2 )) || fail "Missing value for $1"
            case $1 in
                --remote) [[ -n $2 ]] || fail '--remote requires a host'; remote_host=$2 ;;
                --bundle) bundle_archive=$2 ;;
                --sha256) expected=$2 ;;
                --version) version=$2 ;;
                --channel) channel=$2; channel_set=true ;;
                --homebrew) homebrew=$2 ;;
                --backup-dir) [[ -n $2 ]] || fail '--backup-dir requires a path'; backup_directory=$2 ;;
                --service) service=$2 ;;
                --listen) listen=$2 ;;
                --cef-url) cef=$2 ;;
                --inject-frontend) inject=$2 ;;
            esac
            shift 2 ;;
        *) fail "Unknown option: $1 (use --help)" ;;
    esac
done
[[ $channel == stable || $channel == preview ]] || fail '--channel must be stable or preview'
[[ $user =~ ^[a-z_][a-z0-9_-]*$ && $user != root ]] || fail 'DECK_USER must name a non-root target account'
[[ $inject == true || $inject == false ]] || fail '--inject-frontend must be true or false'
[[ -n $homebrew ]] || homebrew="/home/$user/homebrew"
[[ $homebrew == /* && $homebrew != / && $homebrew != /home ]] || fail 'Use an absolute homebrew directory, not / or /home'
[[ -z $backup_directory || $backup_directory == /* ]] || fail '--backup-dir must be an absolute path'
if [[ $migrate_legacy == true && $action != install && $action != check ]]; then
    fail '--migrate-legacy is only valid with installation or --check'
fi
if [[ $restore_legacy == true && ( -n $bundle_archive || -n $expected || -n $version || $migrate_legacy == true ) ]]; then
    fail '--restore-legacy does not take bundle, release, or migration options'
fi
if [[ $action == status || $action == undo || $action == restore-legacy ]]; then
    [[ -z $bundle_archive && -z $expected && -z $version ]] || fail 'Status, undo, and restore operations do not use bundle or release options'
fi
if [[ -n $backup_directory && $action != install && $action != check
        && $action != undo && $action != restore-legacy ]]; then
    fail '--backup-dir is only valid for installation, preflight, rollback, or legacy restoration'
fi
if [[ $remote_host != '' ]]; then
    [[ $remote_host != -* && $remote_host != *[[:space:]]* && $remote_host != *@* ]] || fail 'Invalid --remote host'
    need ssh
    need scp
fi

if [[ $action == install || $action == check ]]; then
    if [[ -n $bundle_archive ]]; then
        [[ -f $bundle_archive ]] || fail "Runtime ZIP not found: $bundle_archive"
        [[ $expected =~ ^[[:xdigit:]]{64}$ ]] || fail '--bundle requires --sha256 with 64 hexadecimal characters'
        [[ -n $version ]] || fail '--bundle requires --version'
        expected=${expected,,}
        if [[ $version =~ ^v[0-9]+\.[0-9]+\.[0-9]+-rust\.[0-9]+$ ]]; then
            [[ $channel_set == false || $channel == stable ]] || fail 'Stable bundle version conflicts with --channel preview'
        elif [[ $version =~ ^v[0-9]+\.[0-9]+\.[0-9]+-rust\.[0-9]+-rc\.[0-9]+$ ]]; then
            [[ $channel_set == false || $channel == preview ]] || fail 'Preview bundle version conflicts with --channel stable'
        elif [[ $version != dev && $version != development ]]; then
            fail 'Offline bundle version must be dev/development or a Rust stable/preview release identity'
        fi
    else
        need curl
        need sha256sum
        need python3
        if [[ -n $version ]]; then
            if [[ $version =~ ^v[0-9]+\.[0-9]+\.[0-9]+-rust\.[0-9]+$ ]]; then
                [[ $channel_set == false || $channel == stable ]] || fail 'Stable release version conflicts with --channel preview'
                channel=stable
            elif [[ $version =~ ^v[0-9]+\.[0-9]+\.[0-9]+-rust\.[0-9]+-rc\.[0-9]+$ ]]; then
                [[ $channel_set == false || $channel == preview ]] || fail 'Preview release version conflicts with --channel stable'
                channel=preview
            else
                fail 'Published releases must use a Rust stable or preview tag'
            fi
        fi
    fi
fi

need python3
need realpath
need mktemp
need rm
if [[ $action == install || $action == check ]]; then
    need sha256sum
fi
tmpbase=${TMPDIR:-/var/tmp/omp-1000}
[[ $tmpbase == /* ]] || fail 'TMPDIR must be absolute'
[[ -d $tmpbase ]] || mkdir -p -- "$tmpbase"
work=$(mktemp -d "$tmpbase/decky-user-install.XXXXXXXX")
remote_stage=''
local_stage=''
ssh_opts=()
scp_opts=()
tty_args=()
target=''
scp_target=''
legacy_migration_attempted=0
legacy_restore_command=()
legacy_record=''
stage=''
unit=''
cleanup() {
    local result=$?
    trap - EXIT
    if [[ -n $remote_stage ]]; then
        local command
        printf -v command 'if [ -d %q ] && [ ! -L %q ] && [ "$(stat -c %%u -- %q)" = "$(id -u)" ]; then rm -rf -- %q; fi' \
            "$remote_stage" "$remote_stage" "$remote_stage" "$remote_stage"
        if ! ssh "${ssh_opts[@]}" "${tty_args[@]}" "$target" "$command"; then
            printf 'Warning: remote staging may remain at %s\n' "$remote_stage" >&2
            result=1
        fi
    fi
    if [[ -n $local_stage && ( -e $local_stage || -L $local_stage ) ]]; then
        if [[ -d $local_stage && ! -L $local_stage && $(stat -c '%u' -- "$local_stage") == "$EUID" ]]; then
            if ! rm -rf -- "$local_stage"; then
                printf 'Warning: local staging may remain at %s\n' "$local_stage" >&2
                result=1
            fi
        else
            printf 'Warning: local staging was not removed because ownership could not be verified: %s\n' "$local_stage" >&2
            result=1
        fi
    fi
    if [[ -d $work && ! -L $work && $(stat -c '%u' -- "$work") == "$EUID" ]]; then
        rm -rf -- "$work"
    else
        printf 'Warning: temporary release files were not removed because ownership could not be verified: %s\n' "$work" >&2
        result=1
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

if [[ $action == install || $action == check ]]; then
    if [[ -z $bundle_archive ]]; then
        need curl
        release_meta="$work/release-meta.json"
        fetch_release_metadata "$version" "$channel" > "$release_meta"
        version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tag"])' "$release_meta")
        zip_url=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["zip_url"])' "$release_meta")
        checksum_url=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["checksum_url"])' "$release_meta")
        curl -q --fail --location --proto '=https' --proto-redir '=https' --output "$work/$asset_name" "$zip_url" \
            || fail 'Published runtime ZIP download failed'
        curl -q --fail --location --proto '=https' --proto-redir '=https' --output "$work/$asset_name.sha256" "$checksum_url" \
            || fail 'Published runtime checksum download failed'
        sidecar_hash=$(python3 - "$work/$asset_name.sha256" "$asset_name" <<'PY'
import re
import sys
path, name = sys.argv[1:]
lines = [line.strip() for line in open(path, encoding='ascii') if line.strip()]
matched = []
for line in lines:
    fields = line.split()
    if len(fields) == 2 and fields[1].lstrip('*') == name:
        if not re.fullmatch(r'[0-9a-fA-F]{64}', fields[0]):
            raise SystemExit('Release sidecar contains an invalid checksum')
        matched.append(fields[0].lower())
if len(matched) != 1:
    raise SystemExit('Release sidecar must contain exactly one checksum for the runtime ZIP')
print(matched[0])
PY
        )
        if [[ -n $expected && ${expected,,} != "$sidecar_hash" ]]; then
            fail 'Supplied SHA256 does not match the published release checksum'
        fi
        expected=$sidecar_hash
        bundle_archive="$work/$asset_name"
    fi
    local_hash=$(sha256sum -- "$bundle_archive")
    actual=${local_hash%% *}
    [[ $actual == "$expected" ]] || fail 'Runtime ZIP checksum mismatch'
fi

if [[ -n $remote_host ]]; then
    if [[ -n ${DECK_PORT:-} ]]; then
        [[ $DECK_PORT =~ ^[0-9]+$ && ${#DECK_PORT} -le 5 ]] || fail 'Invalid DECK_PORT'
        (( 10#$DECK_PORT >= 1 && 10#$DECK_PORT <= 65535 )) || fail 'Invalid DECK_PORT'
        ssh_opts+=(-p "$DECK_PORT")
        scp_opts+=(-P "$DECK_PORT")
    fi
    if [[ $non_interactive == true ]]; then
        ssh_opts+=(-o BatchMode=yes)
        scp_opts+=(-o BatchMode=yes)
    elif [[ -t 0 && -t 1 ]]; then
        tty_args=(-t)
    fi
    target="$user@$remote_host"
    scp_target=$target
    if [[ $remote_host == *:* && $remote_host != \[*\] ]]; then scp_target="$user@[$remote_host]"; fi
    remote_stage=$(ssh "${ssh_opts[@]}" "$target" 'set -eu; umask 077; base=${TMPDIR:-/var/tmp/omp-1000}; if [ ! -d "$base" ]; then mkdir -p -- "$base"; fi; mktemp -d "$base/decky-user-deploy.XXXXXXXX"')
    [[ $remote_stage == /* && $remote_stage != *$'\n'* && $(basename -- "$remote_stage") == decky-user-deploy.???????? ]] || fail 'Unexpected remote staging path'
    scp "${scp_opts[@]}" -- "$(realpath -- "${BASH_SOURCE[0]}")" "$scp_target:$remote_stage/deploy.sh"
    if [[ $action == install || $action == check ]]; then
        scp "${scp_opts[@]}" -- "$bundle_archive" "$scp_target:$remote_stage/runtime.zip"
    fi
    target_args=(--_target "$action" "$homebrew" "$user" "$service" "$expected" "$version" "$listen" "$cef" "$inject" "$migrate_legacy" "$non_interactive" "$backup_directory")
    printf -v command '%q ' bash "$remote_stage/deploy.sh" "${target_args[@]}"
    ssh "${ssh_opts[@]}" "${tty_args[@]}" "$target" "$command"
    exit 0
fi

need cp
local_stage=$(mktemp -d "$tmpbase/decky-user-deploy.XXXXXXXX")
cp -- "$(realpath -- "${BASH_SOURCE[0]}")" "$local_stage/deploy.sh"
if [[ $action == install || $action == check ]]; then
    cp -- "$bundle_archive" "$local_stage/runtime.zip"
fi
target_args=(--_target "$action" "$homebrew" "$user" "$service" "$expected" "$version" "$listen" "$cef" "$inject" "$migrate_legacy" "$non_interactive" "$backup_directory")
bash "$local_stage/deploy.sh" "${target_args[@]}"
