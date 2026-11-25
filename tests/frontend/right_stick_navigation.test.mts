// Run with: node --test 'tests/frontend/*.test.mts'
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  createRightStickController,
  findGamepadSourceBase,
  focusedWindow,
  type Direction,
  type DirectionPayload,
  type NavLike,
  type SourceLike,
  type SteamUILike,
  type SteamWindowLike,
} from '../../frontend/src/rightStickNavigationLogic.ts';

function makeWindow(overrides: Partial<SteamWindowLike> = {}): SteamWindowLike {
  return {
    IsGamepadUIWindow: () => true,
    IsMainGamepadUIWindow: () => true,
    IsGamepadUIOverlayWindow: () => false,
    LocationPathName: '/library/home',
    MenuStore: { IsSideMenuInteractable: () => false },
    GetShowingGlobalModal: () => false,
    BIsOverlayPath: () => false,
    CompositionStateStore: {
      GetCompositionState: () => 3,
      GetCurrentlyFocusedAppidSubscribableValue: () => ({ Value: 769 }),
    },
    ...overrides,
  };
}

class FakeBaseSource implements SourceLike {
  sourceType = -1;
  repeatCheck: (() => boolean) | null = null;
  downHandlers: Array<(...args: number[]) => void> = [];
  upHandlers: Array<(...args: number[]) => void> = [];

  SetSourceType(type: number) {
    this.sourceType = type;
  }
  SetRepeatAllowed(check: () => boolean) {
    this.repeatCheck = check;
  }
  OnButtonDown(button: number, index: number) {
    for (const fn of this.downHandlers) fn(button, index);
  }
  OnButtonUp(button: number, index: number) {
    for (const fn of this.upHandlers) fn(button, index);
  }
  RegisterForGamepadButtonDown(fn: (...args: number[]) => void) {
    this.downHandlers.push(fn);
  }
  RegisterForGamepadButtonUp(fn: (...args: number[]) => void) {
    this.upHandlers.push(fn);
  }
}

class FakePhysicalSource extends FakeBaseSource {
  HandleControllerInputMessages() {}
}

class FakeDeepPhysicalSource extends FakePhysicalSource {}

test('focusedWindow allows Steam UI in Gamescope and blocks in-game focus', () => {
  const home = makeWindow();
  assert.equal(focusedWindow({ ActiveWindowInstance: home, ActiveControllerIndex: 0 }, 'gamescope'), home);

  // CompositionState 1 = pure in-game -> blocked.
  const inGame = makeWindow({
    CompositionStateStore: {
      GetCompositionState: () => 1,
      GetCurrentlyFocusedAppidSubscribableValue: () => ({ Value: 1091500 }),
    },
  });
  assert.equal(focusedWindow({ ActiveWindowInstance: inGame, ActiveControllerIndex: 0 }, 'gamescope'), undefined);

  // Side menu open over a running game -> allowed!
  const qamOverGame = makeWindow({
    LocationPathName: '/apprunning',
    MenuStore: { IsSideMenuInteractable: () => true },
    CompositionStateStore: {
      GetCompositionState: () => 2,
      GetCurrentlyFocusedAppidSubscribableValue: () => ({ Value: 1091500 }),
    },
  });
  assert.equal(
    focusedWindow({ ActiveWindowInstance: qamOverGame, ActiveControllerIndex: 0 }, 'gamescope'),
    qamOverGame,
  );

  // Overlay path in state 2/4 when not /apprunning -> allowed, /apprunning without modal/menu -> blocked.
  const overlay = makeWindow({
    LocationPathName: '/overlay/friends',
    BIsOverlayPath: () => true,
    CompositionStateStore: {
      GetCompositionState: () => 2,
      GetCurrentlyFocusedAppidSubscribableValue: () => ({ Value: 1091500 }),
    },
  });
  assert.equal(focusedWindow({ ActiveWindowInstance: overlay, ActiveControllerIndex: 0 }, 'gamescope'), overlay);

  const appRunningNoOverlay = makeWindow({
    LocationPathName: '/apprunning',
    BIsOverlayPath: () => true,
    CompositionStateStore: {
      GetCompositionState: () => 2,
      GetCurrentlyFocusedAppidSubscribableValue: () => ({ Value: 1091500 }),
    },
  });
  assert.equal(
    focusedWindow({ ActiveWindowInstance: appRunningNoOverlay, ActiveControllerIndex: 0 }, 'gamescope'),
    undefined,
  );
});

test('focusedWindow validates Desktop Big Picture visibility, focus, and /apprunning guard', () => {
  const activeDoc = { hidden: false, hasFocus: () => true };
  const win = makeWindow({ BrowserWindow: { document: activeDoc } });
  const nav: NavLike = {
    m_rgGamepadInputSources: [new FakePhysicalSource()],
    GetActiveContext: () => ({
      BIsGamepadInputSuppressed: () => false,
      BIsActive: () => true,
      ActiveWindow: { document: activeDoc },
    }),
    OnButtonDown: () => {},
    OnButtonUp: () => {},
  };
  const ui: SteamUILike = {
    ActiveWindowInstance: win,
    GetFocusedWindowInstance: () => win,
    NavigationManager: nav,
    ActiveControllerIndex: 1,
  };
  assert.equal(focusedWindow(ui, 'desktop'), win);

  // Unfocused document in desktop mode -> blocked.
  activeDoc.hasFocus = () => false;
  assert.equal(focusedWindow(ui, 'desktop'), undefined);

  // Focused, but /apprunning without side menu or modal -> blocked.
  activeDoc.hasFocus = () => true;
  win.LocationPathName = '/apprunning/123';
  assert.equal(focusedWindow(ui, 'desktop'), undefined);

  // Global modal over /apprunning -> allowed.
  win.GetShowingGlobalModal = () => true;
  assert.equal(focusedWindow(ui, 'desktop'), win);
});

