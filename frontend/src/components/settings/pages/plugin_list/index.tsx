import {
  DialogBody,
  DialogButton,
  DialogControlsSection,
  Focusable,
  GamepadEvent,
  Menu,
  MenuItem,
  NavEntryPositionPreferences,
  ReorderableEntry,
  ReorderableList,
  showContextMenu,
} from '@decky/ui';
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FaDownload, FaEllipsisH, FaRecycle } from 'react-icons/fa';

import { InstallType, disablePlugin, enablePlugin } from '../../../../plugin';
import {
  StorePluginVersion,
  getPluginList,
  requestMultiplePluginInstalls,
  requestPluginInstall,
} from '../../../../store';
import { useSetting } from '../../../../utils/hooks/useSetting';
import { getPluginDisplayName } from '../../../../utils/pluginHelpers';
import { ICON_BUTTON_STYLE } from '../../../../utils/uiStyles';
import { useDeckyState } from '../../../DeckyState';
import PluginListLabel from './PluginListLabel';

async function reinstallPlugin(pluginName: string, currentVersion?: string) {
  const { plugins } = await getPluginList();
  const remotePlugin = plugins.find((x) => x.name == pluginName);
  if (remotePlugin && remotePlugin.versions?.length > 0) {
    const currentVersionData = remotePlugin.versions.find((version) => version.name == currentVersion);
    if (currentVersionData) requestPluginInstall(pluginName, currentVersionData, InstallType.REINSTALL);
  }
}

type PluginTableData = PluginData & {
  name: string;
  disabled: boolean;
  frozen: boolean;
  onFreeze(): void;
  onUnfreeze(): void;
  hidden: boolean;
  onHide(): void;
  onShow(): void;
  isDeveloper: boolean;
};

const reloadPluginBackend = DeckyBackend.callable<[pluginName: string], void>('loader/reload_plugin');

const TOP_BUTTON_STYLE = { width: 'auto', display: 'flex', alignItems: 'center', gap: '8px' };

function PluginInteractables(props: { entry: ReorderableEntry<PluginTableData> }) {
  const { t } = useTranslation();

  // nothing to display without this data...
  if (!props.entry.data) {
    return null;
  }

  const { name, update, version, onHide, onShow, hidden, onFreeze, onUnfreeze, frozen, isDeveloper, disabled } =
    props.entry.data;

  const showCtxMenu = (e: MouseEvent | GamepadEvent) => {
    showContextMenu(
      <Menu label={t('PluginListIndex.plugin_actions')}>
        {!disabled && (
          <MenuItem
            onSelected={async () => {
              try {
                await reloadPluginBackend(name);
              } catch (err) {
                console.error(`Error Reloading Plugin Backend for ${getPluginDisplayName(name, version)}`, err);
              }
            }}
          >
            {t('PluginListIndex.reload')}
          </MenuItem>
        )}
        <MenuItem
          onSelected={() =>
            DeckyPluginLoader.uninstallPlugin(
              name,
              t('PluginLoader.plugin_uninstall.title', { name }),
              t('PluginLoader.plugin_uninstall.button'),
              t('PluginLoader.plugin_uninstall.desc', { name }),
            )
          }
        >
          {t('PluginListIndex.uninstall')}
        </MenuItem>
        {disabled ? (
          <MenuItem onSelected={() => enablePlugin(name)}>{t('PluginListIndex.enable')}</MenuItem>
        ) : (
          <MenuItem
            onSelected={() =>
              DeckyPluginLoader.disablePlugin(
                name,
                t('PluginLoader.plugin_disable.title', { name }),
                t('PluginLoader.plugin_disable.button'),
                t('PluginLoader.plugin_disable.desc', { name }),
              )
            }
          >
            {t('PluginListIndex.disable')}
          </MenuItem>
        )}
        {!disabled &&
          (hidden ? (
            <MenuItem onSelected={onShow}>{t('PluginListIndex.show')}</MenuItem>
          ) : (
            <MenuItem onSelected={onHide}>{t('PluginListIndex.hide')}</MenuItem>
          ))}
        {frozen ? (
          <MenuItem onSelected={onUnfreeze}>{t('PluginListIndex.unfreeze')}</MenuItem>
        ) : (
          isDeveloper && <MenuItem onSelected={onFreeze}>{t('PluginListIndex.freeze')}</MenuItem>
        )}
      </Menu>,
      e.currentTarget ?? window,
    );
  };

  return (
    <Focusable navEntryPreferPosition={NavEntryPositionPreferences.MAINTAIN_X} style={{ display: 'flex', gap: '8px' }}>
      {update ? (
        <DialogButton
          style={{ height: '40px', minWidth: '60px' }}
          onClick={() => requestPluginInstall(name, update, InstallType.UPDATE)}
          onOKButton={() => requestPluginInstall(name, update, InstallType.UPDATE)}
        >
          <div style={{ display: 'flex', minWidth: '180px', justifyContent: 'space-between', alignItems: 'center' }}>
            {t('PluginListIndex.update_to', { name: update.name })}
            <FaDownload style={{ paddingLeft: '1rem' }} />
          </div>
        </DialogButton>
      ) : (
        <DialogButton
          style={{ height: '40px', minWidth: '60px' }}
          onClick={() => reinstallPlugin(name, version)}
          onOKButton={() => reinstallPlugin(name, version)}
        >
          <div style={{ display: 'flex', minWidth: '180px', justifyContent: 'space-between', alignItems: 'center' }}>
            {t('PluginListIndex.reinstall')}
            <FaRecycle style={{ paddingLeft: '1rem' }} />
          </div>
        </DialogButton>
      )}
      <DialogButton style={ICON_BUTTON_STYLE} onClick={showCtxMenu} onOKButton={showCtxMenu}>
        <FaEllipsisH style={{ display: 'block' }} />
      </DialogButton>
    </Focusable>
  );
}

