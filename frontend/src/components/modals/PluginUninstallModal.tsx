import { ConfirmModal, Spinner } from '@decky/ui';
import { FC, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { uninstallPlugin } from '../../plugin';
import { DeckyState } from '../DeckyState';

interface PluginUninstallModalProps {
  deckyState: DeckyState;
  name: string;
  title: string;
  buttonText: string;
  description: string;
  closeModal?(): void;
}

const PluginUninstallModal: FC<PluginUninstallModalProps> = ({
  name,
  title,
  buttonText,
  description,
  deckyState,
  closeModal,
}) => {
  const [uninstalling, setUninstalling] = useState<boolean>(false);
  const { t } = useTranslation();
  return (
    <ConfirmModal
      closeModal={closeModal}
      onOK={async () => {
        setUninstalling(true);
        try {
          await uninstallPlugin(name);
          deckyState.setDisabledPlugins(deckyState.publicState().disabledPlugins.filter((d) => d.name !== name));
          // uninstalling a plugin resets the hidden setting for it server-side
          // we invalidate here so if you re-install it, you won't have an out-of-date hidden filter
          await DeckyPluginLoader.frozenPluginsService.invalidate();
          await DeckyPluginLoader.hiddenPluginsService.invalidate();
        } catch (e) {
          // Fork: a failed uninstall kept the window open with both buttons disabled.
          DeckyPluginLoader.toaster.toast({
            title: t('PluginUninstallModal.failed', { name }),
            body: e instanceof Error ? e.message : String(e),
          });
        }
        closeModal?.();
      }}
      bOKDisabled={uninstalling}
      bCancelDisabled={uninstalling}
      strTitle={
        <div style={{ display: 'flex', flexDirection: 'row', alignItems: 'center', width: '100%' }}>
          {title}
          {uninstalling && <Spinner width="24px" height="24px" style={{ marginLeft: 'auto' }} />}
        </div>
      }
      strOKButtonText={buttonText}
    >
      {description}
    </ConfirmModal>
  );
};

export default PluginUninstallModal;
