import {
  DialogBody,
  DialogButton,
  DialogControlsSection,
  DialogControlsSectionHeader,
  Field,
  Navigation,
  TextField,
  ToggleField,
} from '@decky/ui';
import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FaFileArchive, FaLink, FaReact, FaSteamSymbol, FaTerminal } from 'react-icons/fa';

import { setShouldConnectToReactDevTools, setShowValveInternal } from '../../../../developer';
import { installFromURL } from '../../../../store';
import { useSetting } from '../../../../utils/hooks/useSetting';
import { getSetting } from '../../../../utils/settings';
import { UI_COLORS } from '../../../../utils/uiStyles';
import { FileSelectionType } from '../../../modals/filepicker';
import RemoteDebuggingSettings from '../general/RemoteDebugging';

const getTabID = DeckyBackend.callable<[name: string], string>('utilities/get_tab_id');

export default function DeveloperSettings() {
  const [enableValveInternal, setEnableValveInternal] = useSetting<boolean>('developer.valve_internal', false);
  const [reactDevtoolsEnabled, setReactDevtoolsEnabled] = useSetting<boolean>('developer.rdt.enabled', false);
  const [reactDevtoolsIP, setReactDevtoolsIP] = useSetting<string>('developer.rdt.ip', '');
  const [pluginURL, setPluginURL] = useState('');
  const textRef = useRef<HTMLDivElement>(null);
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
          description={
            <TextField
              label={t('SettingsDeveloperIndex.third_party_plugins.label_desc')}
              value={pluginURL}
              onChange={(e) => setPluginURL(e?.target.value)}
            />
          }
          icon={<FaLink style={{ display: 'block' }} />}
        >
          <DialogButton
            disabled={pluginURL.length == 0}
            onClick={() => {
              if (/^https?:\/\//.test(pluginURL)) {
                installFromURL(pluginURL);
              } else {
                installFromURL('https://' + pluginURL);
              }
            }}
          >
            {t('SettingsDeveloperIndex.third_party_plugins.button_install')}
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
            <>
              <span style={{ whiteSpace: 'pre-line' }}>{t('SettingsDeveloperIndex.react_devtools.desc')}</span>
              <br />
              <br />
              <div ref={textRef}>
                <TextField
                  label={t('SettingsDeveloperIndex.react_devtools.ip_label')}
                  value={reactDevtoolsIP}
                  onChange={(e) => setReactDevtoolsIP(e?.target.value)}
                />
              </div>
            </>
          }
          icon={<FaReact style={{ display: 'block' }} />}
          checked={reactDevtoolsEnabled}
          onChange={(toggleValue) => {
            setReactDevtoolsEnabled(toggleValue);
            setShouldConnectToReactDevTools(toggleValue);
          }}
        />
      </DialogControlsSection>
    </DialogBody>
  );
}
