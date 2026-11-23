import {
  DialogBody,
  DialogButton,
  DialogControlsSection,
  DialogControlsSectionHeader,
  Focusable,
  IconsModule,
  Menu,
  MenuItem,
  NavEntryPositionPreferences,
  Navigation,
  SteamSpinner,
  showContextMenu,
  showModal,
} from '@decky/ui';
import { ComponentType, ExoticComponent, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  FaCaretDown,
  FaClock,
  FaDownload,
  FaInfo,
  FaSortAmountDownAlt,
  FaSortAmountUpAlt,
  FaSyncAlt,
  FaUser,
} from 'react-icons/fa';

import { ICON_BUTTON_STYLE, UI_COLORS } from '../../../../utils/uiStyles';
import { useDeckyState } from '../../../DeckyState';
import TestingInstallModal from '../../../modals/TestingInstallModal';

interface TestingVersion {
  id: number;
  name: string;
  link: string;
  head_sha: string;
  created_at: string;
  updated_at: string;
  author?: string;
}

type SortField = 'updated' | 'opened' | 'number' | 'author' | 'title';

const formatDate = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });

// Fork: Steam's own icons from @decky/ui's IconsModule, validated before rendering.
const steamIcons = IconsModule as Record<string, unknown> | undefined;
const getRenderableIcon = (icon: unknown): ComponentType | ExoticComponent | undefined =>
  typeof icon === 'function' || (typeof icon === 'object' && icon !== null && '$$typeof' in icon)
    ? (icon as ComponentType | ExoticComponent)
    : undefined;
const SteamAuthorIcon = getRenderableIcon(steamIcons?.User);
const SteamClockIcon = getRenderableIcon(steamIcons?.Clock);
const SteamUpdatedIcon = getRenderableIcon(steamIcons?.Reload);

const getTestingVersions = DeckyBackend.callable<[], TestingVersion[]>('updater/get_testing_versions');
const downloadTestingVersion = DeckyBackend.callable<[pr_id: number, sha: string]>('updater/download_testing_version');

