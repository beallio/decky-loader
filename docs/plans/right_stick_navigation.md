# Implementation Plan: Built-in Universal Right-Stick Navigation

## Problem Definition

Users on SteamOS handhelds (Steam Deck, Lenovo Legion Go / Legion Go S, ROG Ally, etc.) and docked controllers want to navigate Steam Gaming Mode, Big Picture, and Decky menus using the right analog stick (Nintendo Switch style), toggled directly from Decky's General Settings page — without installing a separate Python plugin that adds a worker process and continuous 62.5 Hz / 25 Hz / 4 Hz polling overhead.

## Architecture Overview

- **Backend (`backend-rust/src/right_stick.rs`, `backend.rs`, `main.rs`):**
  - On-demand native Rust input engine (`RightStickController`) with zero threads, zero tasks, and zero open input fds when disabled or unfocused.
  - Universal dual-stick evdev discovery (`/dev/input/event*` with `ABS_X`, `ABS_Y`, `ABS_RX`, `ABS_RY`, `BTN_SOUTH`) plus Steam Deck `/dev/hidraw*` (`0x28DE:0x1205`) fallback when `hid-steam` mutes evdev.
  - Concurrent multi-controller monitoring via single `libc::poll()` + `inotify` hotplug on `/dev/input` + wakeup pipe (`pipe2`) for instant teardown.
  - Per-controller axis normalization (clamped to `[-1.0, 1.0]`), Schmitt-trigger hysteresis (`0.58` engage, `0.40` hold, `0.15` diagonal switch margin), centered-before-fire guard, and active-owner arbitration.
  - Cached Steam PID `/proc` session-mode detection (`gamescope` vs `desktop`).
  - Push-only WebSocket event `loader/right_stick_direction` on direction transitions, backed by a focus lease (`utilities/right_stick/set_focus`) and immediate shutdown on disable or backend exit.
- **Frontend (`frontend/src/right-stick-navigation.ts`, `DeckyState.tsx`, `plugin-loader.tsx`, `components/settings/pages/general/index.tsx`):**
  - `RightStickNavigationService` persisted via `rightStickNavigation` setting (default `false`).
  - Zero timers, zero WS listeners, and zero `NavigationManager` touches when disabled.
  - When enabled: monitors Steam UI focus (`focusedWindow`), claims/renews backend focus lease (2 s heartbeat only while focused), listens to `loader/right_stick_direction`, and dispatches `GamepadButton.DIR_UP/DOWN/LEFT/RIGHT` through a synthetic `GamepadInputSource` with `SetRepeatAllowed(valid)` and immediate release on blur/disable/suspend.
  - General Settings toggle under **Other** with localized strings in `backend/decky_loader/locales/en-US.json` and `SDK_OVERLAYS` entry in `scripts/runtime/prepare_bundle.py`.

## Core Data Structures

- Rust: `Direction`, `AxisState`, `ControllerState`, `ControllerArbiter`, `SessionMode`, `RightStickController`.
- TypeScript: `Direction`, `SessionMode`, `RightStickNavigator`, `RightStickNavigationService`.

## Public Interfaces

- Settings key: `rightStickNavigation: boolean` (default `false`).
- WebSocket RPCs:
  - `utilities/right_stick/get_session_mode()` -> `"gamescope" | "desktop" | null`
  - `utilities/right_stick/set_focus(session: string, active: bool)` -> `null`
- WebSocket Event:
  - `loader/right_stick_direction` with payload `{ session: string, direction: "up" | "down" | "left" | "right" | null }`.

## Dependency Requirements

- Zero new Rust, Node.js, or Python dependencies (`libc` and `nix` with `inotify`/`fs`/`process` are already in `backend-rust/Cargo.toml`).

## Testing Strategy

1. Strict TDD (Red → Green → Refactor) for Rust (`backend-rust/src/right_stick.rs`) and Frontend (`tests/frontend/right_stick_navigation.test.mts`).
2. Full local checks: `cargo fmt`, `cargo clippy -D warnings`, `cargo test`, `pnpm build/typecheck/lint`, `node --test`, `tests/runtime`.
3. Live `steamdeck-vm` verification & performance benchmarks on both **Steam Beta (ESM)** and **Steam Stable (Webpack)** using real virtual dual-stick `/dev/uinput` controllers (Lenovo `0x17EF` + Steam Deck `0x28DE:0x1205`).
