import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { crmDownload, crmGet, crmGetBlob } from '../state/useCrmApi';
import styles from './PdfDocumentPicker.module.css';
import { useAssistantRecord } from './AssistantContext';

const GROUP_ORDER = [
  'Source-controlled PDFs',
  'Official public PDFs',
  'Federal form portals',
  'Verified state PDFs',
  'Official form portals',
  'Licensed association forms',
  'My saved PDFs',
];
const FEDERAL_JURISDICTION = '__federal__';

function humanize(value, fallback = 'Contract document') {
  if (!value) return fallback;
  return String(value).replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function filenameFor(item) {
  const stem = `${item.title || 'neoh-document'}`
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-|-$)/g, '');
  return `${stem || 'neoh-document'}.pdf`;
}

function isSafeHttpsUrl(value) {
  try {
    return new URL(value).protocol === 'https:';
  } catch {
    return false;
  }
}

function vaultPdfItems(documents) {
  return documents
    .filter((document) => ['approved', 'signed'].includes(document.status))
    .map((document) => ({
      id: `vault:${document.id}`,
      group: 'My saved PDFs',
      kind: 'contract',
      title: humanize(document.document_type),
      subtitle: `${document.template_key || 'Saved contract'} · ${humanize(document.status)}`,
      source_name: 'Private vault',
      delivery: 'vault_pdf',
      document_id: document.id,
    }));
}

