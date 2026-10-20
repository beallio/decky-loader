// Run with: node --test 'tests/frontend/*.test.mts'
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  EXTENDED_STORE,
  EXTENDED_TESTING_STORE,
  NO_STORE_FILTERS,
  OFFICIAL_STORE,
  OFFICIAL_TESTING_STORE,
  STORE_CACHE_MS,
  filterStorePlugins,
  githubRepo,
  githubRepoPlugins,
  githubRepoUrl,
  loadFromEachStore,
  loadWithCache,
  mergePluginLists,
  migrateStoreSettings,
  providerVersion,
  restoreStoreView,
  tagGroups,
  withoutPrereleases,
} from '../../frontend/src/storeSources.ts';

const official = { url: OFFICIAL_STORE, prerelease: false };
const testing = { url: OFFICIAL_TESTING_STORE, prerelease: true };

const plugin = (name: string, ...versions: string[]) => ({
  id: 1,
  name,
  author: 'a',
  description: '',
  tags: [],
  image_url: '',
  versions: versions.map((version) => ({ name: version, hash: `${name}-${version}`, artifact: null })),
});

const digest = (char: string) => `sha256:${char.repeat(64)}`;
const ghRelease = (tag: string, published: string, assets: object[], extra: object = {}) => ({
  tag_name: tag,
  draft: false,
  prerelease: false,
  published_at: published,
  assets,
  ...extra,
});
const ghZip = (char: string, downloads = 1) => ({
  name: 'Plugin.zip',
  digest: digest(char),
  browser_download_url: `https://github.com/o/r/releases/download/${char}/Plugin.zip`,
  download_count: downloads,
});

test('only a GitHub repository page is a repository store', () => {
  assert.equal(githubRepo('https://github.com/owner/repo'), 'owner/repo');
  assert.equal(githubRepo('https://github.com/owner/my.repo.git/'), 'owner/my.repo');
  assert.equal(githubRepo('https://github.com/owner/repo/releases'), null);
  assert.equal(githubRepo('http://github.com/owner/repo'), null);
  assert.equal(githubRepo(OFFICIAL_STORE), null);
});

test('a repository store lists published releases that have a .zip with a SHA-256 digest', () => {
  const info = { name: 'Plugin', author: 'me', flags: ['root'], publish: { tags: ['tools'], description: 'd', image: 'i' } };
  const releases = [
    ghRelease('v2.0.0-pre', '2026-03-01T00:00:00Z', [ghZip('c')], { prerelease: true }),
    ghRelease('v1.2.0', '2026-02-01T00:00:00Z', [ghZip('b', 5)]),
    ghRelease('v1.1.0', '2026-01-15T00:00:00Z', [{ ...ghZip('x'), digest: null }]),
    ghRelease('v1.0.5', '2026-01-10T00:00:00Z', [ghZip('d')], { draft: true }),
    ghRelease('1.0.0', '2026-01-01T00:00:00Z', [{ name: 'notes.txt' }, ghZip('a', 2)]),
  ];
  const [plugin] = githubRepoPlugins(info, releases);
  assert.deepEqual(
    plugin.versions.map(({ name, hash, prerelease }) => [name, hash[0], prerelease]),
    [
      ['2.0.0-pre', 'c', true],
      ['1.2.0', 'b', false],
      ['1.0.0', 'a', false],
    ],
  );
  assert.deepEqual(plugin.tags, ['tools', 'root']);
  assert.equal(plugin.downloads, 8);
  assert.equal(plugin.created, '2026-01-01T00:00:00Z');
  assert.equal(plugin.updated, '2026-03-01T00:00:00Z');
  assert.equal(githubRepoUrl(plugin.versions[0].artifact), 'https://github.com/o/r');

  // Without "Include pre-releases": the pre-release goes, and the update date follows the newest kept version.
  const [stable] = withoutPrereleases([plugin]);
  assert.deepEqual(
    stable.versions.map(({ name }) => name),
    ['1.2.0', '1.0.0'],
  );
  assert.equal(stable.updated, '2026-02-01T00:00:00Z');
  const onlyPre = githubRepoPlugins(info, [releases[0]]);
  assert.equal(onlyPre.length, 1);
  assert.deepEqual(withoutPrereleases(onlyPre), []);

  // With it: the merged list keeps the mark, so the version list labels it as a pre-release.
  const repo = { url: 'https://github.com/o/r', prerelease: false };
  const merged = mergePluginLists([{ source: repo, value: [plugin] }]).plugins[0];
  assert.deepEqual(
    merged.versions.map(({ prerelease }) => prerelease),
    [true, false, false],
  );
});

