import { useCallback, useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import { useOracleState } from '../state';
import { HarvestControl } from './HarvestControl';
import styles from './AdminOpsTab.module.css';

// Inline stroke glyphs — currentColor, zero icon deps (house rule).
function Svg({ children }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  );
}

const GLYPHS = {
  refresh: <Svg><path d="M20.5 12a8.5 8.5 0 1 1-2.49-6.01" /><path d="M20.5 3.5V8H16" /></Svg>,
  arrowIn: <Svg><path d="M17 7 7 17" /><path d="M15 17H7V9" /></Svg>,
  arrowOut: <Svg><path d="M7 17 17 7" /><path d="M9 7h8v8" /></Svg>,
  check: <Svg><path d="m4.5 12.5 5 5 10-11" /></Svg>,
  shield: <Svg><path d="M12 3 5 6v5c0 4.4 3 7.5 7 9 4-1.5 7-4.6 7-9V6Z" /></Svg>,
};

const fmtInt = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });

// Defensive numeric coercion — counts may arrive as strings from the DB layer.
function num(v) {
  const n = typeof v === 'string' ? Number(v) : v;
  return Number.isFinite(n) ? n : 0;
}

// Pure relative time. Accepts ISO strings, epoch seconds (floats), or epoch ms.
// Future timestamps (session expiry) render as "in 38m".
function relTime(input) {
  if (input === null || input === undefined || input === '') return null;
  let ms;
  if (typeof input === 'number') {
    ms = input > 1e12 ? input : input * 1000;
  } else {
    const d = new Date(input);
    if (Number.isNaN(d.getTime())) return null;
    ms = d.getTime();
  }
  const diff = Date.now() - ms;
  const s = Math.round(Math.abs(diff) / 1000);
  let label;
  if (s < 45) label = `${s}s`;
  else if (s < 2700) label = `${Math.max(1, Math.round(s / 60))}m`;
  else if (s < 129600) label = `${Math.round(s / 3600)}h`;
  else label = `${Math.round(s / 86400)}d`;
  return diff < 0 ? `in ${label}` : `${label} ago`;
}

