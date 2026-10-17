import { Field, Toggle } from '@decky/ui';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FaChrome } from 'react-icons/fa';

import { useSetting } from '../../../../utils/hooks/useSetting';

export default function RemoteDebuggingSettings() {
  const [allowRemoteDebugging, setAllowRemoteDebugging] = useSetting<boolean>('cef_forward', false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const { t } = useTranslation();

  return (
    <Field
      label={t('RemoteDebugging.remote_cef.label')}
      description={
        <span style={{ whiteSpace: 'pre-line' }}>
          {t('RemoteDebugging.remote_cef.desc')}
          {error && (
            <span role="alert" style={{ display: 'block' }}>
              {error}
            </span>
          )}
        </span>
      }
      icon={<FaChrome style={{ display: 'block' }} />}
    >
      <Toggle
        value={allowRemoteDebugging || false}
        disabled={busy}
        onChange={async (toggleValue) => {
          setBusy(true);
          setError(undefined);
          try {
            await DeckyBackend.call(
              toggleValue ? 'utilities/allow_remote_debugging' : 'utilities/disallow_remote_debugging',
            );
            await setAllowRemoteDebugging(toggleValue);
          } catch (failure) {
            setError(`Could not change remote debugging: ${String(failure)}`);
          } finally {
            setBusy(false);
          }
        }}
      />
    </Field>
  );
}
