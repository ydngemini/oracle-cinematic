import { useCallback, useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import styles from './AdminOpsTab.module.css';

/**
 * Whether each public-data source can actually answer right now.
 *
 * `GET /api/data/health` had no caller. That matters more than a typical gap,
 * because this is the endpoint that reports **credential expiry** — and Regrid's
 * token is a 30-day JWT that lapses monthly. When it lapses, every jurisdiction
 * quietly reports "0 of 8 core facts" and nothing else in the product says why.
 * The health payload knew; no screen asked it.
 *
 * `_regrid_health` carries a specific piece of history worth preserving in the
 * UI: it used to report `configured: bool(token)`, so a token that had expired a
 * week earlier still read as configured — the one health signal that existed
 * actively asserted the wrong thing. So this panel treats "expired" as its own
 * state, distinct from "not configured", and surfaces days-remaining before the
 * lapse rather than after.
 */

const LABELS = {
  census_geocoder: 'Census geocoder',
  openfema: 'FEMA',
  epa_envirofacts: 'EPA',
  eviction_lab: 'Eviction Lab',
  fbi_crime: 'FBI crime',
  courtlistener: 'CourtListener',
  regrid_parcel: 'Regrid parcels',
  bls_laus: 'BLS unemployment',
};

function toneFor(source) {
  if (source.credential_expired) return 'bad';
  if (source.configured === false) return 'warn';
  const days = source.credential_days_remaining;
  if (typeof days === 'number' && days <= 7) return 'warn';
  return 'good';
}

function stateOf(source) {
  if (source.credential_expired) return 'CREDENTIAL EXPIRED';
  if (source.configured === false) return 'not configured';
  const days = source.credential_days_remaining;
  if (typeof days === 'number') {
    return days <= 7 ? `expires in ${days}d` : `${days}d left`;
  }
  if (source.keyless_v1) return 'keyless';
  return 'ok';
}

export default function DataSourceHealthPanel() {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');

  const load = useCallback(() => {
    setError('');
    return crmGet('/api/data/health').then(
      (payload) => setData(payload || null),
      (reason) => setError(reason?.message || 'Data source health could not be read.'),
    );
  }, []);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => { void load(); });
    const timer = window.setInterval(load, 300_000);
    return () => { window.cancelAnimationFrame(frame); window.clearInterval(timer); };
  }, [load]);

  const sources = data?.sources || {};
  const names = Object.keys(sources);
  const broken = names.filter((n) => toneFor(sources[n]) === 'bad');

  return (
    <>
      {error ? <p className={styles.quietNote} role="alert">{error}</p> : null}

      {names.length === 0 && !error ? (
        <div className={styles.skelCol} aria-hidden="true"><div className={styles.skel} style={{ height: 90 }} /></div>
      ) : null}

      {names.length > 0 ? (
        <ul className={styles.rowList} role="list">
          {names.map((name) => {
            const source = sources[name] || {};
            return (
              <li key={name} className={styles.userRow}>
                <div className={styles.rowMain}>
                  <span className={styles.rowTitle}>{LABELS[name] || name}</span>
                  {source.credential_note ? (
                    <span className={styles.rowSub}>{source.credential_note}</span>
                  ) : null}
                </div>
                <span className={styles.roleChip} data-role={toneFor(source) === 'bad' ? 'admin' : 'std'}>
                  {stateOf(source)}
                </span>
              </li>
            );
          })}
        </ul>
      ) : null}

      {broken.length > 0 ? (
        <p className={styles.quietNote}>
          {broken.map((n) => LABELS[n] || n).join(', ')} cannot answer. A lapsed parcel credential
          is the usual reason a jurisdiction reports &ldquo;0 of 8 core facts&rdquo; — the property
          panel has no other way to tell you that, so check here first.
        </p>
      ) : null}
    </>
  );
}
