import { compare, validate } from 'compare-versions';

import { DisabledPlugin, InstallType, Plugin, installPlugin, installPlugins } from './plugin';
import {
  MergedStorePlugin,
  SourceResult,
  StoreSettings,
  mergePluginLists,
  migrateStoreSettings,
  providerVersion,
  sortStorePlugins,
  storeSources,
} from './storeSources';
import { getSetting, setSetting } from './utils/settings';

export enum SortOptions {
  name = 'name',
  date = 'date',
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
async function storeFetch(url: string): Promise<Response> {
  const version = await window.DeckyPluginLoader.updateVersion();
  return window.DeckyPluginLoader.fetchNoCors(url, {
    method: 'GET',
    headers: {
      'X-Decky-Version': version.current,
    },
    excludedHeaders: [],
  });
}

async function fromEachStore<T>(load: (url: string) => Promise<T>): Promise<SourceResult<T>[]> {
  const sources = storeSources(await getStoreSettings());
  return Promise.all(
    sources.map(async (source) => {
      try {
        return { source, value: await load(source.url) };
      } catch (e) {
        console.warn(`Store ${source.url} is unavailable`, e);
        return { source, value: null };
      }
    }),
  );
}

async function fetchPluginList(storeURL: string): Promise<StorePlugin[]> {
  const response = await storeFetch(storeURL);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const plugins = await response.json();
  if (!Array.isArray(plugins)) throw new Error('the store did not return a plugin list');
  return plugins;
}

export async function getPluginList(
  sort_by: SortOptions | null = null,
  sort_direction: SortDirections | null = null,
): Promise<{ plugins: MergedStorePlugin[]; failed: string[] }> {
  // Each store sorts only its own list, so the merged list is sorted here.
  const { plugins, failed } = mergePluginLists(await fromEachStore(fetchPluginList));
  return { plugins: sortStorePlugins(plugins, sort_by, sort_direction), failed };
}

async function fetchVersionList(storeURL: string): Promise<UpdateEndpointVersions> {
  try {
    const response = await storeFetch(storeURL + '/versions');
    if (response.ok) {
      const versions = await response.json();
      if (versions && typeof versions === 'object' && !Array.isArray(versions)) return versions;
    }
  } catch (e) {
    console.debug(`Store version list of ${storeURL} unavailable, using the plugin list`, e);
  }
  // Fork: a store that cannot answer <store URL>/versions (for example a single
  // plugins.json file) still gets update checks, from its full plugin list.
  return versionsFromPluginList(await fetchPluginList(storeURL));
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
  await installPlugin(artifactUrl, plugin, selectedVer.name, selectedVer.hash, installType, selectedVer.store);
}

export async function requestMultiplePluginInstalls(requests: PluginInstallRequest[]) {
  await installPlugins(
    requests.map(({ plugin, installType, selectedVer }) => ({
      name: plugin,
      artifact: selectedVer.artifact ?? pluginUrl(selectedVer.hash),
      version: selectedVer.name,
      hash: selectedVer.hash,
      install_type: installType,
      store_url: selectedVer.store,
    })),
  );
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
