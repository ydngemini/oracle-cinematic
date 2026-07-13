import { useCallback, useEffect, useMemo, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import styles from './PortfolioTab.module.css';

const number = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });

function Glyph({ children }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  );
}

const refreshGlyph = <Glyph><path d="M20.5 12a8.5 8.5 0 1 1-2.5-6" /><path d="M20.5 3.5V8H16" /></Glyph>;

function label(value) {
  return String(value || 'unknown').replaceAll('_', ' ');
}

function dateTime(value) {
  if (!value) return 'No date';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return 'No date';
  return new Intl.DateTimeFormat('en-US', {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  }).format(parsed);
}

function percent(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${Math.round(parsed * 100)}%` : '—';
}

function Metric({ name, value, tone }) {
  return (
    <div className={styles.metric} data-tone={tone || 'neutral'}>
      <dt>{name}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function Section({ title, count, children }) {
  return (
    <section className={styles.section} aria-labelledby={`portfolio-${title.replaceAll(' ', '-').toLowerCase()}`}>
      <header className={styles.sectionHead}>
        <h2 id={`portfolio-${title.replaceAll(' ', '-').toLowerCase()}`}>{title}</h2>
        <span aria-label={`${count} items`}>{number.format(count)}</span>
      </header>
      {children}
    </section>
  );
}

function Empty({ children }) {
  return <p className={styles.empty}>{children}</p>;
}

export default function PortfolioTab() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(() => {
    return crmGet('/api/portfolio').then(
      (payload) => {
        setData(payload);
        setError(null);
        setRefreshing(false);
      },
      (reason) => {
        setError(reason);
        setRefreshing(false);
      },
    );
  }, []);

  const refresh = () => {
    setRefreshing(true);
    load();
  };

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 60_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const metrics = data?.metrics || {};
  const urgentMilestones = useMemo(
    () => (data?.milestones || []).filter((item) => item.status === 'at_risk'),
    [data],
  );
  const loading = data === null && !error;

  return (
    <section className={styles.wrap} aria-label="Portfolio intelligence" aria-busy={loading || refreshing}>
      <header className={styles.hero}>
        <div>
          <span className={styles.kicker}>Portfolio intelligence</span>
          <h1>Execution desk</h1>
          <p>Contracts, response health, deadlines, and review queues in one tenant-scoped view.</p>
        </div>
        <button type="button" className={styles.refresh} onClick={refresh} disabled={refreshing} aria-label="Refresh portfolio">
          {refreshGlyph}
        </button>
      </header>

      {error && (
        <div className={styles.error} role="alert">
          <p>{error.message || 'Portfolio data is unavailable.'}</p>
          <button type="button" onClick={refresh}>Retry</button>
        </div>
      )}

      {loading ? (
        <div className={styles.skeletons} aria-hidden="true">
          <div /><div /><div />
        </div>
      ) : data ? (
        <>
          <dl className={styles.metrics} aria-label="Portfolio key metrics">
            <Metric name="Active contracts" value={number.format(metrics.active_contracts || 0)} />
            <Metric name="30d response" value={percent(metrics.response_rate_30d)} tone="good" />
            <Metric name="Silent 72h" value={number.format(metrics.ghosting_72h || 0)} tone={metrics.ghosting_72h ? 'warn' : 'neutral'} />
            <Metric name="Deadlines at risk" value={number.format(metrics.deadlines_at_risk || 0)} tone={metrics.deadlines_at_risk ? 'danger' : 'neutral'} />
            <Metric name="Title review" value={number.format(metrics.unreviewed_title_risks || 0)} tone={metrics.unreviewed_title_risks ? 'warn' : 'neutral'} />
            <Metric name="Zoning upside" value={number.format(metrics.zoning_opportunities || 0)} tone="good" />
          </dl>

          <Section title="Active contracts" count={(data.active_contracts || []).length}>
            {(data.active_contracts || []).length === 0 ? <Empty>No active contract windows.</Empty> : (
              <ul className={styles.cards}>
                {data.active_contracts.map((contract) => (
                  <li key={contract.id} className={styles.card}>
                    <div className={styles.cardTop}>
                      <strong>{contract.address || contract.parcel_id}</strong>
                      <span data-tone={Number(contract.days_remaining) <= 7 ? 'danger' : 'neutral'}>
                        {contract.days_remaining == null ? 'No deadline' : `${contract.days_remaining}d left`}
                      </span>
                    </div>
                    <p>{contract.seller_name || 'Seller not linked'}</p>
                    <small>{label(contract.dossier_status)} · expires {dateTime(contract.contract_expires_at)}</small>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section title="Milestones" count={(data.milestones || []).length}>
            {(data.milestones || []).length === 0 ? <Empty>No open transaction milestones.</Empty> : (
              <ul className={styles.rows}>
                {data.milestones.map((item) => (
                  <li key={item.id}>
                    <span className={styles.statusDot} data-status={item.status} aria-hidden="true" />
                    <div><strong>{item.title}</strong><small>{label(item.milestone_type)} · {item.assigned_to || 'unassigned'}</small></div>
                    <time dateTime={item.due_at || undefined}>{dateTime(item.due_at)}</time>
                  </li>
                ))}
              </ul>
            )}
            {urgentMilestones.length > 0 && <p className={styles.reviewNote}>{urgentMilestones.length} milestone{urgentMilestones.length === 1 ? '' : 's'} require immediate review.</p>}
          </Section>

          <Section title="Title review" count={(data.title_risks || []).length}>
            {(data.title_risks || []).length === 0 ? <Empty>No preliminary title findings awaiting review.</Empty> : (
              <ul className={styles.rows}>
                {data.title_risks.map((item, index) => (
                  <li key={`${item.property_key}:${item.finding_type}:${index}`}>
                    <span className={styles.statusDot} data-status="at_risk" aria-hidden="true" />
                    <div><strong>{item.property_key}</strong><small>{label(item.finding_type)} · {label(item.match_status)}{item.chain_gap ? ' · chain gap' : ''}</small></div>
                  </li>
                ))}
              </ul>
            )}
            <p className={styles.disclaimer}>Preliminary public-record intelligence is not an insured title search.</p>
          </Section>

          <Section title="Zoning opportunities" count={(data.zoning_opportunities || []).length}>
            {(data.zoning_opportunities || []).length === 0 ? <Empty>No reviewed buildable-area opportunities.</Empty> : (
              <ul className={styles.rows}>
                {data.zoning_opportunities.map((item) => (
                  <li key={item.id}>
                    <span className={styles.statusDot} data-status="complete" aria-hidden="true" />
                    <div><strong>{item.property_key}</strong><small>{item.zoning_district} · planning review {label(item.review_status)}</small></div>
                    <span className={styles.numeric}>{number.format(Number(item.remaining_buildable_sqft) || 0)} sf</span>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section title="Intelligence alerts" count={(data.intelligence_alerts || []).length}>
            {(data.intelligence_alerts || []).length === 0 ? <Empty>No low-confidence or review-required analyses in the last 30 days.</Empty> : (
              <ul className={styles.rows}>
                {data.intelligence_alerts.map((item) => (
                  <li key={item.id}>
                    <span className={styles.statusDot} data-status="at_risk" aria-hidden="true" />
                    <div><strong>{item.property_key}</strong><small>{label(item.analysis_type)} · model {item.model_version}</small></div>
                    <span className={styles.numeric}>{percent(item.confidence)}</span>
                  </li>
                ))}
              </ul>
            )}
          </Section>
        </>
      ) : null}
    </section>
  );
}
