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

// Loader code reads @decky/ui from window.DFL, like plugins do. The boot imports the real
// libraries through @decky/ui-v5 and @decky/ui-legacy, which this plugin never maps.
const dflGlobal = () => ({
  name: 'dfl-global',
  resolveId: (source) => (source === '@decky/ui' ? { id: '\0dfl-global', syntheticNamedExports: true } : null),
  load: (id) => (id === '\0dfl-global' ? 'export default window.DFL;' : null),
  generateBundle(_, bundle) {
    // These run before window.DFL is set, so neither they nor their static imports may read it.
    const bootModules = [
      'src/index.ts',
      'src/bootGuard.ts',
      'src/wsrouter.ts',
      'src/wsUrl.ts',
      'src/logger.ts',
      'src/stable/boot.ts',
    ];
    const chunks = Object.values(bundle).filter((output) => output.type === 'chunk');
    const chunkByName = new Map(chunks.map((chunk) => [chunk.fileName, chunk]));
    const pending = chunks.filter((chunk) =>
      Object.keys(chunk.modules).some((id) => bootModules.some((suffix) => id.replace(/\\/g, '/').endsWith(suffix))),
    );
    const reached = new Set();
    while (pending.length) {
      const chunk = pending.pop();
      if (reached.has(chunk)) continue;
      reached.add(chunk);
      for (const name of chunk.imports) {
        const imported = chunkByName.get(name);
        if (imported) pending.push(imported);
      }
    }
    for (const chunk of reached) {
      const dflModule = Object.keys(chunk.modules).find((id) => id.includes('dfl-global'));
      if (dflModule) {
        this.error(`${chunk.fileName} runs before window.DFL is set but statically imports ${dflModule}`);
      }
    }
  },
});

// CEF injects `index.js?v=<version>`. Any chunk importing `./index.js` (without the query string)
// evaluates the boot entry a second time as a distinct ES module, creating a second WSRouter.
const noEntryReimport = () => ({
  name: 'no-entry-reimport',
  generateBundle(_, bundle) {
    const chunks = Object.values(bundle).filter((output) => output.type === 'chunk');
    const entries = new Set(chunks.filter((chunk) => chunk.isEntry).map((chunk) => chunk.fileName));
    for (const chunk of chunks) {
      if (chunk.isEntry && chunk.exports.length > 0) {
        this.error(
          `Entry chunk ${chunk.fileName} must not export bindings (${chunk.exports.join(', ')}); chunks importing it would re-run boot under a different URL than index.js?v=<version>`,
        );
      }
      for (const imported of [...chunk.imports, ...chunk.dynamicImports]) {
        if (entries.has(imported)) {
          this.error(
            `Chunk ${chunk.fileName} imports entry chunk ${imported}; this would re-run boot under a different URL than index.js?v=<version>`,
          );
        }
      }
    }
  },
});

export default defineConfig([
  // Main bundle
  {
    input: 'src/index.ts',
    plugins: [
      dflGlobal(),
      noEntryReimport(),
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
