import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPost } from '../state/useCrmApi';
import { useAssistant } from './AssistantContext';
import styles from './PortfolioTab.module.css';
import { friendlyError } from '../lib/errorMessages';

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

function Metric({ name, value, tone = 'neutral' }) {
  return (
    <div className={styles.metric} data-tone={tone}>
      <dt>{name}</dt>
      <dd>{value}</dd>
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

const REVIEW_STATE = { required: 'Awaiting review', confirmed: 'Confirmed', dismissed: 'Dismissed' };

// The submitter's claim about which property the record belongs to. The
// homeowner's dossier (SecureDossierPage titleMatchFlag) labels `matched` and
// `released` "Not verified" until confirmed, and every other status "Possible
// match" whatever the review says, so the reviewer must see which this is.
const CLAIMED_TITLE_MATCH = new Set(['matched', 'released']);
const TITLE_MATCH = {
  matched: 'Matched (not verified)',
  released: 'Released (not verified)',
  possible_match: 'Possible match',
  unresolved: 'Unresolved match',
};
const wholeDollars = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });

/** What a reviewer decides on: the match claim and the record, never notes. */
function titleEvidence(flag) {
  return [
    TITLE_MATCH[flag.match_status] || 'Possible match',
    flag.record_id && `Record ${flag.record_id}`,
    flag.amount != null && wholeDollars.format(Number(flag.amount)),
    // Calendar dates, shown as sent so they never shift a day.
    flag.recorded_at && `Recorded ${flag.recorded_at}`,
    flag.released_at && `Released ${flag.released_at}`,
  ].filter(Boolean).join(' · ');
}

function titleSubject(flag) {
  return `${flag.label || 'Title finding'} on ${flag.property_key}`;
}

const DECISION_COPY = {
  confirmed: {
    prompt: 'Why this record belongs to this property',
    effect: (flag) => (CLAIMED_TITLE_MATCH.has(flag.match_status)
      ? 'The homeowner’s dossier then shows it without the “Not verified” label.'
      : 'This is not a claimed match: the homeowner’s dossier keeps showing it as a “Possible match” after you confirm. Dismiss it if the record is not this property’s.'),
    submit: 'Confirm finding',
  },
  dismissed: {
    prompt: 'Why this record does not apply',
    effect: () => 'The homeowner’s dossier will no longer list it.',
    submit: 'Dismiss finding',
  },
};

/**
 * A broker owner's decision on one title finding (T11.03, F11.04). The server
 * takes a reason of 8-500 characters once whitespace is collapsed, and refuses
 * an agent, so an agent sees the state only. A saved decision, a 404 or a 409
 * reloads the list, which drops this row, so their outcome goes to `onSettled`
 * and the tab shows it; any other failure stays in the open form.
 */
function TitleFindingReview({ flag, canDecide, onSettled }) {
  const [decision, setDecision] = useState(null);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  // Saved, and shown as such until the refreshed list drops the finding.
  const [decided, setDecided] = useState(null);
  const cleaned = reason.split(/\s+/).filter(Boolean).join(' ');
  const status = decided || flag.review_status;
  const state = REVIEW_STATE[status] || label(status);

  if (!canDecide || !decision || decided) {
    return (
      <div className={styles.reviewActions}>
        <span className={styles.reviewLabel}>{state}</span>
        {canDecide && status === 'required' && (
          <>
            <button type="button" onClick={() => setDecision('confirmed')}>Confirm</button>
            <button type="button" onClick={() => setDecision('dismissed')}>Dismiss</button>
          </>
        )}
      </div>
    );
  }

  const copy = DECISION_COPY[decision];
  const cancel = () => {
    setDecision(null);
    setReason('');
    setError('');
  };
  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      await crmPost(`/api/intelligence/title/findings/${encodeURIComponent(flag.finding_id)}/review`, {
        decision,
        reason: cleaned,
      });
      setDecided(decision);
      onSettled({ findingId: flag.finding_id, subject: titleSubject(flag), text: `${REVIEW_STATE[decision]}.` });
    } catch (failure) {
      const message = friendlyError(failure, { fallback: 'The decision was not saved.' });
      if (failure?.status === 404 || failure?.status === 409) {
        // Someone else decided it, or it is gone: the list is stale either way.
        cancel();
        onSettled({ findingId: flag.finding_id, subject: titleSubject(flag), text: message, failed: true });
      } else {
        setError(message);
      }
    } finally {
      setBusy(false);
    }
  };
  const fieldId = `title-review-${flag.finding_id}`;
  return (
    <form className={styles.reviewForm} onSubmit={submit}>
      <label htmlFor={fieldId}>{copy.prompt}</label>
      <textarea
        id={fieldId}
        rows={2}
        maxLength={500}
        value={reason}
        onChange={(event) => setReason(event.target.value)}
      />
      <small>{copy.effect(flag)} At least 8 characters.</small>
      {error && <p role="alert">{error}</p>}
      <div>
        <button type="submit" disabled={busy || cleaned.length < 8}>{copy.submit}</button>
        <button type="button" onClick={cancel} disabled={busy}>Cancel</button>
      </div>
    </form>
  );
}

