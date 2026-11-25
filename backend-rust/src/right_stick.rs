//! Native right-stick UI navigation input engine.
//!
//! Hysteresis thresholds, Steam Deck HID report layout, and Steam session focus semantics are
//! adapted from `Right-Stick-Navigation` (MIT License, Copyright (c) 2026 Jed Whitaker).

use std::{
    collections::{HashMap, HashSet},
    fs::{File, OpenOptions},
    io::ErrorKind,
    mem::size_of,
    os::{
        fd::{AsFd, AsRawFd, FromRawFd, OwnedFd, RawFd},
        unix::{fs::MetadataExt, fs::OpenOptionsExt},
    },
    path::Path,
    sync::{Arc, Mutex},
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

use nix::sys::inotify::{AddWatchFlags, InitFlags, Inotify};

const EV_SYN: u16 = 0x00;
const EV_KEY: u16 = 0x01;
const EV_ABS: u16 = 0x03;
const SYN_REPORT: u16 = 0;
const SYN_DROPPED: u16 = 3;

const ABS_X: u32 = 0x00;
const ABS_Y: u32 = 0x01;
const ABS_RX: u32 = 0x03;
const ABS_RY: u32 = 0x04;
const BTN_SOUTH: usize = 0x130;

const STEAM_VENDOR_ID: u16 = 0x28DE;
const STEAM_DECK_PRODUCT_ID: u16 = 0x1205;

const ENGAGE_THRESHOLD: f64 = 0.58;
const HOLD_THRESHOLD: f64 = 0.40;
const SWITCH_MARGIN: f64 = 0.15;

const FOCUS_LEASE: Duration = Duration::from_secs(5);
const HIDRAW_STALE_TIMEOUT: Duration = Duration::from_secs(1);
const HIDRAW_PROBE_TIMEOUT_MS: i32 = 80;
const MAX_HIDRAW_DRAIN: usize = 64;
const MAX_EVDEV_EVENTS: usize = 64;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Direction {
    Up,
    Down,
    Left,
    Right,
}

impl Direction {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Up => "up",
            Self::Down => "down",
            Self::Left => "left",
            Self::Right => "right",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum SessionMode {
    Gamescope,
    Desktop,
}

impl SessionMode {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Gamescope => "gamescope",
            Self::Desktop => "desktop",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct Axis {
    pub value: i32,
    pub min: i32,
    pub max: i32,
}

impl Axis {
    pub(crate) fn normalize(self) -> f64 {
        let span = i64::from(self.max) - i64::from(self.min);
        if span <= 0 {
            return 0.0;
        }
        let ratio = (i64::from(self.value) - i64::from(self.min)) as f64 / span as f64;
        (ratio * 2.0 - 1.0).clamp(-1.0, 1.0)
    }
}

pub(crate) fn direction_for(rx: Axis, ry: Axis, previous: Option<Direction>) -> Option<Direction> {
    let x = rx.normalize();
    let y = ry.normalize();
    let side = |direction: Direction| match direction {
        Direction::Right => x,
        Direction::Left => -x,
        Direction::Down => y,
        Direction::Up => -y,
    };
    let peak = x.abs().max(y.abs());
    if let Some(prev) = previous {
        let prev_strength = side(prev);
        if prev_strength >= HOLD_THRESHOLD && peak - prev_strength < SWITCH_MARGIN {
            return Some(prev);
        }
    }
    if peak < ENGAGE_THRESHOLD {
        return None;
    }
    if x.abs() > y.abs() {
        Some(if x > 0.0 {
            Direction::Right
        } else {
            Direction::Left
        })
    } else {
        Some(if y > 0.0 {
            Direction::Down
        } else {
            Direction::Up
        })
    }
}

pub(crate) fn parse_deck_hid_report(report: &[u8]) -> Option<(Axis, Axis)> {
    if report.len() != 64 || report[..3] != [0x01, 0x00, 0x09] {
        return None;
    }
    let x = i16::from_le_bytes([report[52], report[53]]) as i32;
    let y = i16::from_le_bytes([report[54], report[55]]) as i32;
    Some((
        Axis {
            value: x,
            min: -32768,
            max: 32767,
        },
        Axis {
            value: -y,
            min: -32767,
            max: 32768,
        },
    ))
}

pub(crate) fn parse_session_mode_environ(raw: &[u8]) -> Option<SessionMode> {
    let mut has_desktop_display = false;
    for entry in raw.split(|&byte| byte == 0) {
        let Some(split) = entry.iter().position(|&byte| byte == b'=') else {
            continue;
        };
        let key = &entry[..split];
        let value = &entry[split + 1..];
        if (key == b"XDG_CURRENT_DESKTOP" && value == b"gamescope")
            || (key == b"GAMESCOPE_WAYLAND_DISPLAY" && !value.is_empty())
        {
            return Some(SessionMode::Gamescope);
        }
        if (key == b"DISPLAY" || key == b"WAYLAND_DISPLAY") && !value.is_empty() {
            has_desktop_display = true;
        }
    }
    has_desktop_display.then_some(SessionMode::Desktop)
}

pub(crate) fn is_dual_stick_gamepad(
    abs_bits: u64,
    has_btn_south: bool,
    rx: Axis,
    ry: Axis,
) -> bool {
    let required_axes = (1u64 << ABS_X) | (1u64 << ABS_Y) | (1u64 << ABS_RX) | (1u64 << ABS_RY);
    has_btn_south
        && (abs_bits & required_axes) == required_axes
        && rx.max > rx.min
        && ry.max > ry.min
}

struct ControllerTrack {
    previous: Option<Direction>,
    centered: bool,
}

#[derive(Default)]
pub(crate) struct ControllerArbiter {
    tracks: HashMap<u64, ControllerTrack>,
    owner: Option<u64>,
    published: Option<Direction>,
}

impl ControllerArbiter {
    pub(crate) fn add(&mut self, id: u64, rx: Axis, ry: Axis) {
        let centered = direction_for(rx, ry, None).is_none();
        self.tracks.insert(
            id,
            ControllerTrack {
                previous: None,
                centered,
            },
        );
    }

    pub(crate) fn update(&mut self, id: u64, rx: Axis, ry: Axis) -> Option<Option<Direction>> {
        let track = self.tracks.get_mut(&id)?;
        let direction = direction_for(rx, ry, track.previous);
        if direction.is_none() {
            track.centered = true;
        }
        if !track.centered || direction == track.previous {
            return None;
        }
        track.previous = direction;
        match direction {
            Some(dir) => {
                self.owner = Some(id);
                if self.published != Some(dir) {
                    self.published = Some(dir);
                    return Some(Some(dir));
                }
            }
            None if self.owner == Some(id) => {
                return self.reconcile_after_release();
            }
            None => {}
        }
        None
    }

    pub(crate) fn remove(&mut self, id: u64) -> Option<Option<Direction>> {
        self.tracks.remove(&id)?;
        if self.owner == Some(id) {
            return self.reconcile_after_release();
        }
        None
    }

    fn reconcile_after_release(&mut self) -> Option<Option<Direction>> {
        let next_owner = self
            .tracks
            .iter()
            .filter_map(|(&id, track)| (track.centered).then_some((id, track.previous?)))
            .min_by_key(|&(id, _)| id);
        let next_dir = match next_owner {
            Some((id, dir)) => {
                self.owner = Some(id);
                Some(dir)
            }
            None => {
                self.owner = None;
                None
            }
        };
        if self.published != next_dir {
            self.published = next_dir;
            return Some(next_dir);
        }
        None
    }
}

pub(crate) type DirectionEmitter = Arc<dyn Fn(&str, Option<Direction>) + Send + Sync>;

struct ControllerShared {
    enabled: bool,
    focus_session: Option<String>,
    focus_expires: Option<Instant>,
    generation: u64,
    worker_running: bool,
    cached_steam_pid: Option<u32>,
    wake_write: Option<OwnedFd>,
    worker: Option<JoinHandle<()>>,
}

pub(crate) struct RightStickController {
    shared: Arc<Mutex<ControllerShared>>,
    emitter: DirectionEmitter,
}

impl RightStickController {
    pub(crate) fn new(enabled: bool, emitter: DirectionEmitter) -> Self {
        Self {
            shared: Arc::new(Mutex::new(ControllerShared {
                enabled,
                focus_session: None,
                focus_expires: None,
                generation: 0,
                worker_running: false,
                cached_steam_pid: None,
                wake_write: None,
                worker: None,
            })),
            emitter,
        }
    }

    #[cfg(test)]
    pub(crate) fn is_enabled(&self) -> bool {
        self.shared.lock().expect("right_stick lock").enabled
    }

    #[cfg(test)]
    pub(crate) fn is_worker_running(&self) -> bool {
        self.shared.lock().expect("right_stick lock").worker_running
    }

    pub(crate) fn set_enabled(&self, enabled: bool) {
        let handle = {
            let mut state = self.shared.lock().expect("right_stick lock");
            if state.enabled == enabled {
                return;
            }
            state.enabled = enabled;
            if enabled {
                None
            } else {
                stop_worker_locked(&mut state)
            }
        };
        if let Some(handle) = handle {
            let _ = handle.join();
        }
    }

    pub(crate) fn set_focus(&self, session: &str, active: bool) {
        let old_handle = {
            let mut state = self.shared.lock().expect("right_stick lock");
            if !active {
                if state.focus_session.as_deref() == Some(session) {
                    stop_worker_locked(&mut state)
                } else {
                    None
                }
            } else if !state.enabled {
                None
            } else {
                let deadline = Instant::now() + FOCUS_LEASE;
                if state.worker_running && state.focus_session.as_deref() == Some(session) {
                    state.focus_expires = Some(deadline);
                    return;
                }
                let previous = stop_worker_locked(&mut state);
                state.generation = state.generation.wrapping_add(1);
                let generation = state.generation;
                state.focus_session = Some(session.to_owned());
                state.focus_expires = Some(deadline);
                if let Some((wake_read, wake_write)) = create_wakeup_pipe() {
                    state.wake_write = Some(wake_write);
                    state.worker_running = true;
                    let shared = Arc::clone(&self.shared);
                    let emitter = Arc::clone(&self.emitter);
                    let session_name = session.to_owned();
                    match thread::Builder::new()
                        .name("decky-right-stick".into())
                        .spawn(move || {
                            run_worker(shared, emitter, session_name, generation, wake_read);
                        }) {
                        Ok(handle) => {
                            state.worker = Some(handle);
                        }
                        Err(_) => {
                            state.worker_running = false;
                            state.wake_write = None;
                        }
                    }
                }
                previous
            }
        };
        if let Some(handle) = old_handle {
            let _ = handle.join();
        }
    }

    pub(crate) fn session_mode(&self) -> Option<SessionMode> {
        let cached = self
            .shared
            .lock()
            .expect("right_stick lock")
            .cached_steam_pid;
        let (mode, resolved_pid) = detect_steam_session_mode(cached);
        self.shared
            .lock()
            .expect("right_stick lock")
            .cached_steam_pid = resolved_pid;
        mode
    }

    pub(crate) fn shutdown(&self) {
        let handle = {
            let mut state = self.shared.lock().expect("right_stick lock");
            state.enabled = false;
            stop_worker_locked(&mut state)
        };
        if let Some(handle) = handle {
            let _ = handle.join();
        }
    }
}

fn stop_worker_locked(state: &mut ControllerShared) -> Option<JoinHandle<()>> {
    state.focus_session = None;
    state.focus_expires = None;
    state.worker_running = false;
    state.generation = state.generation.wrapping_add(1);
    if let Some(wake) = state.wake_write.as_ref() {
        signal_wakeup(wake.as_raw_fd());
    }
    state.wake_write = None;
    state.worker.take()
}

fn create_wakeup_pipe() -> Option<(OwnedFd, OwnedFd)> {
    let mut fds = [0 as libc::c_int; 2];
    // SAFETY: `fds` is a valid 2-element output array for Linux `pipe2`.
    if unsafe { libc::pipe2(fds.as_mut_ptr(), libc::O_NONBLOCK | libc::O_CLOEXEC) } != 0 {
        return None;
    }
    // SAFETY: `pipe2` succeeded and returned two freshly created, owned file descriptors.
    unsafe { Some((OwnedFd::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1]))) }
}

fn signal_wakeup(fd: RawFd) {
    let byte = [1u8; 1];
    // SAFETY: `fd` is a valid open non-blocking pipe write end and `byte` is valid for 1 byte.
    let _ = unsafe { libc::write(fd, byte.as_ptr().cast(), 1) };
}

fn drain_pipe(fd: RawFd) {
    let mut buffer = [0u8; 64];
    loop {
        // SAFETY: `fd` is a valid open non-blocking pipe read end and `buffer` is valid for its length.
        let read = unsafe { libc::read(fd, buffer.as_mut_ptr().cast(), buffer.len()) };
        if read <= 0 {
            break;
        }
    }
}

fn evdev_ior(number: u32, size: usize) -> libc::Ioctl {
    (((2u32) << 30) | ((size as u32) << 16) | ((b'E' as u32) << 8) | number) as libc::Ioctl
}

fn read_evdev_ioctl(fd: RawFd, number: u32, buffer: &mut [u8]) -> bool {
    let request = evdev_ior(number, buffer.len());
    // SAFETY: `fd` is an open evdev descriptor and `buffer` matches the size encoded in `request`.
    unsafe { libc::ioctl(fd, request, buffer.as_mut_ptr()) >= 0 }
}

fn read_evdev_axis(fd: RawFd, code: u32) -> Option<Axis> {
    let mut raw = [0u8; 24];
    if !read_evdev_ioctl(fd, 0x40 + code, &mut raw) {
        return None;
    }
    let value = i32::from_ne_bytes(raw[0..4].try_into().ok()?);
    let min = i32::from_ne_bytes(raw[4..8].try_into().ok()?);
    let max = i32::from_ne_bytes(raw[8..12].try_into().ok()?);
    Some(Axis { value, min, max })
}

fn read_evdev_id(fd: RawFd) -> Option<(u16, u16)> {
    let mut raw = [0u8; 8];
    if !read_evdev_ioctl(fd, 0x02, &mut raw) {
        return None;
    }
    let vendor = u16::from_ne_bytes(raw[2..4].try_into().ok()?);
    let product = u16::from_ne_bytes(raw[4..6].try_into().ok()?);
    Some((vendor, product))
}

fn read_evdev_name(fd: RawFd, event_name: &str) -> String {
    let mut raw = [0u8; 256];
    if read_evdev_ioctl(fd, 0x06, &mut raw) {
        let len = raw.iter().position(|&b| b == 0).unwrap_or(raw.len());
        if len > 0 {
            return String::from_utf8_lossy(&raw[..len]).into_owned();
        }
    }
    std::fs::read_to_string(
        Path::new("/sys/class/input")
            .join(event_name)
            .join("device/name"),
    )
    .unwrap_or_default()
}

fn evdev_has_dual_stick_capabilities(fd: RawFd) -> Option<(Axis, Axis)> {
    let mut abs_bytes = [0u8; 8];
    if !read_evdev_ioctl(fd, 0x20 + u32::from(EV_ABS), &mut abs_bytes) {
        return None;
    }
    let abs_bits = u64::from_le_bytes(abs_bytes);

    let mut key_bytes = [0u8; (BTN_SOUTH / 8) + 1];
    if !read_evdev_ioctl(fd, 0x20 + u32::from(EV_KEY), &mut key_bytes) {
        return None;
    }
    let has_btn_south = (key_bytes[BTN_SOUTH / 8] & (1u8 << (BTN_SOUTH % 8))) != 0;

    let rx = read_evdev_axis(fd, ABS_RX)?;
    let ry = read_evdev_axis(fd, ABS_RY)?;
    is_dual_stick_gamepad(abs_bits, has_btn_south, rx, ry).then_some((rx, ry))
}

fn is_steam_deck_builtin(vendor: u16, product: u16, name: &str) -> bool {
    if (vendor, product) == (STEAM_VENDOR_ID, STEAM_DECK_PRODUCT_ID) {
        return true;
    }
    let lower = name.to_ascii_lowercase();
    lower.contains("steamdeck") || lower.contains("steam deck")
}

enum DeviceKind {
    Evdev { resync: bool, is_steam_deck: bool },
    SteamDeckHidraw { last_report: Instant },
}

struct OpenController {
    id: u64,
    dev_ino: (u64, u64),
    file: File,
    rx: Axis,
    ry: Axis,
    kind: DeviceKind,
}

fn open_nonblocking_readonly(path: &Path) -> Option<(File, (u64, u64))> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)
        .ok()?;
    let meta = file.metadata().ok()?;
    Some((file, (meta.dev(), meta.ino())))
}

