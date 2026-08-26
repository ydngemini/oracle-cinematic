import { useEffect, useMemo, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import styles from './StateDocumentLibrary.module.css';

const DEFAULT_STATE = 'DE';

function readable(value, fallback = 'Reference') {
  if (!value) return fallback;
  return String(value).replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function stateLabel(state) {
  return state?.state_name ? `${state.state_name} (${state.state_code})` : state?.state_code;
}

/**
 * The regulatory facts behind a state, beside its document library.
 *
 * /api/states/{code}, its advertising rules, and the licensing requirements
 * endpoint all shipped with no caller — so the library could show WHICH forms a
 * state uses while the rules governing their use, and whether the agent is even
 * licensed to practise there, were unreachable.
 *
 * Each of the three resolves independently: a state whose licensing has not
 * been researched must not blank the advertising rules that have been.
 */
function StateRegulatoryFacts({ stateCode }) {
  const [profile, setProfile] = useState(null);
  const [ads, setAds] = useState(null);
  const [license, setLicense] = useState(null);

  useEffect(() => {
    let active = true;
    Promise.allSettled([
      crmGet(`/api/states/${encodeURIComponent(stateCode)}`),
      crmGet(`/api/states/${encodeURIComponent(stateCode)}/advertising-rules`),
      crmGet(`/api/licensing/requirements/${encodeURIComponent(stateCode)}?license_type=salesperson`),
    ]).then(([p, a, l]) => {
      if (!active) return;
      setProfile(p.status === 'fulfilled' ? p.value : null);
      setAds(a.status === 'fulfilled' && Array.isArray(a.value) ? a.value : []);
      setLicense(l.status === 'fulfilled' ? l.value : null);
    });
    return () => { active = false; };
  }, [stateCode]);

  // `null` on a researched-but-unknown field is meaningful here: migration 0069
  // NULLs the agency facts nobody has verified, precisely so the UI cannot
  // imply a form of agency is permitted somewhere it was never checked.
  const yesNo = (value) => (value === null || value === undefined ? 'Not researched' : value ? 'Yes' : 'No');

  return (
    <div className={styles.metadata}>
      {profile ? (
        <>
          <p><strong>{profile.state_name}</strong> · {profile.license_authority}</p>
          <p>
            Attorney review {yesNo(profile.attorney_review_required)} ·
            {' '}Mandatory disclosure {yesNo(profile.mandatory_disclosure)} ·
            {' '}TDS {yesNo(profile.has_tds)} ·
            {' '}Buyer agency agreement {yesNo(profile.buyer_agency_required)}
          </p>
          {profile.regulatory_url ? (
            <p><a href={profile.regulatory_url} target="_blank" rel="noreferrer">Regulator</a></p>
          ) : null}
        </>
      ) : null}

      {license ? (
        <p>
          Salesperson licence: {license.pre_license_hours} pre-licence hours ·
          {' '}{license.ce_hours_per_cycle} CE hours every {license.renewal_cycle_years} years ·
          {' '}E&amp;O {yesNo(license.errors_omissions_required)} ·
          {' '}sponsoring broker {yesNo(license.sponsoring_broker_required)}
        </p>
      ) : null}

      {ads && ads.length > 0 ? (
        <ul className={styles.notes}>
          {ads.slice(0, 6).map((rule) => (
            <li key={rule.rule_id}>
              <strong>{rule.category.replace(/_/g, ' ')}</strong> — {rule.requirement}
              {rule.enforcement_body ? ` (${rule.enforcement_body})` : ''}
            </li>
          ))}
        </ul>
      ) : null}
      {ads && ads.length === 0 ? (
        <p className={styles.notice}>No advertising rules recorded for this state.</p>
      ) : null}
    </div>
  );
}

export function StateDocumentLibrary() {
  const [states, setStates] = useState([]);
  const [stateCode, setStateCode] = useState(() => sessionStorage.getItem('oracle_contract_library_state') || DEFAULT_STATE);
  const [library, setLibrary] = useState(null);
  const [kind, setKind] = useState('all');
  const [selectedItemId, setSelectedItemId] = useState('');
  const [statesError, setStatesError] = useState('');
  const [libraryError, setLibraryError] = useState('');

  useEffect(() => {
    let active = true;
    crmGet('/api/states').then(
      (result) => {
        if (!active) return;
        const nextStates = Array.isArray(result) ? result : [];
        setStates(nextStates);
        setStateCode((current) => (
          nextStates.some((state) => state.state_code === current)
            ? current
            : nextStates[0]?.state_code || current
        ));
      },
      () => active && setStatesError('The state list is unavailable. Refresh to retry.'),
    );
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    crmGet(`/api/states/${encodeURIComponent(stateCode)}/document-library`).then(
      (result) => {
        if (!active) return;
        if (!result || !Array.isArray(result.items)) {
          setLibraryError('The document library returned an incomplete response.');
          return;
        }
        setLibrary(result);
      },
      () => active && setLibraryError('The selected state library is unavailable. Refresh to retry.'),
    );
    return () => { active = false; };
  }, [stateCode]);

  const visibleItems = useMemo(() => (
    (library?.state_code === stateCode ? library.items : []).filter((item) => kind === 'all' || item.kind === kind)
  ), [kind, library, stateCode]);

  const selectedItem = visibleItems.find((item) => item.item_id === selectedItemId) || visibleItems[0] || null;
  const activeLibrary = library?.state_code === stateCode ? library : null;
  const loading = activeLibrary === null && !libraryError;

  const selectState = (event) => {
    const nextState = event.target.value;
    sessionStorage.setItem('oracle_contract_library_state', nextState);
    setLibraryError('');
    setSelectedItemId('');
    setStateCode(nextState);
  };

  return (
    <section className={styles.library} aria-labelledby="state-document-library-title" aria-busy={loading}>
      <header className={styles.header}>
        <div>
          <span className={styles.kicker}>50-state source library</span>
          <h2 id="state-document-library-title">Pick a contract or document</h2>
          <p>Choose a jurisdiction, then inspect a source-controlled template or cited document reference. Use the AI draft workspace to preview, save, and download your working copy.</p>
        </div>
        {activeLibrary && <span className={styles.count}>{activeLibrary.total_contracts} contracts · {activeLibrary.total_documents} documents</span>}
      </header>

      {statesError && <p className={styles.error} role="alert">{statesError}</p>}
      {libraryError && <p className={styles.error} role="alert">{libraryError}</p>}

      <StateRegulatoryFacts stateCode={stateCode} />

      <div className={styles.controls}>
        <label>
          <span>State</span>
          <select value={stateCode} onChange={selectState} aria-label="Choose a state">
            {states.length === 0 ? <option value={stateCode}>{stateCode}</option> : states.map((state) => (
              <option key={state.state_code} value={state.state_code}>{stateLabel(state)}</option>
            ))}
          </select>
        </label>
        <label>
          <span>Type</span>
          <select value={kind} onChange={(event) => { setKind(event.target.value); setSelectedItemId(''); }} aria-label="Filter contracts or documents" disabled={loading}>
            <option value="all">Contracts & documents</option>
            <option value="contract">Contracts</option>
            <option value="document">Documents</option>
          </select>
        </label>
        <label>
          <span>Selection</span>
          <select value={selectedItem?.item_id || ''} onChange={(event) => setSelectedItemId(event.target.value)} disabled={loading || visibleItems.length === 0} aria-label="Choose a contract or document">
            {visibleItems.length === 0 ? <option value="">No matching sources</option> : visibleItems.map((item) => (
              <option key={item.item_id} value={item.item_id}>{readable(item.kind)} · {item.title}</option>
            ))}
          </select>
        </label>
      </div>

      {loading ? <div className={styles.skeleton} aria-hidden="true"><div /><div /></div> : selectedItem ? (
        <article className={styles.selection} aria-live="polite">
          <div className={styles.selectionTop}>
            <div>
              <span className={styles.type}>{readable(selectedItem.kind)}</span>
              <h3>{selectedItem.title}</h3>
              <p>{selectedItem.subtitle || 'Source reference'}</p>
            </div>
            <span className={styles.status} data-status={selectedItem.selection_status}>{readable(selectedItem.selection_status)}</span>
          </div>

          <dl className={styles.metadata}>
            <div><dt>Source</dt><dd>{selectedItem.source_name}</dd></div>
            <div><dt>Source status</dt><dd>{readable(selectedItem.source_status)}</dd></div>
            {selectedItem.version && <div><dt>Version</dt><dd>{selectedItem.version}</dd></div>}
            {selectedItem.effective_date && <div><dt>Effective</dt><dd>{selectedItem.effective_date}</dd></div>}
          </dl>

          {selectedItem.notes && <p className={styles.notes}>{selectedItem.notes}</p>}
          {selectedItem.citations?.length > 0 && <p className={styles.citations}><strong>Citations</strong> {selectedItem.citations.join(' · ')}</p>}

          <footer className={styles.selectionFooter}>
            <p>{selectedItem.attorney_review_required ? 'Source ready to inspect. Your working draft stays editable and saved separately in Personal AI.' : 'Verify the current source and jurisdiction details before using this reference.'}</p>
            {selectedItem.download_url ? <a href={selectedItem.download_url} target="_blank" rel="noopener noreferrer">Open source</a> : <span>Source link not registered</span>}
          </footer>
        </article>
      ) : (
        <p className={styles.empty}>No matching contract or document references are registered for this state yet.</p>
      )}

      {activeLibrary?.regulatory_url && <a className={styles.regulator} href={activeLibrary.regulatory_url} target="_blank" rel="noopener noreferrer">Open {activeLibrary.state_name} regulator</a>}
      {activeLibrary?.source_note && <p className={styles.notice}>{activeLibrary.source_note}</p>}
    </section>
  );
}
