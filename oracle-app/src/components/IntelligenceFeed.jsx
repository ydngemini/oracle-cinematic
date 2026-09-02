import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPost } from '../state/useCrmApi';
import styles from './IntelligenceFeed.module.css';

/**
 * The Intelligence Feed — what needs attention, why, and what to do next.
 *
 * Deliberately not a chat box. A prompt makes the agent responsible for
 * knowing what to ask, which is the hard half of the job; a ranked feed makes
 * the system responsible for it. The order of these cards IS the product
 * claim, which is why this is a column and not a grid — a grid presents eight
 * things as equally important and throws the ranking away.
 *
 * Three rules the surface keeps, because breaking any one turns a finding back
 * into a claim:
 *
 * 1. **Every card shows its evidence, expanded.** Not behind a disclosure. A
 *    citation the agent never opens is the same as no citation, and the whole
 *    reason to trust a ranked feed is being able to check why something ranked.
 *
 * 2. **Confidence is a bar, a number and a word.** Colour alone fails anyone
 *    who cannot separate amber from blue, and this is the figure a decision
 *    gets staked on.
 *
 * 3. **The perception strip sits above the cards, not below them.** An empty
 *    feed because it was a quiet week and an empty feed because nothing is
 *    being captured look identical from the outside. The agent has to be able
 *    to tell those apart or the first surprise destroys their trust in it.
 */

export function ConfidenceMeter({ value }) {
  const pct = Math.round((value ?? 0) * 100);
  // Three bands, because "78%" alone does not tell an agent whether to act.
  const band = pct >= 80 ? 'High' : pct >= 60 ? 'Moderate' : 'Tentative';
  return (
    <div className={styles.confidence}>
      <div
        className={styles.meter}
        role="meter"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`Confidence ${pct} percent, ${band}`}
      >
        <span
          className={`${styles.meterFill} ${pct >= 80 ? styles.meterFillHigh : ''}`}
          style={{ right: `${100 - pct}%` }}
        />
      </div>
      <span className={styles.confidenceText}>{pct}% · {band}</span>
    </div>
  );
}

