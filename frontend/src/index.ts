// Sets up DFL, then loads start.ts which starts up the loader

interface Window {
  _deckyModuleResolverInternals: {
    scopes: Map<number, unknown>;
    scopedObjects: Map<number, unknown>;
  };
  DFLModules: unknown;
}

interface SteamAppInitialization {
  BFinishedInitBeforeLogin?: () => boolean;
  BFinishedInitStageOne?: () => boolean;
}

const isSteamAppInitialized = () => {
  const app = window.App as unknown as SteamAppInitialization | undefined;
  return app?.BFinishedInitBeforeLogin?.() ?? app?.BFinishedInitStageOne?.() ?? false;
};

(async () => {
  console.debug('[Decky:Boot] Frontend init');

  console.debug('[Decky:Boot] Authenticating with Decky backend...');
  window.deckyAuthToken = await fetch('http://127.0.0.1:1337/auth/token').then((r) => r.text());
  console.debug('[Decky:Boot] Connecting to Decky backend...');
  const ws = (window.DeckyBackend = new (await import('./wsrouter')).WSRouter());
  await ws.connect();

  console.time('[Decky:Boot] Waiting for SteamApp init stage 1 to finish...');

  while (!isSteamAppInitialized()) {
    await new Promise((r) => setTimeout(r, 0)); // Can't use DFL sleep here.
  }

  console.timeEnd('[Decky:Boot] Waiting for SteamApp init stage 1 to finish...');

  // Runs before window.DFL exists, so this must not import @decky/ui.
  const { IS_ESM_CLIENT } = await import('./stable/client');
  console.debug(`[Decky:Boot] Steam client: ${IS_ESM_CLIENT ? 'ES modules' : 'Webpack (Stable compatibility)'}`);

  if (!IS_ESM_CLIENT) {
    await (await import('./stable/boot')).bootStable();
  } else {
    console.debug('[Decky:Boot] Setting up Module System & React globals...');

    let moduleId = 0;
    const int = (window._deckyModuleResolverInternals = {
      scopes: new Map<number, unknown>(),
      scopedObjects: new Map<number, unknown>(),
    });
    const resolveThroughBackend = ws.callable<[objectKey: number]>('instrumentation/resolve_scope');
    async function resolveModuleScope(objectFromScope: object, moduleForDebug?: string) {
      let ret: unknown;
      const id = ++moduleId;
      try {
        int.scopedObjects.set(id, objectFromScope);
        await resolveThroughBackend(id);
        ret = int.scopes.get(id);
      } catch (e) {
        console.debug(`[Decky:Instrumentation] Scope resolution failed for module ${moduleForDebug}`, e);
      } finally {
        int.scopes.delete(id);
        int.scopedObjects.delete(id);
      }
      return ret;
    }

    // deliberate partial import
    const DFLModules = await import('@decky/ui-v5/dist/module-system');
    await DFLModules.initModules(resolveModuleScope);
    window.DFLModules = DFLModules;
    window.SP_REACT = DFLModules.findModule((m) => m.Component && m.PureComponent && m.useLayoutEffect);
    window.SP_REACTDOM =
      DFLModules.findModule((m) => m.createPortal && m.createRoot) ||
      DFLModules.findModule((m) => m.createPortal && m.__DOM_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE);

    console.debug('[Decky:Boot] Setting up JSX internals...');
    const jsxModule = DFLModules.findModule((m) => (m.jsx && m.jsxs) || (m.jsx && Object.keys(m).length == 1));
    if (jsxModule?.jsxs) {
      window.SP_JSX = jsxModule;
    } else {
      window.SP_JSX = {
        jsx: jsxModule.jsx,
        jsxs: jsxModule.jsx,
        Fragment: window.SP_REACT.Fragment,
      };
    }
    console.debug('[Decky:Boot] Setting up @decky/ui...');
    window.DFL = await import('@decky/ui-v5');
  }

  // Fork: plugins built against 4.15.1 read these from window.DFL, but neither 4.16.4 nor 5.x exports
  // them from the package root. The namespace is sealed with live bindings, so the aliases go on an
  // object that inherits from it.
  const { EResult, EUIComposition } = await import('@decky/ui-v5/dist/globals/steam-client/shared');
  const { QuickAccessTab } = window.DFL as { QuickAccessTab: { Soundtrack: number } };
  const compatAliases = Object.getOwnPropertyDescriptors({
    EResult,
    EUIComposition,
    // 4.16.4 renamed the soundtrack tab; 4.15.1 plugins still use Music.
    QuickAccessTab: { ...QuickAccessTab, Music: QuickAccessTab.Soundtrack },
  });
  window.DFL = Object.create(window.DFL, compatAliases) as unknown as typeof window.DFL;

  console.debug('[Decky:Boot] Starting Decky!');
  await import('./start');
})();
