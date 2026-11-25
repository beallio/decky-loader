// Pure focus-validation and event-driven right-stick navigation controller.
// Steam UI focus rules and synthetic GamepadInputSource integration are adapted from
// Right-Stick-Navigation (MIT License, Copyright (c) 2026 Jed Whitaker).

export type Direction = 'up' | 'down' | 'left' | 'right';
export type SessionMode = 'desktop' | 'gamescope' | null;

export interface DirectionPayload {
  session: string;
  direction: Direction | null;
}

export type GamepadButtonHandler = (...args: number[]) => void;

export interface SourceLike {
  SetSourceType(type: number): void;
  SetRepeatAllowed(check: () => boolean): void;
  OnButtonDown(button: number, index: number): void;
  OnButtonUp(button: number, index: number): void;
  RegisterForGamepadButtonDown(fn: GamepadButtonHandler): unknown;
  RegisterForGamepadButtonUp(fn: GamepadButtonHandler): unknown;
  HandleControllerInputMessages?: unknown;
}

export interface NavContextLike {
  BIsGamepadInputSuppressed(): boolean;
  BIsActive(): boolean;
  ActiveWindow?: { document: { hidden: boolean; hasFocus(): boolean } };
  m_activeBrowserView?: unknown;
}

export interface NavLike {
  m_rgGamepadInputSources: SourceLike[];
  FindAnActiveContext?(): unknown;
  GetActiveContext(): NavContextLike | undefined;
  OnButtonDown: GamepadButtonHandler;
  OnButtonUp: GamepadButtonHandler;
}

export interface SteamWindowLike {
  BrowserWindow?: { document: { hidden: boolean; hasFocus(): boolean } };
  IsGamepadUIWindow(): boolean;
  IsMainGamepadUIWindow(): boolean;
  IsGamepadUIOverlayWindow(): boolean;
  LocationPathName: string;
  MenuStore?: { IsSideMenuInteractable(): boolean };
  GetShowingGlobalModal(): boolean;
  BIsOverlayPath(): boolean;
  CompositionStateStore?: {
    GetCompositionState(): number;
    GetCurrentlyFocusedAppidSubscribableValue(): { Value: number };
  };
}

export interface SteamUILike {
  ActiveWindowInstance?: SteamWindowLike;
  GetFocusedWindowInstance?(): SteamWindowLike | null;
  WindowStore?: { BHasGamepadUIMainWindow(): boolean };
  NavigationManager?: NavLike;
  ActiveControllerIndex: number;
}

export function focusedWindow(ui: SteamUILike | undefined, mode: SessionMode): SteamWindowLike | undefined {
  const win = ui?.ActiveWindowInstance;
  if (!win?.IsGamepadUIWindow?.()) return;
  if (mode === 'desktop') {
    const ctx = ui?.NavigationManager?.GetActiveContext();
    if (ctx) {
      if (
        ui?.GetFocusedWindowInstance?.() !== win ||
        !ctx.BIsActive?.() ||
        !ctx.ActiveWindow ||
        ctx.ActiveWindow.document.hidden ||
        !(ctx.ActiveWindow.document.hasFocus() || ctx.m_activeBrowserView)
      ) {
        return;
      }
    } else {
      const doc = win.BrowserWindow?.document;
      if (!doc || doc.hidden || !doc.hasFocus()) return;
    }
    const bigPicture =
      win.IsMainGamepadUIWindow?.() ||
      (win.IsGamepadUIOverlayWindow?.() && ui?.WindowStore?.BHasGamepadUIMainWindow?.());
    if (!bigPicture) return;
    if (
      /^\/apprunning(?:\/|$)/.test(win.LocationPathName) &&
      !win.MenuStore?.IsSideMenuInteractable?.() &&
      !win.GetShowingGlobalModal?.()
    ) {
      return;
    }
    return win;
  }
  if (mode !== 'gamescope') return;
  const state = win.CompositionStateStore?.GetCompositionState?.();
  if (state !== 2 && state !== 3 && state !== 4) return;
  if (win.MenuStore?.IsSideMenuInteractable?.() || win.GetShowingGlobalModal?.()) return win;
  if (state === 3 && win.CompositionStateStore?.GetCurrentlyFocusedAppidSubscribableValue?.()?.Value === 769) {
    return win;
  }
  if (
    (state === 2 || state === 4) &&
    win.BIsOverlayPath?.() &&
    win.LocationPathName &&
    !/^\/apprunning(?:\/|$)/.test(win.LocationPathName)
  ) {
    return win;
  }
  return undefined;
}

export function findGamepadSourceBase(physical: SourceLike): (new () => SourceLike) | null {
  let proto = Object.getPrototypeOf(physical);
  while (proto && proto !== Object.prototype) {
    if (
      Object.prototype.hasOwnProperty.call(proto, 'SetSourceType') &&
      Object.prototype.hasOwnProperty.call(proto, 'SetRepeatAllowed') &&
      Object.prototype.hasOwnProperty.call(proto, 'OnButtonDown') &&
      Object.prototype.hasOwnProperty.call(proto, 'OnButtonUp') &&
      typeof proto.constructor === 'function'
    ) {
      return proto.constructor as new () => SourceLike;
    }
    proto = Object.getPrototypeOf(proto);
  }
  return null;
}

export interface RightStickControllerDeps {
  buttons: Record<Direction, number>;
  getSteamUI: () => SteamUILike | undefined;
  getSessionMode: () => Promise<SessionMode>;
  setFocus: (session: string, active: boolean) => Promise<void>;
  subscribeDirection: (listener: (payload: DirectionPayload) => void) => () => void;
  registerResume: (callback: () => void) => () => void;
  setIntervalFn: (fn: () => void, ms: number) => number;
  clearIntervalFn: (id: number) => void;
  now?: () => number;
}

