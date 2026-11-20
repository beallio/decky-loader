#!/usr/bin/env bash
# Minimal cross-distro GUI wrapper for scripts/install-to-deck.sh.
# Works on SteamOS, Bazzite (KDE/GNOME), CachyOS, Fedora, Ubuntu, or terminal fallback.
set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
RAW_INSTALLER_URL=${DECKY_RAW_INSTALLER_URL:-"https://raw.githubusercontent.com/beallio/decky-loader/main/scripts/install-to-deck.sh"}
CHANNEL=${DECKY_CHANNEL:-preview}
HOMEBREW_DIR=${DECKY_HOMEBREW_DIR:-"$HOME/homebrew"}
TARGET_USER=${DECK_USER:-$(id -un)}
SYSTEM_UNIT_DIRS=${DECKY_SYSTEM_UNIT_DIRS:-"/etc/systemd/system:/usr/lib/systemd/system:/lib/systemd/system"}
LEGACY_MIGRATION_ROOT=${DECKY_LEGACY_MIGRATION_ROOT:-"/var/lib/decky-loader-runtime/legacy-migrations"}
ROOT_HOST_STATE_DIR=${DECKY_ROOT_HOST_STATE_DIR:-"/var/lib/decky-loader-runtime/root-host"}

WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/decky-gui.XXXXXXXXXX")
LOG_FILE=$(mktemp "${TMPDIR:-/tmp}/decky-install-gui.XXXXXXXXXX.log")
ASKPASS_DIR=""
PW_VALUE=""
KEEP_LOG=0

cleanup() {
    PW_VALUE=""
    unset PW_VALUE || true
    if [[ -n $ASKPASS_DIR && -d $ASKPASS_DIR ]]; then
        rm -rf -- "$ASKPASS_DIR"
    fi
    if [[ -n $WORK_DIR && -d $WORK_DIR ]]; then
        rm -rf -- "$WORK_DIR"
    fi
    if (( KEEP_LOG == 0 )) && [[ -f $LOG_FILE ]]; then
        rm -f -- "$LOG_FILE"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

detect_backend() {
    if [[ -n ${DECKY_GUI_BACKEND:-} ]]; then
        case $DECKY_GUI_BACKEND in
            zenity|kdialog|tty) printf '%s\n' "$DECKY_GUI_BACKEND"; return 0 ;;
            *) printf 'Unsupported DECKY_GUI_BACKEND: %s\n' "$DECKY_GUI_BACKEND" >&2; exit 1 ;;
        esac
    fi
    local desktop="${XDG_CURRENT_DESKTOP:-}"
    if [[ ( $desktop == *KDE* || ${KDE_FULL_SESSION:-} == true ) ]] && command -v kdialog >/dev/null 2>&1; then
        printf 'kdialog\n'
    elif command -v zenity >/dev/null 2>&1; then
        printf 'zenity\n'
    elif command -v kdialog >/dev/null 2>&1; then
        printf 'kdialog\n'
    else
        printf 'tty\n'
    fi
}

detect_state_json() {
    python3 - "$HOMEBREW_DIR" "$TARGET_USER" "$SYSTEM_UNIT_DIRS" "$LEGACY_MIGRATION_ROOT" "$ROOT_HOST_STATE_DIR" <<'PY'
import json
import os
from pathlib import Path
import sys

homebrew = Path(sys.argv[1])
user = sys.argv[2]
unit_dirs = [Path(p) for p in sys.argv[3].split(':') if p]
legacy_root = Path(sys.argv[4])
root_host_dir = Path(sys.argv[5])
uid = str(os.geteuid())

user_state = homebrew / 'services' / '.decky-user-installer' / 'plugin_loader.service'
installed = False
version = ''
active_path = user_state / 'active.json'
if active_path.is_file():
    installed = True
    try:
        data = json.loads(active_path.read_text(encoding='utf-8'))
        if isinstance(data, dict) and isinstance(data.get('version'), str):
            version = data['version']
    except (OSError, ValueError):
        pass

rollback_count = 0
history_dir = user_state / 'history'
if installed and history_dir.is_dir():
    try:
        entries = [p for p in history_dir.iterdir() if p.is_dir()]
        rollback_count = max(0, len(entries) - 1)
    except OSError:
        pass

def unit_present(names):
    for d in unit_dirs:
        for name in names:
            p = d / name
            if p.exists() or p.is_symlink():
                try:
                    resolved = os.path.realpath(p)
                except OSError:
                    continue
                if resolved != '/dev/null' and Path(resolved).exists():
                    return True
    return False

official_installed = unit_present(['plugin_loader.service', 'plugin_loader-release.service'])
root_enabled = unit_present([f'decky-root-plugins-{user}.socket', f'decky-root-plugins-{user}.service'])
if not root_enabled:
    cfg = root_host_dir / uid / 'config.json'
    if cfg.is_file():
        root_enabled = True

legacy_backup = False
legacy_status = legacy_root / uid / 'plugin_loader.service' / 'status.json'
if legacy_status.is_file():
    try:
        rec = json.loads(legacy_status.read_text(encoding='utf-8'))
        if isinstance(rec, dict) and rec.get('phase') != 'forgotten':
            legacy_backup = True
    except (OSError, ValueError):
        pass

if installed:
    default_action = 'update'
elif official_installed:
    default_action = 'migrate'
else:
    default_action = 'install'

print(json.dumps({
    'installed': installed,
    'version': version,
    'official_installed': official_installed,
    'root_enabled': root_enabled,
    'rollback_count': rollback_count,
    'legacy_backup': legacy_backup,
    'default_action': default_action,
}))
PY
}

