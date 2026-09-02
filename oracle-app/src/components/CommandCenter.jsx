import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, Brain, Clock, Eye, Radar, TrendingUp } from 'lucide-react';

import { crmGet } from '../state/useCrmApi';
import { OpportunityCard } from './IntelligenceFeed';
import styles from './CommandCenter.module.css';

/**
 * The Command Center — the state of the business, in the order it matters.
 *
 * A conventional dashboard opens with totals: 47 leads, 12 tasks, 3 deals. The
 * agent already knew that, and none of it says what to do. This opens with a
 * *diff* — what moved since they last looked — then what needs them, then what
 * is coming. Three questions, in the order a person actually asks them.
 *
 * Design decisions worth defending:
 *
 * 1. **Every count links to the rows behind it.** A number an agent cannot
 *    open is a number they eventually stop believing.
 *
 * 2. **An empty section explains itself.** Silence is rendered as a stated
 *    reason, never as blank space. A calm morning and a broken pipeline look
 *    identical otherwise, and only one of them should be reassuring.
 *
 * 3. **The money figure wears its caveat inline.** Expected value here is
 *    modelled from stated priors, not fitted to this brokerage's own closed
 *    deals — because there are not any yet. Showing "$12,400" with the
 *    qualification a scroll away would be the most dishonest pixel on the
 *    screen, so the qualification sits in the same block as the number.
 *
 * 4. **The horizon shows empty buckets.** "Nothing this week" is information.
 *    Collapsing empty buckets would make a quiet week and an unbuilt feature
 *    render the same way.
 */

const HORIZON_ICONS = {
  now: AlertTriangle,
  today: Clock,
  this_week: Clock,
  this_month: TrendingUp,
  watching: Eye,
};

function greeting(date) {
  const hour = date.getHours();
  if (hour < 12) return 'Good morning';
  if (hour < 18) return 'Good afternoon';
  return 'Good evening';
}

function ChangedRibbon({ changed }) {
  if (!changed) return null;
  const behaviour = changed.behavioural_events ?? 0;
  const handled = changed.handled_automatically ?? 0;
  const contradictions = changed.new_contradictions ?? 0;
  const clients = changed.new_clients ?? 0;

  const entries = [
    { key: 'behaviour', value: behaviour, label: behaviour === 1 ? 'client action' : 'client actions' },
    { key: 'clients', value: clients, label: clients === 1 ? 'new client' : 'new clients' },
    { key: 'handled', value: handled, label: 'handled by Neoh' },
    { key: 'contradictions', value: contradictions, label: contradictions === 1 ? 'changed its mind' : 'changed their mind' },
  ].filter((entry) => entry.value > 0);

  if (entries.length === 0) {
    return (
      <p className={styles.quiet}>
        Nothing moved in the last 24 hours. Not a stalled feed — no new client
        activity, no records changed, and Neoh had nothing it needed to do.
      </p>
    );
  }

  return (
    <ul className={styles.ribbon}>
      {entries.map((entry) => (
        <li className={styles.ribbonItem} key={entry.key}>
          <span className={styles.ribbonValue}>{entry.value}</span>
          <span className={styles.ribbonLabel}>{entry.label}</span>
        </li>
      ))}
    </ul>
  );
}

function Portfolio({ portfolio }) {
  if (!portfolio || !portfolio.opportunity_count) return null;
  const total = Number(portfolio.total_expected_value || 0);
  return (
    <section className={styles.portfolio} aria-labelledby="cc-portfolio">
      <h2 className={styles.portfolioHeading} id="cc-portfolio">
        Today&rsquo;s opportunity
      </h2>
      <p className={styles.portfolioValue}>
        ${total.toLocaleString(undefined, { maximumFractionDigits: 0 })}
      </p>
      <p className={styles.portfolioCaveat}>
        {/* Not a forecast, and it must never read as one. */}
        {portfolio.caveat}
      </p>
      <p className={styles.portfolioMeta}>
        Across {portfolio.opportunity_count}{' '}
        {portfolio.opportunity_count === 1 ? 'opportunity' : 'opportunities'}
        {portfolio.suppressed_negative_ev > 0 && (
          <> · {portfolio.suppressed_negative_ev} cost more time than they are worth</>
        )}
      </p>
    </section>
  );
}

