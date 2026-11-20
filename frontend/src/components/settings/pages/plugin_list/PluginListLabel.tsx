import { FC } from 'react';
import { useTranslation } from 'react-i18next';
import { FaBan, FaEyeSlash, FaLock } from 'react-icons/fa';

import { UI_COLORS } from '../../../../utils/uiStyles';

interface PluginListLabelProps {
  frozen: boolean;
  hidden: boolean;
  disabled: boolean;
  name: string;
  version?: string;
}

const PluginListLabel: FC<PluginListLabelProps> = ({ name, frozen, hidden, version, disabled }) => {
  const { t } = useTranslation();
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
      <div>
        {name}
        {version && (
          <>
            {' - '}
            <span style={{ color: frozen ? UI_COLORS.textMuted : 'inherit' }}>
              {frozen && (
                <>
                  <FaLock />{' '}
                </>
              )}
              {version}
            </span>
          </>
        )}
      </div>
      {hidden && (
        <div
          style={{
            fontSize: '0.8rem',
            color: UI_COLORS.textPrimary,
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
          }}
        >
          <FaEyeSlash />
          {t('PluginListLabel.hidden')}
        </div>
      )}
      {disabled && (
        <div
          style={{
            fontSize: '0.8rem',
            color: UI_COLORS.textPrimary,
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
          }}
        >
          <FaBan />
          {t('PluginListLabel.disabled')}
        </div>
      )}
    </div>
  );
};

export default PluginListLabel;
