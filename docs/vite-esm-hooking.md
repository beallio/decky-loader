# Vite / Rolldown SteamUI Hooking Review & Component Discovery Guide

Starting with Steam Deck `publicbeta` build `1795048523` (`CLSTAMP` `11255874`), Valve migrated the Steam Deck client UI from Webpack to **Vite + Rolldown + Oxc + LightningCSS**:

- `window.webpackChunksteamui` and numeric Webpack module IDs are gone.
- Entry loading switched from classic `<script defer src="/library.js">` to native ES modules (`<script type="module" src="/library.js?contenthash=...">`).
- Rolldown scope-hoists hundreds of source files into shared chunks (`chunk~5ad53110ba.js` is 9.3 MB with **20,120** top-level `var`/`function` bindings, of which only **149** are `export`ed).
- Oxc minifies strings into template literals (`` `...` ``) and destructuring into `let{...}=e`.
- Rolldown tree-shakes ~85% of CSS class-module objects out of JavaScript (`4,542 / 5,347` specific class keys absent from all 491 JS chunks), inlining opaque 12–13 character hashes (e.g., `kg8VJS07NZk-`).

This document records:
1. An **exhaustive review of hooking mechanisms** evaluated for Decky Loader, why the current `<script type="importmap">` + `/steamui/{*path}` + append-only footer design was selected, and its known limitations/follow-ups.
2. A **practical developer guide** for locating, inspecting, using, and patching Steam UI components under Vite/Rolldown.

---

## Part 1: Exhaustive Review of Hooking Mechanisms

### Empirical constraints from `publicbeta 1795048523` & SteamOS CEF

Two empirical facts dictate which mechanisms can work in production on SteamOS:

1. **Unexported scope-hoisted locals:** Shared cross-chunk libraries (`React`/`ReactDOM`, `CommonUI`, `IconsModule`) are exported from their chunks, **but** core Decky targets in the 9.3 MB Gaming Mode mega-chunk (`chunk~5ad53110ba.js`) — Toaster, QAM renderer, HistoryStore, Router/Backstack, Carousel — are unexported private top-level declarations.
2. **All top-level application declarations are mutable `var` or `function` bindings:** AST inspection of `publicbeta 1795048523` shows `0` top-level `let`, `0` top-level `class`, and only `1` `const` (`__vite__mapDeps`) per entry/mega-chunk:

   | Chunk | `var` | `function` | `let` | `const` | `class` |
   |---|---:|---:|---:|---:|---:|
   | `library.js` | 206 | 53 | 0 | 1 | 0 |
   | `chunk~3126832f23.js` (React/ReactDOM) | 692 | 437 | 0 | 0 | 0 |
   | `chunk~1fa4b8f2ff.js` (CommonUI) | 350 | 284 | 0 | 0 | 0 |
   | `chunk~5ad53110ba.js` (9.3 MB Gaming Mode) | 12,587 | 7,533 | 0 | 1 | 0 |

3. **SteamOS CEF custom resource handler:** In SteamOS `steamwebhelper`, `https://steamloopback.host/*` is served through an in-process CEF custom resource handler that **bypasses CDP `Fetch.enable`**.

### Evaluation matrix (9 mechanisms)

