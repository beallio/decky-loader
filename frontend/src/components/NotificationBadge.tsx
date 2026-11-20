import { CSSProperties, FunctionComponent } from 'react';

import { UI_COLORS } from '../utils/uiStyles';

interface NotificationBadgeProps {
  show?: boolean;
  style?: CSSProperties;
}

const NotificationBadge: FunctionComponent<NotificationBadgeProps> = ({ show, style }) => {
  return show ? (
    <div
      style={{
        position: 'absolute',
        top: '8px',
        right: '8px',
        height: '10px',
        width: '10px',
        background: UI_COLORS.warning,
        borderRadius: '50%',
        ...style,
      }}
    />
  ) : null;
};

export default NotificationBadge;
