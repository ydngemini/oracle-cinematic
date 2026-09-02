import { useCallback, useEffect, useState } from 'react';
import { AlertCircle, Eye, Pin, X } from 'lucide-react';

import { crmGet, crmPost } from '../state/useCrmApi';
import styles from './RelationshipIntelligence.module.css';

/**
 * Relationship Intelligence — what Neoh believes about one person, and why.
 *
 * The competitive claim behind this screen is not "we have AI memory". Every
 * CRM will say that within a year. It is that the memory is *inspectable and
 * correctable*: every belief names its source, shows its age, and can be
 * pinned or retracted by the agent in one click.
 *
 * That matters because unverifiable memory is the failure mode of this whole
 * product category. A system that says "Sarah wants Ashburn" with no way to
 * ask how it knows is indistinguishable from one that guessed — and the first
 * time it is confidently wrong in front of a client, the agent stops trusting
 * all of it, including the parts that were right.
 *
 * Three decisions carry the screen:
 *
 * 1. **Provenance is one click, never a hover.** Hover is unreachable on
 *    touch and invisible to keyboard users, and this is the affordance the
 *    entire trust argument rests on.
 *
 * 2. **Contradictions sit at the top, phrased as questions.** When behaviour
 *    disagrees with what someone said, Neoh does not silently pick the newer
 *    signal. It says both, with dates and sources, and asks. Picking would
 *    require knowing which one wins, which depends on facts only the agent
 *    has.
 *
 * 3. **"Unobserved" is rendered as its own state, never as a low score.** A
 *    client nobody has watched and a client who has gone cold produce the same
 *    number and need opposite responses.
 */

const STATUS_COPY = {
  confirmed: { label: 'Confirmed', detail: 'Verified against a document or system of record.' },
  reported: { label: 'They said', detail: 'Stated by the client. True that they said it.' },
  inference: { label: 'Inferred', detail: 'Derived from evidence, which is attached.' },
  hypothesis: { label: 'Hypothesis', detail: 'Worth testing. Not worth acting on unattended.' },
};

function formatValue(value) {
  if (value == null) return '—';
  if (typeof value === 'string') return value;
  if (typeof value === 'number') return value.toLocaleString();
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return JSON.stringify(value);
}

function humanize(text) {
  return String(text || '').replace(/_/g, ' ');
}