show_info() {
    local title="$1" body="$2"
    case $BACKEND in
        zenity) zenity --info --no-markup --title="$title" --text="$body" --width=460 >/dev/null 2>&1 || true ;;
        kdialog) kdialog --title="$title" --msgbox "$body" >/dev/null 2>&1 || true ;;
        tty) printf '\n=== %s ===\n%s\n' "$title" "$body" ;;
    esac
}

show_error() {
    local title="$1" body="$2"
    case $BACKEND in
        zenity) zenity --error --no-markup --title="$title" --text="$body" --width=500 >/dev/null 2>&1 || true ;;
        kdialog) kdialog --title="$title" --error "$body" >/dev/null 2>&1 || true ;;
        tty) printf '\nERROR (%s):\n%s\n' "$title" "$body" >&2 ;;
    esac
}

ask_confirm() {
    local title="$1" body="$2"
    case $BACKEND in
        zenity) zenity --question --no-markup --title="$title" --text="$body" --width=480 >/dev/null 2>&1 ;;
        kdialog) kdialog --title="$title" --warningyesno "$body" >/dev/null 2>&1 ;;
        tty)
            local ans=""
            printf '\n%s\n%s\nContinue? [y/N] ' "$title" "$body"
            read -r ans
            [[ $ans == [yY]* ]]
            ;;
    esac
}

prompt_password_and_setup_askpass() {
    local reason="$1" attempt=0
    local pw_note="If you have not set a Steam Deck desktop password yet, open Konsole and run 'passwd' first."
    while (( attempt < 3 )); do
        attempt=$(( attempt + 1 ))
        local prompt_text="${reason}"$'\n\n'"${pw_note}"
        if (( attempt > 1 )); then
            prompt_text="Incorrect password. Please try again."$'\n\n'"${prompt_text}"
        fi
        PW_VALUE=""
        case $BACKEND in
            zenity)
                if ! PW_VALUE=$(zenity --password --title="Administrator Password Required" --text="$prompt_text" 2>/dev/null); then
                    return 2
                fi
                ;;
            kdialog)
                if ! PW_VALUE=$(kdialog --title "Administrator Password Required" --password "$prompt_text" 2>/dev/null); then
                    return 2
                fi
                ;;
            tty)
                printf '\n%s\n' "$prompt_text"
                if ! read -r -s -p "Password for $TARGET_USER: " PW_VALUE; then
                    printf '\n'
                    return 2
                fi
                printf '\n'
                ;;
        esac
        if printf '%s\n' "$PW_VALUE" | sudo -S -v >/dev/null 2>&1; then
            ASKPASS_DIR=$(mktemp -d "${TMPDIR:-/tmp}/decky-askpass.XXXXXXXXXX")
            chmod 0700 "$ASKPASS_DIR"
            printf '%s\n' "$PW_VALUE" >"$ASKPASS_DIR/secret"
            chmod 0600 "$ASKPASS_DIR/secret"
            cat >"$ASKPASS_DIR/askpass.sh" <<EOF