export function EvidenceList({ items }) {
  if (!items?.length) return null;
  return (
    <dl className={styles.evidence}>
      {items.map((item, i) => (
        <div className={styles.evidenceRow} key={`${item.source}-${i}`}>
          <dt className={styles.evidenceLabel}>{item.label}</dt>
          <dd className={styles.evidenceValue}>
            {item.value}
            {' '}
            <span className={styles.evidenceSource}>({item.source})</span>
          </dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * What the agent decided about one recommendation.
 *
 * This is the Agent Twin's only source of raw material. Until now a
 * disagreement left no trace — an agent who thought the top card was wrong
 * simply did not click it, and the system could not tell "wrong suggestion"
 * from "right suggestion, busy afternoon".
 *
 * Three decisions, not two. "Later" and "Not this" are kept apart because they
 * are different judgements, and merging them would teach the twin that a busy
 * Tuesday means a bad recommendation.
 *
 * The reason chips appear only after a dismissal, and are skippable. Demanding
 * a reason for every dismissal gets the control abandoned within a week, and
 * the data that survives is uniformly whichever option was fastest to click.
 */
export function DecisionBar({ opportunity, rank, onDecided }) {
  const [state, setState] = useState('idle'); // idle | asking | done
  const [chosen, setChosen] = useState(null);
  const [reasons, setReasons] = useState([]);
  // The id of the row this dismissal created, so the reason can be attached to
  // it. The decision and the reason arrive as two interactions but are ONE
  // decision: posting twice double-counted every reasoned dismissal, so a kind
  // the agent explained their way out of scored as twice as disliked as one
  // they skipped in silence.
  const [decisionId, setDecisionId] = useState(null);

  const send = useCallback(async (outcome) => {
    try {
      const created = await crmPost('/api/agent-twin/decisions', {
        opportunity_kind: opportunity.kind,
        // Stated by the engine per card. This used to be hardcoded 'client',
        // which filed every lead-anchored decision under a type it was not and
        // left Outcome Memory unable to join any of them back.
        subject_type: opportunity.subject_type || 'client',
        subject_id: String(opportunity.subject_id ?? ''),
        recommended_action: String(opportunity.recommended_action ?? '').slice(0, 500),
        outcome,
        recommended_confidence: opportunity.confidence ?? null,
        recommended_rank: rank ?? null,
      });
      setDecisionId(created?.id ?? null);
      onDecided?.(outcome);
    } catch {
      // A failed recording must not block the agent's actual work. The card
      // still resolves; the twin simply learns nothing from this one.
    }
  }, [opportunity, rank, onDecided]);

  // Recorded first, reason second, because the agent may never answer — closing
  // the tab must not lose the decision itself.
  const explain = useCallback(async (rationale, rationaleSource) => {
    setState('done');
    if (!decisionId) return;
    try {
      await crmPost(`/api/agent-twin/decisions/${decisionId}/rationale`, {
        rationale, rationale_source: rationaleSource,
      });
    } catch { /* the decision is already recorded; the reason is a bonus */ }
  }, [decisionId]);

  const decide = useCallback(async (outcome) => {
    setChosen(outcome);
    if (outcome === 'dismissed') {
      setState('asking');
      if (reasons.length === 0) {
        try {
          const payload = await crmGet('/api/agent-twin/reasons');
          setReasons(payload?.reasons || []);
        } catch { /* free text alone is fine */ }
      }
      await send(outcome);
      return;
    }
    setState('done');
    await send(outcome);
  }, [send, reasons.length]);

  if (state === 'done') {
    return (
      <p className={styles.decisionDone} role="status">
        {chosen === 'accepted' ? 'Noted — Neoh will weight these higher for you.'
          : chosen === 'deferred' ? 'Held for later.'
            : 'Noted.'}
      </p>
    );
  }

  if (state === 'asking') {
    return (
      <div className={styles.decisionReasons}>
        <p className={styles.decisionPrompt}>Why? Optional, and worth a lot.</p>
        <div className={styles.reasonChips}>
          {reasons.map((reason) => (
            <button
              type="button"
              key={reason.code}
              className={styles.reasonChip}
              onClick={() => { void explain(reason.label, 'agent_selected'); }}
            >
              {reason.label}
            </button>
          ))}
          <button
            type="button"
            className={styles.reasonSkip}
            onClick={() => setState('done')}
          >
            Skip
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.decisions}>
      <button type="button" className={styles.decisionPrimary} onClick={() => decide('accepted')}>
        I&rsquo;ll do this
      </button>
      <button type="button" className={styles.decision} onClick={() => decide('deferred')}>
        Later
      </button>
      <button type="button" className={styles.decision} onClick={() => decide('dismissed')}>
        Not this
      </button>
    </div>
  );
}

export function OpportunityCard({ opportunity, rank, onDecided }) {
  const kind = String(opportunity.kind || '').replace(/_/g, ' ');
  return (
    <article className={styles.card} aria-labelledby={`opp-${rank}-subject`}>
      <span className={styles.rank} aria-hidden="true">{String(rank).padStart(2, '0')}</span>
      <div>
        <div className={styles.cardHead}>
          <h3 className={styles.subject} id={`opp-${rank}-subject`}>{opportunity.subject}</h3>
          <span className={styles.kind}>{kind}</span>
        </div>
        <p className={styles.headline}>{opportunity.headline}</p>
        <p className={styles.why}>{opportunity.why}</p>
        <p className={styles.action}>
          <span className={styles.actionLabel}>Next</span>
          {opportunity.recommended_action}
        </p>
        <ConfidenceMeter value={opportunity.confidence} />
        <EvidenceList items={opportunity.evidence} />
        <DecisionBar opportunity={opportunity} rank={rank} onDecided={onDecided} />
      </div>
    </article>
  );
}

function PerceptionStrip({ perception }) {
  if (!perception) return null;
  const unreachable = perception.high_motivation_unreachable ?? 0;
  const blind = perception.behavioural_detectors_active === false;
  return (
    <section className={styles.perception} aria-label="What this feed can currently see">
      <div className={styles.metric}>
        <span className={styles.metricValue}>{perception.clients ?? 0}</span>
        <span className={styles.metricLabel}>clients</span>
      </div>
      <div className={styles.metric}>
        <span className={styles.metricValue}>{perception.clients_with_intent_model ?? 0}</span>
        <span className={styles.metricLabel}>with an intent model</span>
      </div>
      <div className={`${styles.metric} ${blind ? styles.metricWarn : ''}`}>
        <span className={styles.metricValue}>{perception.interaction_signals ?? 0}</span>
        <span className={styles.metricLabel}>behavioural signals</span>
      </div>
      {unreachable > 0 && (
        <div className={`${styles.metric} ${styles.metricWarn}`}>
          <span className={styles.metricValue}>{unreachable.toLocaleString()}</span>
          <span className={styles.metricLabel}>scored but unreachable</span>
        </div>
      )}
      {blind && <p className={styles.blind}>{perception.note}</p>}
      {unreachable > 0 && (
        <p className={styles.blind}>
          {unreachable.toLocaleString()} records score highly on the motivation model but
          carry no street address, so they cannot become an opportunity you could act on.
          That is a data-acquisition gap, not a quiet week.
        </p>
      )}
    </section>
  );
}

export function IntelligenceFeed() {
  const [state, setState] = useState({ status: 'loading', data: null, error: null });

  const load = useCallback(async (isCancelled = () => false) => {
    setState((s) => ({ ...s, status: 'loading' }));
    try {
      const data = await crmGet('/api/opportunities');
      if (!isCancelled()) setState({ status: 'ready', data, error: null });
    } catch (error) {
      if (!isCancelled()) setState({ status: 'error', data: null, error });
    }
  }, []);

  useEffect(() => {
    // Deferred out of the effect body and cancellable, matching
    // IntelligenceAuthoring: a scan takes long enough that a remount can leave
    // two in flight, and without the guard the slower, older response wins and
    // repaints a feed the agent has already navigated away from.
    let cancelled = false;
    const frame = window.requestAnimationFrame(() => { void load(() => cancelled); });
    return () => { cancelled = true; window.cancelAnimationFrame(frame); };
  }, [load]);

  if (state.status === 'loading') {
    return (
      <div className={styles.feed} aria-busy="true" aria-label="Scanning for opportunities">
        {[0, 1, 2].map((i) => <div className={styles.skeleton} key={i} />)}
      </div>
    );
  }

  if (state.status === 'error') {
    return (
      <div className={styles.feed}>
        <div className={styles.error} role="alert">
          <h2 className={styles.emptyTitle}>The scan could not run</h2>
          <p className={styles.emptyBody}>
            {state.error?.message || 'The opportunities service did not respond.'}
          </p>
          <button type="button" onClick={() => load()}>Try again</button>
        </div>
      </div>
    );
  }

  const { opportunities = [], perception, scanned_at: scannedAt } = state.data || {};

  return (
    <div className={styles.feed}>
      <header className={styles.header}>
        <h2 className={styles.title}>
          {opportunities.length > 0
            ? `${opportunities.length} ${opportunities.length === 1 ? 'opportunity' : 'opportunities'}`
            : 'Nothing needs attention'}
        </h2>
        {scannedAt && (
          <span className={styles.scanned}>
            scanned {new Date(scannedAt).toLocaleTimeString()}
          </span>
        )}
      </header>

      <PerceptionStrip perception={perception} />

      {opportunities.length === 0 ? (
        <div className={styles.empty} role="status">
          <h3 className={styles.emptyTitle}>No opportunities above the confidence floor</h3>
          <p className={styles.emptyBody}>
            Findings below 45% confidence are withheld rather than shown, because a
            low-confidence guess costs more trust than a missed lead earns. The strip above
            says what this scan could see.
          </p>
        </div>
      ) : (
        <ol role="list" style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 'var(--space-4)' }}>
          {opportunities.map((opportunity, i) => (
            <li key={`${opportunity.kind}-${opportunity.subject_id || i}`}>
              <OpportunityCard opportunity={opportunity} rank={i + 1} />
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
