import { useCallback, useEffect, useState } from 'react';
import { ChevronDown, Radar, Sparkles } from 'lucide-react';

import { crmGet } from '../state/useCrmApi';
import { useOptionalAssistant } from '../components/AssistantContext';
import { ConfidenceMeter, DecisionBar, EvidenceList } from '../components/IntelligenceFeed';
import { VIEWS, entityHref } from '../routes';
import { arrange } from './timeOfDay';
import { handledPhrase } from './actionLabels';
import { wireContext } from './useNeohChannel';
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

/**
 * "Ask Neoh about Sarah" — the first thing worth doing in this product, made
 * the obvious thing to do. It opens the Neoh tab with Sarah as the visible,
 * removable context and the question staged; it never sends on its own.
 */
function useAskNeoh(onNavigate) {
  const assistant = useOptionalAssistant();
  return useCallback((subject, question) => {
    if (!assistant) return;
    if (subject && wireContext(subject)) assistant.registerRecord(subject, 'home');
    assistant.requestCommand({ rawText: question, surface: 'conversation' });
    onNavigate?.(VIEWS.neoh);
  }, [assistant, onNavigate]);
}

function AskNeoh({ label, onAsk }) {
  return (
    <button type="button" className={styles.ask} onClick={onAsk}>
      <Sparkles aria-hidden="true" size={15} />
      <span>{label}</span>
    </button>
  );
}

/**
 * A quiet day still has a first move. If there is anyone or anything in the
 * CRM, offer to ask Neoh about the most recent one; if there is nothing at
 * all, say so and point at the one action that fixes it. Fetched only when
 * the briefing had nothing to show, so a busy Home pays nothing for it.
 */
const RECENT_CONTEXT = Object.freeze({ people: 'client', properties: 'lead' });

function QuietStart({ onNavigate, onAsk }) {
  const [state, setState] = useState({ loading: true, record: null, failed: false });
  useEffect(() => {
    let live = true;
    crmGet('/api/search/recent?limit=6', { retries: 0 }).then(
      (data) => {
        if (!live) return;
        const hit = (data?.results || []).find((row) => RECENT_CONTEXT[row.kind] && row.label);
        setState({
          loading: false,
          failed: false,
          record: hit ? { type: RECENT_CONTEXT[hit.kind], id: hit.id, label: hit.label } : null,
        });
      },
      () => { if (live) setState({ loading: false, record: null, failed: true }); },
    );
    return () => { live = false; };
  }, []);

  if (state.loading) return <div className={styles.skeletonAsk} aria-hidden="true" />;
  if (state.record) {
    return (
      <AskNeoh
        label={`Ask Neoh about ${state.record.label}`}
        onAsk={() => onAsk(state.record, `What should I know about ${state.record.label} today?`)}
      />
    );
  }
  // Recent could not be read: say nothing rather than claim the CRM is empty.
  if (state.failed) return null;
  return (
    <div className={styles.firstRun}>
      <p className={styles.firstRunText}>No contacts yet. Import them or add your first client.</p>
      <button type="button" className={styles.firstRunAction} onClick={() => onNavigate?.('people')}>
        Add or import contacts
      </button>
    </div>
  );
}

/** Opportunity subject types that open a record sheet, and the sheet kind. */
const ENTITY_KIND = Object.freeze({ lead: 'property', client: 'person' });

function HomeItem({ opportunity, rank, lead, showDecisions, onDecided, onAsk, onOpenEntity }) {
  const [open, setOpen] = useState(false);
  return (
    <li className={`${styles.item} ${lead ? styles.itemLead : ''}`}>
      {/* The person leads. `kind` was the detector's own name —
          "next best action", "lead reactivation" — printed above the name of
          a real human being, in caps. That is the system describing its
          internal categories to someone who came to find out about Sarah.
          The headline already says what happened; the machine's word for why
          it noticed is not the first thing anyone needs. */}
      <div className={styles.itemHead}>
        {opportunity.deadline && (
          <time className={styles.deadline} dateTime={opportunity.deadline}>
            {new Date(opportunity.deadline).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
          </time>
        )}
      </div>
      <h2 className={styles.subject}>
        {/* The subject IS the record: a property card opens the property, a
            person card the person. Otherwise the first useful thing on Home
            had no way into the thing it was about. */}
        {onOpenEntity && opportunity.subject_id && ENTITY_KIND[opportunity.subject_type] ? (
          <button
            type="button"
            className={styles.subjectLink}
            onClick={() => onOpenEntity(entityHref(ENTITY_KIND[opportunity.subject_type], opportunity.subject_id))}
          >
            {opportunity.subject}
          </button>
        ) : opportunity.subject}
      </h2>
      <p className={styles.headline}>{opportunity.headline}</p>
      <p className={styles.action}>{opportunity.recommended_action}</p>
      {lead && onAsk && opportunity.subject && (
        <AskNeoh label={`Ask Neoh about ${opportunity.subject}`} onAsk={onAsk} />
      )}

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
              <span>{handledPhrase(tool, n)}</span>
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

export function NeohHome({ onNavigate, onOpenEntity }) {
  const [briefing, setBriefing] = useState(null);
  const [status, setStatus] = useState('loading');
  const ask = useAskNeoh(onNavigate);

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
          Today&rsquo;s briefing didn&rsquo;t load. Home only shows live data, so it
          shows nothing rather than an old briefing. Work and Neoh still work.
        </p>
        <button
          type="button"
          className={styles.more}
          onClick={() => { setStatus('loading'); void load(); }}
        >
          Try again
        </button>
      </div>
    );
  }

  const layout = arrange(briefing, new Date());
  const needYou = (briefing?.attention?.opportunities ?? []).length;
  const handled = briefing?.changed?.handled_automatically ?? 0;
  const portfolio = briefing?.attention?.portfolio;
  const newClients = briefing?.changed?.new_clients ?? 0;

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <p className={styles.greeting}>{greeting(new Date())}.</p>
        <h1 className={styles.summary}>
          {needYou === 0 ? 'Nothing needs you right now.' : `${needYou} ${needYou === 1 ? 'thing needs' : 'things need'} you.`}
          {handled > 0 && <span className={styles.summaryHandled}> Neoh handled {handled}.</span>}
        </h1>
        {newClients > 0 && (
          <p className={styles.since}>
            {newClients} new {newClients === 1 ? 'client' : 'clients'} since yesterday.
          </p>
        )}
      </header>

      {layout.items.length === 0 ? (
        <>
          <p className={styles.quiet}>
            Nothing is above the confidence Neoh will speak at.
            {briefing?.suppressed_low_confidence > 0 && (
              <> {briefing.suppressed_low_confidence} weaker signal
                {briefing.suppressed_low_confidence === 1 ? ' was' : 's were'} held back rather than shown as a guess.</>
            )}
          </p>
          <QuietStart onNavigate={onNavigate} onAsk={ask} />
        </>
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
                onOpenEntity={onOpenEntity}
                onAsk={index === 0 ? () => ask(
                  opportunity.subject_id
                    ? { type: opportunity.subject_type || 'client', id: opportunity.subject_id, label: opportunity.subject }
                    : null,
                  `What should I do next with ${opportunity.subject}?`,
                ) : undefined}
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