| # | Mechanism | Sees Cross-Chunk Exports? | Sees Private Locals (Toaster/QAM/Router)? | Supports `afterPatch` / Live Mutation? | Works in SteamOS CEF? | Verdict |
|---|---|---:|---:|---:|---:|---|
| 1 | **`<script type="importmap">` + local `/steamui/{*path}` proxy + append-only footer** | Yes | **Yes** (~20k+ bindings) | **Yes** (live getter/setter closures) | **Yes** (verified on SteamOS VM) | **Selected (implemented)** |
| 2 | **CDP `Fetch.enable` (`Response` stage) + append-only footer** | Yes | Yes | Yes | **No on `steamloopback.host`** (bypassed by CEF custom resource handler; works in stock Chrome) | **Rejected for primary** (used only in headless Chrome CI) |
| 3 | **Pure runtime `import(url)`** (via `performance.getEntriesByType("resource")`) | Yes | **No** (149 of 20k+ bindings in mega-chunk) | **No** (ESM Module Namespace Objects are spec-frozen) | Yes | **Rejected** (insufficient for loader & plugins; only viable as read-only degraded mode) |
| 4 | **React Fiber tree traversal (`__reactFiber$`)** | Mounted tree only | Mounted components only | Partial (only rendered instances/props; misses stores & unmounted modals) | Yes | **Complement only** (great for DevTools discovery & patching rendered subtrees; cannot replace module registry) |
| 5 | **Wrapping `SP_JSX.jsx` / `SP_REACT.createElement`** | Rendered elements only | Rendered components only | Render-time interception only (misses non-React stores/APIs) | Yes | **Complement only** (useful fallback if a component is `const`/closure-captured) |
| 6 | **CDP `Debugger.enable` + `Runtime.getProperties` / `Debugger.setScriptSource`** | Yes | Yes (even `const`) | Unreliable on ESM; async CDP only | Severe V8 deoptimization / CPU overhead | **Rejected** (unusable in production on Steam Deck) |
| 7 | **Service Worker on `steamloopback.host`** | N/A | N/A | N/A | Custom CEF schemes don't reliably support SW; misses first boot | **Rejected** |
| 8 | **On-disk patching of `~/.local/share/Steam/steamui/*.js`** | Yes | Yes | Yes | Overwritten on every Steam launch/verify; breaks clean uninstall | **Rejected on principle** |
| 9 | **Native `LD_PRELOAD` / CEF `CefResourceRequestHandler` binary hook** | Yes | Yes | Yes | Fragile across `steamwebhelper` binaries & `pressure-vessel` | **Rejected** (high crash & maintenance risk) |

### Architecture of the selected mechanism

Implemented in `backend-rust/src/cef.rs`, `backend-rust/src/vite_esm.rs`, and `backend-rust/src/transport.rs`:

1. **Zero-impact Steam Stable isolation (`cef.rs`):** `SharedJSContext` is classified as Vite only when `script[type="module"][src*="library.js"]` is present in the DOM. Webpack Steam Stable skips every step below completely.
2. **Pre-document shim + import map (`vite_esm.rs`, `cef.rs`):**
   - Installed via `Page.addScriptToEvaluateOnNewDocument`.
   - Injects a `<script type="importmap">` into `<head>` as early as possible (immediately or via `MutationObserver`):
     - Maps `"https://steamloopback.host/"` → `"http://localhost:1337/steamui/"` so static and dynamic ES module imports route through Decky's local Axum server.
     - Pins `"http://localhost:1337/steamui/library.js?..."` back to `"https://steamloopback.host/library.js?..."` so chunks that import back into `library.js` share the exact same module instance and React Contexts.
   - If Decky attached after `SharedJSContext` already loaded (`!window.__DECKY_ESM__`), `cef.rs` triggers one `Page.reload({ ignoreCache: true })` and injects Decky on `Page.domContentEventFired`.
3. **Append-only live getter/setter footer (`transport.rs`, `vite_esm.rs`):**
   - `/steamui/{*path}` validates against path traversal and redirects non-`.js` requests (`.css`, fonts, images) via `302 Found` back to `https://steamloopback.host/{path}`.
   - For `.js` files, a zero-dependency Rust JS scanner extracts top-level mutable (`var`, `let`, `function`) binding names (tracking strings, comments, nested template literals `${...}`, regex literals vs division, and destructuring patterns), caches the footer in memory by `(path, size, mtime)`, and appends:
     ```js
     ;(globalThis.__DECKY_ESM_REG__?.("chunk~5ad53110ba.js", {
       get p4(){return p4}, set p4(v){p4=v},
       // ...
     }));
     ```
   - Zero modification to Steam's files on disk and zero line-number shifts in stack traces.
4. **Compatibility layer (`vite_esm.rs`):**
   - `globalThis.__DECKY_ESM_REG__` registers each chunk box under `"esm:<chunk>"` and each plain non-React object export under `"esm:<chunk>:<var>"` (skipping React `$$typeof` objects, DOM nodes, `window`, and `document`).
   - Exposes a `window.webpackChunksteamui` compatibility proxy backed by `globalThis.__DECKY_ESM__` so `@decky/ui` and existing plugins work without rewriting their loader bootstrap.
   - Wraps `Function.prototype.toString` (with a `WeakMap` cache) to normalize Oxc's simple backtick literals (`` `foo` `` → `"foo"`) and `let{` → `const{` so existing `toString().includes(...)` finders keep matching.

### Known limitations & tracked follow-ups

These real limitations are tracked in the `beallio/decky-loader` Vite ESM follow-up issue:

