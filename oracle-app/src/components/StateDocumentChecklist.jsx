import { useCallback, useEffect, useMemo, useState } from 'react';
import { crmGet, crmPost } from '../state/useCrmApi';
import styles from './StateDocumentChecklist.module.css';

const DEFAULT_STATE = 'DE';

function messageOf(error, fallback) {
  const detail = error?.detail ?? error?.message;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (detail && typeof detail === 'object') {
    const missing = detail.missing_variables;
    if (Array.isArray(missing) && missing.length) {
      return `Missing required record data: ${missing.join(', ')}`;
    }
  }
  return fallback;
}

function statusLabel(value) {
  const normalized = String(value || '').toUpperCase();
  if (normalized === 'ENCRYPTED_IN_VAULT') return 'Encrypted in Vault';
  if (normalized === 'LOCAL_PREVIEW_ONLY') return 'Local Preview Only';
  if (normalized === 'GENERATING') return 'Generating';
  if (normalized === 'AUTOFILL_READY') return 'Autofill Ready';
  if (normalized === 'FAILED') return 'Generation Failed';
  return 'Not Started';
}

export default function StateDocumentChecklist({
  clientId = '',
  stateCode = DEFAULT_STATE,
  compact = false,
}) {
  const [selectedState, setSelectedState] = useState(
    String(stateCode || DEFAULT_STATE).toUpperCase(),
  );
  const [catalog, setCatalog] = useState(null);
  const [artifacts, setArtifacts] = useState([]);
  const [busyDoc, setBusyDoc] = useState('');
  const [error, setError] = useState('');
  const [downloads, setDownloads] = useState({});

  useEffect(() => {
    const next = String(stateCode || DEFAULT_STATE).toUpperCase();
    let active = true;
    queueMicrotask(() => {
      if (active) setSelectedState((current) => (current === next ? current : next));
    });
    return () => { active = false; };
  }, [stateCode]);

  const load = useCallback(async () => {
    if (!/^[A-Z]{2}$/.test(selectedState)) return;
    setError('');
    try {
      const [documentResult, artifactResult] = await Promise.all([
        crmGet(`/api/compliance/documents/${encodeURIComponent(selectedState)}`),
        clientId
          ? crmGet(
            `/api/contracts/synthesis-artifacts?client_id=${encodeURIComponent(clientId)}&state_code=${encodeURIComponent(selectedState)}`,
          )
          : Promise.resolve({ artifacts: [] }),
      ]);
      setCatalog(documentResult);
      setArtifacts(Array.isArray(artifactResult?.artifacts) ? artifactResult.artifacts : []);
    } catch (reason) {
      setCatalog((current) => current || { state: selectedState, required_documents: [] });
      setError(messageOf(reason, 'The state document checklist is temporarily unavailable.'));
    }
  }, [clientId, selectedState]);

  useEffect(() => {
    if (!/^[A-Z]{2}$/.test(selectedState)) return undefined;
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      setCatalog(null);
      void load().catch(() => {
        if (active) setError('The state document checklist is temporarily unavailable.');
      });
    });
    return () => { active = false; };
  }, [load, selectedState]);

  const latestByTemplate = useMemo(() => {
    const next = new Map();
    artifacts.forEach((artifact) => {
      const key = artifact.template_key || artifact.doc_id;
      if (key && !next.has(key)) next.set(key, artifact);
    });
    return next;
  }, [artifacts]);

  const generate = async (document) => {
    if (!clientId || busyDoc) return;
    setBusyDoc(document.doc_id);
    setError('');
    try {
      const result = await crmPost('/api/contracts/synthesize', {
        client_id: clientId,
        doc_id: document.template_key || document.doc_id,
        state: selectedState,
      });
      if (result?.download_url) {
        setDownloads((current) => ({
          ...current,
          [result.document_id]: result.download_url,
        }));
      }
      await load();
    } catch (reason) {
      setError(messageOf(reason, 'The document could not be generated.'));
    } finally {
      setBusyDoc('');
    }
  };

  const prepareDownload = async (artifact) => {
    if (!artifact?.id || busyDoc) return;
    setBusyDoc(artifact.id);
    setError('');
    try {
      const result = await crmGet(
        `/api/contracts/synthesis-artifacts/${encodeURIComponent(artifact.id)}/download`,
      );
      setDownloads((current) => ({
        ...current,
        [artifact.id]: result.download_url,
      }));
    } catch (reason) {
      setError(messageOf(reason, 'A secure download link could not be issued.'));
    } finally {
      setBusyDoc('');
    }
  };

  const documents = Array.isArray(catalog?.required_documents)
    ? catalog.required_documents
    : [];

  return (
    <section className={`${styles.wrap} ${compact ? styles.compact : ''}`} aria-busy={catalog === null}>
      <header className={styles.header}>
        <div>
          <span className={styles.kicker}>State document vault</span>
          <h2>{clientId ? 'Client paperwork' : 'Compliance checklist'}</h2>
          <p>Only an exact, attorney-approved tenant template can be generated.</p>
        </div>
        <label className={styles.statePicker}>
          <span>State</span>
          <input
            value={selectedState}
            onChange={(event) => setSelectedState(event.target.value.toUpperCase().replace(/[^A-Z]/g, '').slice(0, 2))}
            inputMode="text"
            pattern="[A-Z]{2}"
            maxLength={2}
            aria-label="State code for document checklist"
          />
        </label>
      </header>

      {error && <p className={styles.error} role="alert">{error}</p>}
      {catalog === null ? (
        <div className={styles.loading} aria-label="Loading state documents" />
      ) : documents.length === 0 ? (
        <p className={styles.empty}>No cited document rules are currently registered for {selectedState}.</p>
      ) : (
        <ul className={styles.list}>
          {documents.map((document) => {
            const artifact = latestByTemplate.get(document.template_key || document.doc_id);
            const status = artifact?.status || document.status;
            const directDownload = downloads[artifact?.id] || downloads[artifact?.document_id];
            const generating = busyDoc === document.doc_id;
            const encrypted = String(status).toUpperCase() === 'ENCRYPTED_IN_VAULT';
            return (
              <li key={document.doc_id} className={styles.item}>
                <div className={styles.itemMain}>
                  <div className={styles.titleRow}>
                    <strong>{document.name}</strong>
                    {document.mandatory && <span className={styles.mandatory}>Required</span>}
                  </div>
                  <span className={styles.meta}>
                    {document.type.replaceAll('_', ' ')}
                    {document.version ? ` · v${document.version}` : ''}
                  </span>
                  <span className={styles.fields}>
                    Autofill: {document.ai_autofill_fields.join(', ')}
                  </span>
                </div>
                <div className={styles.actions}>
                  <span className={styles.status} data-status={String(status || '').toLowerCase()}>
                    {statusLabel(status)}
                  </span>
                  {document.generation_available && clientId && !encrypted && (
                    <button
                      type="button"
                      onClick={() => generate(document)}
                      disabled={Boolean(busyDoc)}
                    >
                      {generating ? 'Generating & encrypting…' : 'Generate & Encrypt Paper'}
                    </button>
                  )}
                  {encrypted && !directDownload && (
                    <button
                      type="button"
                      onClick={() => prepareDownload(artifact)}
                      disabled={Boolean(busyDoc)}
                    >
                      {busyDoc === artifact.id ? 'Securing link…' : 'Prepare Secure Download'}
                    </button>
                  )}
                  {encrypted && directDownload && (
                    <a href={directDownload} target="_blank" rel="noreferrer">
                      Download Secure PDF
                    </a>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
      {!clientId && (
        <p className={styles.note}>Select a client record to enable approved-template generation.</p>
      )}
    </section>
  );
}