fn discover_controllers(
    existing: &HashSet<(u64, u64)>,
    next_id: &mut u64,
    already_has_steam_deck: bool,
) -> Vec<OpenController> {
    let mut discovered = Vec::new();
    let mut found_steam_deck_evdev = already_has_steam_deck;

    if let Ok(entries) = std::fs::read_dir("/dev/input") {
        let mut paths: Vec<_> = entries
            .flatten()
            .map(|entry| entry.path())
            .filter(|path| {
                path.file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| name.starts_with("event"))
            })
            .collect();
        paths.sort();

        for path in paths {
            let Some((file, dev_ino)) = open_nonblocking_readonly(&path) else {
                continue;
            };
            if existing.contains(&dev_ino) {
                continue;
            }
            let fd = file.as_raw_fd();
            let Some((rx, ry)) = evdev_has_dual_stick_capabilities(fd) else {
                continue;
            };
            let (vendor, product) = read_evdev_id(fd).unwrap_or((0, 0));
            let event_name = path
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or_default();
            let name = read_evdev_name(fd, event_name);
            let is_deck = is_steam_deck_builtin(vendor, product, &name);
            if is_deck {
                found_steam_deck_evdev = true;
            }
            let id = *next_id;
            *next_id += 1;
            discovered.push(OpenController {
                id,
                dev_ino,
                file,
                rx,
                ry,
                kind: DeviceKind::Evdev {
                    resync: false,
                    is_steam_deck: is_deck,
                },
            });
        }
    }

    if !found_steam_deck_evdev && let Some(hidraw) = open_steam_deck_hidraw(existing, next_id) {
        discovered.push(hidraw);
    }

    discovered
}