function Horizon({ buckets }) {
  if (!buckets?.length) return null;
  return (
    <section className={styles.horizon} aria-labelledby="cc-horizon">
      <h2 className={styles.sectionHeading} id="cc-horizon">Thought horizon</h2>
      <p className={styles.sectionNote}>
        Where each of these falls in time. Anything without a date of its own
        sits in Watching rather than being given an invented deadline.
      </p>
      <ol className={styles.horizonList}>
        {buckets.map((bucket) => {
          const Icon = HORIZON_ICONS[bucket.key] ?? Clock;
          return (
            <li className={styles.horizonBucket} key={bucket.key}>
              <div className={styles.horizonHead}>
                <Icon className={styles.horizonIcon} aria-hidden="true" size={15} />
                <h3 className={styles.horizonLabel}>{bucket.label}</h3>
                <span className={styles.horizonCount}>
                  {bucket.items.length === 0 ? 'nothing' : bucket.items.length}
                </span>
              </div>
              {bucket.items.length > 0 && (
                <ul className={styles.horizonItems}>
                  {bucket.items.map((item) => (
                    <li className={styles.horizonItem} key={`${item.kind}-${item.subject_id}`}>
                      <span className={styles.horizonSubject}>{item.subject}</span>
                      <span className={styles.horizonHeadline}>{item.headline}</span>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/**
 * What Neoh has learned about how this agent decides.
 *
 * Shown even while it is still learning, and that is the point: "watching, 6
 * decisions so far, not enough to describe how you work" tells the agent the
 * mechanism is real and honest. A panel that appeared only once it had an
 * opinion would look like it arrived from nowhere.
 *
 * Rates are rendered as the interval the API returns, never the bare point
 * estimate. Showing 100% from three decisions is a claim the agent can falsify
 * from memory, and that costs the twin its credibility permanently.
 */
function AgentTwin({ twin }) {
  if (!twin) return null;

  return (
    <section className={styles.twin} aria-labelledby="cc-twin">
      <div className={styles.twinHead}>
        <Brain aria-hidden="true" size={15} />
        <h2 className={styles.sectionHeading} id="cc-twin">How you decide</h2>
      </div>

      {twin.status === 'learning' ? (
        <>
          <p className={styles.twinLearning}>{twin.summary}</p>
          <p className={styles.twinMeta}>
            {twin.decisions_needed} more before Neoh will describe a pattern.
          </p>
        </>
      ) : (
        <>
          <ul className={styles.twinKinds}>
            {twin.by_kind?.map((kind) => (
              <li className={styles.twinKind} key={kind.kind}>
                <span className={styles.twinKindName}>
                  {String(kind.kind).replace(/_/g, ' ')}
                </span>
                <span className={styles.twinKindNote}>{kind.note}</span>
              </li>
            ))}
          </ul>
          {twin.confidence_threshold && (
            <p className={styles.twinThreshold}>{twin.confidence_threshold.detail}</p>
          )}
          {twin.stated_reasons?.length > 0 && (
            <div className={styles.twinReasons}>
              <h3 className={styles.twinReasonsTitle}>Reasons you have given</h3>
              <ul>
                {twin.stated_reasons.map((entry) => (
                  <li key={`${entry.reason}-${entry.latest}`}>
                    &ldquo;{entry.reason}&rdquo;
                    <span className={styles.twinReasonCount}>
                      ×{entry.times}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <p className={styles.twinCaveat}>{twin.caveat}</p>
        </>
      )}
    </section>
  );
}

function BlindSpot({ perception }) {
  if (!perception) return null;
  const unreachable = perception.high_motivation_unreachable ?? 0;
  const signals = perception.interaction_signals ?? 0;
  if (unreachable === 0 && signals > 0) return null;

  return (
    <section className={styles.blindspot} aria-labelledby="cc-blindspot">
      <div className={styles.blindspotHead}>
        <Radar aria-hidden="true" size={15} />
        <h2 className={styles.blindspotHeading} id="cc-blindspot">
          What Neoh cannot see
        </h2>
      </div>
      <ul className={styles.blindspotList}>
        {signals === 0 && (
          <li>
            No client behaviour has been captured yet, so every reading on this
            screen rests on what people <em>said</em> and what staff typed.
            Nothing here should be read as evidence a client is cold.
          </li>
        )}
        {unreachable > 0 && (
          <li>
            <strong>{unreachable.toLocaleString()}</strong> records score high on
            motivation but carry no address, so they cannot be acted on. That is
            a data-acquisition gap, not a quiet market.
          </li>
        )}
      </ul>
    </section>
  );
}

export function CommandCenter() {
  const [briefing, setBriefing] = useState(null);
  const [twin, setTwin] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  // The twin is fetched separately and its failure is swallowed: it is
  // commentary on the briefing, and losing it must not cost the agent the
  // briefing itself.
  const loadTwin = useCallback(async () => {
    try {
      setTwin(await crmGet('/api/agent-twin'));
    } catch { /* the briefing stands on its own */ }
  }, []);

  useEffect(() => {
    // Deferred and cancellable, matching IntelligenceFeed: the briefing runs a
    // full opportunity scan, so a remount can leave two in flight and without
    // the guard the slower, older response repaints a screen the agent has
    // already moved on from.
    let cancelled = false;
    const frame = window.requestAnimationFrame(() => {
      crmGet('/api/command-center')
        .then((data) => {
          if (!cancelled) { setBriefing(data); setLoading(false); }
        })
        .catch((err) => {
          if (!cancelled) { setError(err?.message || 'The briefing did not load.'); setLoading(false); }
        });
      // Inside the frame as well, so no state is set synchronously from the
      // effect body (react-hooks/set-state-in-effect).
      void loadTwin();
    });
    return () => { cancelled = true; window.cancelAnimationFrame(frame); };
  }, [loadTwin]);

  if (loading) {
    return (
      <div className={styles.shell} aria-busy="true" aria-label="Assembling your briefing">
        <div className={styles.skeletonTitle} />
        <div className={styles.skeletonRibbon} />
        <div className={styles.skeletonCard} />
        <div className={styles.skeletonCard} />
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.shell}>
        <p className={styles.error} role="alert">
          {error}. This screen reads live data and will not show a cached
          briefing, because a stale one is worse than none.
        </p>
      </div>
    );
  }

  const opportunities = briefing?.attention?.opportunities ?? [];

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <h1 className={styles.title}>{greeting(new Date())}</h1>
        <p className={styles.subtitle}>Here&rsquo;s what changed while you were away.</p>
        <ChangedRibbon changed={briefing?.changed} />
      </header>

      <Portfolio portfolio={briefing?.attention?.portfolio} />

      <section className={styles.attention} aria-labelledby="cc-attention">
        <h2 className={styles.sectionHeading} id="cc-attention">
          Needs you {opportunities.length > 0 && <span className={styles.count}>{opportunities.length}</span>}
        </h2>
        {opportunities.length === 0 ? (
          <p className={styles.quiet}>
            Nothing is currently above the confidence Neoh will speak at.
            {briefing?.suppressed_low_confidence > 0 && (
              <> {briefing.suppressed_low_confidence} weaker signal
                {briefing.suppressed_low_confidence === 1 ? ' was' : 's were'} held
                back rather than shown as a guess.</>
            )}
          </p>
        ) : (
          <ol className={styles.cards}>
            {opportunities.map((opportunity, index) => (
              <li key={`${opportunity.kind}-${opportunity.subject_id}-${index}`}>
                <OpportunityCard
                  opportunity={opportunity}
                  rank={index + 1}
                  onDecided={loadTwin}
                />
                {opportunity.economics && (
                  <p className={styles.economics}>
                    <span className={styles.economicsValue}>
                      ${Number(opportunity.economics.expected_value).toLocaleString(
                        undefined, { maximumFractionDigits: 0 })}
                    </span>
                    <span className={styles.economicsBasis}>
                      {opportunity.economics.basis.join(' · ')}
                    </span>
                  </p>
                )}
              </li>
            ))}
          </ol>
        )}
      </section>

      <Horizon buckets={briefing?.horizon} />
      <AgentTwin twin={twin} />
      <BlindSpot perception={briefing?.perception} />
    </div>
  );
}

export default CommandCenter;