test('findGamepadSourceBase walks prototype chain regardless of inheritance depth', () => {
  const direct = findGamepadSourceBase(new FakePhysicalSource());
  const deep = findGamepadSourceBase(new FakeDeepPhysicalSource());
  assert.equal(direct, FakeBaseSource);
  assert.equal(deep, FakeBaseSource);
});

test('createRightStickController dispatches direction down/up, releases on blur/stop, and leaves zero timers when stopped', async () => {
  const win = makeWindow();
  let winFocused = true;
  const downEvents: number[][] = [];
  const upEvents: number[][] = [];
  const nav: NavLike = {
    m_rgGamepadInputSources: [new FakePhysicalSource()],
    GetActiveContext: () => ({
      BIsGamepadInputSuppressed: () => false,
      BIsActive: () => true,
    }),
    OnButtonDown: (...args: number[]) => downEvents.push(args),
    OnButtonUp: (...args: number[]) => upEvents.push(args),
  };
  const ui: SteamUILike = {
    ActiveWindowInstance: win,
    NavigationManager: nav,
    ActiveControllerIndex: 2,
  };

  let directionHandler: ((payload: DirectionPayload) => void) | null = null;
  const focusCalls: Array<{ session: string; active: boolean }> = [];
  let intervalFn: (() => void) | null = null;
  let clearedIntervals = 0;
  let resumeCallback: (() => void) | null = null;
  let resumeUnregistered = false;
  let now = 1000;

  const controller = createRightStickController({
    buttons: { up: 9, down: 10, left: 11, right: 12 },
    getSteamUI: () => (winFocused ? ui : undefined),
    getSessionMode: async () => 'gamescope',
    setFocus: async (session, active) => {
      focusCalls.push({ session, active });
    },
    subscribeDirection: (listener) => {
      directionHandler = listener;
      return () => {
        if (directionHandler === listener) directionHandler = null;
      };
    },
    registerResume: (cb) => {
      resumeCallback = cb;
      return () => {
        resumeUnregistered = true;
      };
    },
    setIntervalFn: (fn) => {
      intervalFn = fn;
      return 42;
    },
    clearIntervalFn: () => {
      clearedIntervals++;
      intervalFn = null;
    },
    now: () => now,
  });

  // Start controller and let getSessionMode resolve.
  controller.start();
  await Promise.resolve();
  await Promise.resolve();
  assert.ok(intervalFn !== null);
  assert.ok(directionHandler !== null);
  intervalFn!();

  assert.equal(focusCalls.length, 1);
  assert.equal(focusCalls[0].active, true);
  const activeSession = focusCalls[0].session;

  // Direction push for the active session triggers OnButtonDown(DIR_RIGHT, 2).
  directionHandler!({ session: activeSession, direction: 'right' as Direction });
  assert.deepEqual(downEvents, [[12, 2]]);
  assert.deepEqual(upEvents, []);

  // Direction push for a stale/different session is ignored.
  directionHandler!({ session: 'other-session', direction: 'left' });
  assert.deepEqual(downEvents, [[12, 2]]);

  // Switch direction right -> up releases right then presses up.
  directionHandler!({ session: activeSession, direction: 'up' });
  assert.deepEqual(upEvents, [[12, 2]]);
  assert.deepEqual(downEvents, [
    [12, 2],
    [9, 2],
  ]);

  // Focus lease renews only after heartbeat interval (2000ms), not on every tick.
  now += 500;
  intervalFn!();
  assert.equal(focusCalls.length, 1);
  now += 2000;
  intervalFn!();
  assert.equal(focusCalls.length, 2);
  assert.deepEqual(focusCalls[1], { session: activeSession, active: true });

  // Resume from suspend releases held button and ends focus cleanly.
  resumeCallback!();
  assert.deepEqual(upEvents, [
    [12, 2],
    [9, 2],
  ]);
  assert.deepEqual(focusCalls[2], { session: activeSession, active: false });

  // Re-acquire focus on next tick, hold down, then stop() (toggle OFF).
  intervalFn!();
  const secondSession = focusCalls[3].session;
  assert.notEqual(secondSession, activeSession);
  directionHandler!({ session: secondSession, direction: 'down' });
  assert.deepEqual(downEvents[2], [10, 2]);

  controller.stop();
  assert.deepEqual(upEvents[2], [10, 2]);
  assert.deepEqual(focusCalls[4], { session: secondSession, active: false });
  assert.equal(clearedIntervals, 1);
  assert.equal(directionHandler, null);
  assert.equal(resumeUnregistered, true);
});
