import { compare, validate } from 'compare-versions';

import { DisabledPlugin, InstallType, Plugin, installPlugin, installPlugins } from './plugin';
import {
  ListCache,
  MergedStorePlugin,
  SourceResult,
  StoreSettings,
  githubRepo,
  githubRepoPlugins,
  loadFromEachStore,
  loadWithCache,
  mergePluginLists,
  migrateStoreSettings,
  providerVersion,
  storeSources,
} from './storeSources';
import { getSetting, setSetting } from './utils/settings';

export enum SortOptions {
  name = 'name',
  date = 'date',
  updated = 'updated',
  downloads = 'downloads',
}

export enum SortDirections {
  ascending = 'asc',
  descending = 'desc',
}

export interface StorePluginVersion {
  name: string;
  hash: string;
  artifact: string | undefined | null;
  // Fork: the store that lists this version, and whether it is a pre-release store.
  store?: string;
  prerelease?: boolean;
  created?: string;
}

export type UpdateEndpointVersions = { [name: string]: [version: string, hash: string, artifact?: string] };

export interface StorePlugin {
  id: number;
  name: string;
  versions: StorePluginVersion[];
  author: string;
  description: string;
  tags: string[];
  image_url: string;
  created?: string;
  updated?: string;
  downloads?: number;
}

export interface PluginInstallRequest {
  plugin: string;
  selectedVer: StorePluginVersion;
  installType: InstallType;
}

// name: version
export type PluginUpdateMapping = Map<string, StorePluginVersion>;

// Fork: an ordered list of stores replaces the single 'store' / 'store-url' choice. Those old
// settings stay untouched, so going back to official Decky keeps the user's old store.
export async function getStoreSettings(): Promise<StoreSettings> {
  const stores = await getSetting<string[] | null>('store_urls', null);
  if (Array.isArray(stores)) {
    return {
      stores,
      prereleaseStores: await getSetting<string[]>('prerelease_store_urls', []),
      includePrereleases: await getSetting<boolean>('include_prereleases', false),
    };
  }
  const settings = migrateStoreSettings(await getSetting('store', null), await getSetting('store-url', null));
  await saveStoreSettings(settings);
  return settings;
}

export async function saveStoreSettings(settings: StoreSettings) {
  // store_urls last: it marks the migration as done.
  await setSetting('prerelease_store_urls', settings.prereleaseStores);
  await setSetting('include_prereleases', settings.includePrereleases);
  await setSetting('store_urls', settings.stores);
}

// Fork: store data goes through the backend's authenticated /fetch proxy. A browser fetch
// with X-Decky-Version needs a CORS preflight, which static hosts such as GitHub Pages refuse.
async function storeFetch(url: string, signal: AbortSignal): Promise<Response> {
  const version = await window.DeckyPluginLoader.updateVersion();
  return window.DeckyPluginLoader.fetchNoCors(url, {
    method: 'GET',
    headers: {
      'X-Decky-Version': version.current,
    },
    excludedHeaders: [],
    signal,
  });
}

// Fork: the most one store may take per list (headers and body); a slower store counts as failed.
const STORE_TIMEOUT_MS = 20000;

async function fromEachStore<T>(load: (url: string, signal: AbortSignal) => Promise<T>): Promise<SourceResult<T>[]> {
  return loadFromEachStore(storeSources(await getStoreSettings()), load, STORE_TIMEOUT_MS);
}

async function fetchPluginList(storeURL: string, signal: AbortSignal): Promise<StorePlugin[]> {
  const repo = githubRepo(storeURL);
  if (repo) {
    // Fork: a GitHub repository as a store: plugin.json from its default branch, versions from its releases.
    const [info, releases] = await Promise.all([
      fetchJSON(`https://raw.githubusercontent.com/${repo}/HEAD/plugin.json`, signal),
      fetchJSON(`https://api.github.com/repos/${repo}/releases?per_page=100`, signal),
    ]);
    return githubRepoPlugins(info, releases);
  }
  const plugins = await fetchJSON(storeURL, signal);
  if (!Array.isArray(plugins)) throw new Error('the store did not return a plugin list');
  return plugins;
}

