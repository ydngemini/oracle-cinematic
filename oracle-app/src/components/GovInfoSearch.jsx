import { useEffect, useMemo, useState } from 'react';
import { crmGet, crmPost } from '../state/useCrmApi';
import styles from './GovInfoSearch.module.css';

function safeGovInfoUrl(value, pdf = false) {
  try {
    const url = new URL(value);
    const approvedHost = url.protocol === 'https:' && ['govinfo.gov', 'www.govinfo.gov'].includes(url.hostname);
    const approvedPath = pdf
      ? url.pathname.startsWith('/content/pkg/') && url.pathname.toLowerCase().endsWith('.pdf')
      : url.pathname.startsWith('/app/details/');
    return approvedHost && approvedPath ? url.toString() : '';
  } catch {
    return '';
  }
}

function resultLabel(result) {
  const metadata = [result.collection_code, result.date_issued].filter(Boolean).join(' · ');
  return metadata ? `${result.title} — ${metadata}` : result.title;
}

export default function GovInfoSearch() {
  const [service, setService] = useState(null);
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [selectedId, setSelectedId] = useState('');
  const [searching, setSearching] = useState(false);
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    crmGet('/api/contracts/govinfo/status').then(
      (result) => { if (active) setService(result); },
      () => { if (active) setService({ available: false }); },
    );
    return () => { active = false; };
  }, []);

  const selected = useMemo(
    () => results.find((result) => result.access_id === selectedId) || results[0] || null,
    [results, selectedId],
  );

  if (!service?.available) return null;

  const search = async (event) => {
    event.preventDefault();
    const cleanQuery = query.trim();
    if (cleanQuery.length < 2) {
      setError('Enter a federal source search of at least two characters.');
      return;
    }
    setSearching(true);
    setError('');
    try {
      const response = await crmPost('/api/contracts/govinfo/search', { query: cleanQuery, page_size: 8 });
      const nextResults = Array.isArray(response?.results) ? response.results : [];
      setResults(nextResults);
      setSelectedId(nextResults[0]?.access_id || '');
      if (nextResults.length === 0) setError('No official federal sources matched that search.');
    } catch (requestError) {
      setError(requestError?.message || 'Federal source search is unavailable.');
    } finally {
      setSearching(false);
    }
  };

  const openPdf = async () => {
    if (!selected) return;
    const previewWindow = window.open('', '_blank');
    if (previewWindow) previewWindow.opener = null;
    setOpening(true);
    setError('');
    try {
      const result = await crmGet(`/api/contracts/govinfo/documents/${encodeURIComponent(selected.access_id)}`);
      const pdfUrl = safeGovInfoUrl(result?.pdf_url, true);
      if (!pdfUrl) throw new Error('GovInfo does not offer a direct PDF for this result.');
      if (previewWindow) previewWindow.location.replace(pdfUrl);
      else window.open(pdfUrl, '_blank', 'noopener,noreferrer');
    } catch (requestError) {
      previewWindow?.close();
      setError(requestError?.message || 'Unable to open this official PDF.');
    } finally {
      setOpening(false);
    }
  };

  const detailsUrl = safeGovInfoUrl(selected?.details_url);

  return (
    <section className={styles.search} aria-labelledby="federal-source-search-title" aria-busy={searching || opening}>
      <div className={styles.heading}>
        <div>
          <span className={styles.kicker}>Federal source search</span>
          <h2 id="federal-source-search-title">GovInfo</h2>
        </div>
        <span className={styles.badge}>.gov MCP</span>
      </div>
      <p>Search official federal publications and open a government PDF when one is available. These are source references, not state transaction forms.</p>

      <form className={styles.form} onSubmit={search}>
        <label>
          <span>Federal search</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            maxLength={320}
            placeholder="e.g. HUD lead disclosure regulation"
            autoComplete="off"
          />
        </label>
        <button type="submit" disabled={searching || opening}>
          {searching ? 'Searching…' : 'Search .gov'}
        </button>
      </form>

      {results.length > 0 && (
        <div className={styles.results}>
          <label>
            <span>Official result</span>
            <select value={selected?.access_id || ''} onChange={(event) => setSelectedId(event.target.value)} disabled={opening}>
              {results.map((result) => <option key={result.access_id} value={result.access_id}>{resultLabel(result)}</option>)}
            </select>
          </label>
          <div className={styles.actions}>
            {detailsUrl && <a href={detailsUrl} target="_blank" rel="noopener noreferrer">View source</a>}
            <button type="button" onClick={openPdf} disabled={opening}>
              {opening ? 'Opening…' : 'Open PDF'}
            </button>
          </div>
        </div>
      )}

      {error && <p className={styles.error} role="alert">{error}</p>}
      <small>U.S. Government Publishing Office · GovInfo MCP</small>
    </section>
  );
}
