// Stable: Steam Stable still ships Webpack. Delete src/stable/ once Steam Stable ships ES modules.

// The ES module build loads library.js as a module; Steam Stable loads it as a classic script.
export const IS_ESM_CLIENT: boolean = !!document.querySelector('script[type=module][src*="library.js"]');
