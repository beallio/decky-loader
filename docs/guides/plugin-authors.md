# For Plugin Authors

Plugins written for Official Decky generally work unchanged in this fork. Start with the [SteamDeckHomebrew Plugin Template](https://github.com/SteamDeckHomebrew/decky-plugin-template), then take advantage of the fork capabilities below when relevant.

---

## Root Flag & Folder Permissions

If your `plugin.json` sets `"flags": ["root"]`:

- Your plugin runs only if the user explicitly enabled root plugin support (see [Root Plugins Guide](./root-plugins.md)).
- Your plugin executes inside the isolated `decky-root-plugins` worker process (`root`), while its configuration/settings directories still live under the user's `homebrew` folder.
- Your plugin can **read** its plugin code directory at `DECKY_PLUGIN_DIR`, but **cannot write** inside `DECKY_PLUGIN_DIR` (write runtime/user state under `DECKY_PLUGIN_SETTINGS_DIR` / `DECKY_PLUGIN_RUNTIME_DIR`).

---

## Fast JSON Encoder for Python Plugins

This fork includes a native Rust JSON encoder exposed through `decky.native.dumps`:

```python
from decky import native

text = native.dumps({"name": "Deck", "values": [1, 2, 3]})
```

- Matches `json.dumps` with default formatting (`ensure_ascii=False` is the only supported option).
- Available **only in this fork**. If your plugin targets both Official Decky and this fork, guard the import/attribute:

```python
import json
import decky

dumps = decky.native.dumps if hasattr(decky, "native") else json.dumps
```

---

## Native Rust / Compiled Backends

You can ship a native compiled backend (such as Rust) instead of `main.py`, eliminating Python runtime startup and per-plugin Python memory overhead (see [Speed & Memory Benchmarks](./benchmarks.md#python-plugins-vs-native-rust-plugins)):

- Example SDK crate & sample plugin: [`backend-rust/decky-plugin/examples/native_example.rs`](../../backend-rust/decky-plugin/examples/native_example.rs)
- Full contract & IPC details: [Native plugin backends](../development.md#native-plugin-backends)

---

## Steam Beta (Vite / Rolldown) Frontend Compatibility

Steam Beta builds run Gaming Mode as native ES modules (Vite + Rolldown + Oxc + LightningCSS). Steam Stable still uses Webpack. One Decky release runs on both clients.

Plugins get the same `@decky/ui` API that Official Decky gives them on each client:

| Steam client | `@decky/ui` version | Same as |
|---|---|---|
| Steam Beta (ES modules) | `@decky/ui` 5 | Official Decky `v4.0.0-pre1` |
| Steam Stable (Webpack) | `@decky/ui` 4.16.4 plus the 5.x finder names | Official Decky `v3.3.1` plus shims |

- **Use the finder names from `@decky/ui` 5.** On Steam Beta, `findVar` finds module-scope bindings. On Steam Stable, `findVar` finds exported values only, because Webpack has no module scopes. Guard the result against `undefined`.
- **Match Steam source text in both quote forms.** Webpack prints `"text"`. The Oxc minifier on Steam Beta prints `` `text` ``. A finder that matches one form can fail on the other client:
  ```ts
  /controller:[`"]notification[`"],method:/
  ```
- **Guard Steam CSS class objects.** LightningCSS removes or renames most class names in the JavaScript on Steam Beta. Check for missing keys and objects.
- For patterns, safe component discovery, and testing on Steam Beta, see the [Steam UI plugin hooking guide](../vite-esm-hooking.md).

---

## Building & Contributing to This Fork

To build the loader, run tests, or inspect internal architecture:

- [Development Guide](../development.md)
- [Root Plugin Host Design](../root-plugin-host.md)
- [Steam UI Plugin Hooking Guide](../vite-esm-hooking.md)