type PluginData = {
  update?: StorePluginVersion;
  version?: string;
};

export default function PluginList({ isDeveloper }: { isDeveloper: boolean }) {
  const {
    plugins,
    installedPlugins,
    disabledPlugins,
    updates,
    pluginOrder,
    setPluginOrder,
    frozenPlugins,
    hiddenPlugins,
    sortPlugins,
  } = useDeckyState();
  const [_, setPluginOrderSetting] = useSetting<string[]>(
    'pluginOrder',
    installedPlugins.map((plugin) => plugin.name),
  );
  const { t } = useTranslation();

  useEffect(() => {
    DeckyPluginLoader.checkPluginUpdates();
  }, [installedPlugins, frozenPlugins]);

  const [pluginEntries, setPluginEntries] = useState<ReorderableEntry<PluginTableData>[]>([]);
  const [bulkBusy, setBulkBusy] = useState(false);
  const hiddenPluginsService = DeckyPluginLoader.hiddenPluginsService;
  const frozenPluginsService = DeckyPluginLoader.frozenPluginsService;

  const sortedPlugins = useMemo(() => {
    return sortPlugins
      ? [...installedPlugins].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }))
      : installedPlugins;
  }, [installedPlugins, sortPlugins]);

  useEffect(() => {
    setPluginEntries(
      installedPlugins.map(({ name, version }) => {
        const frozen = frozenPlugins.includes(name);
        const hidden = hiddenPlugins.includes(name);

        return {
          label: (
            <PluginListLabel
              name={name}
              frozen={frozen}
              hidden={hidden}
              version={version}
              disabled={disabledPlugins.find((p) => p.name == name) !== undefined}
            />
          ),
          position: sortPlugins ? sortedPlugins.findIndex((p) => p.name === name) : pluginOrder.indexOf(name),
          data: {
            name,
            disabled: disabledPlugins.some((disabledPlugin) => disabledPlugin.name === name),
            frozen,
            hidden,
            isDeveloper,
            version,
            update: updates?.get(name),
            onFreeze: () => frozenPluginsService.update([...frozenPlugins, name]),
            onUnfreeze: () => frozenPluginsService.update(frozenPlugins.filter((pluginName) => name !== pluginName)),
            onHide: () => hiddenPluginsService.update([...hiddenPlugins, name]),
            onShow: () => hiddenPluginsService.update(hiddenPlugins.filter((pluginName) => name !== pluginName)),
          },
        };
      }),
    );
  }, [installedPlugins, updates, hiddenPlugins, disabledPlugins, sortPlugins, sortedPlugins]);

  if (installedPlugins.length === 0) {
    return (
      <DialogBody>
        <p>{t('PluginListIndex.no_plugin')}</p>
      </DialogBody>
    );
  }

  function onSave(entries: ReorderableEntry<PluginTableData>[]) {
    if (sortPlugins) {
      return;
    }
    const newOrder = entries.map((entry) => entry.data!.name);
    console.log(newOrder);
    setPluginOrder(newOrder);
    setPluginOrderSetting(newOrder);
  }

  async function toggleAllPlugins(names: string[], enable: boolean) {
    setBulkBusy(true);
    // Sequential: enable/disable both reload plugins on the backend, so no concurrent reloads.
    for (const name of names) {
      try {
        await (enable ? enablePlugin(name) : disablePlugin(name));
      } catch (e) {
        DeckyPluginLoader.toaster.toast({
          title: t(enable ? 'PluginLoader.plugin_enable.error' : 'PluginLoader.plugin_disable.error', { name }),
          body: e instanceof Error ? e.message : String(e),
        });
      }
    }
    setBulkBusy(false);
  }

  return (
    <DialogBody>
      <Focusable style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', marginBottom: '12px' }}>
        <DialogButton
          disabled={bulkBusy || disabledPlugins.length === 0}
          onClick={() =>
            toggleAllPlugins(
              disabledPlugins.map((plugin) => plugin.name),
              true,
            )
          }
          style={TOP_BUTTON_STYLE}
        >
          {t('PluginListIndex.enable_all')}
        </DialogButton>
        <DialogButton
          disabled={bulkBusy || plugins.length === 0}
          onClick={() =>
            toggleAllPlugins(
              plugins.map((plugin) => plugin.name),
              false,
            )
          }
          style={TOP_BUTTON_STYLE}
        >
          {t('PluginListIndex.disable_all')}
        </DialogButton>
        {updates && updates.size > 0 && (
          <DialogButton
            onClick={() =>
              requestMultiplePluginInstalls(
                [...updates.entries()].map(([plugin, selectedVer]) => ({
                  installType: InstallType.UPDATE,
                  plugin,
                  selectedVer,
                })),
              )
            }
            style={TOP_BUTTON_STYLE}
          >
            {t('PluginListIndex.update_all', { count: updates.size })}
            <FaDownload style={{ display: 'block' }} />
          </DialogButton>
        )}
      </Focusable>
      <DialogControlsSection style={{ marginTop: 0 }}>
        <ReorderableList<PluginTableData>
          entries={pluginEntries}
          onSave={onSave}
          interactables={PluginInteractables}
          disableReordering={sortPlugins}
        />
      </DialogControlsSection>
    </DialogBody>
  );
}
