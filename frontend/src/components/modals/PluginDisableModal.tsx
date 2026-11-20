import { ConfirmModal, Spinner } from '@decky/ui';
import { FC, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { disablePlugin } from '../../plugin';

interface PluginDisableModalProps {
  name: string;
  title: string;
  buttonText: string;
  description: string;
  closeModal?(): void;
}

const PluginDisableModal: FC<PluginDisableModalProps> = ({ name, title, buttonText, description, closeModal }) => {
  const [disabling, setDisabling] = useState<boolean>(false);
  const { t } = useTranslation();
  return (
    <ConfirmModal
      closeModal={closeModal}
      onOK={async () => {
        setDisabling(true);
        try {
          await disablePlugin(name);
        } catch (e) {
          // Fork: a failed disable kept the modal open with both buttons disabled.
          DeckyPluginLoader.toaster.toast({
            title: t('PluginDisableModal.failed', { name, defaultValue: `Could not disable ${name}` }),
            body: e instanceof Error ? e.message : String(e),
          });
        }
        closeModal?.();
      }}
      bOKDisabled={disabling}
      bCancelDisabled={disabling}
      strTitle={
        <div style={{ display: 'flex', flexDirection: 'row', alignItems: 'center', width: '100%' }}>
          {title}
          {disabling && <Spinner width="24px" height="24px" style={{ marginLeft: 'auto' }} />}
        </div>
      }
      strOKButtonText={buttonText}
    >
      {description}
    </ConfirmModal>
  );
};

export default PluginDisableModal;