#!/usr/bin/env sh
cat -- "$ASKPASS_DIR/secret"
EOF
            chmod 0700 "$ASKPASS_DIR/askpass.sh"
            export SUDO_ASKPASS="$ASKPASS_DIR/askpass.sh"
            PW_VALUE=""
            return 0
        fi
        PW_VALUE=""
    done
    show_error "Authentication Failed" \
        "Administrator password was not accepted after 3 attempts. Nothing was changed."$'\n\n'"${pw_note}"
    return 1
}

resolve_installer_script() {
    if [[ -n ${DECKY_INSTALLER_SCRIPT:-} ]]; then
        printf '%s\n' "$DECKY_INSTALLER_SCRIPT"
        return 0
    fi
    if [[ -f "$SCRIPT_DIR/install-to-deck.sh" ]]; then
        printf '%s\n' "$SCRIPT_DIR/install-to-deck.sh"
        return 0
    fi
    local fetched="$WORK_DIR/install-to-deck.sh"
    curl -fsSL "$RAW_INSTALLER_URL" -o "$fetched" || return 1
    chmod 0755 "$fetched"
    printf '%s\n' "$fetched"
}

close_kdialog_progress() {
    local svc="$1" obj="$2" cmd=""
    for cmd in qdbus6 qdbus-qt6 qdbus; do
        if command -v "$cmd" >/dev/null 2>&1 && "$cmd" "$svc" "$obj" close >/dev/null 2>&1; then
            return 0
        fi
    done
    if command -v dbus-send >/dev/null 2>&1; then
        dbus-send --session --type=method_call --dest="$svc" "$obj" org.qtproject.Qt.QWidget.close >/dev/null 2>&1 || true
    fi
}

run_action_with_progress() {
    local title="$1" label="$2" installer="$3"
    shift 3
    local status_file="$WORK_DIR/exit.code"
    local rc=0

    case $BACKEND in
        zenity)
            (
                set +e
                "$installer" "$@" >"$LOG_FILE" 2>&1
                printf '%s\n' "$?" >"$status_file"
            ) | zenity --progress --pulsate --auto-close --no-cancel \
                --title="$title" --text="$label" --width=440 >/dev/null 2>&1 || true
            rc=$(cat "$status_file" 2>/dev/null || echo 1)
            ;;
        kdialog)
            local dbus_ref=""
            dbus_ref=$(kdialog --title "$title" --progressbar "$label" 0 2>/dev/null || true)
            set +e
            "$installer" "$@" >"$LOG_FILE" 2>&1
            rc=$?
            set -e
            if [[ -n $dbus_ref ]]; then
                # kdialog prints "org.kde.kdialog-... /ProgressDialog"
                read -r svc obj <<<"$dbus_ref"
                close_kdialog_progress "$svc" "$obj"
            fi
            ;;
        tty)
            printf '\n%s\n' "$label"
            set +e
            "$installer" "$@" 2>&1 | tee "$LOG_FILE"
            rc=${PIPESTATUS[0]}
            set -e
            ;;
    esac
    return "$rc"
}

ACTION_MODE=run
case ${1:-} in
    --print-state) detect_state_json; exit 0 ;;
    --print-backend) detect_backend; exit 0 ;;
    --demo-menu) ACTION_MODE=demo ;;
    "") ;;
    --help|-h)
        printf 'Usage: %s [--print-state|--print-backend|--demo-menu]\n' "$0"
        exit 0
        ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 1 ;;
esac

BACKEND=$(detect_backend)
STATE_JSON=$(detect_state_json)

INSTALLED=false
VERSION=""
OFFICIAL=false
ROOT_ENABLED=false
ROLLBACK_COUNT=0
LEGACY_BACKUP=false
DEFAULT_ACTION="install"

eval "$(python3 - "$STATE_JSON" <<'PY'
import json, shlex, sys
d = json.loads(sys.argv[1])
print(f"INSTALLED={'true' if d['installed'] else 'false'}")
print(f"VERSION={shlex.quote(d['version'])}")
print(f"OFFICIAL={'true' if d['official_installed'] else 'false'}")
print(f"ROOT_ENABLED={'true' if d['root_enabled'] else 'false'}")
print(f"ROLLBACK_COUNT={int(d['rollback_count'])}")
print(f"LEGACY_BACKUP={'true' if d['legacy_backup'] else 'false'}")
print(f"DEFAULT_ACTION={shlex.quote(d['default_action'])}")
PY
)"

