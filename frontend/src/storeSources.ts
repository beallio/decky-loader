// Fork: several plugin stores in precedence order. Stable stores come first, then the
// pre-release stores when they are switched on. Keep this file free of enums and value
// imports so that `node --test` can load it directly (tests/frontend).
import type { StorePlugin, StorePluginVersion, UpdateEndpointVersions } from './store';

export const OFFICIAL_STORE = 'https://plugins.deckbrew.xyz/plugins';
export const OFFICIAL_TESTING_STORE = 'https://testing.deckbrew.xyz/plugins';
export const EXTENDED_STORE = 'https://decky-extended-plugins.beallio.com/plugins.json';
export const EXTENDED_TESTING_STORE = 'https://decky-extended-plugins.beallio.com/testing_plugins.json';

const KNOWN_STORES: Record<string, string> = {
  [OFFICIAL_STORE]: 'Official',
  [OFFICIAL_TESTING_STORE]: 'Official testing',
  [EXTENDED_STORE]: 'Decky Extended',
  [EXTENDED_TESTING_STORE]: 'Decky Extended testing',
};

export interface StoreSettings {
  stores: string[];
  prereleaseStores: string[];
  includePrereleases: boolean;
}

export interface StoreSource {
  url: string;
  prerelease: boolean;
}

/** One store's answer; `value` is null when the store could not be read. */
export interface SourceResult<T> {
  source: StoreSource;
  value: T | null;
}

export interface MergedStorePlugin extends StorePlugin {
  /** The store that provides this plugin: the highest store that lists it. */
  store: string;
  /** A higher store did not answer, so it may normally provide this plugin. */
  shadowedByFailedStore: boolean;
}

export function storeLabel(url: string): string {
  if (KNOWN_STORES[url]) return KNOWN_STORES[url];
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
}

export function storeSources(settings: StoreSettings): StoreSource[] {
  return [
    ...settings.stores.map((url) => ({ url, prerelease: false })),
    ...(settings.includePrereleases ? settings.prereleaseStores.map((url) => ({ url, prerelease: true })) : []),
  ];
}

/** Settings for a user who still has the single-store setting (0 official, 1 testing, 2 custom). */
export function migrateStoreSettings(oldStore: unknown, oldURL: unknown): StoreSettings {
  const settings: StoreSettings = {
    stores: [EXTENDED_STORE, OFFICIAL_STORE],
    prereleaseStores: [OFFICIAL_TESTING_STORE, EXTENDED_TESTING_STORE],
    includePrereleases: false,
  };
  const store = Number(oldStore);
  const url = typeof oldURL === 'string' ? oldURL.trim() : '';
  if (store === 1) settings.includePrereleases = true;
  if (store === 2 && url) {
    if (settings.prereleaseStores.includes(url)) settings.includePrereleases = true;
    else if (!settings.stores.includes(url)) settings.stores.unshift(url);
  }
  return settings;
}

/**
 * The highest store that lists a plugin provides it (name, picture, description). Versions
 * that only lower stores have are added after the provider's versions, marked with their store.
 */
export function mergePluginLists(results: SourceResult<StorePlugin[]>[]): {
  plugins: MergedStorePlugin[];
  failed: string[];
} {
  const byName = new Map<string, MergedStorePlugin>();
  const failed: string[] = [];
  for (const { source, value } of results) {
    if (!value) {
      failed.push(source.url);
      continue;
    }
    for (const plugin of value) {
      const versions: StorePluginVersion[] = (plugin.versions ?? []).map((version) => ({
        ...version,
        store: source.url,
        prerelease: source.prerelease,
      }));
      const provider = byName.get(plugin.name);
      if (!provider) {
        byName.set(plugin.name, { ...plugin, versions, store: source.url, shadowedByFailedStore: failed.length > 0 });
        continue;
      }
      for (const version of versions) {
        if (!provider.versions.some((known) => known.name === version.name)) provider.versions.push(version);
      }
    }
  }
  return { plugins: [...byName.values()], failed };
}

/**
 * The version a plugin should update to: the latest version of the highest store that lists it.
 * Returns null when a higher store did not answer, because that store might list the plugin.
 */
export function providerVersion(
  results: SourceResult<UpdateEndpointVersions>[],
  name: string,
): StorePluginVersion | null {
  for (const { source, value } of results) {
    if (!value) return null;
    if (!Object.prototype.hasOwnProperty.call(value, name)) continue;
    const [version, hash, artifact] = value[name];
    return { name: version, hash, artifact, store: source.url, prerelease: source.prerelease };
  }
  return null;
}

/** Sort the merged list the way the official store sorts its own list ('date' is the creation date). */
export function sortStorePlugins<T extends StorePlugin>(
  plugins: T[],
  by: string | null,
  direction: string | null,
): T[] {
  if (!by) return plugins;
  const sign = direction === 'desc' ? -1 : 1;
  const number = (plugin: T) =>
    by === 'date' ? Date.parse(plugin.created ?? '') || 0 : by === 'downloads' ? plugin.downloads ?? 0 : 0;
  return [...plugins].sort(
    (a, b) =>
      sign * (by === 'name' ? a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }) : number(a) - number(b)),
  );
}
