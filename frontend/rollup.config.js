import commonjs from '@rollup/plugin-commonjs';
import image from '@rollup/plugin-image';
import json from '@rollup/plugin-json';
import { nodeResolve } from '@rollup/plugin-node-resolve';
import replace from '@rollup/plugin-replace';
import typescript from '@rollup/plugin-typescript';
import { defineConfig } from 'rollup';
import del from 'rollup-plugin-delete';
import externalGlobals from 'rollup-plugin-external-globals';
import { visualizer } from 'rollup-plugin-visualizer';

const hiddenWarnings = ['THIS_IS_UNDEFINED', 'EVAL'];

// Fork: Vite/Rolldown SteamUI builds inline ~85% of CSS module property accesses and omit Webpack
// module boundaries. Keep every upstream @decky/ui Webpack predicate first and fall back to a
// Vite-safe finder only when the Webpack predicate returns undefined.
function viteCompatDeckyUi() {
  return {
    name: 'vite-compat-decky-ui',
    transform(code, id) {
      const norm = id.replace(/\\/g, '/');
      if (!norm.includes('/@decky/ui/dist/')) return null;

      if (norm.endsWith('/@decky/ui/dist/webpack.js')) {
        return code.replace(
          'export const IconsModule = findModuleByExport(',
          'export const IconsModule = findModule((m) => m?.Spinner && m?.SteamAchievements && m?.Speaker) || findModuleByExport(',
        );
      }
      if (norm.endsWith('/@decky/ui/dist/components/Panel.js')) {
        return `import { findModuleDetailsByExport, findModuleExport } from '../webpack';
const [mod, panelSection] = findModuleDetailsByExport((e) => e.toString?.()?.includes('.PanelSection'));
export const PanelSection = panelSection || findModuleExport((e) => typeof e === 'function' && /const\\{title:/.test(e.toString()) && e.toString().includes('e.spinner&&'));
export const PanelSectionRow = (mod && Object.values(mod).filter((exp) => !exp?.toString?.()?.includes('.PanelSection'))[0]) || (({ children, ...rest }) => window.SP_REACT.createElement('div', rest, children));`;
      }
      if (norm.endsWith('/@decky/ui/dist/components/Field.js')) {
        return (
          code +
          `\n// Vite fallback when ().Field is inlined\n`
        ).replace(
          "e?.render?.toString()?.includes('\"shift-children-below\"'));",
          "e?.render?.toString()?.includes('\"shift-children-below\"') || (typeof e === 'function' && e.toString?.()?.includes('\"shift-children-below\"') && e.toString?.()?.includes('spacingBetweenLabelAndChild')));",
        );
      }
      if (norm.endsWith('/@decky/ui/dist/components/ControlsList.js')) {
        return code.replace(
          "e.toString?.().includes('().ControlsListOuterPanel'));",
          "e.toString?.().includes('().ControlsListOuterPanel')) || findModuleExport((e) => typeof e === 'function' && e.toString().includes('itemMaxSize') && e.toString().includes('e.alignItems') && e.toString().includes('\"gamepad\"'));",
        );
      }
      if (norm.endsWith('/@decky/ui/dist/components/Dialog.js')) {
        return code.replace(
          'return str?.includes("role:\\"heading\\"") && str.includes(")(\\"DialogHeader\\",");',
          'return str?.includes("role:\\"heading\\"") && (str.includes(")(\\"DialogHeader\\",") || (str.includes("\\"DialogHeader\\"") && str.includes("setHeaderId")));',
        );
      }
      if (norm.endsWith('/@decky/ui/dist/components/Marquee.js')) {
        return code.replace(
          "e.toString?.().includes('--fade-length'));",
          "e.toString?.().includes('--fade-length')) || findModuleExport((e) => typeof e === 'function' && e.toString().includes('\"--fade-length-left\"') && e.toString().includes('\"--fade-length-right\"'));",
        );
      }
      if (norm.endsWith('/@decky/ui/dist/components/Modal.js')) {
        return code.replace(
          "return string?.includes('.ShowPortalModal()') && string?.includes('.OnElementReadyCallbacks.Register(');\n});",
          "return string?.includes('.ShowPortalModal()') && string?.includes('.OnElementReadyCallbacks.Register(');\n}) || findModuleExport((e) => typeof e === 'function' && e.toString().includes('.ShowPortalModal()') && e.toString().includes('.OnElementReadyCallbacks.Register('));",
        );
      }
      if (norm.endsWith('/@decky/ui/dist/components/ProgressBar.js')) {
        return code
          .replace(
            'export const ProgressBar = findModuleExport((e) => e?.toString && /\\.ProgressBar,(?:\\w*==)?"standard"(?:==)?/.test(e.toString?.()));',
            'export const ProgressBar = findModuleExport((e) => e?.toString && /\\.ProgressBar,(?:\\w*==)?"standard"(?:==)?/.test(e.toString?.())) || findModuleExport((e) => typeof e === "function" && e.toString().includes(\'role:"progressbar"\') && e.toString().includes(\'"#ProgressBar_ValueUnknown"\'));',
          )
          .replace(
            "export const ProgressBarWithInfo = findModuleExport((e) => e?.toString?.()?.includes('.ProgressBarFieldStatus,children') || e?.toString?.()?.includes('.ProgressBarFieldStatus},'));",
            "export const ProgressBarWithInfo = findModuleExport((e) => e?.toString?.()?.includes('.ProgressBarFieldStatus,children') || e?.toString?.()?.includes('.ProgressBarFieldStatus},')) || findModuleExport((e) => typeof e === 'function' && e.toString().includes('rtEstimatedCompletionTime:') && e.toString().includes('childrenContainerWidth:'));",
          );
      }
      if (norm.endsWith('/@decky/ui/dist/components/Spinner.js')) {
        return code
          .replace(
            'export const Spinner = IconsModule && Object.values(IconsModule)?.find(',
            'export const Spinner = IconsModule?.Spinner || (IconsModule && Object.values(IconsModule)?.find(',
          )
          .replace(/;\s*$/, ');');
      }
      if (norm.endsWith('/@decky/ui/dist/components/Tabs.js')) {
        return code.replace(
          'export const Tabs = tabsModule && Object.values(tabsModule).find((e) => e?.type?.toString?.()?.includes("(function()"));',
          'export const Tabs = (tabsModule && Object.values(tabsModule).find((e) => e?.type?.toString?.()?.includes("(function()"))) || findModuleExport((e) => typeof e === "function" && e.toString().includes(\'role:"tablist"\') && e.toString().includes(\'"aria-orientation":"horizontal"\'));',
        ).replace(
          "import { findModuleByExport } from '../webpack';",
          "import { findModuleByExport, findModuleExport } from '../webpack';",
        );
      }
      if (norm.endsWith('/@decky/ui/dist/utils/static-classes.js')) {
        return code.replace(/findClassModule\(([^;\n]+)\);/g, '(findClassModule($1) || {});');
      }
      return null;
    },
  };
}

