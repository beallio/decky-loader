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
  /** GitHub repository of the newest version (by date, from any store) whose download is on GitHub. */
  repoUrl: string | null;
  /** Every store whose list has this plugin, highest first (the store filter uses it). */
  stores: string[];
}

export function storeLabel(url: string): string {
  if (KNOWN_STORES[url]) return KNOWN_STORES[url];
  const repo = githubRepo(url);
  if (repo) return repo;
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
}

/** "owner/repo" when a store URL is a GitHub repository page (https://github.com/<owner>/<repo>), else null. */
export function githubRepo(url: string): string | null {
  const match = /^https:\/\/github\.com\/([A-Za-z0-9-]+)\/([\w.-]+?)(?:\.git)?\/?$/.exec(url);
  return match ? `${match[1]}/${match[2]}` : null;
}

interface GithubRelease {
  tag_name?: unknown;
  draft?: unknown;
  prerelease?: unknown;
  published_at?: unknown;
  assets?: { name?: unknown; digest?: unknown; browser_download_url?: unknown; download_count?: unknown }[];
}

/**
 * The store list of a GitHub repository: name, author, tags, description and picture from its
 * plugin.json, one version per published release that has exactly one .zip, with a SHA-256 digest. GitHub
 * pre-releases and drafts are left out. No installable release gives an empty list.
 */
export function githubRepoPlugins(pluginJson: unknown, releases: unknown): StorePlugin[] {
  const info = pluginJson as {
    name?: unknown;
    author?: unknown;
    flags?: unknown;
    publish?: { tags?: unknown; description?: unknown; image?: unknown };
  } | null;
  if (!info || typeof info.name !== 'string' || !info.name) throw new Error('plugin.json has no plugin name');
  if (!Array.isArray(releases)) throw new Error('GitHub did not return a release list');
  const versions: StorePluginVersion[] = [];
  let downloads = 0;
  for (const release of releases as GithubRelease[]) {
    if (release.draft !== false || release.prerelease !== false || typeof release.tag_name !== 'string') continue;
    // Only a release with exactly one .zip is used: with several, the plugin download is not known.
    const zips = (release.assets ?? []).filter(
      (asset) => typeof asset.name === 'string' && asset.name.toLowerCase().endsWith('.zip'),
    );
    const asset = zips.length === 1 ? zips[0] : null;
    if (
      !asset ||
      typeof asset.digest !== 'string' ||
      !/^sha256:[0-9a-f]{64}$/.test(asset.digest) ||
      typeof asset.browser_download_url !== 'string' ||
      !asset.browser_download_url.startsWith('https://')
    )
      continue;
    versions.push({
      name: release.tag_name.replace(/^v(?=\d)/, ''),
      hash: (asset.digest as string).slice('sha256:'.length),
      artifact: asset.browser_download_url as string,
      created: typeof release.published_at === 'string' ? release.published_at : undefined,
    });
    if (typeof asset.download_count === 'number') downloads += asset.download_count;
  }
  if (!versions.length) return [];
  versions.sort((a, b) => (Date.parse(b.created ?? '') || 0) - (Date.parse(a.created ?? '') || 0));
  const publish = info.publish ?? {};
  const tags = Array.isArray(publish.tags) ? publish.tags.filter((tag): tag is string => typeof tag === 'string') : [];
  // The store marks root plugins with a 'root' tag; a repository has the flag in plugin.json instead.
  if (Array.isArray(info.flags) && info.flags.includes('root') && !tags.includes('root')) tags.push('root');
  return [
    {
      id: 0,
      name: info.name,
      author: typeof info.author === 'string' ? info.author : '',
      description: typeof publish.description === 'string' ? publish.description : '',
      tags,
      image_url: typeof publish.image === 'string' ? publish.image : '',
      created: versions[versions.length - 1].created,
      updated: versions[0].created,
      downloads,
      versions,
    },
  ];
}

