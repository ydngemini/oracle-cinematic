import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  CheckCircle2,
  Clipboard,
  Link2,
  RefreshCw,
  Route,
  Save,
  ShieldCheck,
  ToggleLeft,
  ToggleRight,
  UserRoundCheck,
  XCircle,
} from 'lucide-react';
import { crmGet, crmPatch, crmPost, crmPut } from '../state/useCrmApi';
import styles from './SalesWorkspace.module.css';

const EMPTY_CONNECTOR = { source_key: '', name: '' };
const EMPTY_RULE = {
  name: '', priority: 100, enabled: true, source_key: '', zip_codes: '', state_codes: '',
  intent: 'any', assignment_mode: 'round_robin', agent_ids: [],
};

function errorText(error) {
  const detail = error?.payload?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map((item) => item?.msg).filter(Boolean).join(' · ');
  return error?.message || 'The routing action could not be completed.';
}

function splitList(value, transform = (item) => item) {
  return [...new Set(String(value || '').split(',').map((item) => transform(item.trim())).filter(Boolean))];
}

function formatDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

async function copyText(value) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = value;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.append(textarea);
  textarea.select();
  document.execCommand('copy');
  textarea.remove();
}

export default function LeadRoutingPage() {
  const [connectors, setConnectors] = useState([]);
  const [rules, setRules] = useState([]);
  const [agents, setAgents] = useState([]);
  const [events, setEvents] = useState([]);
  const [metrics, setMetrics] = useState({ totals: {}, by_source: [], by_agent: [] });
  const [agentDrafts, setAgentDrafts] = useState({});
  const [connector, setConnector] = useState(EMPTY_CONNECTOR);
  const [rule, setRule] = useState(EMPTY_RULE);
  const [secret, setSecret] = useState(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [connectorResponse, ruleResponse, agentResponse, eventResponse, metricResponse] = await Promise.all([
        crmGet('/api/crm/routing/connectors', { retries: 0 }),
        crmGet('/api/crm/routing/rules', { retries: 0 }),
        crmGet('/api/crm/routing/agents', { retries: 0 }),
        crmGet('/api/crm/routing/events?limit=50', { retries: 0 }),
        crmGet('/api/crm/routing/metrics?days=30', { retries: 0 }),
      ]);
      const nextAgents = agentResponse?.agents || [];
      setConnectors(connectorResponse?.connectors || []);
      setRules(ruleResponse?.rules || []);
      setAgents(nextAgents);
      setEvents(eventResponse?.events || []);
      setMetrics(metricResponse || { totals: {}, by_source: [], by_agent: [] });
      setAgentDrafts(Object.fromEntries(nextAgents.map((agent) => [agent.agent_id, {
        accepting_leads: agent.accepting_leads !== false,
        capacity: Number(agent.capacity) || 0,
      }])));
    } catch (loadError) {
      setError(errorText(loadError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const initial = Promise.resolve().then(load);
    return () => { void initial; };
  }, [load]);

  const activeConnectors = useMemo(
    () => connectors.filter((item) => item.active !== false),
    [connectors],
  );

  const createConnector = async (event) => {
    event.preventDefault();
    setWorking('connector:create'); setError(''); setMessage(''); setSecret(null);
    try {
      const response = await crmPost('/api/crm/routing/connectors', connector);
      setSecret({
        value: response.webhook_secret_once,
        path: response.webhook_path,
        publicId: response.connector?.public_id,
      });
      setConnector(EMPTY_CONNECTOR);
      setMessage('Connector created. Copy its secret now; it will not be shown again.');
      await load();
    } catch (createError) {
      setError(errorText(createError));
    } finally {
      setWorking('');
    }
  };

  const toggleConnector = async (item) => {
    setWorking(`connector:${item.id}`); setError(''); setMessage('');
    try {
      await crmPatch(`/api/crm/routing/connectors/${encodeURIComponent(item.id)}`, {
        active: item.active === false,
      });
      setMessage(`${item.name} ${item.active === false ? 'enabled' : 'paused'}.`);
      await load();
    } catch (toggleError) {
      setError(errorText(toggleError));
    } finally {
      setWorking('');
    }
  };

  const createRule = async (event) => {
    event.preventDefault();
    setWorking('rule:create'); setError(''); setMessage('');
    try {
      await crmPost('/api/crm/routing/rules', {
        ...rule,
        priority: Number(rule.priority),
        source_key: rule.source_key || null,
        zip_codes: splitList(rule.zip_codes),
        state_codes: splitList(rule.state_codes, (item) => item.toUpperCase()),
      });
      setRule(EMPTY_RULE);
      setMessage('Routing rule created and added to the priority stack.');
      await load();
    } catch (createError) {
      setError(errorText(createError));
    } finally {
      setWorking('');
    }
  };

  const toggleRule = async (item) => {
    setWorking(`rule:${item.id}`); setError(''); setMessage('');
    try {
      await crmPut(`/api/crm/routing/rules/${encodeURIComponent(item.id)}`, {
        name: item.name,
        priority: item.priority,
        enabled: !item.enabled,
        source_key: item.source_key,
        zip_codes: item.zip_codes || [],
        state_codes: item.state_codes || [],
        intent: item.intent,
        assignment_mode: item.assignment_mode,
        agent_ids: item.agent_ids || [],
      });
      setMessage(`${item.name} ${item.enabled ? 'paused' : 'enabled'}.`);
      await load();
    } catch (toggleError) {
      setError(errorText(toggleError));
    } finally {
      setWorking('');
    }
  };

  const saveAgent = async (agentId) => {
    const draft = agentDrafts[agentId];
    if (!draft) return;
    setWorking(`agent:${agentId}`); setError(''); setMessage('');
    try {
      await crmPut(`/api/crm/routing/agents/${encodeURIComponent(agentId)}`, {
        accepting_leads: Boolean(draft.accepting_leads),
        capacity: Number(draft.capacity),
      });
      setMessage(`${agentId} routing capacity saved.`);
      await load();
    } catch (saveError) {
      setError(errorText(saveError));
    } finally {
      setWorking('');
    }
  };

  const totals = metrics?.totals || {};

  return (
    <div className={styles.page}>
      <div className={styles.pageIntro}>
        <div>
          <h3>Lead intake and routing</h3>
          <p>Capture signed lead webhooks, preserve source evidence, deduplicate contacts, and assign only to active agents who still have capacity.</p>
        </div>
        <button type="button" className={styles.secondaryButton} onClick={load} disabled={loading || Boolean(working)}><RefreshCw aria-hidden="true" /> Refresh</button>
      </div>

      <div className={styles.notice}>
        <ShieldCheck aria-hidden="true" />
        <span>Webhook bodies and connector secrets are encrypted. Duplicate event IDs are idempotent, mismatched replays are rejected, and existing contact ownership is preserved.</span>
      </div>
      {error ? <div className={styles.error} role="alert"><XCircle aria-hidden="true" /> {error}</div> : null}
      {message ? <div className={styles.success} role="status"><CheckCircle2 aria-hidden="true" /> {message}</div> : null}

      <div className={styles.metricGrid} aria-label="Lead routing metrics for the last 30 days">
        <div className={styles.metricCard}><span>Received</span><strong>{totals.received || 0}</strong><small>signed intake events</small></div>
        <div className={styles.metricCard}><span>Routed</span><strong>{totals.routed || 0}</strong><small>{Math.round((totals.routing_rate || 0) * 100)}% routing rate</small></div>
        <div className={styles.metricCard}><span>Unassigned</span><strong>{totals.unassigned || 0}</strong><small>needs capacity or a rule</small></div>
        <div className={styles.metricCard}><span>Contacts</span><strong>{totals.unique_contacts || 0}</strong><small>unique canonical records</small></div>
      </div>

      {secret ? (
        <section className={styles.resultCard} aria-labelledby="connector-secret-title">
          <h4 id="connector-secret-title">Copy this connector secret now</h4>
          <p>The secret is shown once. Store it in the lead provider’s secret manager and sign the exact raw JSON body with the Unix timestamp.</p>
          <pre className={styles.codeBlock}>{JSON.stringify({
            endpoint: `${window.location.origin}${secret.path}`,
            headers: {
              'X-Oracle-Timestamp': '&lt;unix timestamp&gt;',
              'X-Oracle-Signature': 'sha256=&lt;HMAC of timestamp.raw_body&gt;',
            },
            secret: secret.value,
          }, null, 2)}</pre>
          <div className={styles.buttonRow}>
            <button type="button" className={styles.primaryButton} onClick={() => copyText(secret.value).then(() => setMessage('Webhook secret copied.')).catch(() => setError('Clipboard access was denied. Select and copy the secret manually.'))}><Clipboard aria-hidden="true" /> Copy secret</button>
            <button type="button" className={styles.secondaryButton} onClick={() => copyText(`${window.location.origin}${secret.path}`).then(() => setMessage('Webhook endpoint copied.')).catch(() => setError('Clipboard access was denied. Select and copy the endpoint manually.'))}><Link2 aria-hidden="true" /> Copy endpoint</button>
          </div>
        </section>
      ) : null}

      <div className={styles.twoColumn}>
        <section className={styles.panel} aria-labelledby="connector-title">
          <header className={styles.panelHeader}><div><h4 id="connector-title">Lead connectors</h4><p>Zillow, Realtor.com, Meta, websites, or any signed source</p></div><span className={styles.badge} data-tone={activeConnectors.length ? 'good' : 'warn'}>{activeConnectors.length} active</span></header>
          <form className={styles.panelBody} onSubmit={createConnector}>
            <div className={styles.field}><label htmlFor="connector-name">Connector name</label><input id="connector-name" value={connector.name} onChange={(event) => setConnector((current) => ({ ...current, name: event.target.value }))} placeholder="Zillow team account" required /></div>
            <div className={styles.field}><label htmlFor="connector-source">Source key</label><input id="connector-source" value={connector.source_key} onChange={(event) => setConnector((current) => ({ ...current, source_key: event.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, '') }))} placeholder="zillow" pattern="[a-z0-9][a-z0-9_-]+" required /><small>Stable lowercase identifier used by routing rules and reporting.</small></div>
            <button type="submit" className={styles.primaryButton} disabled={Boolean(working)}><Link2 aria-hidden="true" /> Create signed connector</button>
          </form>
          <ul className={styles.itemList}>
            {connectors.map((item) => <li key={item.id}><button type="button" className={styles.itemButton} onClick={() => toggleConnector(item)} disabled={Boolean(working)} aria-label={`${item.active === false ? 'Enable' : 'Pause'} ${item.name}`}><span><strong>{item.name}</strong><small>{item.source_key} · {item.public_id}</small></span><span className={styles.itemMeta}>{item.active === false ? <ToggleLeft aria-hidden="true" /> : <ToggleRight aria-hidden="true" />}{item.active === false ? 'paused' : 'active'}</span></button></li>)}
            {!connectors.length && !loading ? <li className={styles.empty}>No lead connectors yet.</li> : null}
          </ul>
        </section>

        <section className={styles.panel} aria-labelledby="rule-title">
          <header className={styles.panelHeader}><div><h4 id="rule-title">Routing rules</h4><p>Lower priority numbers run first</p></div><span className={styles.badge} data-tone={rules.some((item) => item.enabled) ? 'good' : 'warn'}>{rules.filter((item) => item.enabled).length} enabled</span></header>
          <form className={styles.panelBody} onSubmit={createRule}>
            <div className={styles.fieldGrid}>
              <div className={styles.field}><label htmlFor="rule-name">Rule name</label><input id="rule-name" value={rule.name} onChange={(event) => setRule((current) => ({ ...current, name: event.target.value }))} placeholder="Delaware seller leads" required /></div>
              <div className={styles.field}><label htmlFor="rule-priority">Priority</label><input id="rule-priority" type="number" min="0" max="10000" value={rule.priority} onChange={(event) => setRule((current) => ({ ...current, priority: event.target.value }))} required /></div>
              <div className={styles.field}><label htmlFor="rule-source">Source</label><select id="rule-source" value={rule.source_key} onChange={(event) => setRule((current) => ({ ...current, source_key: event.target.value }))}><option value="">Any source</option>{connectors.map((item) => <option key={item.id} value={item.source_key}>{item.name}</option>)}</select></div>
              <div className={styles.field}><label htmlFor="rule-intent">Intent</label><select id="rule-intent" value={rule.intent} onChange={(event) => setRule((current) => ({ ...current, intent: event.target.value }))}><option value="any">Buyer or seller</option><option value="buyer">Buyer</option><option value="seller">Seller</option></select></div>
              <div className={styles.field}><label htmlFor="rule-zips">ZIP codes</label><input id="rule-zips" value={rule.zip_codes} onChange={(event) => setRule((current) => ({ ...current, zip_codes: event.target.value }))} placeholder="19801, 19803" /><small>Comma-separated; blank matches all.</small></div>
              <div className={styles.field}><label htmlFor="rule-states">States</label><input id="rule-states" value={rule.state_codes} onChange={(event) => setRule((current) => ({ ...current, state_codes: event.target.value }))} placeholder="DE, PA" /><small>Comma-separated two-letter codes.</small></div>
            </div>
            <div className={styles.field}><label htmlFor="rule-mode">Assignment mode</label><select id="rule-mode" value={rule.assignment_mode} onChange={(event) => setRule((current) => ({ ...current, assignment_mode: event.target.value }))}><option value="round_robin">Capacity-aware round robin</option><option value="fixed_agent">Fixed agent order</option></select></div>
            <div className={styles.field}><label htmlFor="rule-agents">Eligible agents</label><select id="rule-agents" multiple size={Math.min(Math.max(agents.length, 3), 7)} value={rule.agent_ids} onChange={(event) => setRule((current) => ({ ...current, agent_ids: [...event.target.selectedOptions].map((option) => option.value) }))}>{agents.map((agent) => <option key={agent.agent_id} value={agent.agent_id}>{agent.agent_id}</option>)}</select><small>{rule.assignment_mode === 'fixed_agent' ? 'Choose at least one. Selection order controls fallback order.' : 'No selection uses all active agents with remaining capacity.'}</small></div>
            <button type="submit" className={styles.primaryButton} disabled={Boolean(working)}><Route aria-hidden="true" /> Add routing rule</button>
          </form>
          <ul className={styles.itemList}>
            {rules.map((item) => <li key={item.id}><button type="button" className={styles.itemButton} onClick={() => toggleRule(item)} disabled={Boolean(working)} aria-label={`${item.enabled ? 'Pause' : 'Enable'} ${item.name}`}><span><strong>{item.name}</strong><small>Priority {item.priority} · {item.source_key || 'any source'} · {item.intent} · {item.assignment_mode.replaceAll('_', ' ')}</small></span><span className={styles.itemMeta}>{item.enabled ? <ToggleRight aria-hidden="true" /> : <ToggleLeft aria-hidden="true" />}{item.enabled ? 'enabled' : 'paused'}</span></button></li>)}
            {!rules.length && !loading ? <li className={styles.empty}>No routing rules. The default capacity-aware round robin remains active.</li> : null}
          </ul>
        </section>
      </div>

      <section className={styles.panel} aria-labelledby="capacity-title">
        <header className={styles.panelHeader}><div><h4 id="capacity-title">Agent capacity</h4><p>Only active brokerage users who accept leads and remain under capacity are eligible</p></div><UserRoundCheck aria-hidden="true" /></header>
        <div className={styles.tableWrap}>
          <table className={styles.dataTable}>
            <thead><tr><th scope="col">Agent</th><th scope="col">Open contacts</th><th scope="col">Capacity</th><th scope="col">Accepting</th><th scope="col">Last assigned</th><th scope="col">Action</th></tr></thead>
            <tbody>{agents.map((agent) => {
              const draft = agentDrafts[agent.agent_id] || { accepting_leads: true, capacity: 100 };
              return <tr key={agent.agent_id}><td>{agent.agent_id}</td><td>{agent.assigned_open_contacts || 0}</td><td><input className={styles.compactInput} type="number" min="0" max="100000" aria-label={`Capacity for ${agent.agent_id}`} value={draft.capacity} onChange={(event) => setAgentDrafts((current) => ({ ...current, [agent.agent_id]: { ...draft, capacity: event.target.value } }))} /></td><td><label className={styles.checkRow}><input type="checkbox" checked={draft.accepting_leads} onChange={(event) => setAgentDrafts((current) => ({ ...current, [agent.agent_id]: { ...draft, accepting_leads: event.target.checked } }))} /><span><strong>{draft.accepting_leads ? 'Yes' : 'No'}</strong></span></label></td><td>{formatDate(agent.last_assigned_at)}</td><td><button type="button" className={styles.iconButton} onClick={() => saveAgent(agent.agent_id)} disabled={Boolean(working)} aria-label={`Save capacity for ${agent.agent_id}`}><Save aria-hidden="true" /></button></td></tr>;
            })}</tbody>
          </table>
          {!agents.length && !loading ? <div className={styles.empty}>No active brokerage users are available for routing.</div> : null}
        </div>
      </section>

      <section className={styles.panel} aria-labelledby="events-title">
        <header className={styles.panelHeader}><div><h4 id="events-title">Recent intake</h4><p>Encrypted payloads stay sealed; operational routing evidence is visible</p></div><span className={styles.badge}>{events.length} shown</span></header>
        <div className={styles.tableWrap}>
          <table className={styles.dataTable}>
            <thead><tr><th scope="col">Received</th><th scope="col">Source</th><th scope="col">Intent</th><th scope="col">Market</th><th scope="col">Status</th><th scope="col">Assigned</th><th scope="col">Reason</th></tr></thead>
            <tbody>{events.map((item) => <tr key={item.id}><td>{formatDate(item.received_at)}</td><td>{item.source_key}</td><td>{item.intent}</td><td>{[item.zip_code, item.state_code].filter(Boolean).join(', ') || '—'}</td><td><span className={styles.badge} data-tone={item.status === 'routed' ? 'good' : item.status === 'unassigned' ? 'warn' : 'bad'}>{item.status}</span></td><td>{item.assigned_agent_id || '—'}</td><td>{item.route_reason || '—'}</td></tr>)}</tbody>
          </table>
          {!events.length && !loading ? <div className={styles.empty}>No signed lead intake events yet.</div> : null}
        </div>
      </section>
    </div>
  );
}
