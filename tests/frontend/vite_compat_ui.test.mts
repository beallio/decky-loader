// Run with: node --test 'tests/frontend/*.test.mts'
import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const REPO = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const ROLLUP_CONFIG = join(REPO, 'frontend/rollup.config.js');
const DECKY_UI_DIST = join(REPO, 'frontend/node_modules/@decky/ui/dist');
const FRONTEND_SRC = join(REPO, 'frontend/src');

function extractViteCompatPlugin(): { transform: (code: string, id: string) => string | { code: string } | null } {
  const src = readFileSync(ROLLUP_CONFIG, 'utf8');
  const start = src.indexOf('function viteCompatDeckyUi()');
  assert.notEqual(start, -1, 'viteCompatDeckyUi() must exist in frontend/rollup.config.js');
  const endMarker = src.indexOf('\nexport default ', start);
  assert.notEqual(endMarker, -1, 'Could not find export default after viteCompatDeckyUi()');
  const fnSrc = src.slice(start, endMarker).trim();
  const plugin = new Function(`${fnSrc}\nreturn viteCompatDeckyUi();`)();
  assert.equal(typeof plugin?.transform, 'function');
  return plugin;
}

function applyTransform(plugin: ReturnType<typeof extractViteCompatPlugin>, relPath: string): string {
  const raw = readFileSync(join(DECKY_UI_DIST, relPath), 'utf8');
  const out = plugin.transform(raw, `/@decky/ui/dist/${relPath}`);
  if (typeof out === 'string') return out;
  if (out && typeof out === 'object' && typeof out.code === 'string') return out.code;
  return raw;
}

function evalTransformedModule(
  code: string,
  mocks: {
    findClassModule?: (filter: (m: any) => any) => any;
    findModuleExport?: (filter: (e: any) => any) => any;
    findModuleDetailsByExport?: (filter: (e: any) => any) => any[];
    findModuleByExport?: (filter: (e: any) => any) => any;
  },
): Record<string, any> {
  // Rewrite ESM imports/exports to plain JS function body for isolated unit evaluation.
  const exportsObj: Record<string, any> = {};
  const body = code
    .replace(/^import\s+[^;]+;\s*$/gm, '')
    .replace(/export\s+const\s+(\w+)\s*=/g, 'const $1 =')
    .concat(
      '\nreturn { ' +
        [...code.matchAll(/export\s+const\s+(\w+)\s*=/g)].map((m) => m[1]).join(', ') +
        ' };',
    );
  const fn = new Function(
    'findClassModule',
    'findModuleExport',
    '__deckyFindExport',
    'findModuleDetailsByExport',
    'findModuleByExport',
    body,
  );
  const findExport = mocks.findModuleExport ?? (() => undefined);
  return Object.assign(
    exportsObj,
    fn(
      mocks.findClassModule ?? (() => undefined),
      findExport,
      findExport,
      mocks.findModuleDetailsByExport ?? (() => [undefined, undefined]),
      mocks.findModuleByExport ?? (() => undefined),
    ),
  );
}

function walkFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walkFiles(full));
    else if (/\.(ts|tsx)$/.test(entry.name)) out.push(full);
  }
  return out;
}

test('PanelSection fallback matches regardless of minified parameter name', () => {
  const plugin = extractViteCompatPlugin();
  const code = applyTransform(plugin, 'components/Panel.js');

  // Parameter named `t` instead of `e` (Oxc can rename props parameter across builds).
  const fakePanelSection = new Function(
    't',
    'const{title:a,children:r}=t;return t.spinner&&a?r:null;',
  );
  const exports = evalTransformedModule(code, {
    findModuleDetailsByExport: () => [undefined, undefined],
    findModuleExport: (pred) => (pred(fakePanelSection) ? fakePanelSection : undefined),
  });
  assert.equal(exports.PanelSection, fakePanelSection);
});

test('ControlsList fallback matches regardless of minified parameter name', () => {
  const plugin = extractViteCompatPlugin();
  const code = applyTransform(plugin, 'components/ControlsList.js');

  const fakeControlsList = new Function(
    't',
    'return { itemMaxSize: t.itemMaxSize, align: t.alignItems, mode: "gamepad" };',
  );
  const exports = evalTransformedModule(code, {
    findModuleExport: (pred) => (pred(fakeControlsList) ? fakeControlsList : undefined),
  });
  assert.equal(exports.ControlsList, fakeControlsList);
});

test('Tabs fallback matches Steam Tabs and rejects RadioGroup (Shared_Radio_Group)', () => {
  const plugin = extractViteCompatPlugin();
  const code = applyTransform(plugin, 'components/Tabs.js');

  const fakeRadioGroup = new Function(
    'e',
    'return { role:"tablist","aria-orientation":"horizontal",className:"Shared_Radio_Group" };',
  );
  const fakeTabs = new Function(
    'e',
    'const{activeTab:t,tabs:n,canBeHeaderBackground:r,cancelSkipTabHeader:i}=e;return{canBeHeaderBackground:r,cancelSkipTabHeader:i};',
  );
  const candidates = [fakeRadioGroup, fakeTabs];
  const exports = evalTransformedModule(code, {
    findModuleByExport: () => undefined,
    findModuleExport: (pred) => candidates.find((c) => pred(c)),
  });
  assert.equal(exports.Tabs, fakeTabs);
});

