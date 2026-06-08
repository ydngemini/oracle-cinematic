import { useState, useCallback } from 'react';
import { useSubscription } from '../state/useSubscription';
import styles from './BillingOverlay.module.css';

const FEATURES = [
  { id: 'graph',   label: 'Graph Engine',        detail: 'Cross-county relationship mapping' },
  { id: 'voice',   label: 'Voice Negotiation',   detail: 'Real-time AI call synthesis' },
  { id: 'legal',   label: 'Legal Matrix',         detail: 'Probate, lien & title intelligence' },
  { id: 'scout',   label: 'Scouting Matrix',      detail: 'Continuous public-record ingestion' },
  { id: 'stage',   label: 'Spatial Staging',      detail: 'WebGL property visualization' },
  { id: 'analyst', label: 'Novelty Analyst',      detail: 'Equity + life-event scoring' },
];

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

export function BillingOverlay() {
  const { active, status, plan, currentPeriodEnd, loading: subLoading, openPortal, tenantId } = useSubscription();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const handleSubscribe = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const res = await fetch(`${API_BASE}/billing/create-checkout-session`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tenant_id: tenantId }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? `Server error ${res.status}`);
      }

      const { url } = await res.json();
      window.location.href = url;
    } catch (err) {
      setError(err.message ?? 'Unknown error');
      setLoading(false);
    }
  }, [tenantId]);

  if (subLoading) return null;
  if (active) return null;

  return (
    <div
      className={styles.backdrop}
      data-visible="true"
      aria-hidden={false}
    >
      <div className={styles.panel} data-visible="true">

        <div className={styles.header}>
          <span className={styles.kicker}>Oracle Swarm — License Required</span>
          <h2 className={styles.heading}>Oracle Swarm License</h2>
          <p className={styles.subheading}>
            Activate full autonomous acquisition intelligence for this agent cluster.
          </p>
        </div>

        <div className={styles.priceBlock}>
          <span className={styles.currency}>$</span>
          <span className={styles.price}>299</span>
          <span className={styles.period}>/mo</span>
        </div>

        <ul className={styles.featureList}>
          {FEATURES.map(({ id, label, detail }) => (
            <li key={id} className={styles.featureItem}>
              <span className={styles.featureDot} />
              <span className={styles.featureLabel}>{label}</span>
              <span className={styles.featureDetail}>{detail}</span>
            </li>
          ))}
        </ul>

        <div className={styles.ctaWrapper}>
          {error && <p className={styles.errorMsg}>{error}</p>}

          <button
            className={styles.cta}
            onClick={handleSubscribe}
            disabled={loading}
            data-loading={loading}
          >
            {loading ? (
              <span className={styles.ctaSpinner} />
            ) : (
              'Activate License'
            )}
          </button>

          {status === 'canceled' && (
            <button className={styles.portalBtn} onClick={openPortal}>
              Reactivate via Portal
            </button>
          )}

          <p className={styles.fine}>
            Billed monthly. Cancel anytime. No setup fees.
          </p>
        </div>

      </div>
    </div>
  );
}
