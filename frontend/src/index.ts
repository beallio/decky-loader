// Sets up DFL, then loads start.ts which starts up the loader

// Enums only, safe to load before Steam's React is found.
import { EResult, EUIComposition } from '@decky/ui/dist/globals/steam-client/shared';

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

  console.time('[Decky:Boot] Waiting for SteamApp init stage 1 to finish...');

  while (!isSteamAppInitialized()) {
    await new Promise((r) => setTimeout(r, 0)); // Can't use DFL sleep here.
  }

  console.timeEnd('[Decky:Boot] Waiting for SteamApp init stage 1 to finish...');

  if (!window.SP_REACT) {
    console.debug('[Decky:Boot] Setting up Webpack & React globals...');
    // deliberate partial import
    const DFLWebpack = await import('@decky/ui/dist/webpack');
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
  const DFL = await import('@decky/ui');
  // Fork: @decky/ui 4.16 dropped these from its root, and plugins built against 4.15 read them from window.DFL.
  // The namespace is frozen and has live bindings, so the aliases sit on an object that inherits from it.
  window.DFL = Object.create(
    DFL,
    Object.getOwnPropertyDescriptors({
      EResult,
      EUIComposition,
      QuickAccessTab: { ...DFL.QuickAccessTab, Music: DFL.QuickAccessTab.Soundtrack },
    }),
  );
  console.debug('[Decky:Boot] Authenticating with Decky backend...');
  window.deckyAuthToken = await fetch('http://127.0.0.1:1337/auth/token').then((r) => r.text());
  console.debug('[Decky:Boot] Connecting to Decky backend...');
  window.DeckyBackend = new (await import('./wsrouter')).WSRouter();
  await DeckyBackend.connect();
  console.debug('[Decky:Boot] Starting Decky!');
  await import('./start');
})();
