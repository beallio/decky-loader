import {
  DialogButton,
  Dropdown,
  Focusable,
  NavEntryPositionPreferences,
  Navigation,
  PanelSectionRow,
  SingleDropdownOption,
  SuspensefulImage,
  showModal,
} from '@decky/ui';
import { compare, validate } from 'compare-versions';
import { CSSProperties, FC, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  FaArrowDown,
  FaArrowUp,
  FaCheck,
  FaDownload,
  FaExclamationTriangle,
  FaGithub,
  FaInfoCircle,
  FaRecycle,
} from 'react-icons/fa';

import { DisabledPlugin, InstallType, Plugin } from '../../plugin';
import { requestPluginInstall } from '../../store';
import { MergedStorePlugin, storeLabel } from '../../storeSources';
import RootInfoModal from '../modals/RootInfoModal';

interface PluginCardProps {
  storePlugin: MergedStorePlugin;
  installedPlugin: Plugin | DisabledPlugin | undefined;
}

const PluginCard: FC<PluginCardProps> = ({ storePlugin, installedPlugin }) => {
  const [selectedOption, setSelectedOption] = useState<number>(0);
  const installedVersionIndex = storePlugin.versions.findIndex((version) => version.name === installedPlugin?.version);
  // Fork: versions from several stores are not in one newest-first order, so compare them when possible.
  const installedVersion = installedPlugin?.version;
  const selectedVersion = storePlugin.versions[selectedOption]?.name;
  const installType = !installedPlugin
    ? InstallType.INSTALL
    : selectedVersion === installedVersion
      ? InstallType.REINSTALL
      : installedVersion && validate(installedVersion) && validate(selectedVersion)
        ? compare(selectedVersion, installedVersion, '>')
          ? InstallType.UPDATE
          : InstallType.DOWNGRADE
        : installedVersionIndex < 0 // can happen if installed version is not in store
          ? InstallType.OVERWRITE
          : selectedOption < installedVersionIndex // index order is newest first
            ? InstallType.UPDATE
            : InstallType.DOWNGRADE;

  const root = storePlugin.tags.some((tag) => tag === 'root');
  // Stores may omit 'updated' or send a bad value; show no date then.
  const updated = new Date(storePlugin.updated ?? '');

  const { t } = useTranslation();

  return (
    <div
      className="deckyStoreCard"
      style={{
        marginLeft: '20px',
        marginRight: '20px',
        marginBottom: '20px',
        display: 'flex',
      }}
    >
      <div
        className="deckyStoreCardImageContainer"
        style={{
          width: '320px',
          height: '200px',
          position: 'relative',
        }}
      >
        <SuspensefulImage
          className="deckyStoreCardImage"
          suspenseHeight="200px"
          suspenseWidth="320px"
          style={{
            width: '320px',
            height: '200px',
            objectFit: 'cover',
          }}
          src={storePlugin.image_url}
        />
      </div>
      <div
        className="deckyStoreCardInfo"
        style={{
          width: 'calc(100% - 320px)', // The calc is here so that the info section doesn't expand into the image
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'space-between',
          marginLeft: '1em',
          gap: '10px',
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
          <span
            className="deckyStoreCardTitle"
            style={{
              fontSize: '1.25em',
              fontWeight: 'bold',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              width: '90%',
            }}
          >
            {storePlugin.name}
          </span>
          <span
            className="deckyStoreCardAuthor"
            style={{
              marginRight: 'auto',
              fontSize: '1em',
            }}
          >
            {storePlugin.author}
            <span style={{ marginLeft: '8px', fontSize: '12px', color: '#969696', whiteSpace: 'nowrap' }}>
              {storeLabel(storePlugin.store)}
            </span>
            {!isNaN(updated.getTime()) && (
              <span style={{ marginLeft: '8px', fontSize: '12px', color: '#1a9fff', whiteSpace: 'nowrap' }}>
                {/* React escapes the text; i18next escaping would show "/" in some date formats as "&#x2F;". */}
                {t('PluginCard.plugin_updated', {
                  date: updated.toLocaleDateString(undefined, { dateStyle: 'medium' }),
                  interpolation: { escapeValue: false },
                })}
              </span>
            )}
          </span>
          <span
            className="deckyStoreCardDescription"
            style={{
              fontSize: '13px',
              color: '#969696',
              WebkitLineClamp: root ? '2' : '3',
              WebkitBoxOrient: 'vertical',
              overflow: 'hidden',
              display: '-webkit-box',
            }}
          >
            {storePlugin.description ? (
              storePlugin.description
            ) : (
              <span>
                <i style={{ color: '#666' }}>{t('PluginCard.plugin_no_desc')}</i>
              </span>
            )}
          </span>
          {root && (
            <Focusable
              className="deckyStoreCardDescription deckyStoreCardDescriptionRoot"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
                marginTop: 'auto',
                padding: '4px 4px 4px 10px',
                fontSize: '13px',
                fontWeight: 'bold',
                color: '#fee75c',
                background: 'rgba(254, 231, 92, 0.12)',
                border: '1px solid rgba(254, 231, 92, 0.5)',
                borderRadius: '4px',
              }}
            >
              <FaExclamationTriangle style={{ flexShrink: 0 }} />
              <span style={{ flexGrow: 1 }}>{t('PluginCard.plugin_full_access')}</span>
              <DialogButton
                aria-label={t('PluginCard.plugin_full_access_info')}
                style={{ width: 'auto', minWidth: 0, height: '26px', padding: '0 10px', color: '#fee75c' }}
                onClick={() => showModal(<RootInfoModal />)}
              >
                <FaInfoCircle style={{ display: 'block' }} />
              </DialogButton>
            </Focusable>
          )}
          {storePlugin.shadowedByFailedStore && (
            <div
              className="deckyStoreCardDescription"
              style={{ fontSize: '13px', color: '#e5a50a', marginTop: 'auto' }}
            >
              <i>{t('PluginCard.plugin_store_shadowed')}</i>
            </div>
          )}
        </div>
        <div className="deckyStoreCardButtonRow">
          <PanelSectionRow>
            <Focusable
              style={{ display: 'flex', gap: '5px', padding: 0 }}
              navEntryPreferPosition={NavEntryPositionPreferences.MAINTAIN_X}
            >
              <div
                className="deckyStoreCardInstallContainer"
                style={
                  {
                    paddingTop: '0px',
                    paddingBottom: '0px',
                    flexGrow: 1,
                    '--field-negative-horizontal-margin': 0,
                  } as CSSProperties
                }
              >
                <DialogButton
                  onClick={() =>
                    requestPluginInstall(storePlugin.name, storePlugin.versions[selectedOption], installType)
                  }
                >
                  <span
                    className="deckyStoreCardInstallText"
                    style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '5px' }}
                  >
                    {installType === InstallType.UPDATE ? (
                      <>
                        <FaArrowUp /> {t('PluginCard.plugin_update')}
                      </>
                    ) : installType === InstallType.REINSTALL ? (
                      <>
                        <FaRecycle /> {t('PluginCard.plugin_reinstall')}
                      </>
                    ) : installType === InstallType.DOWNGRADE ? (
                      <>
                        <FaArrowDown /> {t('PluginCard.plugin_downgrade')}
                      </>
                    ) : installType === InstallType.OVERWRITE ? (
                      <>
                        <FaDownload /> {t('PluginCard.plugin_overwrite')}
                      </>
                    ) : (
                      // installType === InstallType.INSTALL (also fallback)
                      <>
                        <FaDownload /> {t('PluginCard.plugin_install')}
                      </>
                    )}
                  </span>
                </DialogButton>
              </div>
              {storePlugin.repoUrl && (
                <DialogButton
                  aria-label={t('PluginCard.plugin_source')}
                  style={{ width: 'auto', minWidth: 0, flexShrink: 0, padding: '0 14px' }}
                  onClick={() => Navigation.NavigateToExternalWeb(storePlugin.repoUrl!)}
                >
                  <FaGithub style={{ display: 'block' }} />
                </DialogButton>
              )}
              <div className="deckyStoreCardVersionContainer" style={{ minWidth: '130px' }}>
                <Dropdown
                  rgOptions={
                    storePlugin.versions.map((version, index) => ({
                      data: index,
                      label: (
                        <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                          {version.name}
                          {version.store !== storePlugin.store && version.store && (
                            <span style={{ fontSize: '11px', color: '#969696' }}>
                              {version.prerelease ? t('PluginCard.plugin_prerelease') : storeLabel(version.store)}
                            </span>
                          )}
                          {installedPlugin && installedVersionIndex === index ? <FaCheck /> : null}
                        </div>
                      ),
                    })) as SingleDropdownOption[]
                  }
                  menuLabel={t('PluginCard.plugin_version_label') as string}
                  selectedOption={selectedOption}
                  onChange={({ data }) => setSelectedOption(data)}
                />
              </div>
            </Focusable>
          </PanelSectionRow>
        </div>
      </div>
    </div>
  );
};

export default PluginCard;
