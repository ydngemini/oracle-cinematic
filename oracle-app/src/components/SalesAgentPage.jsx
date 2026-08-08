import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Bot,
  CheckCircle2,
  ClipboardList,
  FileText,
  Mail,
  MessageSquareText,
  PhoneCall,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Workflow,
} from 'lucide-react';
import { crmGet, crmPost } from '../state/useCrmApi';
import styles from './SalesWorkspace.module.css';

function errorText(error) {
  const detail = error?.payload?.detail;
  if (typeof detail === 'string') return detail;
  if (detail?.message) return detail.message;
  return error?.message || 'The sales action could not be completed.';
}

export default function SalesAgentPage({ onNavigate }) {
  const [items, setItems] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [query, setQuery] = useState('');
  const [stage, setStage] = useState('');
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(null);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [result, setResult] = useState(null);
  const [draft, setDraft] = useState(null);
  const [taskTitle, setTaskTitle] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const params = new URLSearchParams({ limit: '200' });
      if (query.trim()) params.set('q', query.trim());
      if (stage) params.set('stage', stage);
      const response = await crmGet(`/api/sales/agent/work-queue?${params}`, { retries: 0 });
      const next = response?.items || [];
      setItems(next);
      setSelectedId((current) => (
        next.some((item) => item.contact.id === current) ? current : next[0]?.contact?.id || null
      ));
    } catch (loadError) {
      setError(errorText(loadError));
    } finally {
      setLoading(false);
    }
  }, [query, stage]);

  useEffect(() => {
    const initial = Promise.resolve().then(load);
    return () => { void initial; };
  }, [load]);

  const selected = useMemo(
    () => items.find((item) => item.contact.id === selectedId) || null,
    [items, selectedId],
  );

  const runAction = useCallback(async (action, overrides = {}) => {
    if (!selected) return;
    setWorking(action);
    setError('');
    setMessage('');
    try {
      const response = await crmPost('/api/sales/agent/actions', {
        action,
        contact_id: selected.contact.id,
        ...overrides,
      });
      setResult(response);
      if (response?.draft) {
        setDraft({ action, subject: response.draft.subject || '', body: response.draft.body || response.draft.script || '' });
      }
      if (response?.approval?.id) {
        setMessage(`Approval ${response.approval.id} is staged for human review.`);
      } else if (response?.task?.id) {
        setMessage('Follow-up task created.');
        setTaskTitle('');
        void load();
      }
    } catch (actionError) {
      setError(errorText(actionError));
    } finally {
      setWorking(null);
    }
  }, [load, selected]);

  const stageDraft = useCallback(() => {
    if (!draft) return;
    void runAction(draft.action, {
      subject: draft.subject || undefined,
      body: draft.body,
      stage_for_approval: true,
    });
  }, [draft, runAction]);

  return (
    <div className={styles.page}>
      <div className={styles.pageIntro}>
        <div>
          <h3>Evidence-led work queue</h3>
          <p>Prioritize people from recorded CRM facts, inspect the reason, draft personalized follow-up, and stage every outbound action for approval.</p>
        </div>
        <button type="button" className={styles.secondaryButton} onClick={load} disabled={loading}>
          <RefreshCw aria-hidden="true" /> Refresh
        </button>
      </div>

      <div className={styles.notice}>
        <ShieldCheck aria-hidden="true" />
        <span>Qualification excludes protected-class traits. Drafts are editable, and email, SMS, and calls cannot leave the system until an authorized human approves them.</span>
      </div>
      {error ? <div className={styles.error} role="alert"><Bot aria-hidden="true" /> {error}</div> : null}
      {message ? <div className={styles.success} role="status"><CheckCircle2 aria-hidden="true" /> {message}</div> : null}

      <div className={styles.metricGrid}>
        <div className={styles.metricCard}><span>Queue</span><strong>{items.length}</strong><small>observed contacts</small></div>
        <div className={styles.metricCard}><span>Priority</span><strong>{items.filter((item) => item.client.lead_score >= 70).length}</strong><small>score 70 or higher</small></div>
        <div className={styles.metricCard}><span>Open tasks</span><strong>{items.reduce((sum, item) => sum + item.open_tasks, 0)}</strong><small>recorded actions</small></div>
        <div className={styles.metricCard}><span>Selected score</span><strong>{selected?.client?.lead_score ?? '—'}</strong><small>{selected?.client?.stage || 'choose a contact'}</small></div>
      </div>

      <div className={styles.threeColumn}>
        <section className={styles.panel} aria-labelledby="sales-queue-title">
          <header className={styles.panelHeader}>
            <div><h4 id="sales-queue-title">Work queue</h4><p>Observed CRM ranking inputs</p></div>
          </header>
          <div className={styles.panelBody}>
            <form className={styles.fieldGrid} onSubmit={(event) => { event.preventDefault(); void load(); }}>
              <div className={styles.field}>
                <label htmlFor="sales-agent-search">Search</label>
                <input id="sales-agent-search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Name, email, or phone" />
              </div>
              <div className={styles.field}>
                <label htmlFor="sales-agent-stage">Stage</label>
                <select id="sales-agent-stage" value={stage} onChange={(event) => setStage(event.target.value)}>
                  <option value="">All stages</option>
                  <option value="lead">Lead</option>
                  <option value="active">Active</option>
                  <option value="under_contract">Under contract</option>
                  <option value="closed">Closed</option>
                  <option value="lost">Lost</option>
                </select>
              </div>
              <button type="submit" className={styles.secondaryButton}><Search aria-hidden="true" /> Apply</button>
            </form>
          </div>
          <div className={styles.scrollList}>
            {loading ? <div className={styles.empty}>Loading the work queue…</div> : null}
            {!loading && items.length === 0 ? <div className={styles.empty}>No contacts match this queue. Add canonical contacts or change the filters.</div> : null}
            <ul className={styles.itemList}>
              {items.map((item) => (
                <li key={item.contact.id}>
                  <button
                    type="button"
                    className={styles.itemButton}
                    aria-pressed={selectedId === item.contact.id}
                    onClick={() => { setSelectedId(item.contact.id); setDraft(null); setResult(null); setMessage(''); }}
                  >
                    <span>
                      <strong>{item.contact.full_name}</strong>
                      <small>{item.reasons.join(' · ')}</small>
                    </span>
                    <span className={styles.itemMeta}><b>{item.client.lead_score}</b>{item.client.stage}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section className={styles.panel} aria-labelledby="sales-contact-title">
          <header className={styles.panelHeader}>
            <div><h4 id="sales-contact-title">{selected?.contact?.full_name || 'Select a contact'}</h4><p>{selected ? `${selected.client.stage} · ${selected.contact.preferred_channel || 'no preference'}` : 'CRM context and next actions'}</p></div>
            {selected ? <span className={styles.badge} data-tone={selected.client.lead_score >= 70 ? 'good' : 'warn'}>{selected.client.lead_score} score</span> : null}
          </header>
          {selected ? (
            <div className={styles.panelBody}>
              <div className={styles.resultCard}>
                <h5>Observed facts</h5>
                <ul>
                  <li>Email: {selected.contact.email || 'missing'}</li>
                  <li>Phone: {selected.contact.phone || 'missing'}</li>
                  <li>State / timezone: {selected.contact.state_code || 'missing'} / {selected.contact.timezone || 'missing'}</li>
                  <li>Last contacted: {selected.client.last_contacted_at ? new Date(selected.client.last_contacted_at).toLocaleString() : 'not recorded'}</li>
                </ul>
              </div>
              <div className={styles.actionGrid}>
                <button type="button" onClick={() => runAction('summarize')} disabled={Boolean(working)}><FileText aria-hidden="true" /><span><strong>Summarize</strong><small>Observed facts only</small></span></button>
                <button type="button" onClick={() => runAction('qualify')} disabled={Boolean(working)}><Sparkles aria-hidden="true" /><span><strong>Qualify</strong><small>Score basis and gaps</small></span></button>
                <button type="button" onClick={() => runAction('draft_email')} disabled={Boolean(working)}><Mail aria-hidden="true" /><span><strong>Draft email</strong><small>Edit before approval</small></span></button>
                <button type="button" onClick={() => runAction('draft_sms')} disabled={Boolean(working)}><MessageSquareText aria-hidden="true" /><span><strong>Draft SMS</strong><small>Consent checked later</small></span></button>
                <button type="button" onClick={() => runAction('draft_call_script')} disabled={Boolean(working)}><PhoneCall aria-hidden="true" /><span><strong>Call script</strong><small>AI call approval draft</small></span></button>
                <button type="button" onClick={() => onNavigate('/our-ai/sales/plans')}><Workflow aria-hidden="true" /><span><strong>Smart Plans</strong><small>Manual enrollment</small></span></button>
              </div>

              <div className={styles.field}>
                <label htmlFor="sales-task-title">Follow-up task</label>
                <input id="sales-task-title" value={taskTitle} onChange={(event) => setTaskTitle(event.target.value)} placeholder={`Follow up with ${selected.contact.full_name}`} />
              </div>
              <button type="button" className={styles.secondaryButton} onClick={() => runAction('create_task', { title: taskTitle || undefined })} disabled={Boolean(working)}>
                <ClipboardList aria-hidden="true" /> Create task
              </button>
            </div>
          ) : <div className={styles.empty}>Choose a contact to inspect sales context.</div>}
        </section>

        <section className={styles.panel} aria-labelledby="sales-output-title">
          <header className={styles.panelHeader}>
            <div><h4 id="sales-output-title">Draft and evidence</h4><p>Editable before staging</p></div>
          </header>
          <div className={styles.panelBody} aria-live="polite">
            {working ? <div className={styles.notice}><Bot aria-hidden="true" /> Working on {working.replaceAll('_', ' ')}…</div> : null}
            {draft ? (
              <>
                {draft.action === 'draft_email' ? (
                  <div className={styles.field}>
                    <label htmlFor="sales-draft-subject">Subject</label>
                    <input id="sales-draft-subject" value={draft.subject} onChange={(event) => setDraft((current) => ({ ...current, subject: event.target.value }))} />
                  </div>
                ) : null}
                <div className={styles.field}>
                  <label htmlFor="sales-draft-body">{draft.action === 'draft_call_script' ? 'Call script' : 'Message'}</label>
                  <textarea id="sales-draft-body" value={draft.body} maxLength={draft.action === 'draft_sms' ? 1600 : 20000} onChange={(event) => setDraft((current) => ({ ...current, body: event.target.value }))} />
                  {draft.action === 'draft_sms' ? <small>{draft.body.length}/1600 characters</small> : null}
                </div>
                <button type="button" className={styles.primaryButton} onClick={stageDraft} disabled={Boolean(working) || !draft.body.trim()}>
                  <ShieldCheck aria-hidden="true" /> Stage for approval
                </button>
              </>
            ) : result ? (
              <div className={styles.resultCard}>
                <h5>{result.action?.replaceAll('_', ' ')}</h5>
                <pre className={styles.codeBlock}>{JSON.stringify(result.summary ? { summary: result.summary, facts: result.facts } : result.qualification ? { qualification: result.qualification, score: result.score, data_gaps: result.data_gaps } : result, null, 2)}</pre>
              </div>
            ) : <div className={styles.empty}>Choose an action. Results and editable drafts appear here.</div>}
          </div>
        </section>
      </div>
    </div>
  );
}