fn open_steam_deck_hidraw(
    existing: &HashSet<(u64, u64)>,
    next_id: &mut u64,
) -> Option<OpenController> {
    let entries = std::fs::read_dir("/sys/class/hidraw").ok()?;
    let mut names: Vec<String> = entries
        .flatten()
        .filter_map(|entry| entry.file_name().into_string().ok())
        .filter(|name| name.starts_with("hidraw"))
        .collect();
    names.sort();

    for name in names {
        let uevent_path = Path::new("/sys/class/hidraw")
            .join(&name)
            .join("device/uevent");
        let Ok(uevent) = std::fs::read_to_string(uevent_path) else {
            continue;
        };
        if !uevent
            .lines()
            .any(|line| line.eq_ignore_ascii_case("HID_ID=0003:000028DE:00001205"))
        {
            continue;
        }
        let dev_path = Path::new("/dev").join(&name);
        let Some((file, dev_ino)) = open_nonblocking_readonly(&dev_path) else {
            continue;
        };
        if existing.contains(&dev_ino) {
            continue;
        }
        let fd = file.as_raw_fd();
        let deadline = Instant::now() + Duration::from_millis(HIDRAW_PROBE_TIMEOUT_MS as u64);
        while let Some(remaining) = deadline.checked_duration_since(Instant::now()) {
            let mut pfd = libc::pollfd {
                fd,
                events: libc::POLLIN,
                revents: 0,
            };
            let timeout_ms = remaining.as_millis().min(i32::MAX as u128) as i32;
            // SAFETY: `pfd` points to a single valid `pollfd` struct with an open descriptor.
            let ready = unsafe { libc::poll(&mut pfd, 1, timeout_ms) };
            if ready <= 0 {
                break;
            }
            let mut report = [0u8; 65];
            // SAFETY: `fd` is open and `report` is valid for 65 bytes.
            let read = unsafe { libc::read(fd, report.as_mut_ptr().cast(), report.len()) };
            if read <= 0 {
                break;
            }
            if let Some((rx, ry)) = parse_deck_hid_report(&report[..read as usize]) {
                let id = *next_id;
                *next_id += 1;
                return Some(OpenController {
                    id,
                    dev_ino,
                    file,
                    rx,
                    ry,
                    kind: DeviceKind::SteamDeckHidraw {
                        last_report: Instant::now(),
                    },
                });
            }
        }
    }
    None
}

