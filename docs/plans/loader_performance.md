# Implementation Plan: Loader Startup, Memory, and Runtime Performance

## Problem Definition

Baseline verification on `steamdeck-vm` (Steam Beta `1795048523`, 7 plugins) and local builds identified concrete, measured bottlenecks across five areas:

1. **Cold startup overhead (`main.rs` + `install.py`):** Running `DeckyPluginWorker --describe` on every startup costs a median **446 ms** (and causes a 0-plugin cold start to take **880 ms**), plus `getent passwd` spawns a subprocess (~4 ms).
2. **Binary size & Tokio thread/arena footprint (`Cargo.toml` + `main.rs`):** `[profile.release]` omits `lto = "thin"` and `codegen-units = 1` (12.71 MiB vs. 8.77 MiB, −31%), and `#[tokio::main]` spawns one worker thread per logical CPU (8 threads + arenas on Steam Deck for a single-user loopback daemon).
3. **Worker startup & permission walk (`workers.rs` + `platform.rs`):**
   - `WorkerSupervisor::try_wait` sends a JSON `{"op": "status"}` pipe round-trip to `launcher.py` on every socket poll miss even though a kernel `pidfd` (`exit`) is already held.
   - `set_plugin_permissions_on_approved` recursively toggles every plugin directory `0o755 -> 0o700 -> 0o755` on every startup even when running unprivileged (`effective_uid != 0`), costing **~388 ms** with `Unifideck`'s 2,896 files.
4. **Blocking I/O & install RAM spikes (`plugin_manager.rs` + `utilities.rs` + `backend.rs`):**
   - `perform_install` buffers full plugin ZIPs in RAM (`response.bytes().await`), causing a **39.65 MiB** `Private_Dirty` peak on a 24 MiB ZIP, and extracts synchronously on a Tokio worker thread, spiking `utilities/ping` to **46.25 ms**.
   - `decoding_download_client` and `Utilities::send_legacy_http` rebuild `reqwest::Client` on every request instead of reusing shared clients.
   - `filepicker_listing` runs synchronous `std::fs::read_dir` + `metadata()` on the Tokio executor (200 ms on `/usr/lib`) and stats hidden files before skipping them.
5. **Steam Beta CDP, static assets, and frontend boot/store (`cef.rs` + `transport.rs` + `frontend/src/`):**
   - `resolve_scope_in` fetches 8.4 KB of prototype properties (`ownProperties: false`) instead of 1.3 KB (`ownProperties: true`, verified 40/40 scopes on Steam Beta) and blocks on `Runtime.releaseObjectGroup` (+1.13 ms/call).
   - Static asset serving canonicalizes fixed roots per request, allocates zeroed 16 KiB `Vec`s per chunk, and sends `Cache-Control: no-cache` on content-hashed `/frontend/chunk-*.js`.
   - Frontend boot executes an 8-step serial HTTP waterfall (`index.js` → `/auth/token` → `wsrouter` → `logger` → 356-byte `stable/client` → `module-system` → `@decky/ui-v5` → `EResult/EUIComposition` → `start.tsx`) and spins `setTimeout(0)` during SteamApp init; `storeFetch` calls `updateVersion()` per store URL; `getStoreSettings()` runs 3 serial RPCs; boot runs duplicate `checkPluginUpdates()`; `sortStorePlugins` re-parses dates inside `.sort()`; Store cards load images eagerly.

## Architecture Overview

The optimizations preserve all existing security boundaries, IPC protocols, installer handoff compatibility (`queue-install-bundle`), Steam Stable + Beta dual-boot compatibility, and upstream `SDK_OVERLAYS` boundaries:

- **Phase 1 (Build & Startup Core):** Enable `lto = "thin"` + `codegen-units = 1` in `[profile.release]`; bound Tokio to `worker_threads = 2`; replace `getent` with `nix::unistd::User::from_name`; add optional `--sdk-version` and `--python-version` CLI flags to `decky-loader-runtime` populated by `install.py` from the verified bundle manifest, falling back to `DeckyPluginWorker --describe` when omitted.
- **Phase 2 (Worker Startup & Non-Root Permissions):** Add non-blocking `pidfd` poll (`platform::fd_is_readable`) so `WorkerSupervisor::try_wait` only queries `ForkLauncher`/`RootHost` when the process has exited; skip the no-op `0o700 -> 0o755` directory lock toggle when `effective_uid != 0` and only call `fchmod` when mode bits differ from `0o755`.
- **Phase 3 (Non-Blocking I/O & Install Memory):** Stream plugin ZIP downloads to a temp file inside `StagedPlugin` while computing SHA-256 incrementally; run `inspect_archive` and `extract_archive` inside `tokio::task::spawn_blocking`; reuse a shared `download_client` on `Backend` and `legacy_client` on `Utilities` when default HTTP options apply; run `filepicker_listing` in `spawn_blocking` and check `is_hidden` before `path.metadata()`.
- **Phase 4 (Steam Beta CDP & Static Assets):** Pass `"ownProperties": true` in `resolve_scope_objects` step 2 and send `Runtime.releaseObjectGroup` non-blockingly via `ScopeConnection::notify`; pre-canonicalize `assets`/`locales` at `transport::serve` startup; read files ≤ 2 MiB in a single buffer and reuse a buffer when streaming larger files; emit `Cache-Control: public, max-age=31536000, immutable` for `/frontend/chunk-*.js`.
- **Phase 5 (Frontend Boot & Store Efficiency):** Parallelize `/auth/token` and `wsrouter` import; inline `IS_ESM_CLIENT` check without a separate chunk hop; back off `isSteamAppInitialized` polling to 16 ms; prefetch `EResult`/`EUIComposition` in parallel; cache `version.current` in `storeFetch`; parallelize `getStoreSettings()`; deduplicate overlapping boot `checkPluginUpdates()` calls; precompute timestamps in `sortStorePlugins`; add `loading="lazy"` and `decoding="async"` on Store card images.

## Core Data Structures

- `RuntimeMetadata { python: String, sdk_version: String }` in `backend-rust/src/main.rs` (parsed from CLI flags or fallback `--describe`).
- `DownloadedArchive { path: PathBuf, sha256: String }` in `backend-rust/src/plugin_manager.rs` (temp file auto-cleaned via `StagedPlugin` or RAII drop guard).
- `AppState` in `backend-rust/src/transport.rs` stores pre-canonicalized `assets: Arc<PathBuf>` and `locales: Arc<PathBuf>`.
- `Backend` in `backend-rust/src/backend.rs` adds `pub(crate) download_client: reqwest::Client`.

## Public Interfaces

- `decky-loader-runtime` CLI adds two optional flags (100% backward compatible):
  - `--sdk-version STRING`
  - `--python-version STRING`
- `backend-rust/install.py`: `_generated_unit(..., sdk_version: str | None = None, python_version: str | None = None)` remains backward-compatible with existing positional callers.
- No WebSocket RPC routes, HTTP paths, plugin SDK APIs, or `SDK_OVERLAYS` file lists change.

## Dependency Requirements

- Zero new Rust, Python, or Node.js dependencies. All primitives (`nix::unistd::User`, `libc::poll`, `tokio::task::spawn_blocking`, `tokio::io::AsyncWriteExt`, `sha2::Sha256`) are already in `backend-rust/Cargo.toml`.

## Testing Strategy

1. **Strict TDD (Red → Green → Refactor)** for each phase:
   - Phase 1: Rust tests for `resolve_runtime_metadata` (CLI flags bypass `--describe`, fallback to `--describe`, CPython < 3.11 rejection) and `resolve_host_user`; Python unit test in `tests/runtime/test_user_service_installer.py` verifying `_generated_unit` emits `--sdk-version` and `--python-version`.
   - Phase 2: Rust tests for `platform::fd_is_readable` (live vs exited child), `WorkerSupervisor::try_wait` skipping `child_status` while live and reaping on exit, and non-root `set_plugin_permissions_on_approved` preserving `0o755` repair + symlink/hardlink refusal without toggling already-`0o755` directories.
   - Phase 3: Rust tests for streamed ZIP install (hash check, root handoff digest, rollback, temp cleanup, concurrent ping responsiveness), shared `legacy_client` behavior, and `filepicker_ls` hidden-file skip.
   - Phase 4: Rust tests for `cef.rs` (`ownProperties: true`, non-blocking `releaseObjectGroup` sent on success & failure) and `transport.rs` (`immutable` cache header on `chunk-*.js`, `no-cache` on `index.js`, small vs large file serving).
   - Phase 5: Frontend unit tests in `tests/frontend/store_sources.test.mts` for precomputed sort timestamps + `tsc` / `prettier` / `pnpm run build` (including the `dfl-global` boot chunk graph check).
2. **Full local verification suite** before every atomic commit.
3. **Final A/B verification on `steamdeck-vm`** (isolated disposable runs + live checks) measuring startup ms, `CHOWN=1` walk ms, 24 MiB install peak `Private_Dirty` & `max_ping`, and binary size.