test('a release with several .zip files is left out, in any asset order', () => {
  const debug = { ...ghZip('e'), name: 'Plugin-debug.zip' };
  for (const assets of [
    [ghZip('a'), debug],
    [debug, ghZip('a')],
  ]) {
    assert.deepEqual(githubRepoPlugins({ name: 'Plugin' }, [ghRelease('v1.0.0', '2026-01-01T00:00:00Z', assets)]), []);
  }
});

test('a repository store without an installable release lists nothing, and a bad plugin.json fails', () => {
  assert.deepEqual(githubRepoPlugins({ name: 'Plugin' }, [ghRelease('v1', '2026-01-01T00:00:00Z', [])]), []);
  assert.throws(() => githubRepoPlugins({ author: 'me' }, []));
  assert.throws(() => githubRepoPlugins({ name: 'Plugin' }, { message: 'API rate limit exceeded' }));
});

test('a store that did not answer holds back updates from the stores below it', () => {
  const results = [
    { source: official, value: null },
    { source: testing, value: { 'CSS Loader': ['2.2.0-pr.12', 'h'] as [string, string] } },
  ];
  assert.equal(providerVersion(results, 'CSS Loader'), null);
});

test('a stalled store times out without holding back the other stores or its lower-store updates', async () => {
  const started = Date.now();
  const results = await loadFromEachStore(
    [official, testing, { url: EXTENDED_TESTING_STORE, prerelease: true }],
    // Official never answers and ignores the abort signal; the others answer at once.
    (url) =>
      url === OFFICIAL_STORE
        ? new Promise<never>(() => {})
        : Promise.resolve({ 'CSS Loader': ['2.2.0-pr.12', 'h'] as [string, string] }),
    50,
  );
  assert.ok(Date.now() - started < 2000);
  assert.deepEqual(
    results.map((r) => r.value !== null),
    [false, true, true],
  );
  assert.equal(providerVersion(results, 'CSS Loader'), null);
});

test('the highest store wins even when a lower store has a newer version', () => {
  const results = [
    { source: official, value: { 'CSS Loader': ['2.1.2', 'old'] as [string, string] } },
    { source: testing, value: { 'CSS Loader': ['2.2.0-pr.12', 'new'] as [string, string] } },
  ];
  assert.deepEqual(providerVersion(results, 'CSS Loader'), {
    name: '2.1.2',
    hash: 'old',
    artifact: undefined,
    store: OFFICIAL_STORE,
    prerelease: false,
  });
});

test('a plugin missing from an answering higher store updates from the lower store', () => {
  const results = [
    { source: official, value: {} },
    { source: testing, value: { Only: ['1.0.0', 'h'] as [string, string] } },
  ];
  assert.equal(providerVersion(results, 'Only')?.store, OFFICIAL_TESTING_STORE);
  assert.equal(providerVersion(results, 'constructor'), null);
});

test('the merged list keeps the provider and adds lower-store versions after its own', () => {
  const { plugins, failed } = mergePluginLists([
    { source: official, value: [plugin('CSS Loader', '2.1.2', '2.1.1')] },
    { source: testing, value: [plugin('CSS Loader', '2.2.0-pr.12', '2.1.2'), plugin('Only', '0.1.0')] },
  ]);
  assert.deepEqual(failed, []);
  const css = plugins.find((p) => p.name === 'CSS Loader')!;
  assert.equal(css.store, OFFICIAL_STORE);
  assert.deepEqual(
    css.versions.map((v) => [v.name, v.store, v.prerelease]),
    [
      ['2.1.2', OFFICIAL_STORE, false],
      ['2.1.1', OFFICIAL_STORE, false],
      ['2.2.0-pr.12', OFFICIAL_TESTING_STORE, true],
    ],
  );
  assert.equal(plugins.find((p) => p.name === 'Only')!.store, OFFICIAL_TESTING_STORE);
});

const release = (name: string, created: string, artifact: string | null) => ({ name, hash: name, created, artifact });
const pluginWith = (name: string, ...versions: ReturnType<typeof release>[]) => ({ ...plugin(name), versions });
const zip = (repo: string) => `https://github.com/${repo}/releases/download/v1/p.zip`;

test('the GitHub link comes from the newest GitHub download in any store', () => {
  const { plugins } = mergePluginLists([
    { source: official, value: [pluginWith('P', release('1.0.0', '2026-01-01T00:00:00Z', zip('old/p')))] },
    { source: testing, value: [pluginWith('P', release('1.1.0', '2026-03-01T00:00:00Z', zip('new/p')))] },
  ]);
  assert.equal(plugins[0].store, OFFICIAL_STORE);
  assert.equal(plugins[0].repoUrl, 'https://github.com/new/p');
});

