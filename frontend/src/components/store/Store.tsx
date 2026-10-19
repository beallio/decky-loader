import {
  DialogButton,
  Dropdown,
  DropdownOption,
  Focusable,
  SingleDropdownOption,
  SteamSpinner,
  Tabs,
  TextField,
  findModule,
} from '@decky/ui';
import { Dispatch, FC, SetStateAction, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FaSyncAlt, FaTimes } from 'react-icons/fa';

import logo from '../../../assets/plugin_store.png';
import Logger from '../../logger';
import { SortDirections, SortOptions, getPluginList, getStoreSettings } from '../../store';
import {
  MergedStorePlugin,
  NO_STORE_FILTERS,
  STORE_CACHE_MS,
  StoreFilters,
  filterStorePlugins,
  restoreStoreView,
  sortStorePlugins,
  storeLabel,
  tagGroups,
} from '../../storeSources';
import { getSetting, setSetting } from '../../utils/settings';
import { useDeckyState } from '../DeckyState';
import ExternalLink from '../ExternalLink';
import PluginCard from './PluginCard';

const logger = new Logger('Store');

const StorePage: FC<{}> = () => {
  const [currentTabRoute, setCurrentTabRoute] = useState<string>('browse');
  const [pluginCount, setPluginCount] = useState<number | null>(null);
  const { TabCount } = findModule((m) => {
    if (m?.TabCount && m?.TabTitle) return true;
    return false;
  });

  const { t } = useTranslation();

  return (
    <>
      <div
        style={{
          marginTop: '40px',
          height: 'calc( 100% - 40px )',
          background: '#0005',
        }}
      >
        <Tabs
          activeTab={currentTabRoute}
          onShowTab={(tabId: string) => {
            setCurrentTabRoute(tabId);
          }}
          tabs={[
            {
              title: t('Store.store_tabs.title'),
              content: <BrowseTab setPluginCount={setPluginCount} />,
              id: 'browse',
              renderTabAddon: () => <span className={TabCount}>{pluginCount}</span>,
            },
            {
              title: t('Store.store_tabs.about'),
              content: <AboutTab />,
              id: 'about',
            },
          ]}
        />
      </div>
    </>
  );
};

// Fork: the sort and filters of the last visit (not the search text). Kept in Decky's settings file:
// Steam can restart before CEF writes localStorage to disk, which brought back an older view.
const VIEW_SETTING = 'store_view';

