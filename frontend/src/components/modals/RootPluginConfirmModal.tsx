import { ConfirmModal, TextField, ToggleField } from '@decky/ui';
import { FC, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FaExclamationTriangle } from 'react-icons/fa';

import { InstallType } from '../../plugin';

interface RootPluginConfirmModalProps {
  name: string;
  version: string;
  installType: InstallType;
  artifactHost: string;
  // How long the backend waits for the answer.
  validSeconds: number;
  // The sudo password to confirm, or null to cancel.
  onAnswer(password: string | null): Promise<unknown>;
  closeModal?(): void;
}

// Fork: every install or update of a plugin that needs root waits for this box.
// The user must wait, switch on the acknowledgement and enter the sudo password;
// the root plugin host checks the password.
const WAIT_SECONDS = 10;
// Close the box this long before the backend stops waiting, so a late click cannot be lost.
const EXPIRY_MARGIN_SECONDS = 5;

const RootPluginConfirmModal: FC<RootPluginConfirmModalProps> = ({
  name,
  version,
  installType,
  artifactHost,
  validSeconds,
  onAnswer,
  closeModal,
}) => {
  const { t } = useTranslation();
  const [waitTimer, setWaitTimer] = useState(WAIT_SECONDS);
  const [acknowledged, setAcknowledged] = useState(false);
  const [password, setPassword] = useState('');
  const answered = useRef(false);
  const isUpdate = installType === InstallType.UPDATE || installType === InstallType.DOWNGRADE;

  useEffect(() => {
    if (waitTimer <= 0) return;
    const timer = setTimeout(() => setWaitTimer(waitTimer - 1), 1000);
    return () => clearTimeout(timer);
  }, [waitTimer]);

  useEffect(() => {
    const expiry = setTimeout(
      () => {
        answered.current = true;
        closeModal?.();
        DeckyPluginLoader.toaster.toast({ title: t('RootPluginConfirm.expired', { name }), body: null });
      },
      Math.max(0, validSeconds - EXPIRY_MARGIN_SECONDS) * 1000,
    );
    return () => clearTimeout(expiry);
  }, []);

  const answer = (password: string | null) => {
    if (answered.current) return;
    answered.current = true;
    onAnswer(password).catch((e) =>
      DeckyPluginLoader.toaster.toast({
        title: t('RootPluginConfirm.failed', { name }),
        body: e instanceof Error ? e.message : String(e),
      }),
    );
  };

  return (
    <ConfirmModal
      bOKDisabled={waitTimer > 0 || !acknowledged || !password}
      closeModal={closeModal}
      onOK={() => answer(password)}
      onCancel={() => answer(null)}
      strTitle={
        <div>
          <FaExclamationTriangle /> {t('RootPluginConfirm.title', { name })}
        </div>
      }
      strOKButtonText={
        waitTimer > 0
          ? t('RootPluginConfirm.button_wait', { timer: waitTimer })
          : t(isUpdate ? 'RootPluginConfirm.button_update' : 'RootPluginConfirm.button_install')
      }
      strCancelButtonText={t('RootPluginConfirm.button_cancel')}
    >
      <div style={{ color: 'red' }}>{t('RootPluginConfirm.desc', { name, version })}</div>
      <div style={{ marginTop: '0.5em' }}>{t('RootPluginConfirm.source', { host: artifactHost })}</div>
      <ToggleField
        label={t('RootPluginConfirm.acknowledge', { name })}
        checked={acknowledged}
        onChange={setAcknowledged}
        bottomSeparator="none"
      />
      <TextField
        label={t('RootPluginConfirm.password')}
        bIsPassword
        value={password}
        onChange={(e) => setPassword(e.target.value)}
      />
    </ConfirmModal>
  );
};

export default RootPluginConfirmModal;