1. **Lazy-loaded Vite chunks after initial Decky boot:** Late dynamic chunks (e.g., Settings, Properties, Media, Store routes loaded on first navigation) register themselves into `globalThis.__DECKY_ESM__` when they execute, **but** `@decky/ui`'s `webpack.js` snapshots `Object.keys(webpackRequire.m)` into its static `modules` Map only once at startup. Syncing late `__DECKY_ESM_REG__` arrivals into `window.DFL.modules` (or exposing a live search helper) is needed if plugins want `findModule*` to discover lazy route chunks automatically.
2. **`library.js` entry-chunk bindings:** Because `library.js` is pinned back to `steamloopback.host` to avoid double-evaluation, its own ~259 top-level declarations are not instrumented. Currently harmless (all loader/`@decky/ui`/`decky-extras` targets live in imported chunks), but must be monitored if Valve ever moves a target directly into `library.js`.
3. **Future Rolldown `const` / `class` top-level bindings:** Rolldown currently emits `0` top-level `class` and only `const __vite__mapDeps`. If a future Rolldown release emits top-level `const` or `class` declarations, `extract_top_level_mutable_names` should emit read-only `get X(){return X}` descriptors so discovery keeps working without throwing `TypeError` on assignment.
4. **CSP resilience:** If Valve adds a strict `Content-Security-Policy` to `steamloopback.host` blocking inline `<script type="importmap">` or `http://localhost:1337`, `cef.rs` will need `Page.setBypassCSP({ enabled: true })` (or a fallback transport).
5. **Eagerly captured references:** Assigning through a live `var` setter updates subsequent callers of that module-scoped binding, not references already copied into lookup `Map`s or passed into `React.memo(...)`/`React.forwardRef(...)` before the footer ran. Those still require patching `.type`/`.render` or a wrapper/fiber approach.
6. **Non-standard Steam install paths:** `resolve_steamui_root` checks `$STEAM_DIR` plus standard `~/.local/share/Steam`, `~/.steam/{root,steam}`, and Flatpak paths. Custom install locations require `STEAM_DIR` unless process-path discovery is added.
7. **Retiring `viteCompatDeckyUi()` Rollup fallbacks:** Tracked automatically by [`beallio/steamui-watch#38`](https://github.com/beallio/steamui-watch/issues/38) (`Upstream @decky/ui Vite compatibility tracker`). Once unmodified upstream `@decky/ui` passes on Vite `publicbeta`, the Rollup fallbacks in `frontend/rollup.config.js` can be removed.

---

## Part 2: Developer Guide — Locating & Using Components Now

### 1. Prefer maintained libraries over raw finders

- **Standard components (`@decky/ui` / `window.DFL`):** Keep importing `PanelSection`, `PanelSectionRow`, `Field`, `DialogButton`, `ToggleField`, `SliderField`, `ModalRoot`, `SimpleModal`, `Tabs`, `ProgressBar`, `Spinner`, `Marquee`, `ControlsList`, etc. from `@decky/ui`. On `beallio/decky-loader`, `viteCompatDeckyUi()` transparently supplies Vite-safe fallbacks while preserving original Webpack finders on Steam Stable.
- **Extra Steam components (`@beallio/decky-extras`):** For Steam UI components not exported by `@decky/ui` (e.g., `RadioButton`, `ToggleRow`, `DiscreteSlider`, `DialogSettingsSection`, `WarningBox`, `SectionHeader`, `PinInput`, `PasswordInput`, `TextArea`), use `@beallio/decky-extras`. Its literal-atom manifest is verified daily on both Webpack Stable and Vite Beta by `beallio/steamui-watch`, auto-refreshes from `https://beallio.github.io/steamui-watch/decky-extras.json`, and publishes source-free prop references on the `finder-data` branch.

### 2. Discovering new components in CEF DevTools on Vite

The old Webpack DevTools workflow (`webpackChunksteamui` IDs, browsing 637 CSS class modules by readable name) does not exist on Vite. Use this workflow instead:

1. **Start from the rendered DOM (`__reactFiber$`):**
   - Open CEF DevTools on `SharedJSContext` (or the Gaming Mode window), pick the target element with the Inspector (`$0`), and walk its React Fiber chain in the Console:
     ```js
     let f = Object.entries($0).find(([k]) => k.startsWith("__reactFiber$"))?.[1];
     while (f) {
       if (typeof f.type === "function" || f.type?.render || f.type?.type) {
         console.log(f.type, f.memoizedProps);
       }
       f = f.return;
     }
     ```
2. **Inspect the component's exact body and find its live registry entry (`globalThis.__DECKY_ESM__`):**
   - Once you have a candidate component `target = f.type` (or `f.type.render` / `f.type.type`), locate which chunk and binding holds it by object identity:
     ```js
     for (const [id, mod] of globalThis.__DECKY_ESM__) {
       for (const k of Object.keys(mod)) {
         try {
           if (mod[k] === target) console.log(id, k, target.toString().slice(0, 200));
         } catch {}
       }
     }
     ```
   - Note: `globalThis.__DECKY_ESM__` includes **both** startup chunks and lazily-loaded route chunks loaded after Decky booted.
3. **Remember the Oxc raw-file vs runtime `toString()` difference:**
   - Inside Decky's `SharedJSContext`, `Function.prototype.toString` automatically normalizes simple backticks (`` `foo` `` → `"foo"`) and `let{` → `const{`.
   - When grepping raw files on disk (`~/.local/share/Steam/steamui/chunk~*.js`), you will see Oxc's raw backticks and `let{`.

### 3. Writing Vite-safe custom finders (works on both Stable and Beta)

| Do | Avoid |
|---|---|
| Match on **prop names / destructured option keys** (`spacingBetweenLabelAndChild`, `itemMaxSize`, `rtEstimatedCompletionTime`, `childrenContainerWidth`) | Matching on **CSS class module property accesses** (`.PanelSection`, `().Field`, `.ProgressBar`) — ~85% are inlined to opaque hashes |
| Match on **ARIA / semantic strings and localization tokens** (`role:"tablist"`, `role:"progressbar"`, `"#ProgressBar_ValueUnknown"`, `"shift-children-below"`) | Relying on **`findClassModule(...)`** returning a non-empty object — always guard with `\|\| {}` / `?? {}` |
| Match on **store shapes and method names** (`"m_history" in e && "m_navigator" in e`, `.ShowPortalModal()`, `.OnElementReadyCallbacks.Register(`) | Hardcoded **Webpack numeric module IDs** (`"43088"`) — permanently gone |
| Accept **unwrapped and wrapped React components** (`e`, `e?.render`, `e?.type`) — Rolldown changes/unwraps some `memo`/`forwardRef` shapes | Assuming **sibling exports share one module object** (`findModuleDetailsByExport` returning Component B beside Component A) |
| Combine **2–3 specific literals** and verify uniqueness (`matches === 1`) on both Stable and Beta | Unquoted single-letter minified variable names (`e.a`, `t.x`) that rename every build |

**Example (Vite + Webpack dual-compatible finder):**
```ts
import { findModuleExport } from '@decky/ui';

// Works on Webpack Stable (CSS class access) and Vite Beta (ARIA + localization token)
export const MyProgressBar =
  findModuleExport((e) => e?.toString && /\.ProgressBar,(?:\w*==)?"standard"/.test(e.toString())) ||
  findModuleExport(
    (e) =>
      typeof e === 'function' &&
      e.toString().includes('role:"progressbar"') &&
      e.toString().includes('"#ProgressBar_ValueUnknown"'),
  );
```

### 4. Styling and CSS classes on Vite

- **For your plugin's own UI:** Prefer your own scoped CSS classes, inline styles, or Steam's unhashed global classes (`.BasicUI`, `.GamepadMode`, `.gpfocus`, `.DialogButton`, `.Primary`). Do not assume `staticClasses`, `gamepadDialogClasses`, or `findClassModule(...)` will contain class names on Vite builds.
- **For targeting Steam's hashed CSS classes (themes / DOM queries):** Use the self-hosted CSS mapping feed (`https://beallio.github.io/steamui-watch/{stable,beta}.json`, consumed automatically by `beallio/SDH-CssLoader`), which tracks hashed class renames across Vite builds via structural CSS fingerprinting.

### 5. Automated breakage detection & repair suggestions

`beallio/steamui-watch` runs daily static and headless Chrome runtime checks against both Steam Deck `stable` and `publicbeta` bundles, stores object fingerprints in `finder-data` (`snapshots/beta-*.json` and `snapshots/stable-*.json`), and uses `tools/finder_repair.ts` to suggest replacement finders verified by object identity (`===`) on both branches when Valve changes a component.