test('a GitHub download on a lower-store copy of a provider version still gives a link', () => {
  const { plugins } = mergePluginLists([
    { source: official, value: [pluginWith('P', release('1.0.0', '2026-01-01T00:00:00Z', null))] },
    { source: testing, value: [pluginWith('P', release('1.0.0', '2026-01-01T00:00:00Z', zip('o/p')))] },
  ]);
  assert.deepEqual(
    plugins[0].versions.map((v) => [v.name, v.store, v.artifact]),
    [['1.0.0', OFFICIAL_STORE, null]],
  );
  assert.equal(plugins[0].repoUrl, 'https://github.com/o/p');
});

test('only https github.com downloads with an owner and repository give a link', () => {
  for (const bad of [null, '', 'not a url', 'http://github.com/o/p/x.zip', 'https://evil.example/github.com/o/p', 'https://github.com/o']) {
    assert.equal(githubRepoUrl(bad), null, String(bad));
  }
  assert.equal(githubRepoUrl(zip('o/p')), 'https://github.com/o/p');
});

test('plugins below a failed store are marked, plugins above it are not', () => {
  const { plugins, failed } = mergePluginLists([
    { source: { url: EXTENDED_STORE, prerelease: false }, value: [plugin('Top', '1.0.0')] },
    { source: official, value: null },
    { source: testing, value: [plugin('Below', '1.0.0')] },
  ]);
  assert.deepEqual(failed, [OFFICIAL_STORE]);
  assert.equal(plugins.find((p) => p.name === 'Top')!.shadowedByFailedStore, false);
  assert.equal(plugins.find((p) => p.name === 'Below')!.shadowedByFailedStore, true);
});

test('old single-store settings keep their store', () => {
  const defaults = [EXTENDED_STORE, OFFICIAL_STORE];
  assert.deepEqual(migrateStoreSettings(0, null), {
    stores: defaults,
    prereleaseStores: [OFFICIAL_TESTING_STORE, EXTENDED_TESTING_STORE],
    includePrereleases: false,
  });
  assert.equal(migrateStoreSettings('1', null).includePrereleases, true);
  assert.deepEqual(migrateStoreSettings(2, ' https://example.com/plugins ').stores, [
    'https://example.com/plugins',
    ...defaults,
  ]);
  assert.deepEqual(migrateStoreSettings(2, EXTENDED_STORE).stores, defaults);
  const testingCustom = migrateStoreSettings(2, EXTENDED_TESTING_STORE);
  assert.deepEqual([testingCustom.stores, testingCustom.includePrereleases], [defaults, true]);
  assert.deepEqual(migrateStoreSettings(1, 'https://example.com/plugins').stores, defaults);
});

const memoryCache = (entries: [string, { fetchedAt: number; plugins: ReturnType<typeof plugin>[] }][]) => {
  const saved = new Map(entries);
  return { saved, get: (url: string) => saved.get(url) ?? null, set: (url: string, list: any) => void saved.set(url, list) };
};

test('saved lists are used for 6 hours, then the store is asked again and the answer is saved', async () => {
  const now = 10 * STORE_CACHE_MS;
  const cache = memoryCache([
    [OFFICIAL_STORE, { fetchedAt: now - STORE_CACHE_MS + 1, plugins: [plugin('Saved', '1.0.0')] }],
    [OFFICIAL_TESTING_STORE, { fetchedAt: now - STORE_CACHE_MS, plugins: [plugin('Old', '1.0.0')] }],
  ]);
  const asked: string[] = [];
  const load = async (url: string) => (asked.push(url), [plugin('New', '2.0.0')]);
  const { results, stale, fetchedAt } = await loadWithCache([official, testing], load, 1000, cache, now, false);
  assert.deepEqual(asked, [OFFICIAL_TESTING_STORE]);
  assert.deepEqual(
    results.map((r) => r.value![0].name),
    ['Saved', 'New'],
  );
  assert.equal(cache.saved.get(OFFICIAL_TESTING_STORE)!.fetchedAt, now);
  assert.deepEqual([stale, fetchedAt], [[], now - STORE_CACHE_MS + 1]);
});

test('Refresh asks every store even when its saved list is current', async () => {
  const cache = memoryCache([[OFFICIAL_STORE, { fetchedAt: 100, plugins: [plugin('Saved', '1.0.0')] }]]);
  const { results } = await loadWithCache([official], async () => [plugin('New', '2.0.0')], 1000, cache, 200, true);
  assert.equal(results[0].value![0].name, 'New');
});

