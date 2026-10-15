#!/usr/bin/env bash
# Deploy a checked runtime bundle; install.py owns activation and rollback.
set -Eeuo pipefail
umask 077

fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || fail "Required command is missing: $1"; }

show_help() {
    cat <<'HELP'
Usage: bash install-to-deck.sh [options]

Without --bundle, download the pinned v3.2.10-rust.6-rc.1 CI artifact publicly
through nightly.link. No GitHub account, token, or GitHub CLI is required.
Run this saved script on the Deck in Desktop Mode. Local execution is the default;
no IP address or SSH is needed. No Rust/Python build tools are needed.
The automatic download requires curl and unzip.

  --remote HOST            Deploy over SSH instead of installing on this machine
  --bundle ZIP             Use the inner runtime ZIP from a CI artifact
  --sha256 HASH            Expected runtime ZIP checksum (default: pinned RC)
  --run-id ID              GitHub Actions run (default: 38802815038)
  --version STRING         Expected bundle release (default: v3.2.10-rust.6-rc.1)
  --check                  Verify the bundle and target without changing the service
  --status                 Report installer state using the retained offline kit
  --undo                   Run the maintained installer's rollback, without downloads
  --homebrew DIR           Target homebrew directory (default: /home/DECK_USER/homebrew)
  --service NAME           Target service (default: plugin_loader)
  --backup-dir DIR         Full snapshots, outside homebrew (default: sibling decky-runtime-backups)
  --listen ADDRESS         Backend listener (default: 127.0.0.1:1337)
  --cef-url URL            Steam debugger endpoint (default: http://127.0.0.1:8080)
  --inject-frontend BOOL    true or false (default: true)
  --non-interactive        Do not prompt for sudo or SSH passwords
  --help                   Show help without connecting

Environment: DECK_USER defaults to deck (the plugin account, and SSH account in
remote mode). DECK_PORT overrides the SSH port only with --remote. TMPDIR is used
for owned staging; the fallback on each machine is /var/tmp/omp-1000.

Install briefly stops the selected service for a consistent full backup, then
restores its prior running state before install.py takes its transaction snapshot.
Rollback restores backend/service state, not the separate user-data snapshot.
HELP
}

target_cleanup() {
    local result=$?
    trap - EXIT
    if (( restart_needed )); then
        if ! systemctl start "$unit"; then
            printf 'Error: could not restart the original service after backup; check %s\n' "$unit" >&2
            result=1
        fi
    fi
    rm -rf -- "$stage"
    exit "$result"
}

# Both local and SSH execution use this same staged script and transaction path.
target_action() {
    local action=$1 homebrew=$2 user=$3 service=$4 backup_base=$5 expected=$6 version=$7
    local listen=$8 cef=$9 inject=${10}
    local kit backup='' state fragment wd unpacked
    # EXIT can run after function locals unwind, including on a failed preflight.
    restart_needed=0
    stage=$(realpath -- "$(dirname -- "${BASH_SOURCE[0]}")")
    [[ $(basename -- "$stage") == decky-runtime-deploy.???????? ]] || fail 'Invalid target staging directory'
    [[ $EUID == 0 ]] || fail 'The target operation requires sudo'
    umask 077
    export PYTHONDONTWRITEBYTECODE=1 TMPDIR="${TMPDIR:-/var/tmp/omp-1000}"
    [[ -d $TMPDIR ]] || mkdir -p -- "$TMPDIR"
    unit="${service%.service}.service"
    [[ $unit =~ ^[a-zA-Z0-9][a-zA-Z0-9_.@-]*\.service$ ]] || fail 'Invalid service name'
    [[ $homebrew == /* && $homebrew != / && $homebrew != /home ]] || fail 'Use an absolute homebrew directory, not / or /home'
    homebrew=$(realpath -m -- "$homebrew")
    kit="$homebrew/services/.decky-deploy/$unit"
    trap target_cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM HUP
    for tool in python3 systemctl tar du install sha256sum; do need "$tool"; done
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python 3.10 or newer is required")'
    [[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || fail 'This bundle requires Linux x86-64'
    [[ $(id -u "$user") != 0 ]] || fail 'The plugin account must not be root'
    if [[ $action == undo || $action == status ]]; then
        [[ -f $kit/install.py && -f $kit/process_observer.py ]] || fail "No retained deployment kit at $kit; this wrapper has not installed a bundle here"
        if [[ $action == undo ]]; then action=rollback; fi
        python3 -B "$kit/install.py" "$action" --homebrew "$homebrew" --service-name "$unit"
        exit 0
    fi
    state=$(systemctl show "$unit" --property=LoadState --value)
    if [[ $state == loaded ]]; then
        wd=$(systemctl show "$unit" --property=WorkingDirectory --value)
        [[ $wd == "$homebrew/services" ]] || fail "Selected service WorkingDirectory does not match $homebrew/services: $wd"
    elif [[ $state != not-found ]]; then
        fail "Unsupported service LoadState: $state"
    fi
    printf 'Target: %s; user: %s; homebrew: %s\n' "$unit" "$user" "$homebrew"
    systemctl show "$unit" --property=ActiveState --property=SubState --property=MainPID --property=UnitFileState
    unpacked=$(python3 - "$stage/runtime.zip" "$expected" "$version" "$stage/bundle" "$homebrew" "$backup_base" <<'PY'
import hashlib, json, os, shutil, stat, subprocess, sys, zipfile
from pathlib import Path, PurePosixPath
archive, expected, version, destination, homebrew, backup = sys.argv[1:]
def digest(stream):
    result = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        result.update(block)
    return result.hexdigest()
with open(archive, 'rb') as stream:
    if digest(stream) != expected:
        raise SystemExit('Transferred runtime ZIP checksum does not match the approved checksum')
hb, backup = Path(homebrew).resolve(), Path(backup).resolve()
if not Path(sys.argv[6]).is_absolute() or backup == hb or hb in backup.parents:
    raise SystemExit('Backup directory must be absolute and outside homebrew')
if hb == backup or backup in hb.parents:
    raise SystemExit('Backup directory must not contain the homebrew directory')
with zipfile.ZipFile(archive) as z:
    infos = z.infolist()
    names = [i.filename for i in infos]
    if len(names) != len(set(names)):
        raise SystemExit('Duplicate ZIP member')
    manifest = json.loads(z.read('bundle.json'))
    if manifest.get('kind') != 'decky-loader-runtime' or manifest.get('release_version') != version:
        raise SystemExit('Bundle kind or expected release version does not match')
    hashes = manifest.get('sha256', {})
    if not hashes or set(names) != set(hashes) | {'bundle.json'}:
        raise SystemExit('Bundle inventory does not match its manifest')
    for info in infos:
        path = PurePosixPath(info.filename)
        mode = info.external_attr >> 16
        if (path.is_absolute() or '..' in path.parts or path.as_posix() != info.filename
                or '\\' in info.filename or not stat.S_ISREG(mode) or mode & 0o6022):
            raise SystemExit('Unsupported ZIP path or mode: ' + info.filename)
        if info.filename in hashes:
            with z.open(info) as stream:
                if digest(stream) != hashes[info.filename]:
                    raise SystemExit('Payload checksum mismatch: ' + info.filename)
    size = sum(i.file_size for i in infos)
    home_size = int(subprocess.check_output(['du', '-sb', '--', str(hb)], text=True).split()[0]) if hb.exists() else 0
    # Extraction, installation/rollback copies, and the full snapshot can use
    # different filesystems. Add every reservation on a shared device together.
    requirements = {}
    for path, required in (
        (Path(destination).parent, size + 64 * 1024 * 1024),
        (hb / 'services', size * 2 + 256 * 1024 * 1024),
        (backup, home_size + 64 * 1024 * 1024),
    ):
        while not path.exists():
            path = path.parent
        device = path.stat().st_dev
        previous = requirements.get(device, (path, 0))
        requirements[device] = (previous[0], previous[1] + required)
    for path, required in requirements.values():
        available = shutil.disk_usage(path).free
        if available < required:
            raise SystemExit(f'Not enough free space on {path}: need {required} bytes, available {available} bytes')
    target = Path(destination)
    target.mkdir(mode=0o755)
    for info in infos:
        path = target / info.filename
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        with z.open(info) as src, path.open('xb') as dst:
            shutil.copyfileobj(src, dst)
        path.chmod(stat.S_IMODE(info.external_attr >> 16))
    for directory in target.rglob('*'):
        if directory.is_dir():
            directory.chmod(0o755)
    for binary in ('decky-loader-runtime', 'DeckyPluginWorker'):
        if not os.access(target / binary, os.X_OK):
            raise SystemExit('Missing executable mode: ' + binary)
print(size)
PY
    )
    printf 'Verified %s (%s unpacked bytes).\n' "$version" "$unpacked"
    if [[ $action == check ]]; then
        printf 'Preflight complete. No service, installation, or backup was changed.\n'
        exit 0
    fi
    [[ -d $backup_base ]] || install -d -m 0700 -- "$backup_base"
    backup=$(mktemp -d "$backup_base/$(date -u +%Y%m%dT%H%M%SZ).XXXXXXXX")
    printf 'Full backup: %s\n' "$backup"
    systemctl show "$unit" --property=LoadState --property=ActiveState --property=SubState --property=UnitFileState --property=FragmentPath --property=DropInPaths > "$backup/service-before.txt"
    local active control_group
    active=$(systemctl show "$unit" --property=ActiveState --value)
    control_group=$(systemctl show "$unit" --property=ControlGroup --value)
    if [[ $active == active ]]; then
        restart_needed=1
        systemctl stop "$unit"
    elif [[ $active != inactive && $state != not-found ]]; then
        fail "Refusing backup while selected service is $active"
    fi
    if [[ -n $control_group ]]; then
        python3 - "$stage/bundle" "$control_group" <<'PY'
import sys, time
sys.path.insert(0, sys.argv[1])
from process_observer import cgroup_processes
deadline = time.monotonic() + 10
while cgroup_processes(sys.argv[2]):
    if time.monotonic() >= deadline:
        raise SystemExit('Selected service still has processes; backup cancelled')
    time.sleep(0.05)
PY
    fi
    local -a paths=()
    [[ ! -e $homebrew ]] || paths+=("${homebrew#/}")
    fragment=$(systemctl show "$unit" --property=FragmentPath --value)
    [[ ! -f $fragment ]] || paths+=("${fragment#/}")
    local dropin
    for dropin in "/etc/systemd/system/$unit.d" "/run/systemd/system/$unit.d" "/usr/lib/systemd/system/$unit.d"; do
        [[ ! -d $dropin ]] || paths+=("${dropin#/}")
    done
    if (( ${#paths[@]} )); then
        tar --create --file="$backup/snapshot.tar" --acls --xattrs --numeric-owner --directory=/ -- "${paths[@]}"
    else
        tar --create --file="$backup/snapshot.tar" --files-from=/dev/null
    fi
    sha256sum -- "$backup/snapshot.tar" > "$backup/SHA256SUMS"
    if (( restart_needed )); then
        systemctl start "$unit"
        restart_needed=0
    fi
    for directory in "$homebrew" "$homebrew/services"; do
        [[ -d $directory ]] || install -d -m 0755 -- "$directory"
    done
    install -d -m 0700 -- "$kit"
    install -m 0644 -- "$stage/bundle/install.py" "$stage/bundle/process_observer.py" "$kit/"
    python3 -B "$kit/install.py" install --bundle "$stage/bundle" --homebrew "$homebrew" \
        --user "$user" --service-name "$unit" --version "$version" --worker-mode fork \
        --listen "$listen" --cef-url "$cef" --inject-frontend "$inject"
    printf 'Installation complete. Full backup retained at %s\n' "$backup"
    exit 0
}

if [[ ${1:-} == --_target ]]; then
    shift
    [[ $# == 10 ]] || fail 'Invalid internal deployment arguments'
    target_action "$@"
fi

action=install
remote_host=''
bundle_archive=''
expected=61b549a6d2d7f99a5d9afd6cb4575d4d3d9c4c2cb277d6e465942b7c148f425c
version=v3.2.10-rust.6-rc.1
run_id=38802815038
user=${DECK_USER:-deck}
homebrew="/home/$user/homebrew"
service=plugin_loader
backup_base=''
listen=127.0.0.1:1337
cef=http://127.0.0.1:8080
inject=true
non_interactive=false
while (( $# )); do
    case $1 in
        --help|-h) show_help; exit 0 ;;
        --undo|--status|--check)
            [[ $action == install ]] || fail 'Choose only one of --undo, --status, or --check'
            action=${1#--}; shift ;;
        --non-interactive) non_interactive=true; shift ;;
        --remote|--bundle|--sha256|--version|--run-id|--homebrew|--service|--backup-dir|--listen|--cef-url|--inject-frontend)
            (( $# >= 2 )) || fail "Missing value for $1"
            case $1 in
                --remote) [[ -n $2 ]] || fail '--remote requires a host'; remote_host=$2 ;;
                --bundle) bundle_archive=$2 ;; --sha256) expected=$2 ;; --version) version=$2 ;;
                --run-id) run_id=$2 ;; --homebrew) homebrew=$2 ;; --service) service=$2 ;;
                --backup-dir) backup_base=$2 ;; --listen) listen=$2 ;; --cef-url) cef=$2 ;;
                --inject-frontend) inject=$2 ;;
            esac
            shift 2 ;;
        *) fail "Unknown option: $1 (use --help)" ;;
    esac
done
[[ $user =~ ^[a-z_][a-z0-9_-]*$ && $user != root ]] || fail 'DECK_USER must name a non-root target account'
[[ $expected =~ ^[[:xdigit:]]{64}$ ]] || fail 'Expected SHA256 must contain 64 hexadecimal characters'
expected=${expected,,}
[[ $run_id =~ ^[0-9]+$ ]] || fail '--run-id must be numeric'
[[ $inject == true || $inject == false ]] || fail '--inject-frontend must be true or false'
[[ -n $backup_base ]] || backup_base="$(dirname -- "$homebrew")/decky-runtime-backups"
for tool in sha256sum realpath cp; do need "$tool"; done
ssh_opts=()
scp_opts=()
tty_args=()
# Pass already shell-quoted command strings to SSH without evaluating them locally.
run_ssh_command() {
    local -a invocation=(ssh "${ssh_opts[@]}" "${tty_args[@]}" "$target" "$@")
    "${invocation[@]}"
}
if [[ -n $remote_host ]]; then
    [[ $remote_host != -* && $remote_host != *[[:space:]]* && $remote_host != *@* ]] || fail 'Invalid --remote host'
    need ssh
    need scp
    if [[ -n ${DECK_PORT:-} ]]; then
        [[ $DECK_PORT =~ ^[0-9]+$ && ${#DECK_PORT} -le 5 ]] || fail 'Invalid DECK_PORT'
        (( 10#$DECK_PORT >= 1 && 10#$DECK_PORT <= 65535 )) || fail 'Invalid DECK_PORT'
        ssh_opts+=(-p "$DECK_PORT")
        scp_opts+=(-P "$DECK_PORT")
    fi
    if $non_interactive; then
        ssh_opts+=(-o BatchMode=yes)
        scp_opts+=(-o BatchMode=yes)
    fi
    target="$user@$remote_host"
    scp_target=$target
    if [[ $remote_host == *:* && $remote_host != \[*\] ]]; then scp_target="$user@[$remote_host]"; fi
fi
tmpbase=${TMPDIR:-/var/tmp/omp-1000}
[[ -d $tmpbase ]] || mkdir -p -- "$tmpbase"
work=$(mktemp -d "$tmpbase/decky-deploy.XXXXXXXX")
remote_stage=''
local_stage=''
cleanup() {
    local result=$?
    trap - EXIT
    if [[ -n $remote_stage ]]; then
        local command
        printf -v command 'rm -rf -- %q' "$remote_stage"
        tty_args=()
        if ! run_ssh_command "$command"; then
            printf 'Warning: target staging may remain at %s\n' "$remote_stage" >&2
        fi
    fi
    if [[ -n $local_stage ]] && ! rm -rf -- "$local_stage"; then
        printf 'Warning: local staging may remain at %s; sudo may be needed to remove it\n' "$local_stage" >&2
        result=1
    fi
    rm -rf -- "$work"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
if [[ $action == install || $action == check ]]; then
    if [[ -z $bundle_archive ]]; then
        need curl
        need unzip
        curl -q --fail --location --proto '=https' --proto-redir '=https' \
            --output "$work/artifact.zip" \
            "https://nightly.link/beallio/decky-loader/actions/runs/$run_id/decky-loader-runtime-linux-x86_64.zip" \
            || fail 'Public artifact download failed; CI artifacts can expire. Use --bundle with the approved runtime ZIP if you have saved it.'
        bundle_archive="$work/decky-loader-runtime-linux-x86_64.zip"
        unzip -p "$work/artifact.zip" decky-loader-runtime-linux-x86_64.zip > "$bundle_archive" \
            || fail 'Public artifact does not contain the installable runtime ZIP'
    fi
    [[ -f $bundle_archive ]] || fail "Runtime ZIP not found: $bundle_archive"
    read -r actual _ < <(sha256sum -- "$bundle_archive")
    [[ $actual == "$expected" ]] || fail 'Runtime ZIP checksum mismatch; use the inner runtime ZIP and its approved SHA256'
fi
sudo_args=(sudo -n)
if ! $non_interactive && [[ -t 0 && -t 1 ]]; then
    sudo_args=(sudo)
fi
target_args=(--_target "$action" "$homebrew" "$user" "$service" "$backup_base" "$expected" "$version" "$listen" "$cef" "$inject")
if [[ -z $remote_host ]]; then
    local_stage=$(mktemp -d "$tmpbase/decky-runtime-deploy.XXXXXXXX")
    cp -- "$(realpath -- "${BASH_SOURCE[0]}")" "$local_stage/deploy.sh"
    if [[ $action == install || $action == check ]]; then
        cp -- "$bundle_archive" "$local_stage/runtime.zip"
    fi
    if (( EUID == 0 )); then
        bash "$local_stage/deploy.sh" "${target_args[@]}"
    else
        need sudo
        "${sudo_args[@]}" env "TMPDIR=$tmpbase" bash "$local_stage/deploy.sh" "${target_args[@]}"
    fi
    exit 0
fi
proposed_stage=$(ssh "${ssh_opts[@]}" "$target" 'set -eu; umask 077; base=${TMPDIR:-/var/tmp/omp-1000}; if [ ! -d "$base" ]; then mkdir -p -- "$base"; fi; mktemp -d "$base/decky-runtime-deploy.XXXXXXXX"')
[[ $proposed_stage == /* && $proposed_stage != *$'\n'* && $(basename -- "$proposed_stage") == decky-runtime-deploy.???????? ]] || fail 'Unexpected remote staging path'
remote_stage=$proposed_stage
scp "${scp_opts[@]}" -- "$(realpath -- "${BASH_SOURCE[0]}")" "$scp_target:$remote_stage/deploy.sh"
if [[ $action == install || $action == check ]]; then
    scp "${scp_opts[@]}" -- "$bundle_archive" "$scp_target:$remote_stage/runtime.zip"
fi
tty_args=()
if ! $non_interactive && [[ -t 0 && -t 1 ]]; then
    tty_args=(-t)
fi
printf -v command '%q ' "${sudo_args[@]}" bash "$remote_stage/deploy.sh" "${target_args[@]}"
run_ssh_command "$command"
