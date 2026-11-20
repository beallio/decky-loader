import type { ToastData } from '@decky/api';
import { Focusable, Navigation, findClassModule, joinClassNames } from '@decky/ui';
import { FC, memo } from 'react';

import Logger from '../logger';
import { UI_COLORS } from '../utils/uiStyles';

const logger = new Logger('ToastRenderer');

// TODO there are more of these
export enum ToastLocation {
  /** Big Picture popup toasts */
  GAMEPADUI_POPUP = 1,
  /** QAM Notifications tab */
  GAMEPADUI_QAM = 3,
}

interface ToastProps {
  toast: ToastData;
  newIndicator?: boolean;
}

interface ToastRendererProps extends ToastProps {
  location: ToastLocation;
}

const templateClasses = findClassModule((m) => m.ShortTemplate) || {};
const needsViteToastFallback = !templateClasses.ShortTemplate;

// Fork: Vite/Rolldown tree-shakes Steam's notification CSS module out of JS, leaving
// templateClasses empty. Provide native-looking fallback styles keyed on Decky's classes.
const VITE_TOAST_FALLBACK_CSS = `
.DeckyGamepadUIPopupToast,
.DeckyGamepadUIQAMToast .decky-toast-standard {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  background: ${UI_COLORS.surface};
  color: ${UI_COLORS.textPrimary};
  border-radius: 4px;
  box-sizing: border-box;
  width: 100%;
}
.DeckyGamepadUIQAMToast {
  margin-bottom: 6px;
}
.DeckyGamepadUIPopupToast .decky-toast-logo,
.DeckyGamepadUIQAMToast .decky-toast-logo {
  flex-shrink: 0;
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.DeckyGamepadUIPopupToast .decky-toast-content,
.DeckyGamepadUIQAMToast .decky-toast-content {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.DeckyGamepadUIPopupToast .decky-toast-header,
.DeckyGamepadUIQAMToast .decky-toast-header {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: ${UI_COLORS.textPrimary};
}
.DeckyGamepadUIPopupToast .decky-toast-icon,
.DeckyGamepadUIQAMToast .decky-toast-icon {
  flex-shrink: 0;
  display: flex;
  align-items: center;
}
.DeckyGamepadUIPopupToast .decky-toast-title,
.DeckyGamepadUIQAMToast .decky-toast-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.DeckyGamepadUIQAMToast .decky-toast-timestamp {
  margin-left: auto;
  font-size: 11px;
  font-weight: 400;
  color: ${UI_COLORS.textSub};
  flex-shrink: 0;
}
.DeckyGamepadUIPopupToast .decky-toast-body,
.DeckyGamepadUIQAMToast .decky-toast-body {
  font-size: 12px;
  color: ${UI_COLORS.textBody};
  overflow-wrap: anywhere;
}
.DeckyGamepadUIQAMToast .decky-toast-subtext {
  font-size: 11px;
  color: ${UI_COLORS.textSub};
}
.DeckyGamepadUIQAMToast .decky-toast-new {
  flex-shrink: 0;
  width: 8px;
  height: 8px;
  color: ${UI_COLORS.accentBlue};
}
.DeckyGamepadUIQAMToast .decky-toast-new svg {
  width: 8px;
  height: 8px;
  display: block;
}
`;

// These are memoized as they like to randomly rerender

const GamepadUIPopupToast: FC<Omit<ToastProps, 'newIndicator'>> = memo(({ toast }) => {
  return (
    <div
      style={{ '--toast-duration': `${toast.duration}ms` } as React.CSSProperties}
      onClick={toast.onClick}
      className={joinClassNames(templateClasses.ShortTemplate, toast.className || '', 'DeckyGamepadUIPopupToast')}
    >
      {needsViteToastFallback && <style>{VITE_TOAST_FALLBACK_CSS}</style>}
      {toast.logo && (
        <div className={joinClassNames(templateClasses.StandardLogoDimensions, 'decky-toast-logo')}>{toast.logo}</div>
      )}
      <div className={joinClassNames(templateClasses.Content, toast.contentClassName || '', 'decky-toast-content')}>
        <div className={joinClassNames(templateClasses.Header, 'decky-toast-header')}>
          {toast.icon && <div className={joinClassNames(templateClasses.Icon, 'decky-toast-icon')}>{toast.icon}</div>}
          <div className={joinClassNames(templateClasses.Title, 'decky-toast-title')}>{toast.title}</div>
        </div>
        <div className={joinClassNames(templateClasses.Body, 'decky-toast-body')}>{toast.body}</div>
      </div>
    </div>
  );
});

const GamepadUIQAMToast: FC<ToastProps> = memo(({ toast, newIndicator }) => {
  // The fields aren't mismatched, the logic for these is just a bit weird.
  return (
    <Focusable
      onActivate={() => {
        toast.onClick?.();
        Navigation.CloseSideMenus();
      }}
      className={joinClassNames(
        templateClasses.StandardTemplateContainer,
        toast.className || '',
        'DeckyGamepadUIQAMToast',
      )}
    >
      {needsViteToastFallback && <style>{VITE_TOAST_FALLBACK_CSS}</style>}
      <div className={joinClassNames(templateClasses.StandardTemplate, 'decky-toast-standard')}>
        {toast.logo && (
          <div className={joinClassNames(templateClasses.StandardLogoDimensions, 'decky-toast-logo')}>{toast.logo}</div>
        )}
        <div className={joinClassNames(templateClasses.Content, toast.contentClassName || '', 'decky-toast-content')}>
          <div className={joinClassNames(templateClasses.Header, 'decky-toast-header')}>
            {toast.icon && <div className={joinClassNames(templateClasses.Icon, 'decky-toast-icon')}>{toast.icon}</div>}
            {toast.title && (
              <div className={joinClassNames(templateClasses.Title, 'decky-toast-title')}>{toast.title}</div>
            )}
            {/* timestamp should always be defined by toaster */}
            {/* TODO check how valve does this */}
            {toast.timestamp && (
              <div className={joinClassNames(templateClasses.Timestamp, 'decky-toast-timestamp')}>
                {toast.timestamp.toLocaleTimeString(undefined, { timeStyle: 'short' })}
              </div>
            )}
          </div>
          {toast.body && (
            <div className={joinClassNames(templateClasses.StandardNotificationDescription, 'decky-toast-body')}>
              {toast.body}
            </div>
          )}
          {toast.subtext && (
            <div className={joinClassNames(templateClasses.StandardNotificationSubText, 'decky-toast-subtext')}>
              {toast.subtext}
            </div>
          )}
        </div>
        {newIndicator && (
          <div className={joinClassNames(templateClasses.NewIndicator, 'decky-toast-new')}>
            <svg xmlns="http://www.w3.org/2000/svg" width="50" height="50" viewBox="0 0 50 50" fill="none">
              <circle fill="currentColor" cx="25" cy="25" r="25"></circle>
            </svg>
          </div>
        )}
      </div>
    </Focusable>
  );
});

export const ToastRenderer: FC<ToastRendererProps> = memo(({ toast, location, newIndicator }) => {
  switch (location) {
    default:
      logger.warn(`Toast UI not implemented for location ${location}! Falling back to GamepadUIQAMToast.`);
      return <GamepadUIQAMToast toast={toast} newIndicator={false} />;
    case ToastLocation.GAMEPADUI_POPUP:
      return <GamepadUIPopupToast toast={toast} />;
    case ToastLocation.GAMEPADUI_QAM:
      return <GamepadUIQAMToast toast={toast} newIndicator={newIndicator} />;
  }
});

export default ToastRenderer;
