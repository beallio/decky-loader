import { compare, validate } from 'compare-versions';

import { DisabledPlugin, InstallType, Plugin, installPlugin, installPlugins } from './plugin';
import { getSetting, setSetting } from './utils/settings';

export enum Store {
  Default,
  Testing,
  Custom,
}

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
}

export interface PluginInstallRequest {
  plugin: string;
  selectedVer: StorePluginVersion;
  installType: InstallType;
}

// name: version
export type PluginUpdateMapping = Map<string, StorePluginVersion>;

export async function getStore(): Promise<Store> {
  return await getSetting<Store>('store', Store.Default);
}

export async function getStoreBaseURL(): Promise<string> {
  let store = await getSetting<Store | null>('store', null);
  let customURL = await getSetting<string>('store-url', 'https://plugins.deckbrew.xyz/plugins');
  let storeURL;
  if (store === null) {
    console.log('Could not get store, using Default.');
    await setSetting('store', Store.Default);
    store = Store.Default;
  }
  switch (+store) {
    case Store.Default:
      storeURL = 'https://plugins.deckbrew.xyz/plugins';
      break;
    case Store.Testing:
      storeURL = 'https://testing.deckbrew.xyz/plugins';
      break;
    case Store.Custom:
      storeURL = customURL;
      break;
    default:
      console.error('Somehow you ended up without a standard URL, using the default URL.');
      storeURL = 'https://plugins.deckbrew.xyz/plugins';
      break;
  }
  return storeURL;
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

export async function getPluginList(
  sort_by: SortOptions | null = null,
  sort_direction: SortDirections | null = null,
): Promise<StorePlugin[]> {
  let query: URLSearchParams | string = new URLSearchParams();
  sort_by && query.set('sort_by', sort_by);
  sort_direction && query.set('sort_direction', sort_direction);
  query = '?' + String(query);

  const storeURL = await getStoreBaseURL();

  return storeFetch(storeURL + query).then((r) => r.json());
}

export async function getVersionList(): Promise<UpdateEndpointVersions> {
  const storeURL = await getStoreBaseURL();

  try {
    const response = await storeFetch(storeURL + '/versions');
    if (response.ok) {
      const versions = await response.json();
      if (versions && typeof versions === 'object' && !Array.isArray(versions)) return versions;
    }
  } catch (e) {
    console.debug('Store version list unavailable, using the plugin list', e);
  }
  // Fork: a store that cannot answer <store URL>/versions (for example a single
  // plugins.json file) still gets update checks, from its full plugin list.
  return versionsFromPluginList(await getPluginList());
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
  await installPlugin(artifactUrl, plugin, selectedVer.name, selectedVer.hash, installType);
}

export async function requestMultiplePluginInstalls(requests: PluginInstallRequest[]) {
  await installPlugins(
    requests.map(({ plugin, installType, selectedVer }) => ({
      name: plugin,
      artifact: selectedVer.artifact ?? pluginUrl(selectedVer.hash),
      version: selectedVer.name,
      hash: selectedVer.hash,
      install_type: installType,
    })),
  );
}

export async function checkForPluginUpdates(plugins: (Plugin | DisabledPlugin)[]): Promise<PluginUpdateMapping> {
  const serverData = await getVersionList();
  const updateMap = new Map<string, StorePluginVersion>();
  for (let plugin of plugins) {
    const remotePlugin = serverData?.[plugin.name];
    //FIXME: Ugly hack since plugin.version might be null during evaluation,
    //so this will set the older version possible
    const curVer = plugin.version ? plugin.version : '0.0.0';

    if (
      remotePlugin &&
      plugin.version != remotePlugin[0] &&
      validate(remotePlugin[0]) &&
      validate(curVer) &&
      compare(remotePlugin[0], curVer, '>')
    ) {
      updateMap.set(plugin.name, {
        name: remotePlugin[0],
        hash: remotePlugin[1],
        artifact: remotePlugin[2],
      });
    }
  }
  return updateMap;
}

function pluginUrl(hash: string) {
  return `https://cdn.tzatzikiweeb.moe/file/steam-deck-homebrew/versions/${hash}.zip`;
}
