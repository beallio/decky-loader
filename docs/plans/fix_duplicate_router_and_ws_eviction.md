# Implementation Plan: Fix Duplicate Frontend Router (#19) and WebSocket Socket Eviction Loop (#20)

## Problem Definition

Two linked bugs were reported on 2026-11-24 against the fork Loader running on Steam Deck (Steam Stable):

1. **Issue #19 — Duplicate frontend router (`window.DeckyBackend` missing listeners, install prompts never shown):**
   - Commit `ea72222` on `perf/loader-startup-and-memory` changed `frontend/src/index.ts` to statically `import { IS_ESM_CLIENT } from './stable/client'`.
   - `frontend/src/components/Toast.tsx` also statically imports `IS_ESM_CLIENT` from `../stable/client`.
   - Rollup therefore places `stable/client.ts` inside the entry chunk `index.js` and emits `export { IS_ESM_CLIENT as I };`, causing `chunk-*.js` (containing `start.tsx` and `PluginLoader`) to emit `import { I as IS_ESM_CLIENT } from './index.js';` (plus hoisted `import './index.js';` in six sibling chunks).
   - Because CEF injects `http://localhost:1337/frontend/index.js?v=<version>`, the browser ES module registry treats `./index.js` (no query string) as a **second distinct module** and evaluates the top-level boot IIFE in `index.js` a second time.
   - During that second evaluation, `chunk-*.js` (`start.tsx`) finishes synchronously while the second boot is awaiting `/auth/token`, binding `PluginLoader` listeners and `callPluginMethod` to router #1. Moments later, the second boot overwrites `window.DeckyBackend` with router #2 (which has zero listeners) and connects a second WebSocket.

2. **Issue #20 — Single active WebSocket eviction loop stalling all Loader calls up to 5 s:**
   - `backend-rust/src/transport.rs` maintains only one `active` WebSocket (`SocketRegistry`) and one global `PendingCalls` map keyed only by `call_id`.
   - When any second client connects to `/ws`, the backend closes the previous socket, stops reading it, routes all replies/events only to the newest socket, and lets the second client's `FULL_SYNC` collide with or prune the first client's pending calls.
   - With two routers (or any second client such as a diagnostic tool), each reconnect evicts the other every 5 s, stalling calls up to ~5 s and stealing backend events.

## Architecture Overview

Fix both issues independently so neither failure mode can recur:

