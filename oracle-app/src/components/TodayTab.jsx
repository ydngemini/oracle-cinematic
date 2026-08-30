import { Suspense, lazy, useCallback, useEffect, useMemo, useState } from 'react';
import {
  ArrowUpRight,
  BriefcaseBusiness,
  CircleCheck,
  FileWarning,
  MessageSquareReply,
  RefreshCw,
  Sparkles,
  UserRoundCheck,
  Zap,
} from 'lucide-react';
import { crmGet } from '../state/useCrmApi';
import { useAssistant } from './AssistantContext';
import { normalizeThread } from './CommsShared';
import { PanelDataStatus } from './PanelDataStatus';
import styles from './TodayTab.module.css';

const MarketSnapshot = lazy(() => import('./MarketSnapshot'));

const integer = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });

const SOURCE_CONFIG = {
  portfolio: {
    label: 'Portfolio signals',
    path: '/api/portfolio/summary',
    select: (payload) => payload || {},
  },
  inbox: {
    label: 'Inbox',
    path: '/api/crm/comms/threads',
    select: (payload) => Array.isArray(payload?.threads) ? payload.threads : [],
  },
  deals: {
    label: 'Deals',
    path: '/api/portfolio/transactions?limit=100',
    select: (payload) => Array.isArray(payload?.transactions) ? payload.transactions : [],
  },
  approvals: {
    label: 'Approvals',
    path: '/api/commands?limit=20',
    select: (payload) => Array.isArray(payload?.commands) ? payload.commands : [],
  },
  // Broker-owner scope only, so an agent-role session gets a 403 here. That is
  // not an error worth showing them — `optional` keeps it out of the source
  // status list and the strip simply does not render.
  firstResponse: {
    label: 'First response',
    path: '/api/crm/routing/metrics?days=7',
    select: (payload) => payload?.first_response || null,
    optional: true,
  },
};

// Sub-90-second first response is the threshold the speed-to-lead feature
// exists to hit (see the vault research note referenced in speed_to_lead.py).
// Stated here so the copy and the backend metric cannot drift apart silently.
const FIRST_RESPONSE_TARGET_SECONDS = 90;

function seconds(value) {
  if (value === null || value === undefined) return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return n < 90 ? `${Math.round(n)}s` : `${Math.round(n / 60)}m`;
}

function useLiveSource(config, pollMs = 60_000) {
  const [state, setState] = useState({
    data: null,
    error: null,
    loading: true,
    refreshing: false,
    updatedAt: null,
  });

  const load = useCallback(() => {
    setState((current) => ({
      ...current,
      loading: current.data === null,
      refreshing: current.data !== null,
    }));
    return crmGet(config.path).then(
      (payload) => setState({
        data: config.select(payload),
        error: null,
        loading: false,
        refreshing: false,
        updatedAt: new Date(),
      }),
      (error) => setState((current) => ({
        ...current,
        error,
        loading: false,
        refreshing: false,
      })),
    );
  }, [config]);

  useEffect(() => {
    const initialLoad = Promise.resolve().then(load);
    const timer = window.setInterval(load, pollMs);
    return () => {
      void initialLoad;
      window.clearInterval(timer);
    };
  }, [load, pollMs]);

  return { ...state, load };
}

function label(value, fallback = 'Review') {
  return String(value || fallback).replaceAll('_', ' ');
}

function dateDistance(value) {
  if (!value) return null;
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return null;
  return Math.ceil((timestamp - Date.now()) / 86_400_000);
}