/** The repository page of a GitHub artifact URL (https://github.com/<owner>/<repo>/...), or null. */
export function githubRepoUrl(artifact: string | null | undefined): string | null {
  try {
    const url = new URL(artifact ?? '');
    const [owner, repo] = url.pathname.split('/').filter(Boolean);
    return url.protocol === 'https:' && url.hostname === 'github.com' && owner && repo
      ? `https://github.com/${owner}/${repo}`
      : null;
  } catch {
    return null;
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
 * Ask every store at once. A store that fails or does not finish (headers and body) within
 * `timeoutMs` counts as failed, so one stalled store cannot hold back the others.
 */
export async function loadFromEachStore<T>(
  sources: StoreSource[],
  load: (url: string, signal: AbortSignal) => Promise<T>,
  timeoutMs: number,
): Promise<SourceResult<T>[]> {
  return Promise.all(
    sources.map(async (source) => {
      const signal = AbortSignal.timeout(timeoutMs);
      // The race also ends a load that ignores the signal.
      const deadline = new Promise<never>((_, reject) =>
        signal.addEventListener('abort', () => reject(signal.reason), { once: true }),
      );
      try {
        return { source, value: await Promise.race([load(source.url, signal), deadline]) };
      } catch (e) {
        console.warn(`Store ${source.url} is unavailable`, e);
        return { source, value: null };
      }
    }),
  );
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
  const repos = new Map<string, { url: string; time: number }>();
  const failed: string[] = [];
  for (const { source, value } of results) {
    if (!value) {
      failed.push(source.url);
      continue;
    }
    for (const plugin of value) {
      // Before duplicate versions are dropped: a lower store may hold the only GitHub download of a version.
      for (const version of plugin.versions ?? []) {
        const url = githubRepoUrl(version.artifact);
        const time = Date.parse(version.created ?? '') || 0;
        const best = repos.get(plugin.name);
        if (url && (!best || time > best.time)) repos.set(plugin.name, { url, time });
      }
      const versions: StorePluginVersion[] = (plugin.versions ?? []).map((version) => ({
        ...version,
        store: source.url,
        prerelease: source.prerelease,
      }));
      const provider = byName.get(plugin.name);
      if (!provider) {
        byName.set(plugin.name, {
          ...plugin,
          versions,
          store: source.url,
          shadowedByFailedStore: failed.length > 0,
          repoUrl: null,
          stores: [source.url],
        });
        continue;
      }
      if (!provider.stores.includes(source.url)) provider.stores.push(source.url);
      for (const version of versions) {
        if (!provider.versions.some((known) => known.name === version.name)) provider.versions.push(version);
      }
    }
  }
  for (const [name, plugin] of byName) plugin.repoUrl = repos.get(name)?.url ?? null;
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

/** Sort the merged list ('date' is the creation date, 'updated' the date of the last update). */
export function sortStorePlugins<T extends StorePlugin>(
  plugins: T[],
  by: string | null,
  direction: string | null,
): T[] {
  if (!by) return plugins;
  const sign = direction === 'desc' ? -1 : 1;
  const number = (plugin: T) =>
    by === 'date'
      ? Date.parse(plugin.created ?? '') || 0
      : by === 'updated'
        ? Date.parse(plugin.updated ?? '') || 0
        : by === 'downloads'
          ? plugin.downloads ?? 0
          : 0;
  return [...plugins].sort(
    (a, b) =>
      sign * (by === 'name' ? a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }) : number(a) - number(b)),
  );
}

/** How long a saved store list is used before the store is asked again. */
export const STORE_CACHE_MS = 6 * 60 * 60 * 1000;

/** One store's saved plugin list. */
export interface CachedList {
  fetchedAt: number;
  plugins: StorePlugin[];
}

export interface ListCache {
  get(url: string): CachedList | null;
  set(url: string, list: CachedList): void;
}

/**
 * Each store's list: the saved list while it is younger than `ttlMs` (unless `force`), else the
 * store's answer, which is then saved. A store that does not answer uses its saved list of any
 * age and is named in `stale`. `fetchedAt` is the age of the oldest current list in use.
 */
export async function loadWithCache(
  sources: StoreSource[],
  load: (url: string, signal: AbortSignal) => Promise<StorePlugin[]>,
  timeoutMs: number,
  cache: ListCache,
  now: number,
  force: boolean,
  ttlMs = STORE_CACHE_MS,
): Promise<{
  results: SourceResult<StorePlugin[]>[];
  stale: { url: string; fetchedAt: number }[];
  fetchedAt: number | null;
}> {
  const saved = new Map(sources.map((source) => [source.url, cache.get(source.url)]));
  // A list from the future (the clock went back) counts as expired.
  const current = (url: string) => {
    const list = saved.get(url);
    return !force && list && now >= list.fetchedAt && now - list.fetchedAt < ttlMs ? list : null;
  };
  const answers = await loadFromEachStore(
    sources.filter((source) => !current(source.url)),
    load,
    timeoutMs,
  );
  const answered = new Map(answers.map(({ source, value }) => [source.url, value]));
  const stale: { url: string; fetchedAt: number }[] = [];
  let fetchedAt: number | null = null;
  const results = sources.map((source) => {
    let list = current(source.url);
    if (!list) {
      const value = answered.get(source.url);
      if (!value) {
        const old = saved.get(source.url);
        if (old) stale.push({ url: source.url, fetchedAt: old.fetchedAt });
        return { source, value: old?.plugins ?? null };
      }
      list = { fetchedAt: now, plugins: value };
      cache.set(source.url, list);
    }
    fetchedAt = Math.min(fetchedAt ?? list.fetchedAt, list.fetchedAt);
    return { source, value: list.plugins };
  });
  return { results, stale, fetchedAt };
}

/** Tags that differ only in case, spaces or hyphens, or a plural -s/-ies have the same key ("Utilities" = "utility"). */
export function tagKey(tag: string): string {
  const key = tag.toLowerCase().replace(/[^a-z0-9]+/g, '');
  if (key.length > 4 && key.endsWith('ies')) return key.slice(0, -3) + 'y';
  if (key.length > 3 && /[^su]s$/.test(key)) return key.slice(0, -1);
  return key;
}

/**
 * The tag filter's choices: tag groups used by at least `minPlugins` plugins, most used first,
 * named by their most used spelling. 'root' is left out; it has its own filter.
 */
export function tagGroups(plugins: StorePlugin[], minPlugins = 2): { key: string; label: string; count: number }[] {
  const groups = new Map<string, { spellings: Map<string, number>; plugins: Set<string> }>();
  for (const plugin of plugins) {
    for (const tag of plugin.tags) {
      const key = tagKey(tag);
      if (!key || key === 'root') continue;
      const group = groups.get(key) ?? { spellings: new Map(), plugins: new Set() };
      const spelling = tag.trim().toLowerCase();
      group.spellings.set(spelling, (group.spellings.get(spelling) ?? 0) + 1);
      group.plugins.add(plugin.name);
      groups.set(key, group);
    }
  }
  return [...groups]
    .filter(([, group]) => group.plugins.size >= minPlugins)
    .map(([key, group]) => ({
      key,
      label: [...group.spellings].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0][0],
      count: group.plugins.size,
    }))
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

export interface StoreFilters {
  store: string;
  tag: string;
  installed: '' | 'yes' | 'no';
  root: '' | 'hide' | 'only';
  github: '' | 'yes';
}

export const NO_STORE_FILTERS: StoreFilters = { store: '', tag: '', installed: '', root: '', github: '' };

/** The plugins that match the search text (name, author, description or tag) and every filter. */
export function filterStorePlugins(
  plugins: MergedStorePlugin[],
  search: string,
  filters: StoreFilters,
  installed: Set<string>,
): MergedStorePlugin[] {
  const text = search.trim().toLowerCase();
  return plugins.filter(
    (plugin) =>
      (!text ||
        [plugin.name, plugin.author, plugin.description ?? '', ...plugin.tags].some((value) =>
          value.toLowerCase().includes(text),
        )) &&
      (!filters.store || plugin.stores.includes(filters.store)) &&
      (!filters.tag || plugin.tags.some((tag) => tagKey(tag) === filters.tag)) &&
      (!filters.installed || installed.has(plugin.name) === (filters.installed === 'yes')) &&
      (!filters.root || plugin.tags.includes('root') === (filters.root === 'only')) &&
      (!filters.github || plugin.repoUrl !== null),
  );
}

/** Saved sort and filters from an earlier visit; anything unknown or malformed becomes the default. */
export function restoreStoreView(saved: unknown, sorts: string[]): { sort: string; filters: StoreFilters } {
  const value = saved && typeof saved === 'object' ? (saved as Record<string, unknown>) : {};
  const pick = <T extends string>(item: unknown, allowed: readonly T[]): T =>
    allowed.includes(item as T) ? (item as T) : allowed[0];
  return {
    sort: pick(value.sort, sorts),
    filters: {
      store: typeof value.store === 'string' ? value.store : '',
      tag: typeof value.tag === 'string' ? value.tag : '',
      installed: pick(value.installed, ['', 'yes', 'no'] as const),
      root: pick(value.root, ['', 'hide', 'only'] as const),
      github: pick(value.github, ['', 'yes'] as const),
    },
  };
}