function IntentReading({ title, reading }) {
  if (!reading) return null;
  const unobserved = reading.evidence_state === 'unobserved';
  const weak = reading.evidence_state === 'weak';

  return (
    <div className={styles.reading}>
      <h3 className={styles.readingTitle}>{title}</h3>
      {reading.score == null ? (
        // Never a zero. The words carry the state, and the state is the point.
        <p className={`${styles.readingState} ${unobserved ? styles.stateBlind : styles.stateWeak}`}>
          {unobserved ? 'Not observed' : 'Too little to read'}
        </p>
      ) : (
        <p className={styles.readingScore}>{Math.round(reading.score * 100)}%</p>
      )}
      <p className={styles.readingBasis}>{reading.basis}</p>
      {weak && reading.signals && Object.keys(reading.signals).length > 0 && (
        <ul className={styles.signalList}>
          {Object.entries(reading.signals).map(([signal, count]) => (
            <li key={signal}>{count}× {humanize(signal)}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Dispute({ dispute }) {
  return (
    <li className={styles.dispute}>
      <div className={styles.disputeHead}>
        <AlertCircle aria-hidden="true" size={15} />
        <h3 className={styles.disputeTitle}>{humanize(dispute.predicate)} may have changed</h3>
      </div>
      <p className={styles.disputeQuestion}>{dispute.question}</p>
    </li>
  );
}

function BeliefChip({ belief, onSelect, selected }) {
  const status = STATUS_COPY[belief.status] ?? STATUS_COPY.inference;
  return (
    <button
      type="button"
      className={`${styles.chip} ${selected ? styles.chipSelected : ''} ${belief.pinned ? styles.chipPinned : ''}`}
      onClick={() => onSelect(belief)}
      aria-expanded={selected}
      // The affordance is stated, not implied by styling alone.
      aria-label={`${humanize(belief.predicate)}: ${formatValue(belief.value)}. ${status.label}, ${Math.round(belief.confidence * 100)} percent. Show how Neoh knows this.`}
    >
      {belief.pinned && <Pin aria-hidden="true" size={11} className={styles.chipPin} />}
      <span className={styles.chipValue}>{formatValue(belief.value)}</span>
      <span className={styles.chipConfidence}>{Math.round(belief.confidence * 100)}%</span>
    </button>
  );
}

function Provenance({ belief, onCorrect, busy }) {
  const status = STATUS_COPY[belief.status] ?? STATUS_COPY.inference;
  const decayed = belief.confidence < belief.stored_confidence - 0.02;

  return (
    <aside className={styles.provenance} aria-live="polite">
      <h3 className={styles.provenanceTitle}>How Neoh knows this</h3>

      <dl className={styles.provenanceGrid}>
        <div>
          <dt>Claim</dt>
          <dd>{humanize(belief.predicate)} — <strong>{formatValue(belief.value)}</strong></dd>
        </div>
        <div>
          <dt>Grade</dt>
          <dd>{status.label}<span className={styles.provenanceHint}>{status.detail}</span></dd>
        </div>
        <div>
          <dt>Source</dt>
          <dd>
            {humanize(belief.source?.kind)}
            {belief.source?.ref && <span className={styles.provenanceHint}>{belief.source.ref}</span>}
          </dd>
        </div>
        <div>
          <dt>Learned</dt>
          <dd>
            {new Date(belief.learned_at).toLocaleDateString()}
            <span className={styles.provenanceHint}>{Math.round(belief.age_days)} days ago</span>
          </dd>
        </div>
      </dl>

      {belief.source?.quote && (
        <blockquote className={styles.quote}>{belief.source.quote}</blockquote>
      )}

      {/* Stored vs effective are both shown. A decayed belief and a weak one
          look identical if only one number is given, and they need different
          responses — one wants re-asking, the other wants better evidence. */}
      {decayed && (
        <p className={styles.decay}>
          Recorded at {Math.round(belief.stored_confidence * 100)}%, now read as{' '}
          {Math.round(belief.confidence * 100)}% because of its age. Nothing has
          contradicted it — it simply has not been confirmed lately.
        </p>
      )}

      <div className={styles.actions}>
        <button
          type="button"
          className={styles.action}
          disabled={busy}
          onClick={() => onCorrect(belief.id, belief.pinned ? 'unpin' : 'pin')}
        >
          <Pin aria-hidden="true" size={13} />
          {belief.pinned ? 'Unpin' : 'Pin as correct'}
        </button>
        <button
          type="button"
          className={`${styles.action} ${styles.actionDestructive}`}
          disabled={busy}
          onClick={() => onCorrect(belief.id, 'retract')}
        >
          <X aria-hidden="true" size={13} />
          This is wrong
        </button>
      </div>
      <p className={styles.actionsNote}>
        Pinning stops this ageing and settles any disagreement about it.
        Retracting hides it from reasoning but keeps it in the record.
      </p>
    </aside>
  );
}

export function RelationshipIntelligence({ clientId }) {
  const [intent, setIntent] = useState(null);
  const [knowledge, setKnowledge] = useState(null);
  const [selected, setSelected] = useState(null);
  const [status, setStatus] = useState('loading');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);

  const load = useCallback(async (isCancelled = () => false) => {
    try {
      const [intentData, beliefData] = await Promise.all([
        crmGet(`/api/clients/${clientId}/intent`),
        crmGet(`/api/beliefs/client/${clientId}`),
      ]);
      if (isCancelled()) return;
      setIntent(intentData);
      setKnowledge(beliefData);
      setStatus('ready');
    } catch {
      if (!isCancelled()) setStatus('error');
    }
  }, [clientId]);

  useEffect(() => {
    let cancelled = false;
    const frame = window.requestAnimationFrame(() => { void load(() => cancelled); });
    return () => { cancelled = true; window.cancelAnimationFrame(frame); };
  }, [load]);

  const correct = useCallback(async (beliefId, action) => {
    setBusy(true);
    setNotice(null);
    try {
      await crmPost(`/api/beliefs/${beliefId}/correct`, { action });
      setNotice(
        action === 'retract'
          ? 'Retracted. Neoh will stop reasoning from it.'
          : action === 'pin'
            ? 'Pinned. This now outranks anything Neoh infers.'
            : 'Unpinned. This will age normally again.',
      );
      setSelected(null);
      await load();
    } catch (error) {
      setNotice(error?.message || 'That correction did not save.');
    } finally {
      setBusy(false);
    }
  }, [load]);

  if (status === 'loading') {
    return (
      <div className={styles.shell} aria-busy="true" aria-label="Loading relationship intelligence">
        <div className={styles.skeleton} />
        <div className={styles.skeleton} />
      </div>
    );
  }

  if (status === 'error') {
    return (
      <div className={styles.shell}>
        <p className={styles.error} role="alert">
          Could not load what Neoh knows about this client.
        </p>
      </div>
    );
  }

  const predicates = Object.entries(knowledge?.beliefs ?? {});
  const disputes = knowledge?.disputes ?? [];

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <h2 className={styles.name}>{intent?.client_name}</h2>
        <p className={styles.journey}>
          {humanize(intent?.journey)} · observed over {intent?.window_days} days
        </p>
      </header>

      {notice && <p className={styles.notice} role="status">{notice}</p>}

      {/* Contradictions first. This is the sentence competitors do not say. */}
      {disputes.length > 0 && (
        <ul className={styles.disputes}>
          {disputes.map((dispute) => <Dispute dispute={dispute} key={dispute.predicate} />)}
        </ul>
      )}

      <section className={styles.readings} aria-label="Intent">
        <IntentReading title="What they say" reading={intent?.declared} />
        <IntentReading title="What they do" reading={intent?.observed} />
        <div className={styles.reading}>
          <h3 className={styles.readingTitle}>Neoh&rsquo;s read</h3>
          {intent?.latent?.latent_score != null ? (
            <p className={styles.readingScore}>{Math.round(intent.latent.latent_score * 100)}%</p>
          ) : (
            <p className={`${styles.readingState} ${styles.stateBlind}`}>Not enough to say</p>
          )}
          <p className={styles.readingBasis}>{intent?.latent?.summary}</p>
        </div>
      </section>

      {intent?.state_distribution?.length > 0 && (
        <section className={styles.states} aria-labelledby="ri-states">
          <h3 className={styles.sectionHeading} id="ri-states">Where they are</h3>
          <ul className={styles.stateList}>
            {intent.state_distribution.map((entry) => (
              <li className={styles.stateRow} key={entry.state}>
                <span className={styles.stateName}>{humanize(entry.state)}</span>
                <span
                  className={styles.stateBar}
                  role="meter"
                  aria-valuenow={Math.round(entry.probability * 100)}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-label={`${humanize(entry.state)}: ${Math.round(entry.probability * 100)} percent`}
                >
                  <span className={styles.stateFill} style={{ width: `${entry.probability * 100}%` }} />
                </span>
                <span className={styles.statePct}>{Math.round(entry.probability * 100)}%</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {intent?.levers?.length > 0 && (
        <section className={styles.levers} aria-labelledby="ri-levers">
          <h3 className={styles.sectionHeading} id="ri-levers">What would move this forward</h3>
          <ul className={styles.leverList}>
            {intent.levers.map((lever) => (
              <li className={styles.lever} key={lever.gap}>
                <span className={styles.leverAction}>{lever.action}</span>
                <span className={styles.leverWhy}>{lever.why}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className={styles.memory} aria-labelledby="ri-memory">
        <h3 className={styles.sectionHeading} id="ri-memory">
          <Eye aria-hidden="true" size={14} /> What Neoh remembers
        </h3>
        {predicates.length === 0 ? (
          <p className={styles.empty}>
            Nothing recorded yet. Beliefs are written as calls, messages and
            behaviour come in — none of this is pre-filled.
          </p>
        ) : (
          <div className={styles.memoryBody}>
            <div className={styles.predicates}>
              {predicates.map(([predicate, items]) => (
                <div className={styles.predicate} key={predicate}>
                  <span className={styles.predicateLabel}>{humanize(predicate)}</span>
                  <div className={styles.chips}>
                    {items.map((belief) => (
                      <BeliefChip
                        belief={belief}
                        key={belief.id}
                        selected={selected?.id === belief.id}
                        onSelect={setSelected}
                      />
                    ))}
                  </div>
                </div>
              ))}
            </div>
            {selected && <Provenance belief={selected} onCorrect={correct} busy={busy} />}
          </div>
        )}
      </section>
    </div>
  );
}

export default RelationshipIntelligence;
