import type { CSSProperties } from 'react';

// Fork: shared Valve Steam Deck GamepadUI (--gpSystem* / --gpColor-*) color tokens
// matching steamui-watch's VALVE_PALETTE, with hex fallbacks for contexts where a variable is unset.
export const UI_COLORS = {
  textPrimary: 'var(--gpSystemLightestGrey, #dcdedf)',
  textBody: 'var(--gpSystemLighterGrey, #b8bcbf)',
  textSub: 'var(--gpSystemLightGrey, #8b929a)',
  textMuted: 'var(--gpSystemGrey, #67707b)',
  border: 'var(--gpSystemDarkGrey, #3d4450)',
  surface: 'var(--gpSystemDarkerGrey, #23262e)',
  surfaceDark: 'var(--gpSystemDarkestGrey, #0e141b)',
  accentBlue: 'var(--gpColor-Blue, #1a9fff)',
  accentBlueHi: 'var(--gpColor-ChalkyBlue, #66c0f4)',
  warning: 'var(--gpColor-Yellow, #ffc53d)',
  warningBg: 'rgba(255, 197, 61, 0.1)',
  warningBorder: 'rgba(255, 197, 61, 0.45)',
  danger: 'var(--gpColor-Red, #ec5d5e)',
  dangerBg: 'rgba(236, 93, 94, 0.1)',
  dangerBorder: 'rgba(236, 93, 94, 0.45)',
} as const;

export const ICON_BUTTON_STYLE: CSSProperties = {
  height: '40px',
  width: '40px',
  minWidth: '40px',
  padding: 0,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
};

export const COMPACT_ICON_BUTTON_STYLE: CSSProperties = {
  height: '28px',
  width: '40px',
  minWidth: 0,
  padding: 0,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
};
