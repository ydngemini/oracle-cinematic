import { useEffect, useRef, useState } from 'react';
import { crmGet } from '../../state/useCrmApi';
import StateTag from './StateTag';
import { CAPABILITY_LABELS, relTime, shortSha, stateWords } from './operatorModel';
import styles from './OperatorOverview.module.css';

// One brokerage's support bundle (GET /api/admin/brokerages/{id}/diagnostics).
// The backend has already removed message bodies, transcripts, documents,
// credentials and contact numbers; this view renders what it was given.

function Rows({ items, empty, render }) {
  if (!items || items.length === 0) return <p className={styles.quiet}>{empty}</p>;
  return <ul className={styles.list}>{items.map(render)}</ul>;
}

function Block({ title, children }) {
  return (
    <section className={styles.block}>
      <h5 className={styles.minor}>{title}</h5>
      {children}
    </section>
  );
}

export default function BrokerageDiagnostics({ tenantId, name }) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  const [copied, setCopied] = useState('');
  const headingRef = useRef(null);

  useEffect(() => {
    let alive = true;
    crmGet(`/api/admin/brokerages/${encodeURIComponent(tenantId)}/diagnostics`).then(
      (data) => { if (alive) setState({ data, error: null, loading: false }); },
      (error) => { if (alive) setState({ data: null, error, loading: false }); },
    );
    return () => { alive = false; };
  }, [tenantId]);

  useEffect(() => {
    // Move focus to the panel once it has content, so a keyboard user who
    // pressed "Diagnostics" lands on what opened.
    if (!state.loading) headingRef.current?.focus();
  }, [state.loading]);

  const copy = () => {
    const text = JSON.stringify(state.data, null, 2);
    const done = () => setCopied('Copied');
    const failed = () => setCopied('Copy failed — select and copy manually');
    try {
      const write = globalThis.navigator?.clipboard?.writeText;
      if (!write) { failed(); return; }
      write.call(globalThis.navigator.clipboard, text).then(done, failed);
    } catch {
      failed();
    }
  };

  if (state.loading) return <p className={styles.quiet} aria-live="polite">Loading diagnostics…</p>;
  if (state.error) {
    return (
      <div className={styles.error} role="alert">
        <h4 ref={headingRef} tabIndex={-1} className={styles.minor}>Diagnostics for {name}</h4>
        <p>Couldn’t load diagnostics{state.error?.status === 404 ? ' — brokerage not found' : ''}.</p>
      </div>
    );
  }
  const d = state.data || {};
  const plan = d.plan || {};
  const voice = d.voice || {};
  const messaging = d.messaging || {};
  const jobs = d.jobs || {};
  const errors = d.provider_errors || {};
  return (
    <div className={styles.diag}>
      <div className={styles.diagHead}>
        <h4 ref={headingRef} tabIndex={-1} className={styles.minor}>Diagnostics for {d.tenant?.name || name}</h4>
        <button type="button" className={styles.linkBtn} onClick={copy}>Copy bundle</button>
        {copied ? <span className={styles.sub} role="status">{copied}</span> : null}
      </div>

      <dl className={styles.facts}>
        <div><dt>Tenant ID</dt><dd className={styles.mono}>{d.tenant?.id}</dd></div>
        <div><dt>Account</dt><dd>{d.tenant?.lifecycle_state || 'unknown'}</dd></div>
        <div><dt>Plan</dt><dd>{plan.status === 'none' ? 'No subscription' : `${plan.plan || 'plan'} · ${plan.status}`}</dd></div>
        <div><dt>Agents</dt><dd>{d.agents ? `${d.agents.active} active of ${d.agents.total} · ${d.agents.owners} owner${d.agents.owners === 1 ? '' : 's'}` : 'unknown'}</dd></div>
        <div><dt>Release</dt><dd className={styles.mono}>{shortSha(d.release?.api?.git_sha)}</dd></div>
        {plan.stripe_customer_ref ? <div><dt>Stripe customer</dt><dd className={styles.mono}>{plan.stripe_customer_ref}</dd></div> : null}
      </dl>

      {(d.needs_action || []).length ? (
        <Block title="Needs action">
          <ul className={styles.actions}>{d.needs_action.map((a) => <li key={a.code}>{a.message}</li>)}</ul>
        </Block>
      ) : null}

      <Block title="Capabilities">
        <ul className={styles.list}>
          {Object.entries(CAPABILITY_LABELS).map(([key, label]) => {
            const cap = d.capabilities?.[key];
            if (!cap) return null;
            return (
              <li key={key} className={styles.item}>
                <StateTag state={cap.state} words={`${label}: ${stateWords(cap.state)}`} />
                <span className={styles.sub}>{cap.reason}</span>
              </li>
            );
          })}
        </ul>
      </Block>

      <Block title="Voice routing">
        <Rows
          items={voice.routes}
          empty="No phone routes."
          render={(r) => (
            <li key={`${r.agent_id}-${r.business_number}`} className={styles.item}>
              <span>{r.agent_id} · {r.business_number || 'no number'} · {r.provider}{r.active ? '' : ' · inactive'}</span>
              <span className={styles.sub}>
                caller ID {r.outbound_verification} · forwarding {r.inbound_forwarding}
                {r.outbound_failure ? ` · ${r.outbound_failure}` : ''}{r.inbound_failure ? ` · ${r.inbound_failure}` : ''}
              </span>
            </li>
          )}
        />
        {voice.inbound_calls_24h ? (
          <p className={styles.sub}>Inbound calls 24h: {voice.inbound_calls_24h.total} ({voice.inbound_calls_24h.failed} failed)</p>
        ) : null}
      </Block>

      <Block title="Texting & 10DLC">
        <Rows
          items={messaging.routes}
          empty="No texting routes."
          render={(r) => (
            <li key={`${r.agent_id}-${r.provider}`} className={styles.item}>
              <span>{r.agent_id} · {r.provider} · hosting {r.hosting}</span>
              <span className={styles.sub}>eligibility {r.eligibility}{r.hosting_failure ? ` · ${r.hosting_failure}` : ''}</span>
            </li>
          )}
        />
        <p className={styles.sub}>
          Brand: {messaging.brand_10dlc ? `${messaging.brand_10dlc.status}${messaging.brand_10dlc.failure_reason ? ` — ${messaging.brand_10dlc.failure_reason}` : ''}` : 'not registered'}
          {(messaging.campaigns_10dlc || []).length ? ` · Campaigns: ${messaging.campaigns_10dlc.map((c) => c.status).join(', ')}` : ''}
        </p>
        {messaging.outbound_sms_24h ? (
          <p className={styles.sub}>Outbound texts 24h: {messaging.outbound_sms_24h.total} ({messaging.outbound_sms_24h.failed} failed)</p>
        ) : null}
      </Block>

      <Block title="Email & calendar">
        <Rows
          items={d.email_calendar?.connections}
          empty={`No mailbox or calendar connected${d.email_calendar?.self_reported_status ? ` (brokerage reports: ${d.email_calendar.self_reported_status.toLowerCase()})` : ''}.`}
          render={(c, i) => (
            <li key={`${c.provider}-${i}`} className={styles.item}>
              <span>{c.provider}{c.calendar_scope ? ' · calendar' : ''} · {c.disabled ? 'disabled' : c.validation_status}</span>
              {c.expires_at ? <span className={styles.sub}>expires {relTime(c.expires_at)}</span> : null}
            </li>
          )}
        />
      </Block>

      <Block title="MLS">
        <Rows
          items={d.mls}
          empty="No MLS feed entitled."
          render={(f) => (
            <li key={f.mls_id} className={styles.item}>
              <StateTag state={f.health} words={`${f.mls_name}: ${stateWords(f.health)}`} />
              <span className={styles.sub}>
                {f.licensed ? 'licensed' : 'developer/reference data'}
                {f.last_success_at ? ` · last success ${relTime(f.last_success_at)}` : ' · never synced'}
                {f.error_class ? ` · ${f.error_class} error` : ''}
              </span>
            </li>
          )}
        />
      </Block>

      <Block title="Failed & retrying work (7 days)">
        <Rows
          items={jobs.groups}
          empty="No failed or retrying jobs."
          render={(g) => (
            <li key={`${g.job_type}-${g.state}-${g.error_code}`} className={styles.item}>
              <span>{g.job_type} · {g.state} × {g.count}</span>
              <span className={styles.sub}>{g.error_code || 'no code'} · {relTime(g.last_at) ?? ''}</span>
            </li>
          )}
        />
        <Rows
          items={d.side_effects?.groups}
          empty="No calls, texts or emails awaiting reconciliation."
          render={(g) => (
            <li key={`${g.command_type}-${g.state}-${g.provider}`} className={styles.item}>
              <span>{g.command_type} · {g.state.replace(/_/g, ' ')} × {g.count}</span>
              <span className={styles.sub}>{g.provider || 'provider unknown'} · {relTime(g.last_at) ?? ''}</span>
            </li>
          )}
        />
      </Block>

      <Block title="Provider error categories (7 days)">
        <p className={styles.sub}>
          AI: {(errors.ai_responses || []).map((e) => `${e.code} ×${e.count}`).join(', ') || 'none'}
          {' · '}Tools: {(errors.ai_tools || []).map((e) => `${e.tool}/${e.code} ×${e.count}`).join(', ') || 'none'}
          {' · '}Texts: {(errors.sms || []).map((e) => `${e.code} ×${e.count}`).join(', ') || 'none'}
          {' · '}Email failures: {errors.email_failed ?? 0}
        </p>
      </Block>

      <Block title="Recent audit (actions only)">
        <Rows
          items={(d.recent_audit || []).slice(0, 10)}
          empty="No audit entries."
          render={(a, i) => (
            <li key={`${a.at}-${i}`} className={styles.item}>
              <span>{a.category} / {a.action}</span>
              <span className={styles.sub}>{relTime(a.at) ?? ''}</span>
            </li>
          )}
        />
      </Block>

      {(d.unavailable || []).length ? (
        <p className={styles.warnNote}>Could not read: {d.unavailable.join(', ')}.</p>
      ) : null}
      <p className={styles.quiet}>Never included: {(d.excluded || []).join(', ')}.</p>
    </div>
  );
}