export default function TodayTab({ onNavigate }) {
  const portfolio = useLiveSource(SOURCE_CONFIG.portfolio);
  const inbox = useLiveSource(SOURCE_CONFIG.inbox);
  const deals = useLiveSource(SOURCE_CONFIG.deals);
  const approvals = useLiveSource(SOURCE_CONFIG.approvals);
  // Polled faster than the other sources: a first-response queue measured in
  // seconds is not usefully described by a 60-second-stale number.
  const firstResponse = useLiveSource(SOURCE_CONFIG.firstResponse, 20_000);
  const { requestCommand, setOpen } = useAssistant();

  // A ticking clock rather than Date.now() inside the memo: reading the wall
  // clock during render is impure, and a "waiting 40s" label that only updates
  // when some other source happens to poll would be worse than no label.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 5_000);
    return () => window.clearInterval(timer);
  }, []);

  const openFollowUp = useCallback((client) => {
    requestCommand({
      clientId: client.client_id,
      rawText: `Draft a concise follow-up for ${client.name} about their ${label(client.stage, 'active')} transaction. Ask for the best next step and do not send it without my approval.`,
    });
    setOpen(true);
  }, [requestCommand, setOpen]);

  const actions = useMemo(() => {
    const ranked = [];
    const summary = portfolio.data || {};
    const ghosting = Array.isArray(summary.ghosting_clients) ? summary.ghosting_clients : [];
    const flags = Array.isArray(summary.intelligence_flags) ? summary.intelligence_flags : [];

    ghosting.slice(0, 3).forEach((client, index) => {
      ranked.push({
        id: `follow-up:${client.client_id || index}`,
        priority: 110 + Number(client.last_contact_hours || 0),
        Icon: UserRoundCheck,
        eyebrow: 'Response opportunity',
        title: `Reconnect with ${client.name || 'client'}`,
        detail: `${integer.format(client.last_contact_hours || 0)} hours since contact · ${label(client.stage, 'active')}`,
        action: 'Draft follow-up',
        onRun: () => openFollowUp(client),
      });
    });

    flags.slice(0, 2).forEach((flag, index) => {
      ranked.push({
        id: `property:${flag.type || 'signal'}:${flag.property_key || index}`,
        priority: 100,
        Icon: FileWarning,
        eyebrow: 'Property intelligence',
        title: flag.label || flag.property_key || 'Review property signal',
        detail: `${label(flag.type, 'record')} signal · professional verification required`,
        action: 'Open people',
        onRun: () => onNavigate?.('people'),
      });
    });

    (inbox.data || [])
      .map((thread) => normalizeThread(thread))
      .filter((thread) => thread.unread)
      .slice(0, 2)
      .forEach((thread, index) => {
        ranked.push({
          id: `inbox:${thread.threadId || thread.clientId || index}`,
          priority: 105,
          Icon: MessageSquareReply,
          eyebrow: 'Inbound intent',
          title: `Reply to ${thread.clientName}`,
          detail: `${label(thread.channel, 'message')} · ${thread.snippet}`,
          action: 'Open inbox',
          onRun: () => onNavigate?.('inbox'),
        });
      });

    const pendingCommands = (approvals.data || [])
      .filter((command) => command.state === 'awaiting_approval' || command.state === 'reconciliation_required');

    // A staged first response is the one queue item whose value decays by the
    // second — the lead is sitting there right now deciding whether anyone is
    // going to answer. It outranks every other card, and climbs as it ages, so
    // it can never be pushed off the list by a closing deadline two weeks out.
    pendingCommands
      .filter((command) => command.draft?.context?.source === 'speed-to-lead')
      .slice(0, 3)
      .forEach((command, index) => {
        const createdAt = new Date(command.created_at).getTime();
        // A malformed created_at yields NaN, which would render "Waiting NaNs".
        const waitedSeconds = Number.isFinite(createdAt)
          ? Math.max(0, Math.round((now - createdAt) / 1000))
          : null;
        const missedTarget = waitedSeconds !== null
          && waitedSeconds > FIRST_RESPONSE_TARGET_SECONDS;
        ranked.push({
          id: `speed-to-lead:${command.id || index}`,
          priority: 200 + Math.min(waitedSeconds ?? 0, 600),
          Icon: Zap,
          eyebrow: missedTarget ? 'New lead · past 90s' : 'New lead · respond now',
          title: `Send the first ${label(command.command_type, 'reply')} to a new lead`,
          detail: waitedSeconds === null
            ? 'Drafted and ready to approve'
            : `Waiting ${seconds(waitedSeconds)} · drafted and ready to approve`,
          action: 'Review & send',
          onRun: () => onNavigate?.('inbox'),
        });
      });

    pendingCommands
      .filter((command) => command.draft?.context?.source !== 'speed-to-lead')
      .slice(0, 2)
      .forEach((command, index) => {
        ranked.push({
          id: `approval:${command.id || index}`,
          priority: command.state === 'reconciliation_required' ? 120 : 95,
          Icon: Sparkles,
          eyebrow: command.state === 'reconciliation_required' ? 'Reconcile' : 'AI approval',
          title: `${label(command.command_type, 'AI action')} needs review`,
          detail: 'Nothing sends or changes externally until you approve it.',
          action: 'Review',
          onRun: () => onNavigate?.('inbox'),
        });
      });

    (deals.data || [])
      .filter((transaction) => ['active', 'under_contract'].includes(transaction.status))
      .map((transaction) => ({ transaction, days: dateDistance(transaction.closing_deadline) }))
      .sort((a, b) => (a.days ?? Number.POSITIVE_INFINITY) - (b.days ?? Number.POSITIVE_INFINITY))
      .slice(0, 2)
      .forEach(({ transaction, days }, index) => {
        ranked.push({
          id: `deal:${transaction.id || index}`,
          priority: days !== null && days <= 15 ? 115 - Math.max(days, 0) : 80,
          Icon: BriefcaseBusiness,
          eyebrow: days !== null && days <= 15 ? 'Closing clock' : 'Active deal',
          title: transaction.property_address || transaction.client_name || 'Advance active deal',
          detail: days === null
            ? `${label(transaction.status)} · closing date not set`
            : days < 0
              ? `${Math.abs(days)} days past target closing`
              : `${days} days to target closing`,
          action: 'Open deal',
          onRun: () => onNavigate?.('deals'),
        });
      });

    return ranked.sort((a, b) => b.priority - a.priority).slice(0, 7);
  }, [approvals.data, deals.data, inbox.data, now, onNavigate, openFollowUp, portfolio.data]);

  const sources = [portfolio, inbox, deals, approvals, firstResponse];
  const allLoading = sources.every((source) => source.loading);
  const refreshAll = () => sources.forEach((source) => { void source.load(); });
  const unreadCount = (inbox.data || []).map((thread) => normalizeThread(thread)).filter((thread) => thread.unread).length;
  const activeDealCount = (deals.data || []).filter((transaction) => ['active', 'under_contract'].includes(transaction.status)).length;
  const approvalCount = (approvals.data || []).filter((command) => command.state === 'awaiting_approval' || command.state === 'reconciliation_required').length;

  return (
    <section className={styles.wrap} aria-labelledby="today-title" aria-busy={allLoading}>
      <header className={styles.hero}>
        <div>
          <span className={styles.kicker}>Revenue brief · Live tenant data</span>
          <h1 id="today-title">Today</h1>
          <p>Only the decisions most likely to move a relationship or deal forward.</p>
        </div>
        <button type="button" className={styles.refresh} onClick={refreshAll} aria-label="Refresh Today">
          <RefreshCw aria-hidden="true" />
        </button>
      </header>

      <p className={styles.brief} aria-label="Today summary">
        <strong>{integer.format(actions.length)}</strong> prioritized
        <span aria-hidden="true">/</span>
        <strong>{integer.format(unreadCount)}</strong> inbound
        <span aria-hidden="true">/</span>
        <strong>{integer.format(activeDealCount)}</strong> active deals
        <span aria-hidden="true">/</span>
        <strong>{integer.format(approvalCount)}</strong> approvals
      </p>

      {/* Above first-response: a brand-new broker has no response history and no
          deals, so this is the first section on the page with anything in it. */}
      <Suspense fallback={null}>
        <MarketSnapshot />
      </Suspense>

      {firstResponse.data?.enabled ? (
        <section className={styles.responseStrip} aria-labelledby="first-response-title">
          <header>
            <span className={styles.kicker}>Last 7 days</span>
            <h2 id="first-response-title">First response</h2>
          </header>
          <dl>
            <div>
              <dt>Median</dt>
              <dd>{seconds(firstResponse.data.p50_seconds)}</dd>
            </div>
            <div>
              <dt>Slowest 10%</dt>
              <dd>{seconds(firstResponse.data.p90_seconds)}</dd>
            </div>
            <div
              className={
                firstResponse.data.under_90s_rate >= 0.9 ? styles.onTarget : styles.offTarget
              }
            >
              <dt>Under {FIRST_RESPONSE_TARGET_SECONDS}s</dt>
              <dd>{Math.round((firstResponse.data.under_90s_rate || 0) * 100)}%</dd>
            </div>
            {/* Blocked and never-attempted are shown next to the latency, not
                hidden behind it. A fast median across the third of leads we were
                allowed to contact is not a fast median. */}
            <div>
              <dt>Compliance-held</dt>
              <dd>{integer.format(firstResponse.data.blocked || 0)}</dd>
            </div>
            <div className={firstResponse.data.no_attempt > 0 ? styles.offTarget : undefined}>
              <dt>No attempt</dt>
              <dd>{integer.format(firstResponse.data.no_attempt || 0)}</dd>
            </div>
          </dl>
        </section>
      ) : null}

      <section className={styles.priority} aria-labelledby="priority-title">
        <header className={styles.sectionHead}>
          <div>
            <span className={styles.kicker}>Ordered by urgency</span>
            <h2 id="priority-title">Next best actions</h2>
          </div>
          <span>Maximum 7</span>
        </header>

        {allLoading ? (
          <div className={styles.skeleton} aria-hidden="true"><span /><span /><span /></div>
        ) : actions.length === 0 ? (
          <div className={styles.clear} role="status">
            <CircleCheck aria-hidden="true" />
            <div>
              <strong>No urgent revenue actions</strong>
              <p>New replies, deadlines, approvals, and verified property signals will appear here.</p>
            </div>
          </div>
        ) : (
          <ol className={styles.actions}>
            {actions.map((action, index) => {
              const Icon = action.Icon;
              return (
                <li key={action.id}>
                  <span className={styles.rank} aria-label={`Priority ${index + 1}`}>{String(index + 1).padStart(2, '0')}</span>
                  <Icon className={styles.actionIcon} aria-hidden="true" />
                  <div>
                    <small>{action.eyebrow}</small>
                    <h3>{action.title}</h3>
                    <p>{action.detail}</p>
                  </div>
                  <button type="button" onClick={action.onRun}>
                    <span>{action.action}</span>
                    <ArrowUpRight aria-hidden="true" />
                  </button>
                </li>
              );
            })}
          </ol>
        )}
      </section>

      <section className={styles.sources} aria-labelledby="source-status-title">
        <h2 id="source-status-title" className={styles.srOnly}>Data source status</h2>
        <ul>
          {Object.entries(SOURCE_CONFIG).map(([key, config]) => {
            const source = { portfolio, inbox, deals, approvals, firstResponse }[key];
            // An optional source that errored (403 for an agent-role session)
            // is a capability the user does not have, not a fault to report.
            if (config.optional && (source.error || !source.data)) return null;
            return (
              <PanelDataStatus
                key={key}
                label={config.label}
                loading={source.loading}
                refreshing={source.refreshing}
                error={source.error}
                updatedAt={source.updatedAt}
                onRetry={source.load}
              />
            );
          })}
        </ul>
      </section>
    </section>
  );
}