if [[ $INSTALLED == true ]]; then
    root_label="Off"
    [[ $ROOT_ENABLED == true ]] && root_label="On"
    STATUS_TEXT="Installed: Decky Loader (Rust) ${VERSION:-detected} | Root plugins: ${root_label}"
elif [[ $OFFICIAL == true ]]; then
    STATUS_TEXT="Detected: Official Decky Loader (your plugins & settings will be preserved)"
else
    STATUS_TEXT="Status: Decky Loader is not installed yet"
fi

declare -a OPT_IDS=() OPT_NAMES=() OPT_DESCS=()
add_opt() {
    OPT_IDS+=("$1")
    OPT_NAMES+=("$2")
    OPT_DESCS+=("$3")
}

if [[ $OFFICIAL == true && $INSTALLED == false ]]; then
    add_opt "migrate" "Move from Official Decky" "Switch to Decky (Rust), keep plugins & backup (asks password)"
fi
if [[ $INSTALLED == false && $OFFICIAL == false ]]; then
    add_opt "install" "Install Decky (Rust)" "Download and install the latest preview build (no password)"
fi
if [[ $INSTALLED == true ]]; then
    add_opt "update" "Update / Reinstall Decky (Rust)" "Download latest build and back up plugins (no password)"
    if [[ $ROOT_ENABLED == true ]]; then
        add_opt "disable-root" "Disable Root Plugins" "Turn off root plugins and return files to $TARGET_USER (password)"
    else
        add_opt "enable-root" "Enable Root Plugins" "Allow PowerTools & root plugins in isolated service (password)"
    fi
    if (( ROLLBACK_COUNT > 0 )); then
        add_opt "undo" "Undo Last Update" "Roll back to your previous Decky (Rust) version (no password)"
    fi
fi
if [[ $LEGACY_BACKUP == true ]]; then
    add_opt "restore-legacy" "Restore Official Decky" "Switch back to your saved Official Decky setup (asks password)"
fi

