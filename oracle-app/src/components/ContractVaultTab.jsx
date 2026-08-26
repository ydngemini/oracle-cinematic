import { useCallback, useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import { ContractDraftWorkspace } from './ContractDraftWorkspace';
import GovInfoSearch from './GovInfoSearch';
import { PdfDocumentPicker } from './PdfDocumentPicker';
import StateDocumentChecklist from './StateDocumentChecklist';
import styles from './ContractVaultTab.module.css';

async function fetchVaultDocuments() {
  try {
    const result = await crmGet('/api/contracts/documents?limit=200');
    return {
      documents: Array.isArray(result?.documents) ? result.documents : [],
      error: '',
    };
  } catch {
    return {
      documents: [],
      error: 'Saved contract records are unavailable. Source PDFs can still be opened below.',
    };
  }
}

export default function ContractVaultTab({ embedded = false }) {
  const [documents, setDocuments] = useState(null);
  const [error, setError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [clients, setClients] = useState([]);
  const [selectedClientId, setSelectedClientId] = useState('');
  const Heading = embedded ? 'h2' : 'h1';

  const load = useCallback(async () => {
    setRefreshing(true);
    const next = await fetchVaultDocuments();
    setDocuments(next.documents);
    setError(next.error);
    setRefreshing(false);
  }, []);

  useEffect(() => {
    let active = true;
    void fetchVaultDocuments().then((next) => {
      if (!active) return;
      setDocuments(next.documents);
      setError(next.error);
    });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    crmGet('/api/crm/clients?type=all&sort=recent').then(
      (result) => {
        if (!active) return;
        const rows = Array.isArray(result?.clients) ? result.clients : [];
        setClients(rows);
        setSelectedClientId((current) => current || rows[0]?.id || '');
      },
      () => {
        if (active) setClients([]);
      },
    );
    return () => { active = false; };
  }, []);

  return (
    <section className={styles.wrap} aria-labelledby="contracts-title" aria-busy={documents === null || refreshing}>
      <header className={styles.hero}>
        <div>
          <span className={styles.kicker}>Contracts</span>
          <Heading id="contracts-title">PDF documents</Heading>
          <p>Choose a source-controlled form, verified public document, or an approved record from your private vault.</p>
        </div>
        <button className={styles.refresh} type="button" onClick={load} disabled={refreshing} aria-label="Refresh PDF documents">
          <span aria-hidden="true">↻</span>
        </button>
      </header>

      {error && <div className={styles.error} role="alert"><p>{error}</p><button type="button" onClick={load}>Retry</button></div>}

      {documents === null ? (
        <div className={styles.skeleton} aria-hidden="true" />
      ) : (
        <>
          <section className={styles.clientVault} aria-labelledby="client-vault-title">
            <div className={styles.clientVaultHeader}>
              <div>
                <span className={styles.kicker}>Per-client file</span>
                <h2 id="client-vault-title">State checklist & encrypted generation</h2>
              </div>
              <label>
                <span>Client</span>
                <select
                  value={selectedClientId}
                  onChange={(event) => setSelectedClientId(event.target.value)}
                  aria-label="Client for document generation"
                >
                  <option value="">Choose client</option>
                  {clients.map((client) => (
                    <option key={client.id} value={client.id}>{client.full_name}</option>
                  ))}
                </select>
              </label>
            </div>
            <StateDocumentChecklist clientId={selectedClientId} compact />
          </section>
          {/* Drafting. This tab could previously only LIST documents and open
              their PDFs — the whole template-library → draft → AI-complete →
              review path existed on the backend with no way in from the UI. */}
          <ContractDraftWorkspace surface="contracts" />
          <PdfDocumentPicker documents={documents} />
          <GovInfoSearch />
        </>
      )}
    </section>
  );
}
