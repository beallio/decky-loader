import { findClassModule, quickAccessMenuClasses } from '@decky/ui';

// Steam client builds from 2026-10-13 split the quick access menu class module in two
// (SteamDeckHomebrew/decky-loader#988), so @decky/ui finds no quickAccessMenuClasses and
// staticClasses.Title throws. Merge the two halves so the loader and plugins keep working.
function findQuickAccessClasses(): typeof quickAccessMenuClasses {
  if (quickAccessMenuClasses) return quickAccessMenuClasses;
  const menu = findClassModule((m) => m.Title && m.QuickAccessMenu);
  const panel = findClassModule((m) => m.BatteryDetailsLabels && m.PanelSectionTitle);
  // An empty map leaves elements unstyled instead of crashing the whole loader.
  return { ...panel, ...menu } as typeof quickAccessMenuClasses;
}

export const quickAccessClasses = findQuickAccessClasses();
