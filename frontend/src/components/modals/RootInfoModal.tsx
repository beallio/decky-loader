import { ConfirmModal } from '@decky/ui';
import { FC } from 'react';
import { useTranslation } from 'react-i18next';
import { FaExclamationTriangle } from 'react-icons/fa';

import { UI_COLORS } from '../../utils/uiStyles';

interface RootInfoModalProps {
  closeModal?(): void;
}

// Fork: explains root access in the store, so the user does not have to open deckbrew.xyz/root first.
const RootInfoModal: FC<RootInfoModalProps> = ({ closeModal }) => {
  const { t } = useTranslation();

  return (
    <ConfirmModal
      bAlertDialog
      closeModal={closeModal}
      strTitle={
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <FaExclamationTriangle style={{ color: UI_COLORS.warning, flexShrink: 0 }} />
          {t('RootInfoModal.title')}
        </div>
      }
      strOKButtonText={t('RootInfoModal.button_close')}
    >
      <p>{t('RootInfoModal.desc_definition')}</p>
      <p>{t('RootInfoModal.desc_usage')}</p>
      <p>{t('RootInfoModal.desc_review')}</p>
    </ConfirmModal>
  );
};

export default RootInfoModal;
