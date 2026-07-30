import { useCallback, useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import { useAssistant } from './AssistantContext';
import styles from './PortfolioTab.module.css';

const integer = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
const currency = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  notation: 'compact',
  maximumFractionDigits: 1,
});

function label(value) {
  return String(value || 'unknown').replaceAll('_', ' ');
}

function Metric({ name, value, tone = 'neutral', detail }) {
  return (
    <div className={styles.metric} data-tone={tone}>
      <dt>{name}</dt>
      <dd>{value}</dd>
      {detail && <small>{detail}</small>}
    </div>
  );
}

function ProgressTrack({ title, stages }) {
  const total = Math.max(1, stages.reduce((sum, stage) => sum + Number(stage.value || 0), 0));
  return (
    <section className={styles.progressTrack}>
      <header>
        <h3>{title}</h3>
        <span>{integer.format(total === 1 && stages.every((stage) => !stage.value) ? 0 : total)} deals</span>
      </header>
      <div className={styles.progressBar} aria-label={`${title} transaction stages`}>
        {stages.map((stage) => (
          <span
            key={stage.label}
            style={{ '--stage-width': `${Math.max(0, (Number(stage.value || 0) / total) * 100)}%` }}
            title={`${stage.label}: ${stage.value || 0}`}
          />
        ))}
      </div>
      <dl className={styles.progressLegend}>
        {stages.map((stage) => (
          <div key={stage.label}>
            <dt>{stage.label}</dt>
            <dd>{integer.format(stage.value || 0)}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

export default function PortfolioTab() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const { requestCommand, setOpen } = useAssistant();

  const load = useCallback(async () => {
    try {
      const payload = await crmGet('/api/portfolio/summary');
      setData(payload);
      setError(null);
    } catch (reason) {
      setError(reason);
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    const initialLoad = Promise.resolve().then(load);
    const timer = window.setInterval(load, 60_000);
    return () => {
      void initialLoad;
      window.clearInterval(timer);
    };
  }, [load]);

  const refresh = () => {
    setRefreshing(true);
    void load();
  };

  const metrics = data?.metrics || {};
  const milestones = data?.milestone_breakdown;
  const sellerStages = [
    { label: 'Prospecting', value: milestones?.sellers?.prospecting || 0 },
    { label: 'Under contract', value: milestones?.sellers?.under_contract || 0 },
    { label: 'Closed', value: milestones?.sellers?.closed || 0 },
  ];
  const buyerStages = [
    { label: 'Matched', value: milestones?.buyers?.matched || 0 },
    { label: 'Offer pending', value: milestones?.buyers?.offer_pending || 0 },
    { label: 'Under contract', value: milestones?.buyers?.under_contract || 0 },
    { label: 'Closed', value: milestones?.buyers?.closed || 0 },
  ];

  const draftFollowUp = (client) => {
    requestCommand({
      clientId: client.client_id,
      rawText: `Email ${client.name} with a concise follow-up about their ${client.stage} transaction and ask for the best next step.`,
    });
    setOpen(true);
  };

  const loading = data === null && !error;

  return (
    <section className={styles.wrap} aria-label="Portfolio analytics" aria-busy={loading || refreshing}>
      <header className={styles.hero}>
        <div>
          <span className={styles.kicker}>Portfolio · Live tenant data</span>
          <h1>Portfolio command desk</h1>
          <p>Contract exposure, client response health, transaction progress, and action-required signals.</p>
        </div>
        <button type="button" className={styles.refresh} onClick={refresh} disabled={refreshing} aria-label="Refresh portfolio">
          <span aria-hidden="true">↻</span>
        </button>
      </header>

      {error && (
        <div className={styles.error} role="alert">
          <p>{error.message || 'Portfolio data is unavailable.'}</p>
          <button type="button" onClick={refresh}>Retry</button>
        </div>
      )}

      {loading ? (
        <div className={styles.skeletons} aria-hidden="true"><div /><div /><div /></div>
      ) : data ? (
        <>
          <dl className={styles.metrics} aria-label="Portfolio key metrics">
            <Metric
              name="Active Contracts"
              value={integer.format(metrics.active_contracts || 0)}
              detail="Staged or under contract"
            />
            <Metric
              name="30-Day Response Rate"
              value={`${Number(metrics.response_rate_30d || 0).toFixed(1)}%`}
              tone={Number(metrics.response_rate_30d || 0) >= 80 ? 'good' : 'warn'}
              detail="Inbound vs outbound threads"
            />
            <Metric
              name="Active Volume"
              value={currency.format(Number(metrics.total_volume || 0))}
              detail="Current transaction value"
            />
            <Metric
              name="Ghosting Risk"
              value={integer.format(metrics.ghosting_alerts_count || 0)}
              tone={metrics.ghosting_alerts_count ? 'danger' : 'good'}
              detail="No response for 72+ hours"
            />
          </dl>

          <section className={styles.progressSection} aria-labelledby="transaction-progress-title">
            <header className={styles.sectionHead}>
              <div>
                <span className={styles.kicker}>Dual-track milestones</span>
                <h2 id="transaction-progress-title">Transaction progress</h2>
              </div>
            </header>
            <div className={styles.progressGrid}>
              <ProgressTrack title="Seller pipeline" stages={sellerStages} />
              <ProgressTrack title="Buyer pipeline" stages={buyerStages} />
            </div>
          </section>

          <section className={styles.actionSection} aria-labelledby="ghosting-title">
            <header className={styles.sectionHead}>
              <div>
                <span className={styles.kicker}>Action required</span>
                <h2 id="ghosting-title">Ghosting risk</h2>
              </div>
              <span>{integer.format((data.ghosting_clients || []).length)}</span>
            </header>
            {(data.ghosting_clients || []).length === 0 ? (
              <p className={styles.empty}>No active client has gone silent for more than 72 hours.</p>
            ) : (
              <ul className={styles.riskList}>
                {data.ghosting_clients.map((client) => (
                  <li key={client.client_id}>
                    <div>
                      <strong>{client.name}</strong>
                      <span>{client.stage} · {integer.format(client.last_contact_hours)}h since contact</span>
                    </div>
                    <button type="button" onClick={() => draftFollowUp(client)}>
                      Draft follow-up
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <div className={styles.lowerGrid}>
            <section className={styles.pulseSection} aria-labelledby="activity-title">
              <header className={styles.sectionHead}>
                <div>
                  <span className={styles.kicker}>Audit-backed</span>
                  <h2 id="activity-title">Live activity pulse</h2>
                </div>
              </header>
              {(data.activity_pulse || []).length === 0 ? (
                <p className={styles.empty}>No recent audited activity.</p>
              ) : (
                <ol className={styles.pulseList}>
                  {data.activity_pulse.slice(0, 8).map((event) => (
                    <li key={event.event_id}>
                      <span className={styles.pulseDot} aria-hidden="true" />
                      <div><strong>{label(event.action)}</strong><small>{label(event.category)}</small></div>
                      <time dateTime={event.created_at}>
                        {new Date(event.created_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}
                      </time>
                    </li>
                  ))}
                </ol>
              )}
            </section>

            <section className={styles.flagsSection} aria-labelledby="flags-title">
              <header className={styles.sectionHead}>
                <div>
                  <span className={styles.kicker}>Professional review</span>
                  <h2 id="flags-title">Title, zoning & distress flags</h2>
                </div>
                <span>{integer.format((data.intelligence_flags || []).length)}</span>
              </header>
              {(data.intelligence_flags || []).length === 0 ? (
                <p className={styles.empty}>No active intelligence flags.</p>
              ) : (
                <ul className={styles.flagList}>
                  {data.intelligence_flags.slice(0, 8).map((flag, index) => (
                    <li key={`${flag.type}:${flag.property_key}:${index}`}>
                      <span data-type={flag.type}>{flag.type}</span>
                      <div><strong>{flag.label || flag.property_key}</strong><small>{flag.property_key}</small></div>
                    </li>
                  ))}
                </ul>
              )}
              <p className={styles.disclaimer}>Signals are preliminary and require qualified professional review.</p>
            </section>
          </div>
        </>
      ) : null}
    </section>
  );
}