function fmtUptime(sec) {
  const s = Math.max(0, Math.floor(num(sec)));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${s}s`;
}

const ENDPOINTS = {
  overview: '/api/admin/overview',
  users: '/api/admin/users',
  activity: '/api/admin/activity?limit=60',
  outbox: '/api/admin/outbox?limit=60',
  system: '/api/admin/system',
  // Three more admin_ops endpoints that shipped with no caller. They sit behind
  // different guards — billing and runtime load need platform_admin, anomalies
  // needs broker-owner-or-admin — so a broker sees the panels they are entitled
  // to and an ErrorRow on the rest, rather than the whole console failing.
  billing: '/api/admin/billing-summary',
  anomalies: '/api/admin/anomalies?limit=50',
  runtime: '/api/admin/runtime-load',
};

// Five-endpoint poller. Each key resolves independently (allSettled): a single
// failing service degrades only its own section. setState lands exclusively in
// the promise continuation — never synchronously inside the effect call stack
// (react-hooks/set-state-in-effect compliant).
function useAdminFeeds() {
  const [feeds, setFeeds] = useState({});

  const fetchAll = useCallback(() => {
    const keys = Object.keys(ENDPOINTS);
    return Promise.allSettled(keys.map((k) => crmGet(ENDPOINTS[k]))).then((results) => {
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
    const timer = setInterval(fetchAll, 15000);
    return () => clearInterval(timer);
  }, [fetchAll]);

  return [feeds, fetchAll];
}

/* ── Shared chrome ─────────────────────────────────────────────────────── */

function Section({ label, aside, children }) {
  return (
    <section className={styles.section}>
      <header className={styles.sectionHead}>
        <h2 className={styles.sectionLabel}>{label}</h2>
        {aside}
      </header>
      {children}
    </section>
  );
}

function ErrorRow({ message, onRetry }) {
  return (
    <div className={styles.errRow} role="alert">
      <span className={styles.errTick} aria-hidden="true" />
      <p className={styles.errText}>{message || 'Request failed.'}</p>
      {onRetry && <button type="button" className={styles.retryBtn} onClick={onRetry}>Retry</button>}
    </div>
  );
}

// undefined slot → skeleton; error with no data → error row; otherwise render
// (with a slim error strip above stale data when a poll cycle failed).
function Slot({ slot, onRetry, skelHeights, children }) {
  if (!slot) {
    return (
      <div className={styles.skelCol} aria-hidden="true">
        {skelHeights.map((h, i) => <div key={i} className={styles.skel} style={{ height: h }} />)}
      </div>
    );
  }
  if (slot.error && slot.data === null) return <ErrorRow message={slot.error?.message} onRetry={onRetry} />;
  return (
    <>
      {slot.error ? <ErrorRow message={slot.error?.message} onRetry={onRetry} /> : null}
      {children(slot.data)}
    </>
  );
}

/* ── BILLING / ANOMALIES / RUNTIME ─────────────────────────────────────────
   Three admin_ops endpoints that existed with no UI. Billing states plainly
   when Stripe did not answer, because a zero from our own subscriptions table
   and a zero because the payment processor was unreachable are different facts,
   and only one of them is about revenue. */

function BillingPanel({ data }) {
  const tenants = Array.isArray(data?.tenants) ? data.tenants : [];
  const byStatus = data?.subscriptions_by_status || {};
  const revenue = data?.revenue || {};
  const stripeDown = revenue.stripe_ok === false;
  return (
    <>
      <div className={styles.sysStrip}>
        {Object.entries(byStatus).map(([status, count]) => (
          <SysItem key={status} label={status} value={fmtInt.format(num(count))} />
        ))}
        <SysItem label="MRR" value={stripeDown ? 'unknown' : (revenue.mrr ?? '—')} />
        <SysItem label="MTD" value={stripeDown ? 'unknown' : (revenue.mtd_revenue ?? '—')} />
      </div>
      {stripeDown ? (
        <p className={styles.quietNote}>
          Stripe did not answer, so MRR and month-to-date revenue are unknown — not zero.
          The subscription counts come from our own database and are unaffected.
        </p>
      ) : null}
      {tenants.length > 0 ? (
        <ul className={styles.rowList} role="list">
          {tenants.slice(0, 12).map((row) => (
            <li key={row.tenant_id} className={styles.userRow}>
              <div className={styles.rowMain}>
                <span className={styles.rowTitle}>{row.tenant_name || row.tenant_id}</span>
                <span className={styles.rowSub}>{row.plan || 'no plan'}</span>
              </div>
              <span className={styles.statusChip}>{row.status}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className={styles.quietNote}>No subscriptions recorded.</p>
      )}
    </>
  );
}

function AnomaliesPanel({ data }) {
  const alerts = Array.isArray(data?.alerts) ? data.alerts : [];
  if (alerts.length === 0) {
    return <p className={styles.quietNote}>No anomaly alerts recorded.</p>;
  }
  return (
    <ul className={styles.rowList} role="list">
      {alerts.slice(0, 20).map((alert) => (
        <li key={alert.id} className={styles.userRow}>
          <div className={styles.rowMain}>
            <span className={styles.rowTitle}>
              {alert.description || alert.anomaly_type || 'Anomaly'}
            </span>
            {alert.detected_at ? (
              <span className={styles.rowSub}>{new Date(alert.detected_at).toLocaleString()}</span>
            ) : null}
          </div>
          <span className={styles.roleChip} data-role={
            alert.severity === 'critical' || alert.severity === 'high' ? 'admin' : 'std'
          }>
            {alert.severity || 'unknown'}
          </span>
        </li>
      ))}
    </ul>
  );
}

function RuntimeLoadPanel({ data }) {
  const pool = data?.db_pool || {};
  const sockets = data?.websockets || {};
  return (
    <div className={styles.sysStrip}>
      <SysItem label="Sockets" value={fmtInt.format(num(sockets.total))} />
      <SysItem label="Tenants" value={fmtInt.format(num(sockets.tenants))} />
      <SysItem label="Tasks" value={fmtInt.format(num(data?.asyncio_tasks))} />
      <SysItem label="Pool free" value={fmtInt.format(num(pool.usable_for_requests))} />
      <SysItem label="LLM/min" value={fmtInt.format(num(data?.ambient_llm_calls_last_minute))} />
    </div>
  );
}

/* ── 1 · SYSTEM strip ──────────────────────────────────────────────────── */

function SysItem({ label, value }) {
  return (
    <span className={styles.sysItem}>
      <span className={styles.sysValue}>{value}</span>
      <span className={styles.sysLabel}>{label}</span>
    </span>
  );
}

function SystemStrip({ data }) {
  const db = data?.db || {};
  const online = db.status === 'online';
  return (
    <div className={styles.sysStrip}>
      <span className={styles.sysCore} title={db.version || undefined}>
        <span className={`${styles.dot} ${online ? styles.dotOnline : styles.dotOffline}`} aria-hidden="true" />
        <span className={styles.sysCoreLabel}>Memory Core</span>
      </span>
      <SysItem label="Uptime" value={fmtUptime(data?.uptime_seconds)} />
      <SysItem label="DB Conn" value={fmtInt.format(num(db.connections))} />
      <SysItem label="Sessions" value={fmtInt.format(num(data?.active_sessions))} />
      <SysItem label="Watchers" value={fmtInt.format(num(data?.firehose_watchers))} />
    </div>
  );
}

/* ── 2 · FLEET totals ──────────────────────────────────────────────────── */

const TOTAL_KEYS = [
  ['tenants', 'Tenants'], ['listings', 'Listings'], ['clients', 'Clients'], ['leads', 'Leads'],
  ['showings', 'Showings'], ['agents', 'Agents'], ['queued_emails', 'Queued Mail'],
];

function FleetTotals({ totals }) {
  return (
    <dl className={styles.totals}>
      {TOTAL_KEYS.map(([k, label]) => (
        <div key={k} className={styles.totalCell}>
          <dt className={styles.totalLabel}>{label}</dt>
          <dd className={styles.totalValue}>{fmtInt.format(num(totals?.[k]))}</dd>
        </div>
      ))}
    </dl>
  );
}

/* ── 3 · LIVE FEED ─────────────────────────────────────────────────────── */

function LiveFeedPanel() {
  const state = useOracleState();
  const feed = Array.isArray(state?.liveFeed) ? state.liveFeed : [];
  const entries = feed.slice(0, 30); // reducer prepends — already newest-first
  const newestId = entries[0]?.id ?? null;
  // Pulse only for frames that arrive after mount. The lazy useState pins the
  // head entry seen at mount (initialized exactly once, never re-set); tracking
  // length alone breaks once the reducer's 50-cap makes length stop growing
  // while new frames still land — head identity does not. Each new frame
  // changes `newestId`, remounting the tick (key) and replaying the one-shot
  // CSS pulse. No effects, no setState-in-effect, no ref reads in render.
  const [mountHeadId] = useState(newestId);
  const grew = newestId !== null && newestId !== mountHeadId;

  return (
    <Section
      label="Live Feed"
      aside={
        <span className={styles.liveBadge}>
          <span
            key={grew ? String(newestId) : 'idle'}
            className={`${styles.liveTick} ${grew ? styles.liveTickPulse : ''}`}
            aria-hidden="true"
          />
          Live
        </span>
      }
    >
      {entries.length === 0 ? (
        <p className={styles.quietNote}>Awaiting live frames — the firehose mirrors every tenant’s traffic here.</p>
      ) : (
        <ul className={styles.feedList} role="list">
          {entries.map((e, i) => {
            const tag = e.tag ?? e.type ?? e.kind;
            const who = e.actorName ?? e.agent;
            const text = e.text ?? e.label ?? e.summary;
            const when = relTime(e.timestamp);
            const tenant = e.source_tenant ?? null;
            return (
              <li key={e.id ?? i} className={styles.feedRow}>
                <div className={styles.feedTop}>
                  {tag != null && <span className={styles.feedTag}>{String(tag)}</span>}
                  {e.metric != null && <span className={styles.feedMetric}>{String(e.metric)}</span>}
                  {when && <span className={styles.rowWhen}>{when}</span>}
                </div>
                {(who || text) && (
                  <p className={styles.feedText}>
                    {who ? <span className={styles.feedWho}>{String(who)} </span> : null}
                    {text ? String(text) : null}
                  </p>
                )}
                {tenant != null && <span className={styles.feedTenant}>{String(tenant)}</span>}
              </li>
            );
          })}
        </ul>
      )}
    </Section>
  );
}

/* ── 4 · TENANTS ───────────────────────────────────────────────────────── */

const TENANT_COUNTS = [
  ['listings', 'listings'], ['clients', 'clients'], ['leads', 'leads'],
  ['showings', 'showings'], ['agents', 'agents'], ['queued_emails', 'queued'],
];

function TenantsPanel({ slot, onRetry }) {
  return (
    <Section label="Tenants">
      <Slot slot={slot} onRetry={onRetry} skelHeights={[108, 108]}>
        {(data) => {
          const tenants = Array.isArray(data?.tenants) ? data.tenants : [];
          const ws = data?.ws_groups || {};
          if (tenants.length === 0) return <p className={styles.quietNote}>No tenants registered.</p>;
          return (
            <ul className={styles.cardList} role="list">
              {tenants.map((t) => {
                const socks = num(ws[String(t.id)]);
                const last = relTime(t.last_activity_at);
                return (
                  <li key={t.id} className={styles.card}>
                    <header className={styles.cardHead}>
                      <span className={styles.cardTitle}>{t.name || t.slug || t.id}</span>
                      {t.slug && <span className={styles.cardSub}>/{t.slug}</span>}
                      {socks > 0 && (
                        <span className={styles.sockChip}>{fmtInt.format(socks)} {socks === 1 ? 'socket' : 'sockets'}</span>
                      )}
                    </header>
                    <div className={styles.chipRow}>
                      {TENANT_COUNTS.map(([k, label]) => (
                        <span key={k} className={styles.chip}>
                          <b className={styles.chipNum}>{fmtInt.format(num(t[k]))}</b> {label}
                        </span>
                      ))}
                    </div>
                    <footer className={styles.cardMeta}>{last ? `Last activity ${last}` : 'No activity recorded'}</footer>
                  </li>
                );
              })}
            </ul>
          );
        }}
      </Slot>
    </Section>
  );
}

/* ── 5 · USERS ─────────────────────────────────────────────────────────── */

function UsersPanel({ slot, onRetry }) {
  const onlineCount = slot?.data ? fmtInt.format(num(slot.data.online)) : null;
  return (
    <Section
      label="Users"
      aside={onlineCount !== null ? <span className={styles.asideCount}>{onlineCount} online</span> : null}
    >
      <Slot slot={slot} onRetry={onRetry} skelHeights={[52, 52, 52]}>
        {(data) => {
          const users = Array.isArray(data?.users) ? data.users : [];
          if (users.length === 0) return <p className={styles.quietNote}>No users registered.</p>;
          return (
            <ul className={styles.rowList} role="list">
              {users.map((u) => (
                <li key={`${u.tenant_id}:${u.agent_id}`} className={styles.userRow}>
                  <span className={`${styles.dot} ${u.online ? styles.dotOnline : styles.dotIdle}`} aria-hidden="true" />
                  <div className={styles.rowMain}>
                    <span className={styles.rowTitle}>{u.agent_id}</span>
                    {(u.display_name || u.brokerage || u.tenant_name) && (
                      <span className={styles.rowSub}>
                        {[u.display_name, u.brokerage, u.tenant_name].filter(Boolean).join(' · ')}
                      </span>
                    )}
                  </div>
                  <span className={styles.roleChip} data-role={u.role === 'platform_admin' ? 'admin' : 'std'}>
                    {u.role || '—'}
                  </span>
                  <span
                    className={u.has_profile ? styles.tickOn : styles.tickOff}
                    title={u.has_profile ? 'Profile complete' : 'No public profile'}
                    aria-hidden="true"
                  >
                    {u.has_profile ? GLYPHS.check : '·'}
                  </span>
                  <span className={styles.rowWhen}>
                    {u.online ? `exp ${relTime(u.session_expires_at) ?? '—'}` : 'offline'}
                  </span>
                </li>
              ))}
            </ul>
          );
        }}
      </Slot>
    </Section>
  );
}

/* ── 6 · OUTBOX ────────────────────────────────────────────────────────── */

function OutboxPanel({ slot, onRetry }) {
  return (
    <Section label="Outbox">
      <Slot slot={slot} onRetry={onRetry} skelHeights={[36, 56, 56]}>
        {(data) => {
          const counts = data?.counts || {};
          const emails = Array.isArray(data?.emails) ? data.emails : [];
          const countKeys = Object.keys(counts);
          return (
            <>
              {countKeys.length > 0 && (
                <div className={styles.chipRow}>
                  {countKeys.map((k) => (
                    <span key={k} className={styles.countChip} data-status={k}>
                      <b className={styles.chipNum}>{fmtInt.format(num(counts[k]))}</b> {k}
                    </span>
                  ))}
                </div>
              )}
              {emails.length === 0 ? (
                <p className={styles.quietNote}>Outbox is empty.</p>
              ) : (
                <ul className={styles.rowList} role="list">
                  {emails.map((m) => (
                    <li key={m.id} className={styles.mailRow}>
                      <div className={styles.rowMain}>
                        <span className={styles.rowTitle}>{m.to_email || '—'}</span>
                        <span className={styles.rowSub}>{m.subject || 'No subject'}</span>
                        {m.status === 'failed' && m.error && <span className={styles.mailErr}>{m.error}</span>}
                      </div>
                      <span className={styles.statusChip} data-status={m.status || 'unknown'}>{m.status || '—'}</span>
                      <span className={styles.rowAside}>
                        {m.tenant_name && <span className={styles.rowSub}>{m.tenant_name}</span>}
                        <span className={styles.rowWhen}>{relTime(m.created_at) ?? ''}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </>
          );
        }}
      </Slot>
    </Section>
  );
}

/* ── 7 · ACTIVITY ──────────────────────────────────────────────────────── */

function ActivityPanel({ slot, onRetry }) {
  return (
    <Section label="Activity">
      <Slot slot={slot} onRetry={onRetry} skelHeights={[52, 52, 52]}>
        {(data) => {
          const interactions = Array.isArray(data?.interactions) ? data.interactions : [];
          const audit = Array.isArray(data?.audit) ? data.audit : [];
          const rows = [
            ...interactions.map((x) => ({ kind: 'int', ts: Date.parse(x.created_at) || 0, x })),
            ...audit.map((x) => ({ kind: 'aud', ts: Date.parse(x.created_at) || 0, x })),
          ].sort((a, b) => b.ts - a.ts).slice(0, 40);
          if (rows.length === 0) return <p className={styles.quietNote}>No activity recorded yet.</p>;
          return (
            <ul className={styles.rowList} role="list">
              {rows.map((r, i) => (
                <li key={r.kind === 'int' ? `i-${r.x.id}` : `a-${r.ts}-${i}`} className={styles.actRow}>
                  <span
                    className={styles.actGlyph}
                    data-dir={r.kind === 'aud' ? 'audit' : r.x.direction || 'outbound'}
                    aria-hidden="true"
                  >
                    {r.kind === 'aud' ? GLYPHS.shield : r.x.direction === 'inbound' ? GLYPHS.arrowIn : GLYPHS.arrowOut}
                  </span>
                  <div className={styles.rowMain}>
                    <span className={styles.rowTitle}>
                      {r.kind === 'int'
                        ? [r.x.interaction_type, r.x.subject].filter(Boolean).join(' — ') || 'interaction'
                        : [r.x.category, r.x.action].filter(Boolean).join(' / ') || 'audit'}
                    </span>
                    <span className={styles.rowSub}>
                      {r.kind === 'int'
                        ? [r.x.client_name, r.x.tenant_name].filter(Boolean).join(' · ')
                        : [r.x.user_id, r.x.tenant_id].filter(Boolean).join(' · ')}
                    </span>
                  </div>
                  <span className={styles.rowWhen}>{relTime(r.x.created_at) ?? ''}</span>
                </li>
              ))}
            </ul>
          );
        }}
      </Slot>
    </Section>
  );
}

/**
 * AdminOpsTab — platform-admin OPS console. Five real /api/admin endpoints,
 * polled every 15 s, plus the firehose live feed from Oracle state. Renders
 * only what the backend returns; never simulates data.
 */
export default function AdminOpsTab() {
  const [feeds, refetch] = useAdminFeeds();
  const [refreshing, setRefreshing] = useState(false);
  const loading = Object.keys(feeds).length === 0;

  const refresh = () => {
    setRefreshing(true);
    refetch().finally(() => setRefreshing(false));
  };

  return (
    <section className={styles.wrap} aria-label="Platform operations console" aria-busy={loading || refreshing}>
      <header className={styles.topRow}>
        <span className={styles.kicker}>Platform Ops</span>
        <button
          type="button"
          className={`${styles.refreshBtn} ${refreshing ? styles.refreshing : ''}`}
          onClick={refresh}
          disabled={refreshing || loading}
          aria-label="Refresh all panels"
        >
          {GLYPHS.refresh}
        </button>
      </header>

      <Section label="System">
        <Slot slot={feeds.system} onRetry={refetch} skelHeights={[64]}>
          {(data) => <SystemStrip data={data} />}
        </Slot>
      </Section>

      <Section label="Fleet">
        <Slot slot={feeds.overview} onRetry={refetch} skelHeights={[150]}>
          {(data) => <FleetTotals totals={data?.totals} />}
        </Slot>
      </Section>

      <LiveFeedPanel />
      <TenantsPanel slot={feeds.overview} onRetry={refetch} />
      <UsersPanel slot={feeds.users} onRetry={refetch} />
      <OutboxPanel slot={feeds.outbox} onRetry={refetch} />
      <ActivityPanel slot={feeds.activity} onRetry={refetch} />

      <Section label="Billing">
        <Slot slot={feeds.billing} onRetry={refetch} skelHeights={[110]}>
          {(data) => <BillingPanel data={data} />}
        </Slot>
      </Section>

      <Section label="Anomalies">
        <Slot slot={feeds.anomalies} onRetry={refetch} skelHeights={[110]}>
          {(data) => <AnomaliesPanel data={data} />}
        </Slot>
      </Section>

      <Section label="Runtime load">
        <Slot slot={feeds.runtime} onRetry={refetch} skelHeights={[80]}>
          {(data) => <RuntimeLoadPanel data={data} />}
        </Slot>
      </Section>

      <HarvestControl />
    </section>
  );
}