export default function TestingVersionList() {
  const { t } = useTranslation();
  const { isLoaderUpdating, setIsLoaderUpdating } = useDeckyState();

  const [testingVersions, setTestingVersions] = useState<TestingVersion[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [loadingError, setLoadingError] = useState<string | null>(null);
  const [sortField, setSortField] = useState<SortField>('updated');
  const [sortDescending, setSortDescending] = useState<boolean>(true);

  useEffect(() => {
    let cancelled = false;

    const loadTestingVersions = async () => {
      try {
        const versions = await getTestingVersions();
        if (!cancelled) setTestingVersions(versions);
      } catch (e) {
        if (!cancelled) setLoadingError(e instanceof Error ? `${e.name}: ${e.message}` : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    loadTestingVersions();
    return () => {
      cancelled = true;
    };
  }, []);

  const sortedVersions = useMemo(() => {
    const primaryValue = (version: TestingVersion): number | string => {
      switch (sortField) {
        case 'opened':
          return new Date(version.created_at).getTime();
        case 'number':
          return version.id;
        case 'author':
          return (version.author ?? '').toLowerCase();
        case 'title':
          return version.name.toLowerCase();
        case 'updated':
        default:
          return new Date(version.updated_at).getTime();
      }
    };

    return [...testingVersions].sort((a, b) => {
      const aValue = primaryValue(a);
      const bValue = primaryValue(b);
      const comparison =
        typeof aValue === 'number' && typeof bValue === 'number'
          ? aValue - bValue
          : String(aValue).localeCompare(String(bValue));
      if (comparison !== 0) {
        return sortDescending ? -comparison : comparison;
      }
      // Fork: equal sort values stay newest-PR-first.
      return b.id - a.id;
    });
  }, [testingVersions, sortField, sortDescending]);

  const sortValue = (version: TestingVersion): string => {
    switch (sortField) {
      case 'opened':
        return formatDate(version.created_at);
      case 'updated':
        return formatDate(version.updated_at);
      case 'author':
        return version.author ?? '';
      case 'number':
      case 'title':
      default:
        return '#' + version.id;
    }
  };

  const sortOptions: { label: string; data: SortField }[] = [
    { label: t('Testing.sort.updated'), data: 'updated' },
    { label: t('Testing.sort.opened'), data: 'opened' },
    { label: t('Testing.sort.number'), data: 'number' },
    { label: t('Testing.sort.author'), data: 'author' },
    { label: t('Testing.sort.title'), data: 'title' },
  ];

  const sortLabel = sortOptions.find((option) => option.data === sortField)?.label ?? sortOptions[0].label;

  const openSortMenu = (target: EventTarget | null) => {
    showContextMenu(
      <Menu label={t('Testing.items')}>
        {sortOptions.map((option) => (
          <MenuItem key={option.data} selected={option.data === sortField} onSelected={() => setSortField(option.data)}>
            {option.label}
          </MenuItem>
        ))}
      </Menu>,
      target ?? window,
    );
  };

  const openInstallModal = (version: TestingVersion) => {
    if (isLoaderUpdating) return;

    showModal(
      <TestingInstallModal
        version={version}
        onInstallFailed={() => setIsLoaderUpdating(false)}
        onConfirm={async () => {
          await downloadTestingVersion(version.id, version.head_sha);
        }}
      />,
    );
  };

  if (loading) {
    return (
      <>
        <SteamSpinner background="transparent">{t('Testing.loading')}</SteamSpinner>
      </>
    );
  }

  if (loadingError) {
    return (
      <DialogBody>
        <p>
          {t('Testing.load_error')}: {loadingError}
        </p>
      </DialogBody>
    );
  }

  if (testingVersions.length === 0) {
    return (
      <DialogBody>
        <p>{t('Testing.no_prs', { defaultValue: 'No open PRs found' })}</p>
      </DialogBody>
    );
  }

  return (
    <DialogBody>
      <DialogControlsSection>
        <DialogControlsSectionHeader>{t('Testing.header')}</DialogControlsSectionHeader>
        <Focusable style={{ display: 'flex', alignItems: 'center', padding: '8px 8px 0' }}>
          <span
            style={{
              color: UI_COLORS.textPrimary,
              fontSize: '16px',
              fontWeight: 500,
              lineHeight: '20px',
              whiteSpace: 'nowrap',
            }}
          >
            {t('Testing.items')}
          </span>
          <span
            style={{
              color: UI_COLORS.textSub,
              fontSize: '16px',
              fontWeight: 500,
              lineHeight: '20px',
              marginLeft: '8px',
            }}
          >
            {testingVersions.length}
          </span>
          <div
            aria-hidden="true"
            style={{
              flexGrow: 1,
              height: '2px',
              margin: '0 10px',
              backgroundColor: 'rgba(103, 112, 123, 0.3)',
            }}
          />
          <Focusable
            role="button"
            aria-label={sortLabel}
            aria-haspopup="menu"
            onActivate={(e) => openSortMenu(e.currentTarget)}
            focusClassName="decky-testing-focus"
            style={{
              display: 'flex',
              alignItems: 'center',
              height: '28px',
              padding: '4px 0',
              marginTop: '1px',
              cursor: 'pointer',
            }}
          >
            <FaCaretDown size={16} style={{ margin: '2px 8px 2px 0', flexShrink: 0 }} />
            <span style={{ fontSize: '16px', lineHeight: '20px', whiteSpace: 'nowrap' }}>{sortLabel}</span>
          </Focusable>
          <Focusable
            role="button"
            aria-label={sortDescending ? t('Testing.sort_ascending') : t('Testing.sort_descending')}
            onActivate={() => setSortDescending((descending) => !descending)}
            focusClassName="decky-testing-focus"
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              width: '28px',
              height: '28px',
              marginLeft: '13px',
              flexShrink: 0,
              cursor: 'pointer',
            }}
          >
            {sortDescending ? (
              <FaSortAmountDownAlt aria-hidden="true" size={16} />
            ) : (
              <FaSortAmountUpAlt aria-hidden="true" size={16} />
            )}
          </Focusable>
        </Focusable>
        <ul style={{ listStyleType: 'none', padding: '0', margin: '0' }}>
          {sortedVersions.map((version) => {
            return (
              <li key={`${version.id}_${version.name}`}>
                <div style={{ display: 'flex', alignItems: 'center', minHeight: '58px', padding: '6px 8px' }}>
                  <div style={{ display: 'flex', flexDirection: 'column', flexGrow: 1, minWidth: 0 }}>
                    <div
                      style={{
                        color: UI_COLORS.textPrimary,
                        fontSize: '16px',
                        fontWeight: 500,
                        lineHeight: '20px',
                        marginBottom: '4px',
                      }}
                    >
                      {version.name}
                    </div>
                    <div
                      style={{
                        color: UI_COLORS.textSub,
                        fontSize: '12px',
                        fontWeight: 700,
                        lineHeight: '22px',
                        letterSpacing: '0.5px',
                        textTransform: 'uppercase',
                      }}
                    >
                      <span className="decky-testing-icon">{SteamAuthorIcon ? <SteamAuthorIcon /> : <FaUser />}</span>
                      {t('Testing.author', { author: version.author ?? '' })}
                      {' · '}
                      <span className="decky-testing-icon">{SteamClockIcon ? <SteamClockIcon /> : <FaClock />}</span>
                      {t('Testing.opened', { date: formatDate(version.created_at) })}
                      {' · '}
                      <span className="decky-testing-icon">
                        {SteamUpdatedIcon ? <SteamUpdatedIcon /> : <FaSyncAlt />}
                      </span>
                      {t('Testing.updated', { date: formatDate(version.updated_at) })}
                    </div>
                  </div>
                  <span
                    style={{
                      color: UI_COLORS.textPrimary,
                      fontSize: '12px',
                      fontWeight: 700,
                      lineHeight: '22px',
                      letterSpacing: '0.5px',
                      textTransform: 'uppercase',
                      whiteSpace: 'nowrap',
                      maxWidth: '35%',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      margin: '0 8px',
                      flexShrink: 0,
                    }}
                  >
                    {sortValue(version)}
                  </span>
                  <Focusable
                    style={{ display: 'flex', gap: '8px', height: '40px', flexShrink: 0 }}
                    navEntryPreferPosition={NavEntryPositionPreferences.MAINTAIN_X}
                  >
                    <DialogButton
                      style={ICON_BUTTON_STYLE}
                      aria-label={t('Testing.install')}
                      disabled={isLoaderUpdating}
                      onClick={() => openInstallModal(version)}
                    >
                      <FaDownload aria-hidden="true" style={{ display: 'block' }} />
                    </DialogButton>
                    <DialogButton
                      style={ICON_BUTTON_STYLE}
                      aria-label={t('Testing.info')}
                      onClick={() => Navigation.NavigateToExternalWeb(version.link)}
                    >
                      <FaInfo aria-hidden="true" style={{ display: 'block' }} />
                    </DialogButton>
                  </Focusable>
                </div>
              </li>
            );
          })}
        </ul>
      </DialogControlsSection>
      <style>{`
.decky-testing-focus {
  outline: 2px solid rgba(255, 255, 255, 0.8);
  outline-offset: 2px;
  border-radius: 4px;
}
.decky-testing-icon {
  display: inline-flex;
  align-items: center;
  vertical-align: -3px;
  margin-right: 4px;
  color: ${UI_COLORS.textBody};
}
.decky-testing-icon svg {
  width: 16px !important;
  height: 16px !important;
  flex-shrink: 0;
  fill: ${UI_COLORS.textBody};
}
.decky-testing-icon svg [stroke] {
  stroke: ${UI_COLORS.textBody};
}
`}</style>
    </DialogBody>
  );
}