test('a store that does not answer uses its saved list of any age and is named as stale', async () => {
  const cache = memoryCache([[OFFICIAL_STORE, { fetchedAt: 1, plugins: [plugin('Saved', '1.0.0')] }]]);
  const fail = async () => {
    throw new Error('offline');
  };
  const { results, stale, fetchedAt } = await loadWithCache(
    [official, testing],
    fail,
    1000,
    cache,
    10 * STORE_CACHE_MS,
    false,
  );
  assert.deepEqual(
    results.map((r) => r.value?.[0].name ?? null),
    ['Saved', null],
  );
  assert.deepEqual(stale, [{ url: OFFICIAL_STORE, fetchedAt: 1 }]);
  assert.equal(fetchedAt, null);
  // The saved list is not a failure, so plugins below it are not marked.
  assert.deepEqual(mergePluginLists(results).failed, [OFFICIAL_TESTING_STORE]);
});

test('a saved list from the future (clock went back) is not trusted', async () => {
  const cache = memoryCache([[OFFICIAL_STORE, { fetchedAt: 5000, plugins: [plugin('Saved', '1.0.0')] }]]);
  const { results } = await loadWithCache([official], async () => [plugin('New', '2.0.0')], 1000, cache, 1000, false);
  assert.equal(results[0].value![0].name, 'New');
});

const tagged = (name: string, ...tags: string[]) => ({ ...plugin(name, '1.0.0'), tags });

test('tag spellings that differ in case, spaces, hyphens or plural form are one group', () => {
  const groups = tagGroups([
    tagged('A', 'utility', 'Steam Deck', 'root'),
    tagged('B', 'utility', 'steam-deck', 'root'),
    tagged('C', 'Utilities', 'Screenshots'),
    tagged('D', 'screenshot', 'glass'),
    tagged('E', 'chaos', 'Steam Deck'),
  ]);
  // Equal counts list by name; 'glass' and 'chaos' have one plugin each, 'root' has its own filter.
  assert.deepEqual(groups, [
    { key: 'steamdeck', label: 'steam deck', count: 3 },
    { key: 'utility', label: 'utility', count: 3 },
    { key: 'screenshot', label: 'screenshot', count: 2 },
  ]);
});

test('filters combine, and a store matches every plugin in its list, even one with only the same versions', () => {
  const { plugins } = mergePluginLists([
    { source: official, value: [tagged('Root', 'root', 'Tools'), tagged('Plain', 'tool'), tagged('Both', 'misc')] },
    { source: testing, value: [plugin('Plain', '1.0.0'), plugin('Both', '2.0.0'), plugin('Lower', '1.0.0')] },
  ]);
  const names = (filters: Partial<typeof NO_STORE_FILTERS>, search = '', installed = new Set<string>()) =>
    filterStorePlugins(plugins, search, { ...NO_STORE_FILTERS, ...filters }, installed).map((p) => p.name);
  assert.deepEqual(names({ store: OFFICIAL_TESTING_STORE }), ['Plain', 'Both', 'Lower']);
  assert.deepEqual(names({ store: OFFICIAL_STORE }), ['Root', 'Plain', 'Both']);
  assert.deepEqual(names({ tag: 'tool' }), ['Root', 'Plain']);
  assert.deepEqual(names({ tag: 'tool', root: 'hide' }), ['Plain']);
  assert.deepEqual(names({ root: 'only' }), ['Root']);
  assert.deepEqual(names({ installed: 'yes' }, '', new Set(['Plain'])), ['Plain']);
  assert.deepEqual(names({ installed: 'no' }, 'p', new Set(['Plain'])), []);
  assert.deepEqual(names({ github: 'yes' }), []);
});

test('a saved store view keeps only known values', () => {
  const sorts = ['name:asc', 'updated:desc'];
  const saved = { sort: 'updated:desc', store: 'x', tag: 'y', installed: 'no', root: 'only', github: 'yes' };
  assert.deepEqual(restoreStoreView(saved, sorts), {
    sort: 'updated:desc',
    filters: { store: 'x', tag: 'y', installed: 'no', root: 'only', github: 'yes' },
  });
  assert.deepEqual(restoreStoreView({ sort: 'bad', store: 3, installed: 'maybe', root: null }, sorts), {
    sort: 'name:asc',
    filters: NO_STORE_FILTERS,
  });
  assert.deepEqual(restoreStoreView('garbage', sorts).filters, NO_STORE_FILTERS);
});