const BrowseTab: FC<{ setPluginCount: Dispatch<SetStateAction<number | null>> }> = ({ setPluginCount }) => {
  const { t } = useTranslation();

  const dropdownSortOptions = useMemo(
    (): SingleDropdownOption[] => [
      // ascending and descending order are the wrong way around for the alphabetical sort
      // this is because it was initially done incorrectly for i18n and 'fixing' it would
      // make all the translations incorrect
      { data: `${SortOptions.name}:${SortDirections.ascending}`, label: t('Store.store_tabs.alph_desc') },
      { data: `${SortOptions.name}:${SortDirections.descending}`, label: t('Store.store_tabs.alph_asce') },
      { data: `${SortOptions.updated}:${SortDirections.descending}`, label: t('Store.store_tabs.updated_desc') },
      { data: `${SortOptions.updated}:${SortDirections.ascending}`, label: t('Store.store_tabs.updated_asce') },
      { data: `${SortOptions.date}:${SortDirections.descending}`, label: t('Store.store_tabs.date_desc') },
      { data: `${SortOptions.date}:${SortDirections.ascending}`, label: t('Store.store_tabs.date_asce') },
      { data: `${SortOptions.downloads}:${SortDirections.descending}`, label: t('Store.store_tabs.downloads_desc') },
      { data: `${SortOptions.downloads}:${SortDirections.ascending}`, label: t('Store.store_tabs.downloads_asce') },
    ],
    [],
  );

  const sorts = useMemo(() => dropdownSortOptions.map((option) => String(option.data)), [dropdownSortOptions]);
  const [viewLoaded, setViewLoaded] = useState<boolean>(false);
  const [selectedSort, setSort] = useState<string>(() => restoreStoreView(null, sorts).sort);
  const [filters, setFilters] = useState<StoreFilters>(NO_STORE_FILTERS);
  const [searchFieldValue, setSearchValue] = useState<string>('');
  const [pluginList, setPluginList] = useState<MergedStorePlugin[] | null>(null);
  const [failedStores, setFailedStores] = useState<string[]>([]);
  const [staleStores, setStaleStores] = useState<{ url: string; fetchedAt: number }[]>([]);
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [now, setNow] = useState<number>(Date.now());
  const [isTesting, setIsTesting] = useState<boolean>(false);
  const searchField = useRef<HTMLDivElement>(null);

  const load = useCallback(async (force: boolean) => {
    setRefreshing(true);
    try {
      const { plugins, failed, stale, fetchedAt } = await getPluginList(force);
      logger.debug('got data!', plugins, failed, stale);
      setPluginList(plugins);
      setFailedStores(failed);
      setStaleStores(stale);
      setFetchedAt(fetchedAt);
      setPluginCount(plugins.length);
    } finally {
      setRefreshing(false);
      setNow(Date.now());
    }
  }, []);

  useEffect(() => {
    load(false);
    const timer = setInterval(() => setNow(Date.now()), 60000);
    return () => clearInterval(timer);
  }, []);

  // Lists that expire while the page is open are downloaded again.
  useEffect(() => {
    if (!refreshing && fetchedAt !== null && now - fetchedAt >= STORE_CACHE_MS) load(false);
  }, [now]);

  useEffect(() => {
    getSetting<unknown>(VIEW_SETTING, null)
      .catch((e) => {
        logger.warn('Could not read the saved store sort and filters', e);
        return null;
      })
      .then((saved) => {
        const view = restoreStoreView(saved, sorts);
        setSort(view.sort);
        setFilters(view.filters);
        setViewLoaded(true);
      });
  }, []);

  useEffect(() => {
    if (!viewLoaded) return;
    setSetting(VIEW_SETTING, { sort: selectedSort, ...filters }).catch((e) =>
      logger.warn('Could not save the store sort and filters', e),
    );
  }, [viewLoaded, selectedSort, filters]);

  useEffect(() => {
    (async () => {
      const { includePrereleases } = await getStoreSettings();
      logger.debug(`includePrereleases is ${includePrereleases}`);
      setIsTesting(includePrereleases);
    })();
  }, []);

  const { installedPlugins } = useDeckyState();
  const installedNames = useMemo(() => new Set(installedPlugins.map((plugin) => plugin.name)), [installedPlugins]);

  const storeOptions = useMemo(() => {
    const counts = new Map<string, number>();
    for (const plugin of pluginList ?? []) {
      for (const store of plugin.stores) counts.set(store, (counts.get(store) ?? 0) + 1);
    }
    return [...counts].map(([store, count]) => ({ data: store, label: `${storeLabel(store)} (${count})` }));
  }, [pluginList]);
  const tagOptions = useMemo(
    () => tagGroups(pluginList ?? []).map(({ key, label, count }) => ({ data: key, label: `${label} (${count})` })),
    [pluginList],
  );

  // A remembered store or tag that is gone now counts as "all".
  const activeFilters: StoreFilters = {
    ...filters,
    store: !pluginList || storeOptions.some((option) => option.data === filters.store) ? filters.store : '',
    tag: !pluginList || tagOptions.some((option) => option.data === filters.tag) ? filters.tag : '',
  };
  const filtering = !!searchFieldValue.trim() || Object.values(activeFilters).some(Boolean);

  const shownPlugins = useMemo(() => {
    if (!pluginList || !viewLoaded) return null;
    const [by, direction] = selectedSort.split(':');
    return sortStorePlugins(
      filterStorePlugins(pluginList, searchFieldValue, activeFilters, installedNames),
      by,
      direction,
    );
  }, [pluginList, viewLoaded, selectedSort, searchFieldValue, JSON.stringify(activeFilters), installedNames]);

  const relativeTime = (ms: number) => {
    const format = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
    const minutes = Math.round(ms / 60000);
    return Math.abs(minutes) < 60 ? format.format(minutes, 'minute') : format.format(Math.round(ms / 3600000), 'hour');
  };

  const filterDropdown = (
    key: keyof StoreFilters,
    label: string,
    options: DropdownOption[],
    allLabel: string = t('Store.store_filter.all_plugins'),
  ) => (
    <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
      <span className="DialogLabel">{label}</span>
      <Dropdown
        menuLabel={label}
        rgOptions={[{ data: '', label: allLabel }, ...options]}
        selectedOption={activeFilters[key]}
        onChange={({ data }) => setFilters({ ...filters, [key]: data })}
        renderButtonValue={(value) => (activeFilters[key] ? <span style={{ color: '#1a9fff' }}>{value}</span> : value)}
      />
    </div>
  );

  return (
    <>
      <style>{`
              .deckyStoreCardInstallContainer > .Panel {
                padding: 0;
              }
            `}</style>
      <Focusable style={{ display: 'flex', flexDirection: 'column', gap: '10px', margin: '0 20px 20px' }}>
        <div ref={searchField}>
          <TextField
            label={t('Store.store_search.label')}
            value={searchFieldValue}
            onChange={(e) => setSearchValue(e.target.value)}
            inlineControls={
              searchFieldValue ? (
                <DialogButton
                  aria-label={t('Store.store_search.clear')}
                  style={{ width: 'auto', minWidth: 0, height: '40px', padding: '0 14px' }}
                  onClick={() => {
                    setSearchValue('');
                    // The button disappears with the text; keep the focus in the search field.
                    searchField.current?.querySelector('input')?.focus();
                  }}
                >
                  <FaTimes style={{ display: 'block' }} />
                </DialogButton>
              ) : undefined
            }
          />
        </div>
        {/* Two rows of three: six columns cut the dropdown labels at the Deck's width. */}
        <Focusable style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '8px' }}>
          <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
            <span className="DialogLabel">{t('Store.store_sort.label')}</span>
            <Dropdown
              menuLabel={t('Store.store_sort.label') as string}
              rgOptions={dropdownSortOptions}
              strDefaultLabel={t('Store.store_sort.label_def') as string}
              selectedOption={selectedSort}
              onChange={(e) => setSort(e.data)}
            />
          </div>
          {filterDropdown('store', t('Store.store_filter.store'), storeOptions, t('Store.store_filter.all_stores'))}
          {filterDropdown('tag', t('Store.store_filter.tag'), tagOptions, t('Store.store_filter.all_tags'))}
          {filterDropdown('installed', t('Store.store_filter.installed'), [
            { data: 'yes', label: t('Store.store_filter.installed_yes') },
            { data: 'no', label: t('Store.store_filter.installed_no') },
          ])}
          {filterDropdown('root', t('Store.store_filter.root'), [
            { data: 'hide', label: t('Store.store_filter.root_hide') },
            { data: 'only', label: t('Store.store_filter.root_only') },
          ])}
          {filterDropdown('github', t('Store.store_filter.github'), [
            { data: 'yes', label: t('Store.store_filter.github_yes') },
          ])}
        </Focusable>
        <Focusable style={{ display: 'flex', alignItems: 'center', gap: '8px', minHeight: '32px' }}>
          <span style={{ flexGrow: 1, fontSize: '14px', color: '#8b929a' }}>
            {pluginList &&
              shownPlugins &&
              (filtering
                ? t('Store.store_count_filtered', { shown: shownPlugins.length, count: pluginList.length })
                : t('Store.store_count', { count: pluginList.length }))}
          </span>
          {/* Always shown and never disabled: a button that disappears or is disabled loses the controller focus. */}
          <DialogButton
            style={{ width: 'auto', minWidth: 0, height: '32px', padding: '0 16px', whiteSpace: 'nowrap' }}
            onClick={() => {
              setSearchValue('');
              setFilters(NO_STORE_FILTERS);
            }}
          >
            {t('Store.store_filter.clear_all')}
          </DialogButton>
          <DialogButton
            style={{ width: 'auto', minWidth: 0, height: '32px', padding: '0 16px', whiteSpace: 'nowrap' }}
            onClick={() => !refreshing && load(true)}
          >
            <span style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <FaSyncAlt /> {refreshing ? t('Store.store_refreshing') : t('Store.store_refresh')}
            </span>
          </DialogButton>
        </Focusable>
        {fetchedAt !== null && (
          <span style={{ marginTop: '-6px', textAlign: 'right', fontSize: '12px', color: '#8b929a' }}>
            {t('Store.store_cache.age', { when: relativeTime(fetchedAt - now) })}
            {' · '}
            {t('Store.store_cache.next', { when: relativeTime(Math.max(fetchedAt + STORE_CACHE_MS - now, 0)) })}
          </span>
        )}
      </Focusable>
      {isTesting && (
        <div
          style={{
            alignItems: 'center',
            display: 'flex',
            flexDirection: 'column',
            marginLeft: '20px',
            marginRight: '20px',
            marginBottom: '20px',
            padding: '8px 36px',
            background: 'rgba(255, 255, 0, 0.067)',
            textAlign: 'center',
            border: '2px solid rgba(255, 255, 0, 0.467)',
          }}
        >
          <h2 style={{ margin: 0 }}>{t('Store.store_testing_warning.label')}</h2>
          <span>
            {`${t('Store.store_testing_warning.desc')} `}
            <ExternalLink
              href="https://decky.xyz/testing"
              target="_blank"
              style={{
                textDecoration: 'none',
              }}
            >
              decky.xyz/testing
            </ExternalLink>
          </span>
        </div>
      )}
      {staleStores.map(({ url, fetchedAt: savedAt }) => (
        <div
          key={url}
          style={{
            marginLeft: '20px',
            marginRight: '20px',
            marginBottom: '20px',
            padding: '8px 12px',
            color: '#fee75c',
            background: 'rgba(254, 231, 92, 0.08)',
            border: '2px solid rgba(254, 231, 92, 0.45)',
          }}
        >
          {/* React escapes the text; i18next escaping would show "/" as "&#x2F;". */}
          {t('Store.store_stale', {
            url: storeLabel(url),
            when: relativeTime(savedAt - now),
            interpolation: { escapeValue: false },
          })}
        </div>
      ))}
      {failedStores.map((url) => (
        <div
          key={url}
          style={{
            marginLeft: '20px',
            marginRight: '20px',
            marginBottom: '20px',
            padding: '8px 12px',
            background: 'rgba(255, 0, 0, 0.067)',
            border: '2px solid rgba(255, 0, 0, 0.467)',
          }}
        >
          {/* React escapes the text; i18next escaping would show "/" as "&#x2F;". */}
          {t('Store.store_unavailable', { url, interpolation: { escapeValue: false } })}
        </div>
      ))}
      <div>
        {!shownPlugins ? (
          <div style={{ height: '100%' }}>
            <SteamSpinner background="transparent" />
          </div>
        ) : shownPlugins.length === 0 ? (
          <div style={{ margin: '40px 20px', textAlign: 'center', color: '#8b929a' }}>{t('Store.store_no_match')}</div>
        ) : (
          shownPlugins.map((plugin: MergedStorePlugin) => (
            <PluginCard
              key={`${plugin.id}_${plugin.name}`}
              storePlugin={plugin}
              installedPlugin={installedPlugins.find((installedPlugin) => installedPlugin.name === plugin.name)}
            />
          ))
        )}
      </div>
    </>
  );
};

const AboutTab: FC<{}> = () => {
  const { t } = useTranslation();

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      <style>{`
              .deckyStoreAboutHeader {
                font-size: 24px;
                font-weight: 600;
                margin-top: 20px;
              }
            `}</style>
      <img
        src={logo}
        style={{
          width: '256px',
          height: 'auto',
          alignSelf: 'center',
        }}
      />
      <span className="deckyStoreAboutHeader">Testing</span>
      <span>
        {t('Store.store_testing_cta')}{' '}
        <ExternalLink
          href="https://decky.xyz/testing"
          target="_blank"
          style={{
            textDecoration: 'none',
          }}
        >
          decky.xyz/testing
        </ExternalLink>
      </span>
      <span className="deckyStoreAboutHeader">{t('Store.store_contrib.label')}</span>
      <span>{t('Store.store_contrib.desc')}</span>
      <span className="deckyStoreAboutHeader">{t('Store.store_source.label')}</span>
      <span>{t('Store.store_source.desc')}</span>
    </div>
  );
};

export default StorePage;
