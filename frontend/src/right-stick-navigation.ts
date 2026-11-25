import { GamepadButton } from '@decky/ui';

import { DeckyState } from './components/DeckyState';
import Logger from './logger';
import { DirectionPayload, SessionMode, SteamUILike, createRightStickController } from './rightStickNavigationLogic';
import { getSetting, setSetting } from './utils/settings';

const SETTING_KEY = 'rightStickNavigation';

export class RightStickNavigationService extends Logger {
  private controller: ReturnType<typeof createRightStickController>;
  private generation = 0;

  constructor(private deckyState: DeckyState) {
    super('RightStickNavigationService');
    this.controller = createRightStickController({
      buttons: {
        up: GamepadButton.DIR_UP,
        down: GamepadButton.DIR_DOWN,
        left: GamepadButton.DIR_LEFT,
        right: GamepadButton.DIR_RIGHT,
      },
      getSteamUI: () => window.SteamUIStore as unknown as SteamUILike | undefined,
      getSessionMode: () => DeckyBackend.call<[], SessionMode>('utilities/right_stick/get_session_mode'),
      setFocus: (session, active) =>
        DeckyBackend.call<[string, boolean], void>('utilities/right_stick/set_focus', session, active),
      subscribeDirection: (listener) => {
        const handler = (payload: DirectionPayload) => listener(payload);
        DeckyBackend.addEventListener('loader/right_stick_direction', handler);
        return () => DeckyBackend.removeEventListener('loader/right_stick_direction', handler);
      },
      registerResume: (callback) => {
        try {
          const registration = (
            window as unknown as {
              SteamClient?: {
                System?: { RegisterForOnResumeFromSuspend?: (cb: () => void) => { unregister(): void } };
              };
            }
          ).SteamClient?.System?.RegisterForOnResumeFromSuspend?.(callback);
          return () => registration?.unregister();
        } catch {
          return () => {};
        }
      },
      setIntervalFn: (fn, ms) => window.setInterval(fn, ms),
      clearIntervalFn: (id) => window.clearInterval(id),
    });
  }

  async init() {
    const gen = ++this.generation;
    const enabled = await getSetting<boolean>(SETTING_KEY, false);
    if (gen !== this.generation) return;
    this.apply(Boolean(enabled));
  }

  async setEnabled(enabled: boolean) {
    const gen = ++this.generation;
    const normalized = Boolean(enabled);
    this.deckyState.setRightStickNavigation(normalized);
    if (!normalized) {
      this.controller.stop();
      await setSetting(SETTING_KEY, false);
      return;
    }
    await setSetting(SETTING_KEY, true);
    if (gen === this.generation) {
      this.controller.start();
    }
  }

  deinit() {
    this.generation++;
    this.controller.stop();
  }

  private apply(enabled: boolean) {
    this.deckyState.setRightStickNavigation(enabled);
    if (enabled) {
      this.controller.start();
    } else {
      this.controller.stop();
    }
  }
}