1. **Frontend entry chunk isolation & boot idempotency (#19):**
   - Stop `frontend/src/index.ts` from statically importing `./stable/client` (keep the one-line ESM client check inline in `index.ts` so boot keeps zero extra HTTP hops while `stable/client.ts` stays private to the `start.tsx` chunk with `Toast.tsx`).
   - Add a Rollup `generateBundle` check (`noEntryReimport`) in `frontend/rollup.config.js` that fails the build if any entry chunk exports bindings or is imported (statically or dynamically) by any other chunk.
   - Add an idempotent boot guard (`claimFrontendBoot`) at the top of `frontend/src/index.ts` before any `await` so even an accidental second evaluation in the same `window` is a no-op.
   - Give each `WSRouter` instance a stable per-instance `clientId` (`&client_id=...` query parameter on `/ws`) so reconnects of the same router identify themselves unambiguously to the backend.

2. **Per-session multi-client WebSocket transport (#20):**
   - Replace the single `SocketRegistry` + global `PendingCalls` + `socket_lifecycle` mutex in `backend-rust/src/transport.rs` with a unified `SessionRegistry` protected by a single mutex (no `.await` ever held under the lock).
   - Key sessions by `SessionKey`:
     - `Client(String)` when a valid non-empty `client_id` (<= 128 bytes) is provided.
     - `AnonymousDefault` when no `client_id` is provided and no anonymous socket is currently connected (preserving `FULL_SYNC` replay deduplication for single legacy/anonymous clients reconnecting after disconnect).
     - `Anonymous(u64)` when no `client_id` is provided and an anonymous socket is already live (preventing concurrent anonymous clients from evicting each other).
   - Reconnecting with the **same** `client_id` replaces only that session's prior socket and preserves its `PendingCalls` for `FULL_SYNC` deduplication.
   - Distinct sessions never evict each other:
     - Replies go to the session that originated the call.
     - `PendingCalls` and `FULL_SYNC` pruning are strictly per-session.
     - Backend pub/sub `EVENT` frames broadcast to all connected sessions.
     - Graceful shutdown closes all connected sockets.
   - Disconnected sessions with empty `PendingCalls` are removed immediately; disconnected sessions with in-flight/unacknowledged calls are retained up to a bounded TTL (60 s) and count cap (16) to support reconnect replay without unbounded memory growth.

## Core Data Structures

- `frontend/src/bootGuard.ts`:
  - `FrontendBootHost`: `{ __deckyFrontendBooted?: boolean }`
  - `claimFrontendBoot(host: FrontendBootHost): boolean`
  - `buildWsUrl(authToken: string, clientId: string): string`
  - `createClientId(): string`
- `backend-rust/src/transport.rs`:
  - `WsQuery { auth: Option<String>, client_id: Option<String> }`
  - `SessionKey`: `Client(String) | AnonymousDefault | Anonymous(u64)`
  - `ClientSession { socket: Option<ActiveSocket>, pending: PendingCalls, disconnected_at: Option<Instant> }`
  - `SessionRegistry { sessions: HashMap<SessionKey, ClientSession> }`

## Public Interfaces

- WebSocket endpoint `/ws?auth=<token>[&client_id=<id>]`: backward compatible; `client_id` is optional.
- Existing WebSocket message protocol (`CALL`, `REPLY`, `DISCARD`, `RECEIVED_RESPONSE`, `FULL_SYNC`, `EVENT`, `ERROR`) is unchanged.
- No changes to HTTP routes, plugin SDK APIs, or `SDK_OVERLAYS`.

## Dependency Requirements

- Zero new dependencies across Rust, Node/TypeScript, and Python.

## Testing Strategy

1. **Frontend TDD (Red → Green):**
   - Add `noEntryReimport` plugin to `frontend/rollup.config.js` and confirm `pnpm --dir frontend run build` fails on the current broken `index.ts`.
   - Add `tests/frontend/boot_router.test.mts` testing `claimFrontendBoot` (first boot succeeds, second boot rejected, fresh context succeeds), `createClientId` uniqueness, and `buildWsUrl` encoding/query format.
   - Fix `frontend/src/index.ts` and `frontend/src/wsrouter.ts`, verify `node --test 'tests/frontend/*.test.mts'`, `pnpm --dir frontend run lint`, `pnpm --dir frontend run typecheck`, and `pnpm --dir frontend run build` all pass, and inspect built `backend/decky_loader/static/index.js` to confirm zero exports and zero chunk imports of `./index.js`.

2. **Rust TDD (Red → Green):**
   - Add integration tests in `backend-rust/src/transport.rs` covering:
     - Concurrent clients (both distinct `client_id`s and concurrent anonymous clients) do not evict each other, route identical `call_id`s independently without collision, isolate `FULL_SYNC` pruning, and both receive broadcast `EVENT`s.
     - Same `client_id` reconnect replaces its own previous socket and preserves `FULL_SYNC` deduplication without disturbing other connected clients.
     - Single anonymous client disconnect + reconnect preserves `FULL_SYNC` deduplication.
     - Shutdown closes all connected sockets cleanly.
   - Verify tests fail before refactor (or fail to compile/pass against single-socket registry), implement `SessionRegistry`, verify `cargo fmt --all --check`, `cargo clippy --locked --workspace --all-targets -- -D warnings`, and `cargo test --locked --workspace` pass with `CARGO_TARGET_DIR=/tmp/decky-loader/target`.
