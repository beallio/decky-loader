import {
  DialogBody,
  DialogButton,
  DialogControlsSection,
  DialogControlsSectionHeader,
  Field,
  Focusable,
  NavEntryPositionPreferences,
  Navigation,
  ProgressBar,
  SteamSpinner,
} from '@decky/ui';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FaDownload, FaFlask, FaInfo } from 'react-icons/fa';

import { setSetting } from '../../../../utils/settings';
import { ICON_BUTTON_STYLE, UI_COLORS } from '../../../../utils/uiStyles';
import { UpdateBranch } from '../general/BranchSelect';

interface TestingVersion {
  id: number;
  name: string;
  link: string;
  head_sha: string;
  created_at: string;
  updated_at: string;
}

const formatDate = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });

const getTestingVersions = DeckyBackend.callable<[], TestingVersion[]>('updater/get_testing_versions');
const downloadTestingVersion = DeckyBackend.callable<[pr_id: number, sha: string]>('updater/download_testing_version');

export default function TestingVersionList() {
  const { t } = useTranslation();

  const [testingVersions, setTestingVersions] = useState<TestingVersion[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [updateProgress, setUpdateProgress] = useState<number | null>(null);
  const [reloading, setReloading] = useState<boolean>(false);

  useEffect(() => {
    (async () => {
      setTestingVersions(await getTestingVersions());
      setLoading(false);
    })();
  }, []);

  useEffect(() => {
    const a = DeckyBackend.addEventListener('updater/update_download_percentage', (percentage) => {
      setUpdateProgress(percentage);
    });

    const b = DeckyBackend.addEventListener('updater/finish_download', () => {
      setReloading(true);
    });

    return () => {
      DeckyBackend.removeEventListener('updater/update_download_percentage', a);
      DeckyBackend.removeEventListener('updater/finish_download', b);
    };
  }, []);

  if (loading) {
    return (
      <>
        <SteamSpinner background="transparent">{t('Testing.loading')}</SteamSpinner>
      </>
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
      {updateProgress !== null && <ProgressBar nProgress={updateProgress} indeterminate={reloading} />}
      <DialogControlsSection>
        <DialogControlsSectionHeader>{t('Testing.header')}</DialogControlsSectionHeader>
        <ul style={{ listStyleType: 'none', padding: '0' }}>
          {testingVersions.map((version) => {
            return (
              <li key={`${version.id}_${version.name}`}>
                <Field
                  label={
                    <>
                      <div>
                        {version.name}
                        <div style={{ color: UI_COLORS.textSub, fontSize: '12px' }}>
                          {t('Testing.opened', { date: formatDate(version.created_at) })}
                          {' · '}
                          {t('Testing.updated', { date: formatDate(version.updated_at) })}
                        </div>
                      </div>
                      <span
                        style={{
                          color: UI_COLORS.textSub,
                          whiteSpace: 'nowrap',
                          marginLeft: 'auto',
                          alignSelf: 'center',
                        }}
                      >
                        {'#' + version.id}
                      </span>
                    </>
                  }
                >
                  <Focusable
                    style={{ height: '40px', marginLeft: 'auto', display: 'flex', gap: '8px' }}
                    navEntryPreferPosition={NavEntryPositionPreferences.MAINTAIN_X}
                  >
                    <DialogButton
                      style={{ height: '40px', minWidth: '60px' }}
                      onClick={async () => {
                        const downloadToast = DeckyPluginLoader.toaster.toast({
                          title: t('Testing.start_download_toast', { id: version.id }),
                          body: null,
                          icon: <FaFlask />,
                        });
                        try {
                          await downloadTestingVersion(version.id, version.head_sha);
                          downloadToast.dismiss();
                          // Fork: switch the branch only when the download worked.
                          setSetting('branch', UpdateBranch.Testing);
                        } catch (e) {
                          if (e instanceof Error) {
                            DeckyPluginLoader.toaster.toast({
                              title: t('Testing.error'),
                              body: `${e.name}: ${e.message}`,
                              icon: <FaFlask />,
                            });
                          }
                        }
                      }}
                    >
                      <div
                        style={{
                          display: 'flex',
                          minWidth: '150px',
                          justifyContent: 'space-between',
                          alignItems: 'center',
                        }}
                      >
                        {t('Testing.download')}
                        <FaDownload style={{ paddingLeft: '1rem' }} />
                      </div>
                    </DialogButton>
                    <DialogButton
                      style={ICON_BUTTON_STYLE}
                      onClick={() => Navigation.NavigateToExternalWeb(version.link)}
                    >
                      <FaInfo style={{ display: 'block' }} />
                    </DialogButton>
                  </Focusable>
                </Field>
              </li>
            );
          })}
        </ul>
      </DialogControlsSection>
    </DialogBody>
  );
}