fn read_controller_updates(controller: &mut OpenController) -> bool {
    let fd = controller.file.as_raw_fd();
    match &mut controller.kind {
        DeviceKind::SteamDeckHidraw { last_report } => {
            let mut report = [0u8; 65];
            for _ in 0..MAX_HIDRAW_DRAIN {
                // SAFETY: `fd` is open non-blocking and `report` is valid for 65 bytes.
                let read = unsafe { libc::read(fd, report.as_mut_ptr().cast(), report.len()) };
                if read < 0 {
                    let err = std::io::Error::last_os_error();
                    if err.kind() == ErrorKind::WouldBlock {
                        return true;
                    }
                    return false;
                }
                if read == 0 {
                    return false;
                }
                if let Some((rx, ry)) = parse_deck_hid_report(&report[..read as usize]) {
                    controller.rx = rx;
                    controller.ry = ry;
                    *last_report = Instant::now();
                }
            }
            true
        }
        DeviceKind::Evdev { resync, .. } => {
            let event_size = size_of::<libc::input_event>();
            let mut buffer = [0u8; MAX_EVDEV_EVENTS * size_of::<libc::input_event>()];
            loop {
                // SAFETY: `fd` is open non-blocking and `buffer` is valid for its full length.
                let read = unsafe { libc::read(fd, buffer.as_mut_ptr().cast(), buffer.len()) };
                if read < 0 {
                    let err = std::io::Error::last_os_error();
                    if err.kind() == ErrorKind::WouldBlock {
                        return true;
                    }
                    return false;
                }
                if read == 0 {
                    return false;
                }
                let count = (read as usize) / event_size;
                for index in 0..count {
                    let offset = index * event_size;
                    // SAFETY: `buffer[offset..offset + event_size]` holds a full kernel `input_event`.
                    let event = unsafe {
                        std::ptr::read_unaligned(
                            buffer.as_ptr().add(offset).cast::<libc::input_event>(),
                        )
                    };
                    if event.type_ == EV_SYN && event.code == SYN_DROPPED {
                        *resync = true;
                    } else if *resync {
                        if event.type_ == EV_SYN && event.code == SYN_REPORT {
                            if let Some(rx) = read_evdev_axis(fd, ABS_RX) {
                                controller.rx = rx;
                            }
                            if let Some(ry) = read_evdev_axis(fd, ABS_RY) {
                                controller.ry = ry;
                            }
                            *resync = false;
                        }
                    } else if event.type_ == EV_ABS {
                        if u32::from(event.code) == ABS_RX {
                            controller.rx.value = event.value;
                        } else if u32::from(event.code) == ABS_RY {
                            controller.ry.value = event.value;
                        }
                    }
                }
                if count < MAX_EVDEV_EVENTS {
                    return true;
                }
            }
        }
    }
}

