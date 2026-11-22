// Stable: Steam Stable still runs Webpack, so the loader boots @decky/ui 4.x from it and shims the
// 5.x names the loader reads. Delete src/stable/ once Steam Stable ships ES modules.

export async function bootStable(): Promise<void> {
  if (!window.SP_REACT) {
    console.debug('[Decky:Boot] Setting up Webpack & React globals...');
    // deliberate partial import
    const DFLWebpack = await import('@decky/ui-legacy/dist/webpack');
    window.SP_REACT = DFLWebpack.findModule((m) => m.Component && m.PureComponent && m.useLayoutEffect);
    window.SP_REACTDOM =
      DFLWebpack.findModule((m) => m.createPortal && m.createRoot) ||
      DFLWebpack.findModule((m) => m.createPortal && m.__DOM_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE);

    console.debug('[Decky:Boot] Setting up JSX internals...');
    const jsxModule = DFLWebpack.findModule((m) => (m.jsx && m.jsxs) || (m.jsx && Object.keys(m).length == 1));
    if (jsxModule.jsxs) {
      window.SP_JSX = jsxModule;
    } else {
      window.SP_JSX = {
        jsx: jsxModule.jsx,
        jsxs: jsxModule.jsx,
        Fragment: window.SP_REACT.Fragment,
      };
    }
  }

  console.debug('[Decky:Boot] Setting up @decky/ui...');
  const legacy = await import('@decky/ui-legacy');
  const shims = Object.getOwnPropertyDescriptors({
    // 4.x has no module scopes, so the 5.x names map onto the Webpack module cache.
    findVar: legacy.findModuleExport,
    findModuleScopeByVar: legacy.findModuleByExport,
    findScopeDetailsByVar: legacy.findModuleDetailsByExport,
    findScope: legacy.findModule,
    createScopeMapping: legacy.createModuleMapping,
    // Same shape as 5.x createVarMapping, over Webpack export values instead of module scopes.
    createVarMapping(filter: (variable: unknown) => boolean) {
      const mapping = new Map<string, unknown>();
      for (const [id, mod] of legacy.modules) {
        for (const key in mod) {
          try {
            if (filter(mod[key])) mapping.set(`${id}#.${key}`, mod[key]);
          } catch (e) {
            console.debug('[Decky:Modules] Var mapping error', e);
          }
        }
      }
      return mapping;
    },
    get moduleScopes() {
      return legacy.modules;
    },
    processedModules: [],
    toastClasses: legacy.findClassModule((m) => m.ShortTemplate && m.StandardTemplateContainer) ?? {},
    tabsClasses: legacy.findClassModule((m) => m.TabCount && m.TabTitle) ?? {},
  });
  // The namespace holds live bindings (e.g. modules), so inherit from it instead of copying it.
  // Object.create is untyped and window.DFL is `any` in start.tsx.
  window.DFL = Object.create(legacy, shims) as unknown as typeof window.DFL;
}
