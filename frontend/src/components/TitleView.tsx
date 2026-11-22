import { DialogButton, Focusable, Navigation, staticClasses } from '@decky/ui';
import { CSSProperties, FC } from 'react';
import { useTranslation } from 'react-i18next';
import { BsGearFill } from 'react-icons/bs';
import { FaArrowLeft, FaStore } from 'react-icons/fa';

import { COMPACT_ICON_BUTTON_STYLE } from '../utils/uiStyles';
import { useDeckyState } from './DeckyState';

const titleStyles: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: '8px',
  paddingRight: '16px',
  position: 'sticky',
  top: '0px',
};

const TitleView: FC = () => {
  const { activePlugin, closeActivePlugin } = useDeckyState();
  const { t } = useTranslation();

  const onSettingsClick = () => {
    Navigation.Navigate('/decky/settings');
    Navigation.CloseSideMenus();
  };

  const onStoreClick = () => {
    Navigation.Navigate('/decky/store');
    Navigation.CloseSideMenus();
  };

  if (activePlugin === null) {
    return (
      <Focusable style={titleStyles} className={staticClasses.Title}>
        <div style={{ marginRight: 'auto', flex: 0.9 }}>Decky</div>
        <DialogButton
          style={COMPACT_ICON_BUTTON_STYLE}
          onClick={onStoreClick}
          onOKActionDescription={t('TitleView.decky_store_desc')}
        >
          <FaStore style={{ display: 'block' }} />
        </DialogButton>
        <DialogButton
          style={COMPACT_ICON_BUTTON_STYLE}
          onClick={onSettingsClick}
          onOKActionDescription={t('TitleView.settings_desc')}
        >
          <BsGearFill style={{ display: 'block' }} />
        </DialogButton>
      </Focusable>
    );
  }

  return (
    <Focusable className={staticClasses.Title} style={titleStyles}>
      <DialogButton style={COMPACT_ICON_BUTTON_STYLE} onClick={closeActivePlugin}>
        <FaArrowLeft style={{ display: 'block' }} />
      </DialogButton>
      {activePlugin?.titleView || <div style={{ flex: 0.9 }}>{activePlugin.name}</div>}
    </Focusable>
  );
};

export default TitleView;
