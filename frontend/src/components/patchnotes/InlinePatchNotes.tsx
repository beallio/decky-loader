import { Focusable, updaterFieldClasses } from '@decky/ui';
import { FunctionComponent, ReactNode } from 'react';

import { UI_COLORS } from '../../utils/uiStyles';

interface InlinePatchNotesProps {
  date: ReactNode;
  title: string;
  children: ReactNode;
  onClick?: () => void;
}

const InlinePatchNotes: FunctionComponent<InlinePatchNotesProps> = ({ date, title, children, onClick }) => {
  const hasSteamClasses = Boolean(updaterFieldClasses.PatchNotes);
  return (
    <Focusable
      className={updaterFieldClasses.PatchNotes}
      style={
        hasSteamClasses
          ? undefined
          : {
              background: UI_COLORS.surface,
              borderRadius: '4px',
              padding: '12px 16px',
              marginTop: '8px',
            }
      }
      onActivate={onClick}
    >
      <div
        className={updaterFieldClasses.PostedTime}
        style={hasSteamClasses ? undefined : { fontSize: '12px', color: UI_COLORS.textSub, marginBottom: '4px' }}
      >
        {date}
      </div>
      <div
        className={updaterFieldClasses.EventDetailTitle}
        style={
          hasSteamClasses
            ? undefined
            : { fontSize: '16px', fontWeight: 600, color: UI_COLORS.textPrimary, marginBottom: '8px' }
        }
      >
        {title}
      </div>
      <div
        className={updaterFieldClasses.EventDetailsBody}
        style={hasSteamClasses ? undefined : { fontSize: '13px', color: UI_COLORS.textBody }}
      >
        {children}
      </div>
    </Focusable>
  );
};

export default InlinePatchNotes;
