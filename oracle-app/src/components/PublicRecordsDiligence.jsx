import { useCallback, useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import styles from './DossierPanel.module.css';

/**
 * Keyless public-records diligence for one property.
 *
 * `data_sources_api` exposes eleven federal and public feeds — FEMA disaster
 * declarations, EPA regulated sites, eviction rates, BLS unemployment, FBI
 * crime, federal bankruptcy dockets, Regrid parcels — and not one of them had a
 * caller anywhere in the frontend. These are exactly the overlays an agent
 * wants beside a property before advising on it.
 *
 * Each feed answers independently. One provider being down, unconfigured, or
 * out of coverage must not blank the others, so every panel carries its own
 * state and says what happened rather than rendering an empty list that reads
 * as "nothing found". A missing API key and a genuine zero are different facts.
 */

// Only the feeds a property's state and ZIP can answer without further input.
// Eviction needs a tract FIPS and bankruptcy needs a court id; neither is
// derivable from what a dossier carries, so they are not requested here.
const FEEDS = [
  {
    id: 'fema',
    label: 'FEMA disaster declarations',
    path: ({ state }) => (state ? `/api/data/fema/disasters?state=${state}&top=5` : null),
    summarise: (payload) => {
      const rows = payload?.disasters || payload?.results || [];
      if (!Array.isArray(rows) || rows.length === 0) return 'No declarations returned.';
      const latest = rows[0];
      return `${rows.length} recent · latest ${latest?.incidentType || latest?.incident_type || 'incident'}`;
    },
  },
  {
    id: 'epa',
    label: 'EPA regulated sites',
    path: ({ zip }) => (zip ? `/api/data/epa/sites?zip=${zip}&rows=5` : null),
    summarise: (payload) => {
      const rows = payload?.sites || payload?.results || [];
      if (!Array.isArray(rows) || rows.length === 0) return 'None in this ZIP.';
      return `${rows.length} site${rows.length === 1 ? '' : 's'} in this ZIP`;
    },
  },
  {
    id: 'crime',
    label: 'FBI violent crime',
    path: ({ state }) => (state ? `/api/data/fbi/crime?state=${state}&offense=violent-crime` : null),
    summarise: (payload) => {
      const rows = payload?.series || payload?.results || payload?.data || [];
      if (!Array.isArray(rows) || rows.length === 0) return 'No series returned.';
      return `${rows.length} period${rows.length === 1 ? '' : 's'} of state-level data`;
    },
  },
  {
    id: 'jobs',
    label: 'BLS unemployment',
    path: ({ state }) => (state ? `/api/data/bls/unemployment?area=${state}` : null),
    summarise: (payload) => {
      const rows = payload?.series || payload?.observations || payload?.data || [];
      if (!Array.isArray(rows) || rows.length === 0) return 'No series returned.';
      const latest = rows[0];
      const value = latest?.value ?? latest?.rate;
      return value !== undefined ? `latest ${value}%` : `${rows.length} observations`;
    },
  },
];

function Feed({ feed, subject }) {
  const [state, setState] = useState({ status: 'idle', detail: '' });

  const load = useCallback(() => {
    const path = feed.path(subject);
    if (!path) {
      setState({
        status: 'skipped',
        detail: 'Not requested — this property has no state or ZIP recorded.',
      });
      return undefined;
    }
    setState({ status: 'loading', detail: '' });
    return crmGet(path).then(
      (payload) => setState({ status: 'ok', detail: feed.summarise(payload) }),
      (reason) => setState({
        status: 'error',
        // 503 from this API means a provider is unconfigured or its credential
        // has lapsed, which is an operator fact rather than a property fact.
        detail: reason?.status === 503
          ? (reason?.message || 'Source unavailable — check its credential.')
          : (reason?.message || 'Source did not answer.'),
      }),
    );
  }, [feed, subject]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => { void load(); });
    return () => window.cancelAnimationFrame(frame);
  }, [load]);

  return (
    <li>
      <strong>{feed.label}</strong>
      {' · '}
      {state.status === 'loading' ? 'checking…' : state.detail}
    </li>
  );
}

export default function PublicRecordsDiligence({ state, zip }) {
  const subject = { state: (state || '').trim().toUpperCase(), zip: (zip || '').trim() };
  if (!subject.state && !subject.zip) return null;

  return (
    <section className={styles.section} aria-label="Public records diligence">
      <h3 className={styles.kicker}>Public Records Diligence</h3>
      <ul className={styles.provenance}>
        {FEEDS.map((feed) => (
          <Feed key={feed.id} feed={feed} subject={subject} />
        ))}
      </ul>
    </section>
  );
}
