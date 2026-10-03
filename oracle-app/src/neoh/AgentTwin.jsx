import { useEffect, useState } from 'react';
import { Brain } from 'lucide-react';

import { crmGet } from '../state/useCrmApi';
import styles from './AgentTwin.module.css';

/**
 * How you decide — what Neoh has learned about how this agent decides.
 *
 * Moved here from the retired Command Center (a duplicate of Home), because
 * it was the one part of it nothing else showed. It sits under the ranked
 * opportunities in Work: the place an agent checks why Neoh ranks things the
 * way it does is the place to show what it has learned from their choices.
 *
 * Shown even while it is still learning, and that is the point: "watching, 6
 * decisions so far" says the mechanism is real and honest. Rates are the
 * interval the API returns, never a bare point estimate. Its failure is
 * swallowed: it is commentary, and losing it must not cost the feed above.
 */
export function AgentTwin() {
  const [twin, setTwin] = useState(null);

  useEffect(() => {
    let live = true;
    crmGet('/api/agent-twin', { retries: 0 }).then(
      (data) => { if (live) setTwin(data || null); },
      () => {},
    );
    return () => { live = false; };
  }, []);

  if (!twin) return null;

  return (
    <section className={styles.twin} aria-labelledby="work-twin">
      <div className={styles.twinHead}>
        <Brain aria-hidden="true" size={15} />
        <h2 className={styles.heading} id="work-twin">How you decide</h2>
      </div>

      {twin.status === 'learning' ? (
        <>
          <p className={styles.learning}>{twin.summary}</p>
          <p className={styles.meta}>
            {twin.decisions_needed} more before Neoh will describe a pattern.
          </p>
        </>
      ) : (
        <>
          <ul className={styles.kinds}>
            {twin.by_kind?.map((kind) => (
              <li className={styles.kind} key={kind.kind}>
                <span className={styles.kindName}>{String(kind.kind).replace(/_/g, ' ')}</span>
                <span className={styles.kindNote}>{kind.note}</span>
              </li>
            ))}
          </ul>
          {twin.confidence_threshold && (
            <p className={styles.threshold}>{twin.confidence_threshold.detail}</p>
          )}
          {twin.stated_reasons?.length > 0 && (
            <div className={styles.reasons}>
              <h3 className={styles.reasonsTitle}>Reasons you have given</h3>
              <ul>
                {twin.stated_reasons.map((entry) => (
                  <li key={`${entry.reason}-${entry.latest}`}>
                    &ldquo;{entry.reason}&rdquo;
                    <span className={styles.reasonCount}>×{entry.times}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {twin.caveat && <p className={styles.caveat}>{twin.caveat}</p>}
        </>
      )}
    </section>
  );
}

export default AgentTwin;