fn check_steam_pid_mode(pid: u32) -> Option<SessionMode> {
    let proc_dir = Path::new("/proc").join(pid.to_string());
    let exe = std::fs::read_link(proc_dir.join("exe")).ok()?;
    let name = exe.file_name()?.to_str()?;
    if !name.eq_ignore_ascii_case("steam") {
        return None;
    }
    let environ = std::fs::read(proc_dir.join("environ")).ok()?;
    parse_session_mode_environ(&environ)
}

fn detect_steam_session_mode(cached_pid: Option<u32>) -> (Option<SessionMode>, Option<u32>) {
    if let Some(pid) = cached_pid
        && let Some(mode) = check_steam_pid_mode(pid)
    {
        return (Some(mode), Some(pid));
    }
    let Ok(entries) = std::fs::read_dir("/proc") else {
        return (None, None);
    };
    for entry in entries.flatten() {
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<u32>().ok())
        else {
            continue;
        };
        if let Some(mode) = check_steam_pid_mode(pid) {
            return (Some(mode), Some(pid));
        }
    }
    (None, None)
}

fn run_worker(
    shared: Arc<Mutex<ControllerShared>>,
    emitter: DirectionEmitter,
    session: String,
    generation: u64,
    wake_read: OwnedFd,
) {
    let inotify = Inotify::init(InitFlags::IN_NONBLOCK | InitFlags::IN_CLOEXEC)
        .ok()
        .and_then(|instance| {
            instance
                .add_watch(
                    "/dev/input",
                    AddWatchFlags::IN_CREATE
                        | AddWatchFlags::IN_DELETE
                        | AddWatchFlags::IN_ATTRIB
                        | AddWatchFlags::IN_MOVED_TO,
                )
                .ok()?;
            Some(instance)
        });

    let mut controllers: Vec<OpenController> = Vec::new();
    let mut open_dev_inos: HashSet<(u64, u64)> = HashSet::new();
    let mut arbiter = ControllerArbiter::default();
    let mut next_controller_id: u64 = 1;
    let mut needs_scan = true;

    loop {
        let remaining_lease = {
            let state = shared.lock().expect("right_stick lock");
            if !state.enabled || state.generation != generation {
                break;
            }
            match state.focus_expires {
                Some(deadline) => match deadline.checked_duration_since(Instant::now()) {
                    Some(remaining) if !remaining.is_zero() => remaining,
                    _ => break,
                },
                None => break,
            }
        };

        if needs_scan {
            let has_steam_deck = controllers.iter().any(|controller| {
                matches!(
                    controller.kind,
                    DeviceKind::Evdev {
                        is_steam_deck: true,
                        ..
                    } | DeviceKind::SteamDeckHidraw { .. }
                )
            });
            for controller in
                discover_controllers(&open_dev_inos, &mut next_controller_id, has_steam_deck)
            {
                open_dev_inos.insert(controller.dev_ino);
                arbiter.add(controller.id, controller.rx, controller.ry);
                controllers.push(controller);
            }
            needs_scan = false;
        }

        let has_hidraw = controllers
            .iter()
            .any(|controller| matches!(controller.kind, DeviceKind::SteamDeckHidraw { .. }));
        let poll_timeout = if has_hidraw {
            remaining_lease.min(HIDRAW_STALE_TIMEOUT)
        } else {
            remaining_lease
        };
        let timeout_ms = poll_timeout.as_millis().clamp(1, i32::MAX as u128) as i32;

        let mut pollfds = Vec::with_capacity(2 + controllers.len());
        pollfds.push(libc::pollfd {
            fd: wake_read.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        });
        let inotify_slot = inotify.as_ref().map(|instance| {
            let index = pollfds.len();
            pollfds.push(libc::pollfd {
                fd: instance.as_fd().as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            });
            index
        });
        let controllers_start = pollfds.len();
        for controller in &controllers {
            pollfds.push(libc::pollfd {
                fd: controller.file.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            });
        }

        // SAFETY: Every `pollfd` in `pollfds` refers to an owned open descriptor alive for this call.
        let ready = unsafe {
            libc::poll(
                pollfds.as_mut_ptr(),
                pollfds.len() as libc::nfds_t,
                timeout_ms,
            )
        };
        if ready < 0 {
            if std::io::Error::last_os_error().kind() == ErrorKind::Interrupted {
                continue;
            }
            break;
        }

        if pollfds[0].revents != 0 {
            drain_pipe(wake_read.as_raw_fd());
            continue;
        }

        if let Some(index) = inotify_slot
            && pollfds[index].revents != 0
        {
            if let Some(instance) = inotify.as_ref() {
                let _ = instance.read_events();
            }
            needs_scan = true;
        }

        let now = Instant::now();
        let mut dead_indices = Vec::new();
        for (offset, controller) in controllers.iter_mut().enumerate() {
            let revents = pollfds[controllers_start + offset].revents;
            if (revents & (libc::POLLERR | libc::POLLHUP | libc::POLLNVAL)) != 0 {
                dead_indices.push(offset);
                continue;
            }
            if (revents & libc::POLLIN) != 0 {
                if !read_controller_updates(controller) {
                    dead_indices.push(offset);
                    continue;
                }
                if let Some(direction) = arbiter.update(controller.id, controller.rx, controller.ry)
                {
                    emitter(&session, direction);
                }
            } else if let DeviceKind::SteamDeckHidraw { last_report } = controller.kind
                && now.saturating_duration_since(last_report) > HIDRAW_STALE_TIMEOUT
            {
                dead_indices.push(offset);
                needs_scan = true;
            }
        }

        for index in dead_indices.into_iter().rev() {
            let removed = controllers.swap_remove(index);
            open_dev_inos.remove(&removed.dev_ino);
            if let Some(direction) = arbiter.remove(removed.id) {
                emitter(&session, direction);
            }
        }
    }

    emitter(&session, None);
    drop(controllers);
    let mut state = shared.lock().expect("right_stick lock");
    if state.generation == generation {
        state.worker_running = false;
        state.wake_write = None;
        state.focus_session = None;
        state.focus_expires = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stick(value: i32) -> Axis {
        Axis {
            value,
            min: -32768,
            max: 32767,
        }
    }

    #[test]
    fn axis_normalization_and_clamping() {
        assert!((stick(0).normalize()).abs() < 1e-4);
        assert!((stick(32767).normalize() - 1.0).abs() < 1e-6);
        assert!((stick(-32768).normalize() + 1.0).abs() < 1e-6);
        assert_eq!(
            Axis {
                value: 40000,
                min: -32768,
                max: 32767
            }
            .normalize(),
            1.0
        );
        assert_eq!(
            Axis {
                value: -40000,
                min: -32768,
                max: 32767
            }
            .normalize(),
            -1.0
        );
        let unsigned_mid = Axis {
            value: 128,
            min: 0,
            max: 255,
        };
        assert!(unsigned_mid.normalize().abs() < 0.01);
        assert_eq!(
            Axis {
                value: 10,
                min: 0,
                max: 0
            }
            .normalize(),
            0.0
        );
    }

    #[test]
    fn direction_hysteresis_engage_hold_and_diagonal_margin() {
        assert_eq!(direction_for(stick(18000), stick(0), None), None);
        assert_eq!(
            direction_for(stick(22000), stick(0), None),
            Some(Direction::Right)
        );
        assert_eq!(
            direction_for(stick(0), stick(-22000), None),
            Some(Direction::Up)
        );
        assert_eq!(
            direction_for(stick(-22000), stick(0), None),
            Some(Direction::Left)
        );
        assert_eq!(
            direction_for(stick(15000), stick(0), Some(Direction::Right)),
            Some(Direction::Right)
        );
        assert_eq!(
            direction_for(stick(10000), stick(0), Some(Direction::Right)),
            None
        );
        assert_eq!(
            direction_for(stick(20000), stick(23000), Some(Direction::Right)),
            Some(Direction::Right)
        );
        assert_eq!(
            direction_for(stick(15000), stick(28000), Some(Direction::Right)),
            Some(Direction::Down)
        );
    }

    #[test]
    fn steam_deck_hid_report_parsing() {
        let mut report = [0u8; 64];
        report[0..3].copy_from_slice(&[0x01, 0x00, 0x09]);
        report[52..54].copy_from_slice(&(25000i16).to_le_bytes());
        report[54..56].copy_from_slice(&(24000i16).to_le_bytes());
        let (rx, ry) = parse_deck_hid_report(&report).expect("valid deck report");
        assert_eq!(rx.value, 25000);
        assert_eq!(ry.value, -24000);
        assert_eq!(direction_for(rx, ry, None), Some(Direction::Right));

        assert!(parse_deck_hid_report(&report[..63]).is_none());
        report[0] = 0x02;
        assert!(parse_deck_hid_report(&report).is_none());
    }

    #[test]
    fn session_mode_parses_gamescope_and_desktop_environ() {
        assert_eq!(
            parse_session_mode_environ(
                b"HOME=/home/deck\0XDG_CURRENT_DESKTOP=gamescope\0DISPLAY=:0\0"
            ),
            Some(SessionMode::Gamescope)
        );
        assert_eq!(
            parse_session_mode_environ(b"GAMESCOPE_WAYLAND_DISPLAY=gamescope-0\0"),
            Some(SessionMode::Gamescope)
        );
        assert_eq!(
            parse_session_mode_environ(b"DISPLAY=:0\0XDG_CURRENT_DESKTOP=KDE\0"),
            Some(SessionMode::Desktop)
        );
        assert_eq!(
            parse_session_mode_environ(b"WAYLAND_DISPLAY=wayland-0\0"),
            Some(SessionMode::Desktop)
        );
        assert_eq!(parse_session_mode_environ(b"HOME=/home/deck\0"), None);
    }

    #[test]
    fn dual_stick_capability_filter_accepts_any_dual_stick_gamepad_and_rejects_non_gamepads() {
        let full_dual_stick =
            (1u64 << ABS_X) | (1u64 << ABS_Y) | (1u64 << ABS_RX) | (1u64 << ABS_RY);
        assert!(is_dual_stick_gamepad(
            full_dual_stick,
            true,
            stick(0),
            stick(0)
        ));
        assert!(!is_dual_stick_gamepad(
            full_dual_stick,
            false,
            stick(0),
            stick(0)
        ));
        let single_stick = (1u64 << ABS_X) | (1u64 << ABS_Y);
        assert!(!is_dual_stick_gamepad(
            single_stick,
            true,
            stick(0),
            stick(0)
        ));
        assert!(!is_dual_stick_gamepad(
            full_dual_stick,
            true,
            Axis {
                value: 0,
                min: 0,
                max: 0
            },
            stick(0)
        ));
    }

    #[test]
    fn multi_controller_arbiter_handles_centering_handoff_and_disconnect() {
        let mut arbiter = ControllerArbiter::default();
        arbiter.add(1, stick(25000), stick(0));
        assert_eq!(arbiter.update(1, stick(26000), stick(0)), None);
        assert_eq!(arbiter.update(1, stick(0), stick(0)), None);
        assert_eq!(
            arbiter.update(1, stick(25000), stick(0)),
            Some(Some(Direction::Right))
        );
        arbiter.add(2, stick(0), stick(0));
        assert_eq!(
            arbiter.update(2, stick(0), stick(25000)),
            Some(Some(Direction::Down))
        );
        assert_eq!(arbiter.remove(2), Some(Some(Direction::Right)));
        assert_eq!(arbiter.remove(1), Some(None));
    }

    #[test]
    fn disabled_controller_does_not_spawn_worker_and_disable_stops_active_focus() {
        let emitted = Arc::new(Mutex::new(Vec::<(String, Option<Direction>)>::new()));
        let recorded = Arc::clone(&emitted);
        let emitter: DirectionEmitter = Arc::new(move |session, dir| {
            recorded.lock().unwrap().push((session.to_owned(), dir));
        });
        let controller = RightStickController::new(false, emitter);
        assert!(!controller.is_enabled());
        controller.set_focus("session-1", true);
        assert!(!controller.is_worker_running());

        controller.set_enabled(true);
        assert!(controller.is_enabled());
        controller.set_focus("session-1", true);
        assert!(controller.is_worker_running());

        // Disabling immediately wakes, joins, emits None, and leaves zero running workers.
        controller.set_enabled(false);
        assert!(!controller.is_worker_running());
        assert!(
            emitted
                .lock()
                .unwrap()
                .iter()
                .any(|(session, dir)| session == "session-1" && dir.is_none())
        );
        controller.shutdown();
    }
}
