import { ConfirmModal, Field, ProgressBar } from '@decky/ui';
import { FC, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { UI_COLORS } from '../../utils/uiStyles';
import PluginInstallProgress from './PluginInstallProgress';

interface TestingInstallVersion {
  id: number;
  name: string;
  author: string;
}

interface TestingInstallModalProps {
  version: TestingInstallVersion;
  onConfirm(): Promise<void>;
  closeModal?(): void;
}

const TestingInstallModal: FC<TestingInstallModalProps> = ({ version, onConfirm, closeModal }) => {
  const { t } = useTranslation();
  const [loading, setLoading] = useState<boolean>(false);
  const [reloading, setReloading] = useState<boolean>(false);
  const [percentage, setPercentage] = useState<number>(0);
  const [error, setError] = useState<string | null>(null);
  const finishedRef = useRef<boolean>(false);

  useEffect(() => {
    const progressListener = DeckyBackend.addEventListener('updater/update_download_percentage', (value) => {
      setPercentage(value);
    });
    const finishListener = DeckyBackend.addEventListener('updater/finish_download', () => {
      finishedRef.current = true;
      setReloading(true);
    });

    return () => {
      DeckyBackend.removeEventListener('updater/update_download_percentage', progressListener);
      DeckyBackend.removeEventListener('updater/finish_download', finishListener);
    };
  }, []);

  return (
    <ConfirmModal
      bOKDisabled={loading}
      bCancelDisabled={loading}
      bHideCloseIcon={loading}
      closeModal={closeModal}
      strTitle={version.name}
      strOKButtonText={loading ? t('Testing.installing') : t('Testing.download')}
      onOK={async () => {
        setError(null);
        setReloading(false);
        setPercentage(0);
        finishedRef.current = false;
        setLoading(true);
        try {
          await onConfirm();
          // Fork: the RPC resolves after the install handoff is queued, so the reload is next.
          setReloading(true);
        } catch (e) {
          // Fork: a dropped WebSocket after finish_download is the restart, not a failed download.
          if (finishedRef.current) return;
          setError(e instanceof Error ? `${e.name}: ${e.message}` : String(e));
          setReloading(false);
          setLoading(false);
        }
      }}
      onCancel={() => {
        if (!loading) closeModal?.();
      }}
    >
      <div
        style={{
          color: UI_COLORS.textSub,
          fontSize: '12px',
          fontWeight: 700,
          letterSpacing: '0.5px',
          textTransform: 'uppercase',
        }}
      >
        {'#' + version.id}
        {' · '}
        {t('Testing.author', { author: version.author })}
      </div>
      {loading &&
        (reloading ? (
          <div style={{ width: '100%', margin: '16px 0 0' }}>
            <Field bottomSeparator="none" childrenLayout="below" focusable={false} padding="standard">
              <div style={{ width: '100%' }}>
                <div style={{ minHeight: '22px', width: '100%', textAlign: 'left', textTransform: 'uppercase' }}>
                  {t('Testing.reloading')}
                </div>
                <ProgressBar focusable={false} indeterminate />
              </div>
            </Field>
          </div>
        ) : (
          <PluginInstallProgress
            percentage={percentage}
            operationText={t('Testing.start_download_toast', { id: version.id })}
          />
        ))}
      {error && (
        <div style={{ color: UI_COLORS.danger, marginTop: '16px', overflowWrap: 'anywhere' }}>
          {t('Testing.error')}: {error}
        </div>
      )}
    </ConfirmModal>
  );
};

export default TestingInstallModal;