export default defineConfig([
  // Main bundle
  {
    input: 'src/index.ts',
    plugins: [
      viteCompatDeckyUi(),
      del({ targets: ['../backend/decky_loader/static/*', '!../backend/decky_loader/static/fallback.js'], force: true }),
      commonjs(),
      nodeResolve({
        browser: true,
      }),
      externalGlobals({
        react: 'SP_REACT',
        'react/jsx-runtime': 'SP_JSX',
        'react-dom': 'SP_REACTDOM',
        // hack to shut up react-markdown
        process: '{cwd: () => {}}',
        path: '{dirname: () => {}, join: () => {}, basename: () => {}, extname: () => {}}',
        url: '{fileURLToPath: (f) => f}',
      }),
      typescript(),
      json(),
      replace({
        preventAssignment: false,
        'process.env.NODE_ENV': JSON.stringify('production'),
      }),
      image(),
      visualizer(),
    ],
    preserveEntrySignatures: false,
    treeshake: {
      // Assume all external modules have imports with side effects (the default) while allowing decky libraries to treeshake
      pureExternalImports: true,
      preset: 'smallest'
    },
    output: {
      dir: '../backend/decky_loader/static',
      format: 'esm',
      chunkFileNames: (chunkInfo) => {
        return 'chunk-[hash].js';
      },
      sourcemap: true,
      sourcemapPathTransform: (relativeSourcePath) => relativeSourcePath.replace(/^\.\.\//, `decky://decky/loader/`),
    },
    onwarn: function (message, handleWarning) {
      if (hiddenWarnings.some((warning) => message.code === warning)) return;
      handleWarning(message);
    },
  },
  // Fallback
  {
    input: 'src/fallback.ts',
    plugins: [
      typescript()
    ],
    output: {
      file: '../backend/decky_loader/static/fallback.js',
      format: 'esm',
    }
  }
]);