function canDecideTitle() {
  const role = sessionStorage.getItem('oracle_role');
  return role === 'broker_owner' || role === 'platform_admin';
}

export default function PortfolioTab() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  // The outcome of the latest title review. It lives here, not in the row:
  // the reload that follows drops the decided finding's row.
  const [titleNotice, setTitleNotice] = useState(null);
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

  const titleSettled = (notice) => {
    setTitleNotice(notice);
    refresh();
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
  const ghostingClients = data?.ghosting_clients || [];
  const intelligenceFlags = data?.intelligence_flags || [];
  const activityPulse = data?.activity_pulse || [];
  const primaryGhosting = ghostingClients.slice(0, 3);
  const primaryFlags = intelligenceFlags.slice(0, Math.max(0, 3 - primaryGhosting.length));
  const actionCount = ghostingClients.length + intelligenceFlags.length;
  const hiddenActionCount = Math.max(0, actionCount - primaryGhosting.length - primaryFlags.length);

  return (
    <section className={styles.wrap} aria-label="Portfolio analytics" aria-busy={loading || refreshing}>
      <header className={styles.hero}>
        <div>
          <span className={styles.kicker}>Today · Your brokerage’s live data</span>
          <h1>Portfolio</h1>
        </div>
        <button type="button" className={styles.refresh} onClick={refresh} disabled={refreshing} aria-label="Refresh portfolio">
          <span aria-hidden="true">↻</span>
        </button>
      </header>

      {error && (
        <div className={styles.error} role="alert">
          <p>{friendlyError(error, { fallback: 'Portfolio data is unavailable.' })}</p>
          <button type="button" onClick={refresh}>Retry</button>
        </div>
      )}

      {loading ? (
        <div className={styles.skeletons} aria-hidden="true">
          <div className={styles.skeletonMetrics}><span /><span /><span /></div>
          <div className={styles.skeletonActions}><span /><span /><span /></div>
        </div>
      ) : data ? (
        <>
          <dl className={styles.metrics} aria-label="Portfolio key metrics">
            <Metric
              name="Active Contracts"
              value={integer.format(metrics.active_contracts || 0)}
            />
            <Metric
              name="30-Day Response Rate"
              value={`${Number(metrics.response_rate_30d || 0).toFixed(1)}%`}
              tone={Number(metrics.response_rate_30d || 0) >= 80 ? 'good' : 'warn'}
            />
            <Metric
              name="Active Volume"
              value={currency.format(Number(metrics.total_volume || 0))}
            />
          </dl>

          <section className={styles.actionSection} aria-labelledby="next-actions-title">
            <header className={styles.sectionHead}>
              <div>
                <span className={styles.kicker}>Priority queue</span>
                <h2 id="next-actions-title">Next actions</h2>
              </div>
              <span aria-label={`${actionCount} actions requiring review`}>{integer.format(actionCount)}</span>
            </header>
            {actionCount === 0 ? (
              <div className={styles.clearState} role="status">
                <span aria-hidden="true">✓</span>
                <div><strong>No urgent actions</strong><small>Client follow-ups and property signals are clear.</small></div>
              </div>
            ) : (
              <ul className={styles.actionList}>
                {primaryGhosting.map((client) => (
                  <li key={client.client_id}>
                    <span className={styles.actionGlyph} aria-hidden="true">◎</span>
                    <div>
                      <strong>{client.name}</strong>
                      <small>{label(client.stage)} · {integer.format(client.last_contact_hours)}h since contact</small>
                    </div>
                    <button type="button" onClick={() => draftFollowUp(client)}>
                      Draft follow-up
                    </button>
                  </li>
                ))}
                {primaryFlags.map((flag, index) => (
                  <li key={`${flag.type}:${flag.property_key}:${index}`}>
                    <span className={styles.actionGlyph} aria-hidden="true">!</span>
                    <div>
                      <strong>{flag.label || flag.property_key}</strong>
                      <small>{label(flag.type)} · Professional review</small>
                    </div>
                    <span className={styles.reviewLabel}>Review</span>
                  </li>
                ))}
              </ul>
            )}
            {hiddenActionCount > 0 && <p className={styles.moreActions}>+ {integer.format(hiddenActionCount)} more in details</p>}
          </section>

          <details className={styles.details}>
            <summary>
              <span>View details</span>
              <small>Milestones, activity and intelligence</small>
            </summary>
            <div className={styles.detailsBody}>
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

              {ghostingClients.length > primaryGhosting.length && (
                <section className={styles.actionDetailSection} aria-labelledby="all-follow-ups-title">
                  <header className={styles.sectionHead}>
                    <div>
                      <span className={styles.kicker}>All follow-ups</span>
                      <h2 id="all-follow-ups-title">Client response risk</h2>
                    </div>
                  </header>
                  <ul className={styles.riskList}>
                    {ghostingClients.map((client) => (
                      <li key={client.client_id}>
                        <div>
                          <strong>{client.name}</strong>
                          <span>{label(client.stage)} · {integer.format(client.last_contact_hours)}h since contact</span>
                        </div>
                        <button type="button" onClick={() => draftFollowUp(client)}>Draft follow-up</button>
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              <div className={styles.lowerGrid}>
                <section className={styles.pulseSection} aria-labelledby="activity-title">
                  <header className={styles.sectionHead}>
                    <div>
                      <span className={styles.kicker}>Audit-backed</span>
                      <h2 id="activity-title">Live activity pulse</h2>
                    </div>
                  </header>
                  {activityPulse.length === 0 ? (
                    <p className={styles.empty}>No recent activity yet.</p>
                  ) : (
                    <ol className={styles.pulseList}>
                      {activityPulse.slice(0, 8).map((event) => (
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
                    <span>{integer.format(intelligenceFlags.length)}</span>
                  </header>
                  {titleNotice && (
                    <div
                      key={titleNotice.findingId}
                      className={styles.reviewNotice}
                      data-tone={titleNotice.failed ? 'error' : 'done'}
                      role={titleNotice.failed ? 'alert' : 'status'}
                    >
                      <p><strong>{titleNotice.subject}</strong> {titleNotice.text}</p>
                      <button type="button" onClick={() => setTitleNotice(null)}>Close</button>
                    </div>
                  )}
                  {intelligenceFlags.length === 0 ? (
                    <p className={styles.empty}>No active intelligence flags.</p>
                  ) : (
                    <ul className={styles.flagList}>
                      {intelligenceFlags.slice(0, 8).map((flag, index) => (
                        // A finding's own id keys its row, so an open review
                        // form stays on its finding when the list refreshes.
                        <li key={flag.finding_id || `${flag.type}:${flag.property_key}:${index}`}>
                          <span data-type={flag.type}>{flag.type}</span>
                          <div>
                            <strong>{flag.label || flag.property_key}</strong>
                            <small>{flag.property_key}</small>
                            {flag.type === 'TITLE' && <small className={styles.titleEvidence}>{titleEvidence(flag)}</small>}
                          </div>
                          {flag.type === 'TITLE' && flag.finding_id && (
                            <TitleFindingReview flag={flag} canDecide={canDecideTitle()} onSettled={titleSettled} />
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                  <p className={styles.disclaimer}>Signals are preliminary and require qualified professional review.</p>
                </section>
              </div>
            </div>
          </details>
        </>
      ) : null}
    </section>
  );
}
