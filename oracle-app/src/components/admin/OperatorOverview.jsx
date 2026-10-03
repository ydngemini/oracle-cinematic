import { useCallback, useEffect, useState } from 'react';
import { crmGet } from '../../state/useCrmApi';
import BrokerageDiagnostics from './BrokerageDiagnostics';
import StateTag from './StateTag';
import {
  CAPABILITY_LABELS,
  OPERATOR_ENDPOINTS,
  WORK_COMPONENTS,
  componentLabel,
  componentProblems,
  fmtMoney,
  providerDegradation,
  relTime,
  shortSha,
  stateWords,
  statusWords,
} from './operatorModel';
import styles from './OperatorOverview.module.css';

const POLL_MS = 60000;

function useOperatorFeeds() {
  const [feeds, setFeeds] = useState({});
  const fetchAll = useCallback(() => {
    const keys = Object.keys(OPERATOR_ENDPOINTS);
    return Promise.allSettled(keys.map((k) => crmGet(OPERATOR_ENDPOINTS[k]))).then((results) => {
      setFeeds((prev) => {
        const next = { ...prev };
        results.forEach((r, i) => {
          const k = keys[i];
          next[k] = r.status === 'fulfilled'
            ? { data: r.value, error: null }
            : { data: prev[k]?.data ?? null, error: r.reason };
        });
        return next;
      });
    });
  }, []);
  useEffect(() => {
    fetchAll();
    const timer = setInterval(fetchAll, POLL_MS);
    return () => clearInterval(timer);
  }, [fetchAll]);
  return [feeds, fetchAll];
}

/* ── Small pieces ──────────────────────────────────────────────────────── */

function Card({ title, id, children, aside }) {
  return (
    <section className={styles.card} aria-labelledby={id}>
      <header className={styles.cardHead}>
        <h3 id={id} className={styles.cardTitle}>{title}</h3>
        {aside}
      </header>
      {children}
    </section>
  );
}

// undefined → loading; error without data → message; otherwise children(data).
function Feed({ slot, label, onRetry, children }) {
  if (!slot) return <p className={styles.quiet} aria-live="polite">Loading {label}…</p>;
  if (slot.error && slot.data === null) {
    return (
      <div className={styles.error} role="alert">
        <p>Couldn’t load {label}{slot.error?.status === 403 ? ' — platform admin only' : ''}.</p>
        <button type="button" className={styles.linkBtn} onClick={onRetry}>Retry</button>
      </div>
    );
  }
  return (
    <>
      {slot.error ? <p className={styles.staleNote}>Showing the last answer — the latest refresh failed.</p> : null}
      {children(slot.data)}
    </>
  );
}

function Empty({ children }) {
  return <p className={styles.quiet}>{children}</p>;
}

/* ── Cards ─────────────────────────────────────────────────────────────── */

