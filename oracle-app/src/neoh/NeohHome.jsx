import { useCallback, useEffect, useState } from 'react';
import { ChevronDown, Radar } from 'lucide-react';

import { crmGet } from '../state/useCrmApi';
import { ConfidenceMeter, DecisionBar, EvidenceList } from '../components/IntelligenceFeed';
import { VIEWS } from '../routes';
import { arrange } from './timeOfDay';
import styles from './NeohHome.module.css';

/**
 * Home — what matters right now, and nothing else.
 *
 * A conventional dashboard opens with totals in boxes: 47 leads, 12 tasks.
 * The agent already knew that and none of it says what to do. This opens
 * with one sentence — "3 things need you. Neoh handled 14." — then at most
 * three things, large, each with the decision the agent is being asked to
 * make. Everything else is one scroll or one tap away, never gone.
 *
 * What it refuses to do:
 *
 * - Show a KPI mosaic. The portfolio value is one line with its own caveat
 *   attached, because an uncalibrated dollar figure sitting in a tile reads
 *   as a forecast, and it is not one.
 * - Hide anything by the hour. `arrange` reorders and collapses around the
 *   time of day; it never removes. A home that hides an opportunity because
 *   it is 9pm has made a decision on the agent's behalf.
 * - Show evidence by default here. On the Work feed the evidence is expanded
 *   because that surface is for checking the ranking; here the surface is for
 *   deciding, and the case sits behind one tap of "Why?" — progressive
 *   disclosure, so a new agent is not drowned and an experienced one is one
 *   tap from the citation.
 * - Look empty for the wrong reason. A calm morning and a broken feed look
 *   identical otherwise, so an empty screen states why it is empty.
 */

function greeting(date) {
  const hour = date.getHours();
  if (hour < 12) return 'Good morning';
  if (hour < 18) return 'Good afternoon';
  return 'Good evening';
}

function humanize(text) {
  return String(text || '').replace(/_/g, ' ');
}

