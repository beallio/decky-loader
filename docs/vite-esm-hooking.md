# Steam UI plugin hooking

Steam Deck has two Steam UI clients:

- **Steam Beta** loads the Steam UI as native ES modules. The build uses Vite, Rolldown, and the Oxc minifier.
- **Steam Stable** loads the Steam UI as Webpack chunks.

This loader supports both clients in one release. Use `@decky/ui` as upstream documents it. Plugin code does not change between clients.

## What the finders search

The loader gives plugins the same library on each client as upstream would:

| Client | Library | `findVar` and `findModuleScopeByVar` search |
| --- | --- | --- |
| Beta | `@decky/ui` 5 | Module scopes. These include variables that a module does not export. |
| Stable | `@decky/ui` 4.16.4 | Webpack module exports only. Webpack has no module scopes. |

These 5.x names also exist on Stable: `findVar`, `findModuleScopeByVar`, `findScopeDetailsByVar`, `findScope`, `createScopeMapping`, `createVarMapping`, `moduleScopes`, `processedModules`, `toastClasses`, `tabsClasses`. Write one finder for both clients. Do not test the client type unless you must.

The loader also puts these values on `window.DFL` on both clients: `EResult`, `EUIComposition`, and `QuickAccessTab.Music`.

## Match Steam source text on both clients

Steam source text uses different quote characters on each client. Webpack prints `"x"`. The Oxc minifier on Beta prints `` `x` ``. A finder that matches source text must accept both forms. Use a character class in the pattern:

```ts
findVar((e) => typeof e == 'function' && /controller:[`"]notification[`"],method:/.test(e?.toString?.() ?? ''));
```

Prefer these matches:

- Prop names and destructured option keys, for example `spacingBetweenLabelAndChild` or `itemMaxSize`.
- ARIA roles and localization tokens, for example `role:"tablist"` or `"#ProgressBar_ValueUnknown"`.
- Store shapes and method names, for example `"m_history" in e`, `.ShowPortalModal()`, or `.OnElementReadyCallbacks.Register(`.
- Unwrapped and wrapped React components: `e`, `e?.render`, and `e?.type`.

Avoid these matches:

- CSS class module keys such as `.PanelSection` or `.Field`. Rolldown removes many of them from JavaScript.
- Webpack numeric module ids. The ES module client does not have them.
- Unquoted single-letter minified names such as `e.a` or `t.x`. Their names change on every build.
- The assumption that sibling exports always share one module object.

Combine two or three specific literals, then confirm that the finder matches one object on both clients.

## Guard optional values

A Steam CSS class map can be absent or empty, mainly on Beta. Guard it:

```ts
const classes = findClassModule((m) => m.TabCount && m.TabTitle) ?? {};
```

Do not call `Object.values(findModule(...))` without a guard. It can return `undefined`.

## Find a component in CEF DevTools

1. Open CEF DevTools on `SharedJSContext`.
2. Select the element in the Inspector. `$0` is then that element.
3. Walk the React Fiber chain in the Console:

```js
let f = Object.entries($0).find(([k]) => k.startsWith('__reactFiber$'))?.[1];
while (f) {
  if (typeof f.type === 'function' || f.type?.render || f.type?.type) {
    console.log(f.type, f.memoizedProps);
  }
  f = f.return;
}
```

4. Copy a stable string from the component body. Search for that string with `DFL.findVar` or `DFL.findModuleScopeByVar` in the Console. On Beta, these search module scopes. On Stable, they search Webpack exports.
5. Put the same literal in your plugin, with both quote forms.

Do not compare the text of the file on disk with the runtime `toString()` text. They can differ in quoting, destructuring form, and property order. Match only the stable parts.

## Styles and CSS classes

- For your own UI, use your own CSS classes or Steam's global classes such as `.BasicUI`, `.GamepadMode`, `.gpfocus`, `.DialogButton`, and `.Primary`.
- Do not assume that `staticClasses`, `gamepadDialogClasses`, or `findClassModule(...)` contains a given class name on Beta.
- Oxc strips most `SVGIcon_*` class names on Beta. Find icons by their registry key, not by a class name.
- For hashed Steam classes in themes or DOM queries, use the class mapping feed from `beallio/steamui-watch`: `https://beallio.github.io/steamui-watch/{stable,beta}.json`. `beallio/SDH-CssLoader` reads this feed.

## Extra Steam components

Use `@beallio/decky-extras` for Steam components that `@decky/ui` does not export, for example `RadioButton`, `ToggleRow`, `DiscreteSlider`, `DialogSettingsSection`, `WarningBox`, `SectionHeader`, `PinInput`, `PasswordInput`, and `TextArea`. This is a separate library from the same author. `beallio/steamui-watch` checks it daily on Webpack Stable and ES module Beta, and it publishes its finder data on the `finder-data` branch.

## Breakage detection

`beallio/steamui-watch` runs daily static and headless Chrome checks against the Steam Deck `stable` and `publicbeta` bundles. It stores object fingerprints on the `finder-data` branch. It also uses `tools/finder_repair.ts` to suggest replacement finders, and it verifies each suggestion by object identity on both branches.
