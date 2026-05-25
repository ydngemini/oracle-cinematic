import { useState, useCallback } from 'react';
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

/**
 * BillingOverlay
 *
 * Slides into view from below when `isActive` is false.
 * Calls /billing/create-checkout-session then redirects to Stripe.
 *
 * Props:
 *   isActive  {boolean}  — when false, overlay is shown
 *   agentId   {string}   — agent identifier forwarded to Stripe metadata
 */
export function BillingOverlay({ isActive = false, agentId = 'default' }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const handleSubscribe = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const res = await fetch(`${API_BASE}/billing/create-checkout-session`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent_id: agentId }),
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
  }, [agentId]);

  return (
    <div
      className={styles.backdrop}
      data-visible={!isActive}
      aria-hidden={isActive}
    >
      <div className={styles.panel} data-visible={!isActive}>

        {/* ── Kicker + heading ── */}
        <div className={styles.header}>
          <span className={styles.kicker}>Oracle Swarm — License Required</span>
          <h2 className={styles.heading}>Oracle Swarm License</h2>
          <p className={styles.subheading}>
            Activate full autonomous acquisition intelligence for this agent cluster.
          </p>
        </div>

        {/* ── Price block ── */}
        <div className={styles.priceBlock}>
          <span className={styles.currency}>$</span>
          <span className={styles.price}>299</span>
          <span className={styles.period}>/mo</span>
        </div>

        {/* ── Feature list ── */}
        <ul className={styles.featureList}>
          {FEATURES.map(({ id, label, detail }) => (
            <li key={id} className={styles.featureItem}>
              <span className={styles.featureDot} />
              <span className={styles.featureLabel}>{label}</span>
              <span className={styles.featureDetail}>{detail}</span>
            </li>
          ))}
        </ul>

        {/* ── CTA ── */}
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

          <p className={styles.fine}>
            Billed monthly. Cancel anytime. No setup fees.
          </p>
        </div>

      </div>
    </div>
  );
}
