// Run with: node --test 'tests/frontend/*.test.mts'
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { claimFrontendBoot } from '../../frontend/src/bootGuard.ts';
import { buildWsUrl, createClientId } from '../../frontend/src/wsUrl.ts';

test('claimFrontendBoot succeeds once per context and blocks duplicate boots', () => {
  const host: { __deckyFrontendBooted?: boolean } = {};
  assert.equal(claimFrontendBoot(host), true);
  assert.equal(host.__deckyFrontendBooted, true);
  assert.equal(claimFrontendBoot(host), false);

  // A fresh SharedJSContext window starts clean and can boot once.
  const freshHost: { __deckyFrontendBooted?: boolean } = {};
  assert.equal(claimFrontendBoot(freshHost), true);
});

test('createClientId generates distinct non-empty client identifiers', () => {
  const first = createClientId();
  const second = createClientId();
  assert.ok(first.length > 0);
  assert.ok(second.length > 0);
  assert.notEqual(first, second);
});

test('buildWsUrl encodes auth token and stable client_id query parameter', () => {
  const url = buildWsUrl('tok+1/2', 'client-abc');
  const parsed = new URL(url);
  assert.equal(parsed.origin, 'ws://127.0.0.1:1337');
  assert.equal(parsed.pathname, '/ws');
  assert.equal(parsed.searchParams.get('auth'), 'tok+1/2');
  assert.equal(parsed.searchParams.get('client_id'), 'client-abc');
});
