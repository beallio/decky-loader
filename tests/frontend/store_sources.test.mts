// Run with: node --test 'tests/frontend/*.test.mts'
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  EXTENDED_STORE,
  EXTENDED_TESTING_STORE,
  OFFICIAL_STORE,
  OFFICIAL_TESTING_STORE,
  loadFromEachStore,
  mergePluginLists,
  migrateStoreSettings,
  providerVersion,
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
