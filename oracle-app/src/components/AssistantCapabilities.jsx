import { useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import { friendlyError } from '../lib/errorMessages';
import { groupAreas } from '../lib/capabilityOrder';
import styles from './AssistantCapabilities.module.css';

/**
 * What Neoh can do in chat, for this person, right now (audit A09).
 *
 * The list is not written here. It comes from GET /api/ai/chat/capabilities,
 * which derives every area's state from the tools the assistant can actually
 * run (backend/ai_capabilities.py), instead of hand-written copy that drifted
 * from the tool list.
 */

// Tone per state. The words themselves come from the API (`state_label`),
// so the page and the model use one vocabulary.
const STATE_TONE = {
  available: 'good',
  degraded: 'warn',
  approval_gated: 'warn',
  setup_required: 'neutral',
  unavailable: 'muted',
};

export default function AssistantCapabilities() {
  const [state, setState] = useState({ loading: true, error: '', body: null });

  useEffect(() => {
    let alive = true;
    crmGet('/api/ai/chat/capabilities').then(
      (body) => { if (alive) setState({ loading: false, error: '', body }); },
      (error) => {
        if (alive) {
          setState({
            loading: false,
            error: friendlyError(error, { fallback: 'couldn’t load right now.' }),
            body: null,
          });
        }
      },
    );
    return () => { alive = false; };
  }, []);

  const areas = groupAreas(state.body?.areas);
  const usable = areas.filter((area) => area.state !== 'unavailable').length;

  return (
    <section className={styles.panel} aria-labelledby="assistant-capabilities-title" aria-busy={state.loading}>
      <header>
        <h2 id="assistant-capabilities-title">What Neoh can do in chat</h2>
        {state.body ? <span>{usable} of {areas.length} usable</span> : null}
      </header>
      {state.loading ? <p className={styles.note}>Checking what Neoh can run…</p> : null}
      {state.error ? (
        <p className={styles.note} role="status">
          What Neoh can do {state.error} Nothing below is assumed to work until this loads.
        </p>
      ) : null}
      {state.body ? (
        <>
          <p className={styles.note}>{state.body.note}</p>
          <ul className={styles.list}>
            {areas.map((area) => (
              <li key={area.id} data-state={area.state}>
                <div>
                  <strong>{area.label}</strong>
                  <small>
                    {area.summary}
                    {area.limits?.length ? ` Limited: ${area.limits.join('; ')}.` : ''}
                    {area.role_limited ? ' Limited to the broker-owner.' : ''}
                  </small>
                </div>
                <span data-tone={STATE_TONE[area.state] || 'muted'}>
                  {area.role_limited ? 'Broker-owner only' : area.state_label}
                </span>
              </li>
            ))}
          </ul>
          {state.body.not_offered?.length ? (
            <p className={styles.note}>Not offered: {state.body.not_offered.join('; ')}.</p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
