import { useCallback, useEffect, useMemo, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import { zipMarkets } from '../lib/targetMarkets';
import styles from './TodayTab.module.css';

/**
 * The properties in the agent's own farm area, on the first screen they see.
 *
 * Onboarding (OnboardingGate) asks a new broker which ZIPs they work and saves
 * them to `/api/agents/profile`. Nothing read them back. So a broker answered a
 * wizard about their market and then faced an app with zero leads, zero clients
 * and zero listings — the moment a trial dies, and the only moment where the
 * 8.59M public records already in the catalog would have changed their mind.
 *
 * Nothing new was needed on the server: `mls_pipeline_search` already filters by
 * zip and answers in ~80ms. This is the join that was missing.
 *
 * What it deliberately does NOT do is dress the data up. The catalog is strong
 * on exactly three things — address (93.8%), owner name (89.3%) and assessed
 * value (87.7%) — and weak on beds, sqft and coordinates. Showing an owner and a
 * value is a real farming and skip-trace product; showing empty "beds" and
 * "sqft" columns beside them would make a complete record look like a broken one.
 */

const MAX_MARKETS = 3;
const PREVIEW_ROWS = 4;

function money(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) return null;
  return parsed.toLocaleString(undefined, {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 0,
  });
}

export default function MarketSnapshot() {
  const [markets, setMarkets] = useState(null);
  const [results, setResults] = useState({});
  const [error, setError] = useState('');

  const loadMarkets = useCallback(
    () =>
      crmGet('/api/crm/profile').then(
        // The row is wrapped: GET /api/crm/profile returns {"profile": {...}}.
        // Reading target_markets off the envelope yields undefined, which looks
        // exactly like "this broker set no markets" and silently hides the panel.
        (payload) => setMarkets(zipMarkets(payload?.profile?.target_markets)),
        (reason) => {
          setMarkets([]);
          setError(reason?.message || 'Your target markets could not be read.');
        },
      ),
    [],
  );

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => { void loadMarkets(); });
    return () => window.cancelAnimationFrame(frame);
  }, [loadMarkets]);

  const shown = useMemo(() => (markets || []).slice(0, MAX_MARKETS), [markets]);

  useEffect(() => {
    if (shown.length === 0) return undefined;
    let cancelled = false;
    // Sequential rather than parallel: three ZIPs is three cheap indexed
    // lookups, and firing them together only helps if the server is idle —
    // which, on the tab that also loads five other live sources, it is not.
    (async () => {
      for (const zip of shown) {
        try {
          const payload = await crmGet(
            `/api/mls/public-records?zip=${encodeURIComponent(zip)}`,
          );
          if (cancelled) return;
          setResults((prev) => ({
            ...prev,
            [zip]: {
              total: Number(payload?.total) || 0,
              rows: Array.isArray(payload?.listings) ? payload.listings : [],
            },
          }));
        } catch {
          if (cancelled) return;
          // One unreachable ZIP must not blank the others; it simply stays absent.
          setResults((prev) => ({ ...prev, [zip]: null }));
        }
      }
    })();
    return () => { cancelled = true; };
  }, [shown]);

  if (markets === null) return null;

  return (
    <section className={styles.responseStrip} aria-labelledby="market-snapshot-title">
      <h3 id="market-snapshot-title" className={styles.kicker}>Your market</h3>

      {error ? <p className={styles.brief} role="alert">{error}</p> : null}

      {shown.length === 0 ? (
        <p className={styles.brief}>
          No target ZIP codes on your profile yet. Add them under My Profile and the
          public record for every property in those ZIPs — owner, address and
          assessed value — shows up here.
        </p>
      ) : null}

      {shown.map((zip) => {
        const result = results[zip];
        if (result === undefined) {
          return (
            <p key={zip} className={styles.brief}>Reading the public record for {zip}…</p>
          );
        }
        if (result === null) {
          return (
            <p key={zip} className={styles.brief}>
              {zip} could not be read just now.
            </p>
          );
        }
        return (
          <div key={zip}>
            <p className={styles.brief}>
              <strong>{zip}</strong>
              {' · '}
              {result.total.toLocaleString()} propert{result.total === 1 ? 'y' : 'ies'} on
              public record
            </p>
            {result.rows.length === 0 ? (
              <p className={styles.brief}>
                Nothing published for this ZIP yet. Coverage is per county, so a
                neighbouring ZIP may still have records.
              </p>
            ) : (
              <ul>
                {result.rows.slice(0, PREVIEW_ROWS).map((row) => (
                  <li key={row.id}>
                    {row.address || 'address withheld'}
                    {row.owner_name ? ` · ${row.owner_name}` : ''}
                    {money(row.price) ? ` · assessed ${money(row.price)}` : ''}
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      })}

      {shown.length > 0 ? (
        <p className={styles.brief}>
          Owner and assessed value come from county public records, not an estimate.
          Beds, size and coordinates are missing on most of this catalog and are
          left out rather than shown blank.
        </p>
      ) : null}
    </section>
  );
}
