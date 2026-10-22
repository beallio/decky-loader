import {
  ConfirmModal,
  DialogButton,
  Field,
  Focusable,
  NavEntryPositionPreferences,
  ReorderableEntry,
  ReorderableList,
  TextField,
  Toggle,
  showModal,
} from '@decky/ui';
import { FC, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FaEdit, FaPlus, FaTrash } from 'react-icons/fa';

import { getStoreSettings, saveStoreSettings } from '../../../../store';
import { StoreSettings, storeLabel } from '../../../../storeSources';

// Fork: an ordered list of stores (higher stores win) and a separate pre-release list,
// instead of the single Default/Testing/Custom choice. Adding a store shows no warning.

const StoreURLModal: FC<{
  title: string;
  initialURL: string;
  takenURLs: string[];
  onSave(url: string): void;
  closeModal?(): void;
}> = ({ title, initialURL, takenURLs, onSave, closeModal }) => {
  const { t } = useTranslation();
  const [url, setURL] = useState(initialURL);
  const trimmed = url.trim();
  const valid = /^https?:\/\/\S+$/.test(trimmed) && (trimmed === initialURL || !takenURLs.includes(trimmed));
  return (
    <ConfirmModal
      strTitle={title}
      bOKDisabled={!valid}
      closeModal={closeModal}
      onOK={() => {
        onSave(trimmed);
        closeModal?.();
      }}
    >
      <TextField label={t('StoreSelect.url_label')} value={url} onChange={(e) => setURL(e.target.value)} />
    </ConfirmModal>
  );
};

type StoreEntryData = { url: string; onEdit(): void; onRemove?(): void };

const iconButton = { minWidth: '40px', width: '40px', padding: '10px 12px' };

function StoreInteractables({ entry }: { entry: ReorderableEntry<StoreEntryData> }) {
  if (!entry.data) return null;
  const { onEdit, onRemove } = entry.data;
  return (
    <Focusable navEntryPreferPosition={NavEntryPositionPreferences.MAINTAIN_X} style={{ display: 'flex', gap: '6px' }}>
      <DialogButton style={iconButton} onClick={onEdit} onOKButton={onEdit}>
        <FaEdit />
      </DialogButton>
      {onRemove && (
        <DialogButton style={iconButton} onClick={onRemove} onOKButton={onRemove}>
          <FaTrash />
        </DialogButton>
      )}
    </Focusable>
  );
}

const StoreList: FC<{
  label: string;
  description: string;
  urls: string[];
  takenURLs: string[];
  minimum: number;
  onChange(urls: string[]): void;
}> = ({ label, description, urls, takenURLs, minimum, onChange }) => {
  const { t } = useTranslation();
  const addButton = useRef<HTMLDivElement>(null);
  const editURL = (title: string, initialURL: string, onSave: (url: string) => void) =>
    showModal(<StoreURLModal title={title} initialURL={initialURL} takenURLs={takenURLs} onSave={onSave} />);

  const entries: ReorderableEntry<StoreEntryData>[] = urls.map((url, position) => ({
    label: (
      <div>
        {storeLabel(url)}
        <div style={{ fontSize: '12px', color: '#969696', overflowWrap: 'anywhere' }}>{url}</div>
      </div>
    ),
    position,
    data: {
      url,
      onEdit: () =>
        editURL(t('StoreSelect.edit_store'), url, (edited) =>
          onChange(urls.map((other) => (other === url ? edited : other))),
        ),
      onRemove:
        urls.length > minimum
          ? () => {
              addButton.current?.focus();
              onChange(urls.filter((other) => other !== url));
            }
          : undefined,
    },
  }));

  return (
    <>
      <Field label={label} description={description}>
        <DialogButton
          ref={addButton}
          style={{ display: 'flex', alignItems: 'center', gap: '8px' }}
          onClick={() => editURL(t('StoreSelect.add_store'), '', (added) => onChange([...urls, added]))}
        >
          <FaPlus /> {t('StoreSelect.add_store')}
        </DialogButton>
      </Field>
      {/* Fork: the first store row touched the field above; match the 6px gap between rows and add a divider. */}
      <div style={{ marginTop: '6px', paddingTop: '6px', borderTop: '1px solid #3a4652' }}>
        <ReorderableList<StoreEntryData>
          entries={entries}
          onSave={(saved) => onChange(saved.map((entry) => entry.data!.url))}
          interactables={StoreInteractables}
          fieldProps={{ indentLevel: 1 }}
        />
      </div>
    </>
  );
};

const StoreSelect: FC<{}> = () => {
  const { t } = useTranslation();
  const [settings, setSettings] = useState<StoreSettings | null>(null);

  useEffect(() => {
    getStoreSettings().then(setSettings);
  }, []);

  if (!settings) return null;

  const update = (changes: Partial<StoreSettings>) => {
    const next = { ...settings, ...changes };
    setSettings(next);
    saveStoreSettings(next);
  };
  const takenURLs = [...settings.stores, ...settings.prereleaseStores];

  return (
    <>
      <StoreList
        label={t('StoreSelect.stores.label')}
        description={t('StoreSelect.stores.desc')}
        urls={settings.stores}
        takenURLs={takenURLs}
        minimum={1}
        onChange={(stores) => update({ stores })}
      />
      <Field label={t('StoreSelect.prereleases.label')} description={t('StoreSelect.prereleases.desc')}>
        <Toggle value={settings.includePrereleases} onChange={(includePrereleases) => update({ includePrereleases })} />
      </Field>
      {settings.includePrereleases && (
        <StoreList
          label={t('StoreSelect.prerelease_stores.label')}
          description={t('StoreSelect.prerelease_stores.desc')}
          urls={settings.prereleaseStores}
          takenURLs={takenURLs}
          minimum={0}
          onChange={(prereleaseStores) => update({ prereleaseStores })}
        />
      )}
    </>
  );
};

export default StoreSelect;