function HealthCard({ data }) {
  const problems = componentProblems(data);
  const alerts = Array.isArray(data?.open_alerts) ? data.open_alerts : [];
  return (
    <>
      <p className={styles.lead}><StateTag state={data?.state} /></p>
      {problems.length === 0 ? <Empty>Every component is healthy.</Empty> : (
        <ul className={styles.list}>
          {problems.map((p) => (
            <li key={p.name} className={styles.item}>
              <StateTag state={p.state} words={`${p.label}: ${stateWords(p.state)}`} />
              {p.summary ? <span className={styles.sub}>{p.summary}</span> : null}
            </li>
          ))}
        </ul>
      )}
      {alerts.length > 0 ? (
        <>
          <h4 className={styles.minor}>Open alerts ({alerts.length})</h4>
          <ul className={styles.list}>
            {alerts.slice(0, 6).map((a) => (
              <li key={`${a.component}-${a.opened_at}`} className={styles.item}>
                <span>{componentLabel(a.component)} — {a.summary}</span>
                <span className={styles.sub}>
                  opened {relTime(a.opened_at) ?? '—'}{a.notify_error ? ' · alert delivery failed' : ''}
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </>
  );
}

function ReleaseCard({ data }) {
  const warnings = Array.isArray(data?.warnings) ? data.warnings : [];
  const workers = data?.workers || {};
  return (
    <>
      <dl className={styles.facts}>
        <div><dt>API build</dt><dd className={styles.mono}>{shortSha(data?.api?.git_sha)}</dd></div>
        <div><dt>Environment</dt><dd>{data?.api?.environment || 'unset'}</dd></div>
        <div>
          <dt>Workers</dt>
          <dd>
            {workers.live ? `${workers.live} live on ${(workers.live_git_shas || []).map(shortSha).join(', ') || 'unknown'}` : 'none alive'}
          </dd>
        </div>
        <div><dt>Database</dt><dd className={styles.mono}>{data?.migration_head || 'unknown'}</dd></div>
      </dl>
      {warnings.length === 0 ? <Empty>API, workers and database agree.</Empty> : (
        <ul className={styles.list}>
          {warnings.map((w) => (
            <li key={w.code} className={styles.item}><StateTag state="DEGRADED" words={w.message} /></li>
          ))}
        </ul>
      )}
    </>
  );
}

function CapabilityRow({ capabilities }) {
  return (
    <ul className={styles.caps} aria-label="Capabilities">
      {Object.entries(CAPABILITY_LABELS).map(([key, label]) => {
        const cap = capabilities?.[key];
        if (!cap) return null;
        return (
          <li key={key} title={cap.reason}>
            <StateTag state={cap.state} words={`${label}: ${stateWords(cap.state)}`} />
          </li>
        );
      })}
    </ul>
  );
}

function BrokeragesCard({ data, openId, onToggle }) {
  const rows = Array.isArray(data?.brokerages) ? data.brokerages : [];
  const summary = data?.summary || {};
  const [showAll, setShowAll] = useState(false);
  const needing = rows.filter((r) => (r.needs_action || []).length > 0);
  const fine = rows.filter((r) => (r.needs_action || []).length === 0);
  const visibleFine = showAll ? fine : [];
  return (
    <>
      <p className={styles.counts}>
        {['live', 'onboarding', 'canceled', 'suspended', 'closing'].filter((k) => summary[k]).map((k) => (
          <span key={k} className={styles.count}><b>{summary[k]}</b> {statusWords(k).toLowerCase()}</span>
        ))}
        <span className={styles.count}><b>{summary.needs_action || 0}</b> need action</span>
      </p>
      {rows.length === 0 ? <Empty>No brokerages yet.</Empty> : null}
      {needing.length === 0 && rows.length > 0 ? <Empty>No brokerage needs action.</Empty> : null}
      <ul className={styles.list}>
        {[...needing, ...visibleFine].map((b) => {
          const open = openId === b.id;
          const panelId = `diag-${b.id}`;
          return (
            <li key={b.id} className={styles.brokerage}>
              <div className={styles.brokerageHead}>
                <span className={styles.name}>{b.name || b.slug || b.id}</span>
                <span className={styles.sub}>
                  {statusWords(b.status)} · {b.agents?.active ?? 0} active agent{b.agents?.active === 1 ? '' : 's'}
                  {b.last_activity_at ? ` · active ${relTime(b.last_activity_at)}` : ''}
                </span>
                <button
                  type="button"
                  className={styles.linkBtn}
                  aria-expanded={open}
                  aria-controls={panelId}
                  onClick={() => onToggle(open ? null : b.id)}
                >
                  {open ? 'Hide diagnostics' : 'Diagnostics'}
                </button>
              </div>
              {(b.needs_action || []).length > 0 ? (
                <ul className={styles.actions} aria-label={`What ${b.name} needs`}>
                  {b.needs_action.map((a) => <li key={a.code}>{a.message}</li>)}
                </ul>
              ) : null}
              <CapabilityRow capabilities={b.capabilities} />
              {open ? <div id={panelId}><BrokerageDiagnostics tenantId={b.id} name={b.name} /></div> : null}
            </li>
          );
        })}
      </ul>
      {fine.length > 0 ? (
        <button type="button" className={styles.linkBtn} aria-expanded={showAll} onClick={() => setShowAll((v) => !v)}>
          {showAll ? 'Hide brokerages without issues' : `Show ${fine.length} brokerage${fine.length === 1 ? '' : 's'} without issues`}
        </button>
      ) : null}
      {data?.page?.has_more ? <p className={styles.quiet}>Showing the first {rows.length} of {data.page.total}.</p> : null}
      {data?.not_tracked ? (
        <p className={styles.quiet}>Not tracked: usage limits (single flat plan), support tickets (handled by email).</p>
      ) : null}
    </>
  );
}

function ProvidersCard({ ai, mls, comms }) {
  const items = providerDegradation({ ai: ai?.data, mls: mls?.data, comms: comms?.data });
  const current = ai?.data?.gateway?.current?.analysis;
  const p95 = ai?.data?.chat_24h?.latency_seconds?.p95;
  const failed = [ai, mls, comms].filter((s) => s?.error && !s?.data).length;
  return (
    <>
      {current ? (
        <p className={styles.sub}>
          AI: {current.provider} · {current.model}{p95 !== null && p95 !== undefined ? ` · p95 ${p95}s` : ''}
        </p>
      ) : null}
      {items.length === 0 ? <Empty>No provider degradation.</Empty> : (
        <ul className={styles.list}>
          {items.map((i) => (
            <li key={i.key} className={styles.item}><StateTag state={i.tone === 'bad' ? 'UNAVAILABLE' : 'DEGRADED'} words={i.text} /></li>
          ))}
        </ul>
      )}
      {failed ? <p className={styles.staleNote}>{failed} provider view{failed === 1 ? '' : 's'} could not be loaded.</p> : null}
    </>
  );
}

function WorkCard({ health, brokerages }) {
  const components = health?.components || {};
  const rows = Array.isArray(brokerages?.brokerages) ? brokerages.brokerages : [];
  const failed = rows.reduce((n, b) => n + (b.work?.failed_jobs_24h || 0), 0);
  const unresolved = rows.reduce((n, b) => n + (b.work?.unresolved_side_effects || 0), 0);
  return (
    <>
      <ul className={styles.list}>
        {WORK_COMPONENTS.filter((k) => components[k]).map((k) => (
          <li key={k} className={styles.item}>
            <StateTag state={components[k].state} words={`${componentLabel(k)}: ${stateWords(components[k].state)}`} />
            {components[k].summary ? <span className={styles.sub}>{components[k].summary}</span> : null}
          </li>
        ))}
      </ul>
      {brokerages ? (
        <p className={styles.sub}>{failed} job{failed === 1 ? '' : 's'} failed in 24h · {unresolved} send{unresolved === 1 ? '' : 's'} awaiting reconciliation</p>
      ) : null}
    </>
  );
}

function SecurityCard({ data }) {
  const alerts = (Array.isArray(data?.alerts) ? data.alerts : [])
    .filter((a) => a.severity === 'high' || a.severity === 'critical');
  if (alerts.length === 0) return <Empty>No high or critical security alerts.</Empty>;
  return (
    <ul className={styles.list}>
      {alerts.slice(0, 8).map((a) => (
        <li key={a.id} className={styles.item}>
          <StateTag state={a.severity === 'critical' ? 'UNAVAILABLE' : 'DEGRADED'} words={`${a.severity}: ${String(a.anomaly_type || 'anomaly').replace(/_/g, ' ')}`} />
          <span className={styles.sub}>{relTime(a.created_at) ?? ''}</span>
        </li>
      ))}
    </ul>
  );
}

function BillingCard({ data }) {
  const groups = [
    ['payment_issue', 'Payment issue'],
    ['no_subscription', 'No subscription'],
    ['canceled', 'Canceled'],
  ];
  const hooks = data?.webhooks || {};
  const refusals = Object.entries(hooks.refused_since_start || {});
  const usage = data?.usage_metering || {};
  const any = groups.some(([k]) => (data?.[k] || []).length) || (data?.reactivated_90d || []).length;
  return (
    <>
      {!any ? <Empty>No billing exceptions.</Empty> : null}
      {groups.map(([key, label]) => {
        const rows = data?.[key] || [];
        if (!rows.length) return null;
        return (
          <div key={key} className={styles.group}>
            <h4 className={styles.minor}>{label} ({rows.length})</h4>
            <ul className={styles.list}>
              {rows.slice(0, 8).map((r) => (
                <li key={r.tenant_id} className={styles.item}>
                  <span>{r.name || r.tenant_id}</span>
                  <span className={styles.sub}>{r.status}{r.since ? ` · since ${relTime(r.since)}` : ''}</span>
                </li>
              ))}
            </ul>
          </div>
        );
      })}
      {(data?.reactivated_90d || []).length ? (
        <p className={styles.sub}>Reactivated in the last 90 days: {data.reactivated_90d.map((r) => r.name).join(', ')}</p>
      ) : null}
      <dl className={styles.facts}>
        <div><dt>Stripe</dt><dd>{hooks.stripe_mode || 'unknown'}</dd></div>
        <div><dt>Webhook secret</dt><dd>{hooks.webhook_secret_configured ? 'configured' : 'missing'}</dd></div>
        <div><dt>Last Stripe event</dt><dd>{relTime(hooks.last_processed_at) ?? 'none recorded'}</dd></div>
        <div><dt>Usage metering</dt><dd>{usage.state === 'off' ? 'off (history only)' : usage.state === 'backlog' ? `${usage.unreported} unreported` : usage.state || 'unknown'}</dd></div>
      </dl>
      {refusals.length ? (
        <p className={styles.warnNote}>
          Webhooks refused on this replica: {refusals.map(([k, n]) => `${k.replace(/_/g, ' ')} ×${n}`).join(', ')}
        </p>
      ) : null}
    </>
  );
}

function PilotCard({ data }) {
  const t = data?.total;
  if (!t) return <Empty>No pilot activity recorded.</Empty>;
  const o = t.outreach_through_neoh || {};
  const d = t.deal_activity || {};
  return (
    <>
      <dl className={styles.metrics}>
        <div><dt>Active agents</dt><dd>{t.active_agents}</dd></div>
        <div><dt>Neoh conversation turns</dt><dd>{t.neoh_conversation_turns}</dd></div>
        <div><dt>Actions Neoh completed</dt><dd>{t.neoh_completed_actions}</dd></div>
        <div><dt>Calls · texts · emails via Neoh</dt><dd>{o.calls ?? 0} · {o.texts ?? 0} · {o.emails ?? 0}</dd></div>
        <div><dt>Buyer matches acted on</dt><dd>{t.matches_acted_on}</dd></div>
        <div>
          <dt>Closed deals associated with Neoh</dt>
          <dd>{d.associated ?? 0} of {d.closed ?? 0}{d.associated ? ` · ${fmtMoney(d.associated_deal_value)} deal value` : ''}</dd>
        </div>
      </dl>
      <p className={styles.quiet}>
        {data.window_days}-day window; deals over {data.deal_window_days} days. Associated = Neoh’s action was the last
        touch before the close — not proof it caused it. Deal value is purchase price, not commission. Time saved is not measured.
      </p>
    </>
  );
}

/* ── Overview ──────────────────────────────────────────────────────────── */

export default function OperatorOverview() {
  const [feeds, refetch] = useOperatorFeeds();
  const [openId, setOpenId] = useState(null);
  const needing = feeds.brokerages?.data?.summary?.needs_action;
  const overall = feeds.health?.data?.state;
  return (
    <section className={styles.overview} aria-labelledby="operator-overview-title">
      <header className={styles.head}>
        <h2 id="operator-overview-title" className={styles.title}>Operator overview</h2>
        <p className={styles.sub} aria-live="polite">
          {overall ? `Production ${stateWords(overall).toLowerCase()}` : 'Checking production…'}
          {typeof needing === 'number' ? ` · ${needing} brokerage${needing === 1 ? '' : 's'} need${needing === 1 ? 's' : ''} action` : ''}
        </p>
        <button type="button" className={styles.linkBtn} onClick={refetch}>Refresh</button>
      </header>

      <div className={styles.grid}>
        <Card id="op-health" title="Production health">
          <Feed slot={feeds.health} label="health" onRetry={refetch}>{(d) => <HealthCard data={d} />}</Feed>
        </Card>
        <Card id="op-release" title="Current release">
          <Feed slot={feeds.release} label="release" onRetry={refetch}>{(d) => <ReleaseCard data={d} />}</Feed>
        </Card>
      </div>

      <Card id="op-brokerages" title="Brokerages">
        <Feed slot={feeds.brokerages} label="brokerages" onRetry={refetch}>
          {(d) => <BrokeragesCard data={d} openId={openId} onToggle={setOpenId} />}
        </Feed>
      </Card>

      <div className={styles.grid}>
        <Card id="op-providers" title="Providers">
          {feeds.ai || feeds.mls || feeds.comms
            ? <ProvidersCard ai={feeds.ai} mls={feeds.mls} comms={feeds.comms} />
            : <p className={styles.quiet}>Loading providers…</p>}
        </Card>
        <Card id="op-work" title="Workers & jobs">
          <Feed slot={feeds.health} label="worker health" onRetry={refetch}>
            {(d) => <WorkCard health={d} brokerages={feeds.brokerages?.data} />}
          </Feed>
        </Card>
        <Card id="op-security" title="Security alerts">
          <Feed slot={feeds.security} label="security alerts" onRetry={refetch}>{(d) => <SecurityCard data={d} />}</Feed>
        </Card>
        <Card id="op-billing" title="Billing exceptions">
          <Feed slot={feeds.billing} label="billing exceptions" onRetry={refetch}>{(d) => <BillingCard data={d} />}</Feed>
        </Card>
      </div>

      <Card id="op-pilot" title="Pilot metrics · 7 days">
        <Feed slot={feeds.pilot} label="pilot metrics" onRetry={refetch}>{(d) => <PilotCard data={d} />}</Feed>
      </Card>
    </section>
  );
}