const FOCUS_POLL_MS = 100;
const LEASE_RENEW_MS = 2000;
const MODE_RETRY_MS = 1000;
const CLOCK_JUMP_MS = 2500;

export function createRightStickController(deps: RightStickControllerDeps) {
  const now = deps.now ?? (() => Date.now());
  const instance =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${now().toString(36)}-${Math.random().toString(36).slice(2)}`;

  let running = false;
  let mode: SessionMode = null;
  let modeFetching = false;
  let retryAt = 0;
  let owner: SteamWindowLike | undefined;
  let navManager: NavLike | undefined;
  let source: SourceLike | undefined;
  let held: number | undefined;
  let heldIndex = -1;
  let session = '';
  let generation = 0;
  let lastLeaseAt = 0;
  let lastTickAt = 0;
  let timerId: number | null = null;
  let unsubscribeDirection: (() => void) | null = null;
  let unregisterResume: (() => void) | null = null;

  const isValid = () => {
    const ui = deps.getSteamUI();
    return (
      running &&
      !!owner &&
      focusedWindow(ui, mode) === owner &&
      navManager === ui?.NavigationManager &&
      !!navManager?.GetActiveContext() &&
      !navManager.GetActiveContext()?.BIsGamepadInputSuppressed()
    );
  };

  function release() {
    const button = held;
    held = undefined;
    if (button !== undefined) {
      source?.OnButtonUp(button, heldIndex);
    }
  }

  function clearFocus() {
    release();
    if (session) {
      const ending = session;
      session = '';
      void deps.setFocus(ending, false).catch(() => {});
    }
    owner = undefined;
    lastLeaseAt = 0;
  }

  function resetAll() {
    clearFocus();
    navManager = undefined;
    source = undefined;
    modeFetching = false;
    retryAt = 0;
    lastTickAt = now();
  }

  function connect(nav: NavLike): boolean {
    const physical = nav.m_rgGamepadInputSources.find((s) => typeof s.HandleControllerInputMessages === 'function');
    if (!physical) return false;
    const Base = findGamepadSourceBase(physical);
    if (!Base) return false;
    const input = new Base();
    input.SetSourceType(1);
    input.SetRepeatAllowed(isValid);
    input.RegisterForGamepadButtonDown((...args) => {
      if (isValid()) nav.OnButtonDown(...args);
    });
    input.RegisterForGamepadButtonUp((...args) => {
      nav.OnButtonUp(...args);
    });
    source = input;
    navManager = nav;
    return true;
  }

  function onDirection(payload: DirectionPayload) {
    if (!running || !session || payload.session !== session) return;
    if (focusedWindow(deps.getSteamUI(), mode) !== owner) {
      clearFocus();
      return;
    }
    const nextButton = payload.direction === null ? undefined : deps.buttons[payload.direction];
    if (held === nextButton) return;
    release();
    if (nextButton !== undefined && !navManager?.GetActiveContext()) {
      navManager?.FindAnActiveContext?.();
    }
    if (nextButton !== undefined && isValid()) {
      heldIndex = deps.getSteamUI()?.ActiveControllerIndex ?? -1;
      held = nextButton;
      source?.OnButtonDown(nextButton, heldIndex);
    }
  }

  function tick() {
    if (!running) return;
    try {
      const currentNow = now();
      if (lastTickAt > 0 && (currentNow - lastTickAt > CLOCK_JUMP_MS || currentNow < lastTickAt)) {
        resetAll();
      }
      lastTickAt = currentNow;

      if (!mode) {
        if (!modeFetching && currentNow >= retryAt) {
          modeFetching = true;
          void deps
            .getSessionMode()
            .then((result) => {
              if (!running) return;
              mode = result;
              retryAt = mode ? 0 : now() + MODE_RETRY_MS;
              if (mode) tick();
            })
            .catch(() => {
              if (running) retryAt = now() + MODE_RETRY_MS;
            })
            .finally(() => {
              modeFetching = false;
            });
        }
        return;
      }

      const ui = deps.getSteamUI();
      const win = focusedWindow(ui, mode);
      const nav = ui?.NavigationManager;
      if (!win || !nav || win !== owner || nav !== navManager || (held !== undefined && !isValid())) {
        clearFocus();
      }
      if (!win || !nav || currentNow < retryAt) return;
      if (nav !== navManager && !connect(nav)) {
        clearFocus();
        retryAt = currentNow + MODE_RETRY_MS;
        return;
      }
      if (!owner) {
        owner = win;
        session = `${instance}-${++generation}`;
        lastLeaseAt = currentNow;
        void deps.setFocus(session, true).catch(() => {
          if (running) clearFocus();
        });
      } else if (currentNow - lastLeaseAt >= LEASE_RENEW_MS) {
        lastLeaseAt = currentNow;
        void deps.setFocus(session, true).catch(() => {
          if (running) clearFocus();
        });
      }
    } catch {
      clearFocus();
      retryAt = now() + MODE_RETRY_MS;
    }
  }

  return {
    isRunning: () => running,
    start() {
      if (running) return;
      running = true;
      lastTickAt = now();
      unsubscribeDirection = deps.subscribeDirection(onDirection);
      unregisterResume = deps.registerResume(resetAll);
      timerId = deps.setIntervalFn(tick, FOCUS_POLL_MS);
      tick();
    },
    stop() {
      if (!running) return;
      if (timerId !== null) {
        deps.clearIntervalFn(timerId);
        timerId = null;
      }
      unsubscribeDirection?.();
      unsubscribeDirection = null;
      unregisterResume?.();
      unregisterResume = null;
      clearFocus();
      running = false;
      navManager = undefined;
      source = undefined;
      mode = null;
      modeFetching = false;
    },
  };
}