test('static-classes transform recovers QAM Title and PanelSection classes on Vite and survives throwing probes', () => {
  const plugin = extractViteCompatPlugin();
  const code = applyTransform(plugin, 'utils/static-classes.js');

  const fakeTitleComp = new Function(
    't',
    'const loc=(x)=>x;return {props:{className:"ldDpFgRYF7Q-",children:loc(t.locId)}};',
  );
  const fakePanelSection = new Function(
    't',
    'const{title:a,children:r}=t;return { props: { className: "cGtmpwuGgRg-", children: [{ props: { className: "_9wgVBRkm6zw-" } }, t.spinner && r] } };',
  );
  const candidates = [fakeTitleComp, fakePanelSection];

  const viteExports = evalTransformedModule(code, {
    findClassModule: () => undefined,
    findModuleExport: (pred) => candidates.find((c) => pred(c)),
  });
  assert.equal(viteExports.staticClasses.Title, 'ldDpFgRYF7Q-');
  assert.equal(viteExports.quickAccessMenuClasses.Title, 'ldDpFgRYF7Q-');
  assert.equal(viteExports.quickAccessControlsClasses.PanelSection, 'cGtmpwuGgRg-');
  assert.equal(viteExports.quickAccessControlsClasses.PanelSectionTitle, '_9wgVBRkm6zw-');

  // Webpack classes must take precedence untouched when present.
  const webpackTitle = { Title: 'WebpackTitle', QuickAccessMenu: 'WebpackQAM' };
  const webpackControls = { BatteryPercentageLabel: 'B', PanelSection: 'WebpackPS', PanelSectionTitle: 'WebpackPST' };
  const webpackExports = evalTransformedModule(code, {
    findClassModule: (pred) => [webpackTitle, webpackControls].find((m) => pred(m)),
    findModuleExport: () => {
      throw new Error('Should not probe when Webpack classes are already present');
    },
  });
  assert.equal(webpackExports.staticClasses.Title, 'WebpackTitle');
  assert.equal(webpackExports.quickAccessControlsClasses.PanelSection, 'WebpackPS');

  // Throwing probes (e.g., React invalid hook call) must never crash module evaluation.
  const throwingTitle = new Function('t', 'const children=t.locId; throw new Error("Invalid hook call");');
  const safeExports = evalTransformedModule(code, {
    findClassModule: () => undefined,
    findModuleExport: (pred) => (pred(throwingTitle) ? throwingTitle : undefined),
  });
  assert.deepEqual(safeExports.staticClasses, {});
});

test('frontend UI hygiene invariants hold (no Webpack numeric IDs, WarnThirdParty removed, valid CSS units/colors)', () => {
  assert.equal(existsSync(join(FRONTEND_SRC, 'components/modals/WarnThirdParty.tsx')), false);
  assert.equal(existsSync(join(FRONTEND_SRC, 'utils/globalTypes.ts')), false);

  const files = walkFiles(FRONTEND_SRC);
  for (const file of files) {
    const content = readFileSync(file, 'utf8');
    assert.equal(
      /findClass\(\s*['"]\d+['"]/.test(content),
      false,
      `Hardcoded Webpack numeric module ID in ${file}`,
    );
    assert.equal(
      content.includes('WarnThirdParty'),
      false,
      `Leftover WarnThirdParty reference in ${file}`,
    );
    assert.equal(
      content.includes('decky.xyz/testing'),
      false,
      `Legacy decky.xyz/testing link in ${file}`,
    );
    assert.equal(
      /style=\{\{\s*width:\s*['"]\d+['"]/.test(content),
      false,
      `Unitless numeric CSS string dimension in ${file}`,
    );
    if (file.includes('/components/')) {
      assert.equal(
        /\b(?:color|background):\s*['"](?:red|orange)['"]/.test(content),
        false,
        `Raw CSS color name (red/orange) in ${file}; use UI_COLORS tokens`,
      );
    }
  }

  const disableModal = readFileSync(join(FRONTEND_SRC, 'components/modals/PluginDisableModal.tsx'), 'utf8');
  assert.match(disableModal, /try\s*\{[\s\S]*disablePlugin\(name\)[\s\S]*\}\s*catch/);

  const updater = readFileSync(join(FRONTEND_SRC, 'components/settings/pages/general/Updater.tsx'), 'utf8');
  assert.match(
    updater,
    /gamepadDialogClasses\.FieldLeftColumn\s*\?/,
    'Updater.tsx must guard gamepadDialogClasses.FieldLeftColumn instead of injecting .undefined on Vite',
  );
});

test('Install Plugin from URL uses a modal triggered by Enter URL and only closes on successful install', () => {
  const locales = JSON.parse(readFileSync(join(REPO, 'backend/decky_loader/locales/en-US.json'), 'utf8'));
  assert.equal(locales.SettingsDeveloperIndex?.third_party_plugins?.button_url, 'Enter URL');

  const developerPage = readFileSync(
    join(FRONTEND_SRC, 'components/settings/pages/developer/index.tsx'),
    'utf8',
  );
  assert.match(developerPage, /SettingsDeveloperIndex\.third_party_plugins\.button_url/);
  assert.match(developerPage, /showModal\s*\(\s*<\w+/);
  assert.match(developerPage, /bCloseAfterOK:\s*false/);
  assert.match(developerPage, /await\s+installFromURL\(/);

  const storeSrc = readFileSync(join(FRONTEND_SRC, 'store.tsx'), 'utf8');
  assert.match(storeSrc, /consumeNextInstallPrompt/);
  assert.match(storeSrc, /utilities\/confirm_plugin_install/);
});