function HomeItem({ opportunity, rank, lead, showDecisions, onDecided }) {
  const [open, setOpen] = useState(false);
  return (
    <li className={`${styles.item} ${lead ? styles.itemLead : ''}`}>
      <div className={styles.itemHead}>
        <span className={styles.kind}>{humanize(opportunity.kind)}</span>
        {opportunity.deadline && (
          <time className={styles.deadline} dateTime={opportunity.deadline}>
            {new Date(opportunity.deadline).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
          </time>
        )}
      </div>
      <h2 className={styles.subject}>{opportunity.subject}</h2>
      <p className={styles.headline}>{opportunity.headline}</p>
      <p className={styles.action}>{opportunity.recommended_action}</p>

      <button
        type="button"
        className={styles.why}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <ChevronDown aria-hidden="true" size={14} className={open ? styles.whyOpen : ''} />
        {open ? 'Hide why' : 'Why?'}
      </button>
      {open && (
        <div className={styles.evidence}>
          <p className={styles.reason}>{opportunity.why}</p>
          <ConfidenceMeter value={opportunity.confidence} />
          <EvidenceList items={opportunity.evidence} />
        </div>
      )}

      {showDecisions && (
        <DecisionBar opportunity={opportunity} rank={rank} onDecided={onDecided} />
      )}
    </li>
  );
}

function Handled({ changed, expanded }) {
  const [open, setOpen] = useState(expanded);
  const total = changed?.handled_automatically ?? 0;
  const rows = Object.entries(changed?.handled_breakdown ?? {});
  if (!total) return null;
  return (
    <section className={styles.handled} aria-labelledby="home-handled">
      <button
        type="button"
        className={styles.handledToggle}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        id="home-handled"
      >
        <span>Neoh handled {total}</span>
        <ChevronDown aria-hidden="true" size={14} className={open ? styles.whyOpen : ''} />
      </button>
      {open && (
        <ul className={styles.handledList}>
          {rows.map(([tool, n]) => (
            <li key={tool}>
              <span className={styles.handledCount}>{n}</span>
              <span>{humanize(tool)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function CannotSee({ perception }) {
  if (!perception) return null;
  const unreachable = perception.high_motivation_unreachable ?? 0;
  const signals = perception.client_originated_signals ?? perception.interaction_signals ?? 0;
  if (unreachable === 0 && signals > 0) return null;
  return (
    <p className={styles.blind}>
      <Radar aria-hidden="true" size={13} />
      {signals === 0
        ? 'No client behaviour has been captured yet, so everything here rests on what people said and what staff typed.'
        : `${unreachable.toLocaleString()} records score high on motivation but carry no address, so they cannot be acted on.`}
    </p>
  );
}

export function NeohHome({ onNavigate }) {
  const [briefing, setBriefing] = useState(null);
  const [status, setStatus] = useState('loading');

  const load = useCallback(async (isCancelled = () => false) => {
    try {
      const data = await crmGet('/api/command-center');
      if (!isCancelled()) { setBriefing(data); setStatus('ready'); }
    } catch {
      if (!isCancelled()) setStatus('error');
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const frame = window.requestAnimationFrame(() => { void load(() => cancelled); });
    return () => { cancelled = true; window.cancelAnimationFrame(frame); };
  }, [load]);

  if (status === 'loading') {
    return (
      <div className={styles.shell} aria-busy="true" aria-label="Assembling what matters">
        <div className={styles.skeletonLine} />
        <div className={styles.skeletonItem} />
        <div className={styles.skeletonItem} />
      </div>
    );
  }

  if (status === 'error') {
    return (
      <div className={styles.shell}>
        <p className={styles.error} role="alert">
          The briefing did not load. This screen reads live data and will not
          show a stale one, because a stale briefing is worse than none.
        </p>
      </div>
    );
  }

  const layout = arrange(briefing, new Date());
  const needYou = (briefing?.attention?.opportunities ?? []).length;
  const handled = briefing?.changed?.handled_automatically ?? 0;
  const portfolio = briefing?.attention?.portfolio;

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <p className={styles.greeting}>{greeting(new Date())}.</p>
        <h1 className={styles.summary}>
          {needYou === 0 ? 'Nothing needs you right now.' : `${needYou} ${needYou === 1 ? 'thing needs' : 'things need'} you.`}
          {handled > 0 && <span className={styles.summaryHandled}> Neoh handled {handled}.</span>}
        </h1>
      </header>

      {layout.items.length === 0 ? (
        <p className={styles.quiet}>
          Nothing is above the confidence Neoh will speak at.
          {briefing?.suppressed_low_confidence > 0 && (
            <> {briefing.suppressed_low_confidence} weaker signal
              {briefing.suppressed_low_confidence === 1 ? ' was' : 's were'} held back rather than shown as a guess.</>
          )}
        </p>
      ) : (
        <ol className={styles.items}>
          {layout.items.map((opportunity, index) => (
            (layout.collapseRest && index > 0) ? (
              <li key={`${opportunity.kind}-${opportunity.subject_id}`} className={styles.itemCollapsed}>
                <span className={styles.subjectSmall}>{opportunity.subject}</span>
                <span className={styles.headlineSmall}>{opportunity.headline}</span>
              </li>
            ) : (
              <HomeItem
                key={`${opportunity.kind}-${opportunity.subject_id}`}
                opportunity={opportunity}
                rank={index + 1}
                lead={index === 0}
                showDecisions={layout.showDecisions}
                onDecided={load}
              />
            )
          ))}
        </ol>
      )}

      {layout.remaining > 0 && (
        <button
          type="button"
          className={styles.more}
          onClick={() => onNavigate?.('opportunities')}
        >
          {layout.remaining} more in Work
        </button>
      )}

      <Handled changed={briefing?.changed} expanded={layout.handledExpanded} />

      {portfolio?.opportunity_count > 0 && (
        <p className={styles.metric}>
          <span className={styles.metricValue}>
            ${Number(portfolio.total_expected_value || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}
          </span>
          <span className={styles.metricCaveat}>{portfolio.caveat}</span>
        </p>
      )}

      <CannotSee perception={briefing?.perception} />
    </div>
  );
}

// The Work view that lists everything Home did not show.
NeohHome.moreView = VIEWS.work;

export default NeohHome;