CHOICE=""
case $BACKEND in
    zenity)
        ZEN_ROWS=()
        for i in "${!OPT_IDS[@]}"; do
            sel="FALSE"
            [[ ${OPT_IDS[$i]} == "$DEFAULT_ACTION" ]] && sel="TRUE"
            ZEN_ROWS+=("$sel" "${OPT_NAMES[$i]}" "${OPT_DESCS[$i]}" "${OPT_IDS[$i]}")
        done
        if ! CHOICE=$(zenity --list --radiolist \
            --title="Decky Loader (Rust) Installer" \
            --text="${STATUS_TEXT}"$'\n\n'"Choose an action:" \
            --width=780 --height=340 \
            --column="" --column="Action" --column="Details" --column="id" \
            --hide-column=4 --print-column=4 \
            "${ZEN_ROWS[@]}" 2>/dev/null); then
            exit 0
        fi
        ;;
    kdialog)
        KD_ROWS=()
        for i in "${!OPT_IDS[@]}"; do
            sel="off"
            [[ ${OPT_IDS[$i]} == "$DEFAULT_ACTION" ]] && sel="on"
            KD_ROWS+=("${OPT_IDS[$i]}" "${OPT_NAMES[$i]} — ${OPT_DESCS[$i]}" "$sel")
        done
        if ! CHOICE=$(kdialog --geometry 780x320 --title "Decky Loader (Rust) Installer" \
            --radiolist "$STATUS_TEXT

Choose an action:" \
            "${KD_ROWS[@]}" 2>/dev/null); then
            exit 0
        fi
        ;;
    tty)
        printf '\n=== Decky Loader (Rust) Installer ===\n%s\n\n' "$STATUS_TEXT"
        default_num=1
        for i in "${!OPT_IDS[@]}"; do
            n=$(( i + 1 ))
            mark=""
            if [[ ${OPT_IDS[$i]} == "$DEFAULT_ACTION" ]]; then
                default_num=$n
                mark=" (default)"
            fi
            printf '  [%d] %s%s - %s\n' "$n" "${OPT_NAMES[$i]}" "$mark" "${OPT_DESCS[$i]}"
        done
        printf '  [q] Cancel\n\n'
        read -r -p "Choose [1-${#OPT_IDS[@]}, Enter=$default_num]: " reply || exit 0
        [[ -z $reply ]] && reply="$default_num"
        [[ $reply == [qQ] ]] && exit 0
        if [[ ! $reply =~ ^[0-9]+$ ]] || (( reply < 1 || reply > ${#OPT_IDS[@]} )); then
            printf 'Invalid selection.\n' >&2
            exit 1
        fi
        CHOICE="${OPT_IDS[$(( reply - 1 ))]}"
        ;;
esac

CHOICE=${CHOICE%%$'\n'*}
[[ -z $CHOICE ]] && exit 0
[[ $ACTION_MODE == demo ]] && exit 0

NEEDS_SUDO=false
declare -a INSTALLER_ARGS=()
PROGRESS_LABEL="Working..."

case $CHOICE in
    install)
        INSTALLER_ARGS=("--channel" "$CHANNEL")
        PROGRESS_LABEL="Downloading and installing Decky Loader (Rust)..."
        ;;
    update)
        INSTALLER_ARGS=("--channel" "$CHANNEL")
        PROGRESS_LABEL="Backing up plugins and updating Decky Loader (Rust)..."
        ;;
    migrate)
        NEEDS_SUDO=true
        INSTALLER_ARGS=("--migrate-legacy" "--channel" "$CHANNEL" "--non-interactive")
        PROGRESS_LABEL="Saving Official Decky backup and installing Decky Loader (Rust)..."
        PW_REASON="Enter your login password once to stop Official Decky and save a backup before moving to Decky (Rust)."
        ;;
    enable-root)
        ask_confirm "Enable Root Plugins?" \
            "Plugins marked as needing administrator (root) access will run in an isolated root service. Only enable this if you trust those plugins." || exit 0
        NEEDS_SUDO=true
        INSTALLER_ARGS=("--enable-root-plugins" "--channel" "$CHANNEL" "--non-interactive")
        PROGRESS_LABEL="Installing and turning on root plugin support..."
        PW_REASON="Enter your login password once to set up the isolated root plugin service."
        ;;
    disable-root)
        NEEDS_SUDO=true
        INSTALLER_ARGS=("--disable-root-plugins" "--non-interactive")
        PROGRESS_LABEL="Turning off root plugin support..."
        PW_REASON="Enter your login password once to turn off root plugin support and return plugin files to $TARGET_USER."
        ;;
    undo)
        INSTALLER_ARGS=("--undo")
        PROGRESS_LABEL="Restoring your previous Decky Loader (Rust) version..."
        ;;
    restore-legacy)
        ask_confirm "Restore Official Decky?" \
            "This switches back to your saved Official Decky Loader installation. Your plugins and settings stay intact." || exit 0
        NEEDS_SUDO=true
        INSTALLER_ARGS=("--restore-legacy" "--non-interactive")
        PROGRESS_LABEL="Restoring Official Decky Loader..."
        PW_REASON="Enter your login password once to restore the Official Decky system service."
        ;;
    *)
        printf 'Unknown action selected: %s\n' "$CHOICE" >&2
        exit 1
        ;;
esac

if [[ $NEEDS_SUDO == true ]]; then
    set +e
    prompt_password_and_setup_askpass "$PW_REASON"
    pw_rc=$?
    set -e
    if (( pw_rc == 2 )); then
        exit 0
    elif (( pw_rc != 0 )); then
        exit 1
    fi
fi

if ! INSTALLER_PATH=$(resolve_installer_script); then
    show_error "Download Failed" "Could not download the installer script. Please check your internet connection and try again."
    exit 1
fi

set +e
run_action_with_progress "Decky Loader (Rust)" "$PROGRESS_LABEL" "$INSTALLER_PATH" "${INSTALLER_ARGS[@]}"
run_rc=$?
set -e

if (( run_rc == 0 )); then
    show_info "Decky Loader (Rust)" \
        "All done!"$'\n\n'"Switch back to Gaming Mode to use Decky. If Decky does not appear right away, restart your Steam Deck."
    exit 0
fi

KEEP_LOG=1
TAIL_ERR=$(tail -n 12 "$LOG_FILE" 2>/dev/null || true)
show_error "Decky Installer Failed" \
    "Something went wrong (log saved at $LOG_FILE):"$'\n\n'"$TAIL_ERR"
if [[ $BACKEND == zenity ]]; then
    zenity --text-info --title="Installer Log ($LOG_FILE)" --filename="$LOG_FILE" --width=700 --height=420 >/dev/null 2>&1 || true
elif [[ $BACKEND == kdialog ]]; then
    kdialog --title "Installer Log ($LOG_FILE)" --textbox "$LOG_FILE" 700 420 >/dev/null 2>&1 || true
fi
exit "$run_rc"