async function fetchJSON(url: string, signal: AbortSignal): Promise<unknown> {
  const response = await storeFetch(url, signal);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

// Fork: each store's plugin list is saved in localStorage for 6 hours (update checks stay live).
const CACHE_PREFIX = 'decky-store-cache:';

const savedLists: ListCache = {
  get(url) {
    try {
      const list = JSON.parse(localStorage.getItem(CACHE_PREFIX + url) ?? 'null');
      return list && typeof list.fetchedAt === 'number' && Array.isArray(list.plugins) ? list : null;
    } catch {
      return null;
    }
  },
  set(url, list) {
    try {
      localStorage.setItem(CACHE_PREFIX + url, JSON.stringify(list));
    } catch (e) {
      console.warn(`Could not save the plugin list of ${url}`, e);
    }
  },
};

/** The merged plugin list of all stores. `force` asks every store again instead of using saved lists. */
export async function getPluginList(force = false): Promise<{
  plugins: MergedStorePlugin[];
  failed: string[];
  stale: { url: string; fetchedAt: number }[];
  fetchedAt: number | null;
}> {
  const sources = storeSources(await getStoreSettings());
  // Forget lists of stores that were removed from the settings. Storage errors only cost the clean-up.
  try {
    for (const key of Object.keys(localStorage)) {
      if (key.startsWith(CACHE_PREFIX) && !sources.some((source) => CACHE_PREFIX + source.url === key)) {
        localStorage.removeItem(key);
      }
    }
  } catch (e) {
    console.warn('Could not clean the saved plugin lists', e);
  }
  const { results, stale, fetchedAt } = await loadWithCache(
    sources,
    fetchPluginList,
    STORE_TIMEOUT_MS,
    savedLists,
    Date.now(),
    force,
  );
  return { ...mergePluginLists(results), stale, fetchedAt };
}

async function fetchVersionList(storeURL: string, signal: AbortSignal): Promise<UpdateEndpointVersions> {
  // A GitHub repository has no /versions; its plugin list is small.
  if (!githubRepo(storeURL)) {
    try {
      const response = await storeFetch(storeURL + '/versions', signal);
      if (response.ok) {
        const versions = await response.json();
        if (versions && typeof versions === 'object' && !Array.isArray(versions)) return versions;
      }
    } catch (e) {
      console.debug(`Store version list of ${storeURL} unavailable, using the plugin list`, e);
    }
  }
  // Fork: a store that cannot answer <store URL>/versions (for example a single
  // plugins.json file) still gets update checks, from its full plugin list.
  return versionsFromPluginList(await fetchPluginList(storeURL, signal));
}

function versionsFromPluginList(plugins: StorePlugin[]): UpdateEndpointVersions {
  const versions: UpdateEndpointVersions = {};
  for (const plugin of plugins) {
    const latest = plugin.versions?.[0];
    if (!latest || plugin.name in versions) continue;
    versions[plugin.name] = latest.artifact ? [latest.name, latest.hash, latest.artifact] : [latest.name, latest.hash];
  }
  return versions;
}

export async function installFromURL(url: string) {
  const splitURL = url.split('/');
  await installPlugin(url, splitURL[splitURL.length - 1].replace('.zip', ''));
}

export async function requestPluginInstall(plugin: string, selectedVer: StorePluginVersion, installType: InstallType) {
  const artifactUrl = selectedVer.artifact ?? pluginUrl(selectedVer.hash);
  await installPlugin(
    artifactUrl,
    plugin,
    selectedVer.name,
    selectedVer.hash,
    installType,
    countingStore(selectedVer.store),
  );
}

export async function requestMultiplePluginInstalls(requests: PluginInstallRequest[]) {
  await installPlugins(
    requests.map(({ plugin, installType, selectedVer }) => ({
      name: plugin,
      artifact: selectedVer.artifact ?? pluginUrl(selectedVer.hash),
      version: selectedVer.name,
      hash: selectedVer.hash,
      install_type: installType,
      store_url: countingStore(selectedVer.store),
    })),
  );
}

// Fork: a GitHub repository has no install counter.
function countingStore(store: string | undefined): string | undefined {
  return store && !githubRepo(store) ? store : undefined;
}

export async function checkForPluginUpdates(plugins: (Plugin | DisabledPlugin)[]): Promise<PluginUpdateMapping> {
  const serverData = await fromEachStore(fetchVersionList);
  const updateMap = new Map<string, StorePluginVersion>();
  for (let plugin of plugins) {
    // Fork: only the store that provides the plugin offers updates for it.
    const remotePlugin = providerVersion(serverData, plugin.name);
    //FIXME: Ugly hack since plugin.version might be null during evaluation,
    //so this will set the older version possible
    const curVer = plugin.version ? plugin.version : '0.0.0';

    if (
      remotePlugin &&
      plugin.version != remotePlugin.name &&
      validate(remotePlugin.name) &&
      validate(curVer) &&
      compare(remotePlugin.name, curVer, '>')
    ) {
      updateMap.set(plugin.name, remotePlugin);
    }
  }
  return updateMap;
}

function pluginUrl(hash: string) {
  return `https://cdn.tzatzikiweeb.moe/file/steam-deck-homebrew/versions/${hash}.zip`;
}
