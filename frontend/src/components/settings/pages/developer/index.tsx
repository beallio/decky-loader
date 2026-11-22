import {
  ConfirmModal,
  DialogBody,
  DialogButton,
  DialogControlsSection,
  DialogControlsSectionHeader,
  Field,
  Navigation,
  QuickAccessTab,
  TextField,
  ToggleField,
  showModal,
} from '@decky/ui';
import { FC, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FaFileArchive, FaLink, FaReact, FaSteamSymbol, FaTerminal } from 'react-icons/fa';

import { setShouldConnectToReactDevTools, setShowValveInternal } from '../../../../developer';
import { installFromURL } from '../../../../store';
import { useSetting } from '../../../../utils/hooks/useSetting';
import { getSetting } from '../../../../utils/settings';
import { UI_COLORS } from '../../../../utils/uiStyles';
import { FileSelectionType } from '../../../modals/filepicker';
import PluginInstallProgress from '../../../modals/PluginInstallProgress';
import RemoteDebuggingSettings from '../general/RemoteDebugging';

const getTabID = DeckyBackend.callable<[name: string], string>('utilities/get_tab_id');

const PluginURLModal: FC<{ closeModal?(): void }> = ({ closeModal }) => {
  const { t } = useTranslation();
  const [url, setURL] = useState('');
  const [loading, setLoading] = useState(false);
  const [percentage, setPercentage] = useState(0);
  const [downloadInfo, setDownloadInfo] = useState<string | null>(null);
  const trimmed = url.trim();
  const valid = trimmed.length > 0 && !/\s/.test(trimmed);

  useEffect(() => {
    function updateDownloadState(percent: number, transText: string | undefined, transInfo: Record<string, string>) {
      setPercentage(percent);
      setDownloadInfo(transText === undefined ? null : t(transText, transInfo));
    }
    DeckyBackend.addEventListener('loader/plugin_download_info', updateDownloadState);
    return () => {
      DeckyBackend.removeEventListener('loader/plugin_download_info', updateDownloadState);
    };
  }, [t]);

  return (
    <ConfirmModal
      strTitle={t('SettingsDeveloperIndex.third_party_plugins.label_url')}
      strOKButtonText={
        loading
          ? t('PluginInstallModal.install.button_processing')
          : t('SettingsDeveloperIndex.third_party_plugins.button_install')
      }
      bOKDisabled={!valid || loading}
      bCancelDisabled={loading}
      closeModal={closeModal}
      {...({ bCloseAfterOK: false } as any)}
      onOK={async () => {
        const targetURL = /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`;
        const splitURL = targetURL.replace(/\/+$/, '').split('/');
        const artifact = splitURL[splitURL.length - 1]?.replace(/\.zip$/i, '') || targetURL;
        setLoading(true);
        setPercentage(0);
        setDownloadInfo(null);
        try {
          await installFromURL(targetURL);
        } catch (e) {
          setLoading(false);
          setPercentage(0);
          setDownloadInfo(null);
          DeckyPluginLoader.toaster.toast({
            title: t('PluginInstallModal.failed', { artifact }),
            body: e instanceof Error ? e.message : String(e),
          });
          return;
        }
        closeModal?.();
        setTimeout(() => Navigation.OpenQuickAccessMenu(QuickAccessTab.Decky), 250);
        setTimeout(() => DeckyPluginLoader.checkPluginUpdates(), 1000);
      }}
    >
      <TextField
        label={t('SettingsDeveloperIndex.third_party_plugins.label_desc')}
        value={url}
        disabled={loading}
        onChange={(e) => setURL(e?.target.value ?? '')}
      />
      {loading && <PluginInstallProgress percentage={percentage} operationText={downloadInfo} />}
    </ConfirmModal>
  );
};

// Same input behavior as Steam's Settings > Internet manual IPv4 fields.
const isValidIPv4Input = (value: string) => {
  const octets = value.split('.');
  if (octets.length > 4) return false;
  return octets.every((octet, i) => {
    if (octet === '') return i === octets.length - 1;
    return /^\d+$/.test(octet) && Number(octet) <= 255;
  });
};

export default function DeveloperSettings() {
  const [enableValveInternal, setEnableValveInternal] = useSetting<boolean>('developer.valve_internal', false);
  const [reactDevtoolsEnabled, setReactDevtoolsEnabled] = useSetting<boolean>('developer.rdt.enabled', false);
  const [reactDevtoolsIP, setReactDevtoolsIP] = useSetting<string>('developer.rdt.ip', '');
  const { t } = useTranslation();

  const installFromZip = async () => {
    try {
      const path = await getSetting<string>('user_info.user_home', '');
      const file = await DeckyPluginLoader.openFilePicker(
        FileSelectionType.FILE,
        path,
        true,
        true,
        undefined,
        ['zip'],
        false,
        false,
      );
      await installFromURL(`file://${file.path}`);
    } catch (e) {
      if (e === 'User canceled') return;
      DeckyPluginLoader.toaster.toast({
        title: t('SettingsDeveloperIndex.third_party_plugins.zip_failed'),
        body: e instanceof Error ? e.message : String(e),
      });
    }
  };

  return (
    <DialogBody>
      <DialogControlsSection>
        <DialogControlsSectionHeader>
          {t('SettingsDeveloperIndex.third_party_plugins.header')}
        </DialogControlsSectionHeader>
        <Field
          label={t('SettingsDeveloperIndex.third_party_plugins.label_zip')}
          icon={<FaFileArchive style={{ display: 'block' }} />}
        >
          <DialogButton onClick={installFromZip}>
            {t('SettingsDeveloperIndex.third_party_plugins.button_zip')}
          </DialogButton>
        </Field>
        <Field
          label={t('SettingsDeveloperIndex.third_party_plugins.label_url')}
          icon={<FaLink style={{ display: 'block' }} />}
        >
          <DialogButton onClick={() => showModal(<PluginURLModal />)}>
            {t('SettingsDeveloperIndex.third_party_plugins.button_url')}
          </DialogButton>
        </Field>
      </DialogControlsSection>
      <DialogControlsSection>
        <DialogControlsSectionHeader>{t('SettingsDeveloperIndex.header')}</DialogControlsSectionHeader>
        <Field
          label={t('SettingsDeveloperIndex.cef_console.label')}
          description={<span style={{ whiteSpace: 'pre-line' }}>{t('SettingsDeveloperIndex.cef_console.desc')}</span>}
          icon={<FaTerminal style={{ display: 'block' }} />}
        >
          <DialogButton
            onClick={async () => {
              try {
                let tabId = await getTabID('SharedJSContext');
                Navigation.NavigateToExternalWeb(
                  'localhost:8080/devtools/inspector.html?ws=localhost:8080/devtools/page/' + tabId,
                );
              } catch (e) {
                console.error('Unable to find ID for SharedJSContext tab ', e);
                Navigation.NavigateToExternalWeb('localhost:8080');
              }
            }}
          >
            {t('SettingsDeveloperIndex.cef_console.button')}
          </DialogButton>
        </Field>
        <RemoteDebuggingSettings />
        <ToggleField
          label={t('SettingsDeveloperIndex.valve_internal.label')}
          description={
            <span style={{ whiteSpace: 'pre-line' }}>
              {t('SettingsDeveloperIndex.valve_internal.desc1')}{' '}
              <span style={{ color: UI_COLORS.danger }}>{t('SettingsDeveloperIndex.valve_internal.desc2')}</span>
            </span>
          }
          icon={<FaSteamSymbol style={{ display: 'block' }} />}
          checked={enableValveInternal}
          onChange={(toggleValue) => {
            setEnableValveInternal(toggleValue);
            setShowValveInternal(toggleValue);
          }}
        />
        <ToggleField
          label={t('SettingsDeveloperIndex.react_devtools.label')}
          description={
            <span style={{ whiteSpace: 'pre-line' }}>{t('SettingsDeveloperIndex.react_devtools.desc')}</span>
          }
          icon={<FaReact style={{ display: 'block' }} />}
          checked={reactDevtoolsEnabled}
          onChange={(toggleValue) => {
            setReactDevtoolsEnabled(toggleValue);
            setShouldConnectToReactDevTools(toggleValue);
          }}
        />
        <TextField
          label={t('SettingsDeveloperIndex.react_devtools.ip_label')}
          placeholder="   .   .   .   "
          value={reactDevtoolsIP}
          onChange={(e) => {
            if (isValidIPv4Input(e.target.value)) setReactDevtoolsIP(e.target.value);
            else e.target.value = reactDevtoolsIP;
          }}
        />
      </DialogControlsSection>
    </DialogBody>
  );
}