export function PdfDocumentPicker({ documents = [] }) {
  const [library, setLibrary] = useState([]);
  const [states, setStates] = useState([]);
  const [selectedStateCode, setSelectedStateCode] = useState('');
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState('');
  const [loadError, setLoadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [action, setAction] = useState('');
  const generatedUrl = useRef(null);

  const releaseGeneratedUrl = useCallback(() => {
    if (!generatedUrl.current) return;
    URL.revokeObjectURL(generatedUrl.current);
    generatedUrl.current = null;
  }, []);

  useEffect(() => {
    let active = true;
    crmGet('/api/contracts/pdf-library').then(
      (result) => {
        if (!active) return;
        setLibrary(Array.isArray(result?.items) ? result.items : []);
        setStates(Array.isArray(result?.states) ? result.states : []);
        setLoadError('');
        setLoading(false);
      },
      () => {
        if (!active) return;
        setLoadError('Document sources are unavailable. Your saved PDFs are still listed below when available.');
        setLoading(false);
      },
    );
    return () => { active = false; };
  }, []);

  useEffect(() => releaseGeneratedUrl, [releaseGeneratedUrl]);

  const items = useMemo(
    () => [...library, ...vaultPdfItems(documents)],
    [documents, library],
  );

  const federalItems = useMemo(
    () => items.filter((item) => item.authority_scope === 'federal'),
    [items],
  );
  const federalSelected = selectedStateCode === FEDERAL_JURISDICTION;

  const filteredItems = useMemo(
    () => (federalSelected
      ? federalItems
      : selectedStateCode
      ? items.filter((item) => !item.state_code || item.state_code === selectedStateCode)
      : items),
    [federalItems, federalSelected, items, selectedStateCode],
  );
  const selectedState = states.find((state) => state.state_code === selectedStateCode) || null;
  const selectedStateItems = selectedStateCode
    ? items.filter((item) => item.state_code === selectedStateCode)
    : [];

  const activeSelectedId = filteredItems.some((item) => item.id === selectedId)
    ? selectedId
    : (filteredItems[0]?.id || '');
  const selectedItem = filteredItems.find((item) => item.id === activeSelectedId) || null;
  useAssistantRecord(
    selectedItem?.delivery === 'vault_pdf' ? 'contract' : null,
    selectedItem?.delivery === 'vault_pdf' ? selectedItem.document_id : null,
    selectedItem?.delivery === 'vault_pdf' ? selectedItem.title : '',
    selectedItem?.delivery === 'vault_pdf' ? selectedItem.subtitle : '',
  );
  const groups = useMemo(() => GROUP_ORDER.map((group) => ({
    group,
    items: filteredItems.filter((item) => item.group === group),
  })).filter(({ items: groupItems }) => groupItems.length > 0), [filteredItems]);

  const resolvePdfUrl = useCallback(async (item) => {
    if (item.delivery === 'authenticated_pdf') {
      releaseGeneratedUrl();
      const blob = await crmGetBlob(item.pdf_url);
      if (blob.type && !blob.type.includes('pdf')) {
        throw new Error('The selected source did not return a PDF.');
      }
      const objectUrl = URL.createObjectURL(blob);
      generatedUrl.current = objectUrl;
      return objectUrl;
    }
    if (item.delivery === 'vault_pdf') {
      const result = await crmGet(`/api/contracts/documents/${encodeURIComponent(item.document_id)}/download`);
      if (!isSafeHttpsUrl(result?.url)) throw new Error('The secure PDF link is unavailable.');
      return result.url;
    }
    if (item.delivery === 'external_pdf' && isSafeHttpsUrl(item.pdf_url)) return item.pdf_url;
    if (item.delivery === 'source_link' && isSafeHttpsUrl(item.source_url)) return item.source_url;
    throw new Error('The selected item does not have a safe document link.');
  }, [releaseGeneratedUrl]);

  const openPdf = async () => {
    if (!selectedItem) return;
    const previewWindow = window.open('', '_blank');
    if (previewWindow) previewWindow.opener = null;
    setAction('open');
    setActionError('');
    try {
      const url = await resolvePdfUrl(selectedItem);
      if (previewWindow) {
        previewWindow.location.replace(url);
        return;
      }
      const link = window.document.createElement('a');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.click();
    } catch (error) {
      previewWindow?.close();
      setActionError(error?.message || 'Unable to open this PDF.');
    } finally {
      setAction('');
    }
  };

  const savePdf = async () => {
    if (!selectedItem) return;
    setAction('save');
    setActionError('');
    try {
      if (selectedItem.delivery === 'source_link') {
        throw new Error('This source is available through its authorized provider and cannot be saved from NEOH.');
      }
      if (selectedItem.download_url) {
        await crmDownload(selectedItem.download_url, filenameFor(selectedItem));
        return;
      }
      if (selectedItem.delivery === 'authenticated_pdf') {
        await crmDownload(selectedItem.pdf_url, filenameFor(selectedItem));
        return;
      }
      const url = await resolvePdfUrl(selectedItem);
      const link = window.document.createElement('a');
      link.href = url;
      link.download = filenameFor(selectedItem);
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.click();
    } catch (error) {
      setActionError(error?.message || 'Unable to save this PDF.');
    } finally {
      setAction('');
    }
  };

  const selectedItemIsSourceLink = selectedItem?.delivery === 'source_link';
  const openLabel = selectedItemIsSourceLink
    ? (selectedItem?.access_mode === 'licensed_association' ? 'Open licensed source' : 'Open official source')
    : 'Open PDF';

  return (
    <section className={styles.picker} aria-labelledby="pdf-document-picker-title" aria-busy={loading || Boolean(action)}>
      <div className={styles.heading}>
        <div>
          <span className={styles.kicker}>Contract &amp; document library</span>
          <h2 id="pdf-document-picker-title">Choose a form or document</h2>
        </div>
        <span className={styles.count}>
          {federalSelected
            ? `${federalItems.length} federal sources`
            : (selectedState ? `${selectedStateItems.length} state sources` : `${items.length} sources`)}
        </span>
      </div>

      <div className={styles.selectRow}>
        <label className={styles.selectLabel}>
          <span>Jurisdiction</span>
          <select
            value={selectedStateCode}
            onChange={(event) => { setSelectedStateCode(event.target.value); setSelectedId(''); setActionError(''); }}
            disabled={loading || states.length === 0}
            aria-label="Filter document sources by jurisdiction"
          >
            <option value="">All sources</option>
            <option value={FEDERAL_JURISDICTION}>United States federal · {federalItems.length} source{federalItems.length === 1 ? '' : 's'}</option>
            {states.map((state) => (
              <option key={state.state_code} value={state.state_code}>
                {state.state_name} ({state.state_code}) · {state.document_count} source{state.document_count === 1 ? '' : 's'}
              </option>
            ))}
          </select>
        </label>

        <label className={styles.selectLabel}>
          <span>Document or contract</span>
          <select
            value={activeSelectedId}
            onChange={(event) => { setSelectedId(event.target.value); setActionError(''); }}
            disabled={loading || filteredItems.length === 0}
            aria-label="Choose a document, contract, or approved source"
          >
            {filteredItems.length === 0 ? <option value="">No sources are available</option> : groups.map(({ group, items: groupItems }) => (
              <optgroup key={group} label={group}>
                {groupItems.map((item) => <option key={item.id} value={item.id}>{item.title} — {item.subtitle}</option>)}
              </optgroup>
            ))}
          </select>
        </label>
      </div>

      {selectedItem && (
        <div className={styles.selection} aria-live="polite">
          <div className={styles.selectionText}>
            <strong>{selectedItem.title}</strong>
            <span>{selectedItem.subtitle}</span>
            <small>{selectedItem.source_name}</small>
            {selectedItem.access_note && <p className={styles.accessNote}>{selectedItem.access_note}</p>}
          </div>
          <div className={styles.actions}>
            <button type="button" onClick={openPdf} disabled={Boolean(action)}>
              {action === 'open' ? 'Opening…' : openLabel}
            </button>
            {!selectedItemIsSourceLink && (
              <button type="button" className={styles.save} onClick={savePdf} disabled={Boolean(action)}>
                {action === 'save' ? 'Saving…' : 'Save PDF'}
              </button>
            )}
          </div>
        </div>
      )}

      {loadError && <p className={styles.error} role="alert">{loadError}</p>}
      {actionError && <p className={styles.error} role="alert">{actionError}</p>}
      {!loading && selectedState && selectedStateItems.length === 0 && !loadError && <p className={styles.empty}>No approved source is registered for {selectedState.state_name} yet. Federal and source-controlled PDFs remain available above.</p>}
      {!loading && federalSelected && federalItems.length === 0 && !loadError && <p className={styles.empty}>No approved federal source is registered yet.</p>}
      {!loading && !selectedItem && !loadError && <p className={styles.empty}>No approved documents or sources are registered for this tenant yet.</p>}
      <p className={styles.note}>All 50 states and federal sources are available. Public government PDFs can be opened and saved; association forms open their approved portal and may require a membership or license.</p>
    </section>
  );
}
