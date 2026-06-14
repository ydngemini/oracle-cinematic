import { useCallback, useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import { getUserId } from '../state/identity';
import styles from './LicenseStatusWidget.module.css';

const GLYPHS = {
  shield: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 3 5 6v5c0 4.4 3 7.5 7 9 4-1.5 7-4.6 7-9V6Z" />
    </svg>
  ),
  plus: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 5v14M5 12h14" />
    </svg>
  ),
};

function daysUntil(isoDate) {
  if (!isoDate) return null;
  const diff = new Date(isoDate).getTime() - Date.now();
  return Math.ceil(diff / 86400000);
}

function statusClass(days) {
  if (days === null) return '';
  if (days < 0) return styles.expired;
  if (days < 30) return styles.urgent;
  if (days < 90) return styles.warning;
  return styles.active;
}

function statusLabel(days) {
  if (days === null) return 'Unknown';
  if (days < 0) return 'Expired';
  if (days < 30) return `${days}d left`;
  if (days < 90) return `${days}d left`;
  return 'Active';
}

export function LicenseStatusWidget() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const agentId = getUserId();

  const fetch_ = useCallback(() => {
    if (!agentId) return;
    setLoading(true);
    crmGet(`/api/licensing/agent/${encodeURIComponent(agentId)}/status`).then(
      (res) => { setData(res); setLoading(false); setError(null); },
      (err) => { setError(err); setLoading(false); }
    );
  }, [agentId]);

  useEffect(() => { fetch_(); }, [fetch_]);

  const licenses = data?.licenses || [];
  const reciprocity = data?.reciprocity || [];

  return (
    <section className={styles.wrap} aria-label="License status">
      <header className={styles.header}>
        <span className={styles.headerGlyph}>{GLYPHS.shield}</span>
        <span className={styles.title}>State Licenses</span>
      </header>

      {loading ? (
        <div className={styles.skeleton} aria-hidden="true">
          <div className={styles.skelCard} />
          <div className={styles.skelCard} />
        </div>
      ) : error ? (
        <div className={styles.errorBox} role="alert">
          <span className={styles.errorText}>
            {error.status === 404 ? 'Licensing service not deployed.' : error.message}
          </span>
          <button type="button" className={styles.retryBtn} onClick={fetch_}>Retry</button>
        </div>
      ) : (
        <>
          <div className={styles.cards}>
            {licenses.map((lic) => {
              const days = daysUntil(lic.expiry_date);
              return (
                <div key={lic.id || lic.state_code} className={`${styles.card} ${statusClass(days)}`}>
                  <div className={styles.cardTop}>
                    <span className={styles.stateCode}>{lic.state_code}</span>
                    <span className={styles.statusBadge}>{statusLabel(days)}</span>
                  </div>
                  <span className={styles.licNum}>{lic.license_number || '—'}</span>
                  {lic.ce_earned != null && lic.ce_required != null && (
                    <div className={styles.ceWrap}>
                      <div className={styles.ceBar}>
                        <div
                          className={styles.ceFill}
                          style={{ width: `${Math.min(100, (lic.ce_earned / lic.ce_required) * 100)}%` }}
                        />
                      </div>
                      <span className={styles.ceLabel}>
                        {lic.ce_earned}/{lic.ce_required} CE hrs
                      </span>
                    </div>
                  )}
                  {lic.expiry_date && (
                    <span className={styles.expiry}>Expires {lic.expiry_date}</span>
                  )}
                </div>
              );
            })}
          </div>

          {reciprocity.length > 0 && (
            <div className={styles.reciprocity}>
              <span className={styles.recipLabel}>Reciprocity</span>
              <div className={styles.recipPills}>
                {reciprocity.map((code) => (
                  <span key={code} className={styles.recipPill}>{code}</span>
                ))}
              </div>
            </div>
          )}

          {licenses.length === 0 && (
            <p className={styles.emptyText}>No licenses on file.</p>
          )}

          <button type="button" className={styles.addBtn}>
            <span className={styles.addGlyph}>{GLYPHS.plus}</span>
            Add License
          </button>
        </>
      )}
    </section>
  );
}
