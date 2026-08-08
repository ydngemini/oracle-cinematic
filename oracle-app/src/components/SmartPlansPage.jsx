import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ArrowDown,
  ArrowUp,
  CalendarClock,
  CheckCircle2,
  CirclePause,
  CirclePlay,
  Clock3,
  FileCheck2,
  Plus,
  RefreshCw,
  Search,
  ShieldAlert,
  Trash2,
  UserRoundCheck,
  Workflow,
  XCircle,
} from 'lucide-react';
import { crmGet, crmPatch, crmPost } from '../state/useCrmApi';
import styles from './SalesWorkspace.module.css';

const STEP_TYPES = [
  { value: 'wait', label: 'Wait' },
  { value: 'task', label: 'Task' },
  { value: 'email', label: 'Email approval' },
  { value: 'sms', label: 'SMS approval' },
  { value: 'approved_call', label: 'AI call approval' },
];

function localStartValue() {
  const date = new Date(Date.now() + 5 * 60 * 1000);
  date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
  return date.toISOString().slice(0, 16);
}

function errorText(error) {
  const detail = error?.payload?.detail;
  if (typeof detail === 'string') return detail;
  if (detail?.message) return detail.message;
  return error?.message || 'The Smart Plan action could not be completed.';
}

function newStep(index, type = 'wait') {
  return {
    key: `step_${Date.now().toString(36)}_${index + 1}`,
    type,
    delay_minutes: 0,
    title: '',
    subject: '',
    body: '',
    priority: 'normal',
  };
}

function editableStep(step) {
  return {
    key: step.key,
    type: step.type,
    delay_minutes: Number(step.delay_minutes || 0),
    title: step.title || '',
    subject: step.subject || '',
    body: step.body || '',
    priority: step.priority || 'normal',
  };
}

function serializeStep(step) {
  const value = {
    key: step.key,
    type: step.type,
    delay_minutes: Number(step.delay_minutes || 0),
    priority: step.priority || 'normal',
  };
  if (step.type === 'task') value.title = step.title.trim();
  if (step.type === 'email') {
    value.subject = step.subject.trim();
    value.body = step.body.trim();
  }
  if (step.type === 'sms' || step.type === 'approved_call') value.body = step.body.trim();
  return value;
}

export default function SmartPlansPage() {
  const [plans, setPlans] = useState([]);
  const [contacts, setContacts] = useState([]);
  const [enrollments, setEnrollments] = useState([]);
  const [selectedPlanId, setSelectedPlanId] = useState(null);
  const [selectedContactIds, setSelectedContactIds] = useState([]);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [scope, setScope] = useState('personal');
  const [steps, setSteps] = useState([newStep(0, 'task')]);
  const [contactQuery, setContactQuery] = useState('');
  const [startAt, setStartAt] = useState(localStartValue);
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  const applyPlan = useCallback((plan) => {
    setSelectedPlanId(plan?.id || null);
    setName(plan?.name || '');
    setDescription(plan?.description || '');
    setScope(plan?.scope || 'personal');
    setSteps(plan?.definition?.steps?.length
      ? plan.definition.steps.map(editableStep)
      : [newStep(0, 'task')]);
    setPreview(null);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [planResponse, contactResponse, enrollmentResponse] = await Promise.all([
        crmGet('/api/sales/plans', { retries: 0 }),
        crmGet('/api/crm/contacts?limit=200', { retries: 0 }),
        crmGet('/api/sales/plans/enrollments', { retries: 0 }),
      ]);
      const nextPlans = planResponse?.plans || [];
      setPlans(nextPlans);
      setContacts(contactResponse?.contacts || []);
      setEnrollments(enrollmentResponse?.enrollments || []);
      setSelectedPlanId((current) => {
        if (current && nextPlans.some((plan) => plan.id === current)) return current;
        if (nextPlans[0]) {
          window.setTimeout(() => applyPlan(nextPlans[0]), 0);
          return nextPlans[0].id;
        }
        return null;
      });
    } catch (loadError) {
      setError(errorText(loadError));
    } finally {
      setLoading(false);
    }
  }, [applyPlan]);

  useEffect(() => {
    const initial = Promise.resolve().then(load);
    return () => { void initial; };
  }, [load]);

  const selectedPlan = useMemo(
    () => plans.find((plan) => plan.id === selectedPlanId) || null,
    [plans, selectedPlanId],
  );

  const filteredContacts = useMemo(() => {
    const needle = contactQuery.trim().toLowerCase();
    if (!needle) return contacts;
    return contacts.filter((contact) => [contact.full_name, contact.email, contact.phone]
      .some((value) => String(value || '').toLowerCase().includes(needle)));
  }, [contactQuery, contacts]);

  const invalidatePreview = useCallback(() => setPreview(null), []);

  const updateStep = useCallback((index, field, value) => {
    setSteps((current) => current.map((step, stepIndex) => (
      stepIndex === index ? { ...step, [field]: value } : step
    )));
    invalidatePreview();
  }, [invalidatePreview]);

  const moveStep = useCallback((index, direction) => {
    setSteps((current) => {
      const target = index + direction;
      if (target < 0 || target >= current.length) return current;
      const next = [...current];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
    invalidatePreview();
  }, [invalidatePreview]);

  const validateDraft = useCallback(() => {
    if (!name.trim()) throw new Error('Plan name is required.');
    if (!steps.length) throw new Error('Add at least one plan step.');
    const keys = steps.map((step) => step.key.trim());
    if (keys.some((key) => !/^[a-z][a-z0-9_-]{1,63}$/.test(key))) {
      throw new Error('Each step key must start with a lowercase letter and contain 2-64 lowercase letters, numbers, underscores, or dashes.');
    }
    if (new Set(keys).size !== keys.length) throw new Error('Step keys must be unique.');
    steps.forEach((step) => {
      if (step.type === 'task' && !step.title.trim()) throw new Error(`Task ${step.key} needs a title.`);
      if (step.type === 'email' && (!step.subject.trim() || !step.body.trim())) throw new Error(`Email ${step.key} needs a subject and message.`);
      if ((step.type === 'sms' || step.type === 'approved_call') && !step.body.trim()) throw new Error(`${step.key} needs message content.`);
      if (step.type === 'sms' && step.body.length > 1600) throw new Error(`${step.key} exceeds the 1,600-character SMS limit.`);
    });
  }, [name, steps]);

  const savePlan = useCallback(async ({ quiet = false } = {}) => {
    validateDraft();
    const payload = {
      name: name.trim(),
      description: description.trim(),
      scope,
      definition: { steps: steps.map(serializeStep) },
    };
    const response = selectedPlanId
      ? await crmPatch(`/api/sales/plans/${selectedPlanId}`, payload)
      : await crmPost('/api/sales/plans', payload);
    const saved = response.plan;
    setPlans((current) => {
      const rest = current.filter((plan) => plan.id !== saved.id);
      return [saved, ...rest];
    });
    setSelectedPlanId(saved.id);
    if (!quiet) setMessage(selectedPlanId ? 'Smart Plan draft saved.' : 'Smart Plan created.');
    return saved;
  }, [description, name, scope, selectedPlanId, steps, validateDraft]);

  const runSave = useCallback(async () => {
    setWorking('save'); setError(''); setMessage(''); setPreview(null);
    try { await savePlan(); } catch (saveError) { setError(errorText(saveError)); } finally { setWorking(''); }
  }, [savePlan]);

  const publish = useCallback(async () => {
    setWorking('publish'); setError(''); setMessage(''); setPreview(null);
    try {
      const saved = await savePlan({ quiet: true });
      const response = await crmPost(`/api/sales/plans/${saved.id}/publish`, {});
      setMessage(`Published immutable revision ${response.revision.revision_number}. Existing enrollments remain on their original revision.`);
      await load();
      const refreshed = await crmGet(`/api/sales/plans/${saved.id}`, { retries: 0 });
      applyPlan(refreshed.plan);
    } catch (publishError) {
      setError(errorText(publishError));
    } finally {
      setWorking('');
    }
  }, [applyPlan, load, savePlan]);

  const toggleContact = useCallback((contactId) => {
    setSelectedContactIds((current) => (
      current.includes(contactId)
        ? current.filter((id) => id !== contactId)
        : [...current, contactId]
    ));
    invalidatePreview();
  }, [invalidatePreview]);

  const previewEnrollment = useCallback(async () => {
    if (!selectedPlanId) { setError('Save and publish a Smart Plan first.'); return; }
    if (!selectedContactIds.length) { setError('Select at least one contact manually.'); return; }
    setWorking('preview'); setError(''); setMessage(''); setPreview(null);
    try {
      const startIso = new Date(startAt).toISOString();
      const response = await crmPost(`/api/sales/plans/${selectedPlanId}/preview`, {
        contact_ids: selectedContactIds,
        start_at: startIso,
      });
      setPreview({ ...response, start_at: startIso, contact_ids: [...selectedContactIds] });
      setMessage(response.can_enroll
        ? 'Preview passed. Review the schedule, then enroll this exact selection.'
        : 'Preview found blockers. Resolve them before enrollment.');
    } catch (previewError) {
      setError(errorText(previewError));
    } finally {
      setWorking('');
    }
  }, [selectedContactIds, selectedPlanId, startAt]);

  const enroll = useCallback(async () => {
    if (!preview?.can_enroll) return;
    setWorking('enroll'); setError(''); setMessage('');
    try {
      const response = await crmPost(`/api/sales/plans/${selectedPlanId}/enroll`, {
        contact_ids: preview.contact_ids,
        start_at: preview.start_at,
        preview_token: preview.preview_token,
      });
      setMessage(`Enrolled ${response.created} contact${response.created === 1 ? '' : 's'} with ${response.scheduled_steps} durable step runs.`);
      setPreview(null);
      setSelectedContactIds([]);
      await load();
    } catch (enrollError) {
      setError(errorText(enrollError));
      setPreview(null);
    } finally {
      setWorking('');
    }
  }, [load, preview, selectedPlanId]);

  const changeEnrollment = useCallback(async (enrollment, action) => {
    setWorking(`${action}:${enrollment.id}`); setError(''); setMessage('');
    try {
      await crmPost(`/api/sales/plans/enrollments/${enrollment.id}/${action}`, {});
      setMessage(`Enrollment ${action === 'cancel' ? 'cancelled' : action === 'pause' ? 'paused' : 'resumed'}.`);
      await load();
    } catch (stateError) {
      setError(errorText(stateError));
    } finally {
      setWorking('');
    }
  }, [load]);

  const newDraft = useCallback(() => {
    setSelectedPlanId(null);
    setName(''); setDescription(''); setScope('personal');
    setSteps([newStep(0, 'task')]);
    setPreview(null); setMessage(''); setError('');
  }, []);

  return (
    <div className={styles.page}>
      <div className={styles.pageIntro}>
        <div>
          <h3>Versioned nurture with manual enrollment</h3>
          <p>Build a visual sequence, publish an immutable revision, preview compliance and provider readiness for the exact contacts you select, then schedule durable work.</p>
        </div>
        <div className={styles.pageActions}>
          <button type="button" className={styles.secondaryButton} onClick={newDraft}><Plus aria-hidden="true" /> New plan</button>
          <button type="button" className={styles.secondaryButton} onClick={load} disabled={loading || Boolean(working)}><RefreshCw aria-hidden="true" /> Refresh</button>
        </div>
      </div>

      <div className={styles.notice}>
        <ShieldAlert aria-hidden="true" />
        <span>Email, SMS, and AI call steps create approval records—not provider sends. Tasks and waits can run automatically. Published revisions are immutable, and enrollment is always based on a signed preview.</span>
      </div>
      {error ? <div className={styles.error} role="alert"><XCircle aria-hidden="true" /> {error}</div> : null}
      {message ? <div className={styles.success} role="status"><CheckCircle2 aria-hidden="true" /> {message}</div> : null}

      <div className={styles.metricGrid}>
        <div className={styles.metricCard}><span>Plans</span><strong>{plans.length}</strong><small>active drafts and publications</small></div>
        <div className={styles.metricCard}><span>Published</span><strong>{plans.filter((plan) => plan.status === 'published').length}</strong><small>immutable current revisions</small></div>
        <div className={styles.metricCard}><span>Enrollments</span><strong>{enrollments.length}</strong><small>recorded contact journeys</small></div>
        <div className={styles.metricCard}><span>Waiting approval</span><strong>{enrollments.reduce((sum, item) => sum + Number(item.approvals_waiting || 0), 0)}</strong><small>outbound steps</small></div>
      </div>

      <div className={styles.threeColumn}>
        <section className={styles.panel} aria-labelledby="plan-library-title">
          <header className={styles.panelHeader}><div><h4 id="plan-library-title">Plan library</h4><p>Select or create a draft</p></div><Workflow aria-hidden="true" /></header>
          {loading ? <div className={styles.empty}>Loading Smart Plans…</div> : null}
          {!loading && !plans.length ? <div className={styles.empty}>No plans yet. Start a new draft.</div> : null}
          <ul className={styles.itemList}>
            {plans.map((plan) => (
              <li key={plan.id}>
                <button type="button" className={styles.itemButton} aria-pressed={selectedPlanId === plan.id} onClick={() => applyPlan(plan)}>
                  <span><strong>{plan.name}</strong><small>{plan.definition?.steps?.length || 0} steps · {plan.scope}</small></span>
                  <span className={styles.badge} data-tone={plan.status === 'published' ? 'good' : 'warn'}>{plan.status}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>

        <section className={styles.panel} aria-labelledby="plan-builder-title">
          <header className={styles.panelHeader}>
            <div><h4 id="plan-builder-title">Visual plan builder</h4><p>{selectedPlan ? `Editing ${selectedPlan.name}` : 'Unsaved plan'}</p></div>
            {selectedPlan?.current_revision_number ? <span className={styles.badge} data-tone="good">Revision {selectedPlan.current_revision_number}</span> : null}
          </header>
          <div className={styles.panelBody}>
            <div className={styles.fieldGrid}>
              <div className={styles.field}><label htmlFor="plan-name">Name</label><input id="plan-name" value={name} onChange={(event) => { setName(event.target.value); invalidatePreview(); }} maxLength={160} placeholder="Buyer consultation follow-up" /></div>
              <div className={styles.field}><label htmlFor="plan-scope">Scope</label><select id="plan-scope" value={scope} onChange={(event) => { setScope(event.target.value); invalidatePreview(); }}><option value="personal">Personal</option><option value="team">Team (broker)</option></select></div>
            </div>
            <div className={styles.field}><label htmlFor="plan-description">Description</label><textarea id="plan-description" className={styles.shortTextarea} value={description} onChange={(event) => setDescription(event.target.value)} maxLength={2000} placeholder="What this plan does and when it should be used" /></div>

            <div className={styles.definitionList}>
              {steps.map((step, index) => (
                <article className={styles.stepCard} key={step.key}>
                  <div className={styles.stepHeader}>
                    <strong>Step {index + 1} · {STEP_TYPES.find((item) => item.value === step.type)?.label}</strong>
                    <div className={styles.stepControls}>
                      <button type="button" aria-label={`Move step ${index + 1} up`} onClick={() => moveStep(index, -1)} disabled={index === 0}><ArrowUp aria-hidden="true" /></button>
                      <button type="button" aria-label={`Move step ${index + 1} down`} onClick={() => moveStep(index, 1)} disabled={index === steps.length - 1}><ArrowDown aria-hidden="true" /></button>
                      <button type="button" aria-label={`Remove step ${index + 1}`} onClick={() => { setSteps((current) => current.filter((_, itemIndex) => itemIndex !== index)); invalidatePreview(); }} disabled={steps.length === 1}><Trash2 aria-hidden="true" /></button>
                    </div>
                  </div>
                  <div className={styles.fieldGrid}>
                    <div className={styles.field}><label htmlFor={`step-type-${index}`}>Type</label><select id={`step-type-${index}`} value={step.type} onChange={(event) => updateStep(index, 'type', event.target.value)}>{STEP_TYPES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></div>
                    <div className={styles.field}><label htmlFor={`step-delay-${index}`}>Delay after prior step (minutes)</label><input id={`step-delay-${index}`} type="number" min="0" max="525600" value={step.delay_minutes} onChange={(event) => updateStep(index, 'delay_minutes', Number(event.target.value))} /></div>
                    <div className={styles.field}><label htmlFor={`step-key-${index}`}>Stable step key</label><input id={`step-key-${index}`} value={step.key} onChange={(event) => updateStep(index, 'key', event.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, '_'))} maxLength={64} /></div>
                    {step.type === 'task' ? <div className={styles.field}><label htmlFor={`step-priority-${index}`}>Priority</label><select id={`step-priority-${index}`} value={step.priority} onChange={(event) => updateStep(index, 'priority', event.target.value)}><option value="low">Low</option><option value="normal">Normal</option><option value="high">High</option><option value="urgent">Urgent</option></select></div> : null}
                  </div>
                  {step.type === 'task' ? <div className={styles.field}><label htmlFor={`step-title-${index}`}>Task title</label><input id={`step-title-${index}`} value={step.title} onChange={(event) => updateStep(index, 'title', event.target.value)} maxLength={200} /></div> : null}
                  {step.type === 'email' ? <div className={styles.field}><label htmlFor={`step-subject-${index}`}>Email subject</label><input id={`step-subject-${index}`} value={step.subject} onChange={(event) => updateStep(index, 'subject', event.target.value)} maxLength={200} /></div> : null}
                  {['email', 'sms', 'approved_call'].includes(step.type) ? <div className={styles.field}><label htmlFor={`step-body-${index}`}>{step.type === 'approved_call' ? 'Approved call script' : 'Message draft'}</label><textarea id={`step-body-${index}`} value={step.body} onChange={(event) => updateStep(index, 'body', event.target.value)} maxLength={step.type === 'sms' ? 1600 : 20000} />{step.type === 'sms' ? <small>{step.body.length}/1600 characters</small> : null}</div> : null}
                </article>
              ))}
            </div>
            <div className={styles.buttonRow}>
              <button type="button" className={styles.secondaryButton} onClick={() => { setSteps((current) => [...current, newStep(current.length)]); invalidatePreview(); }}><Plus aria-hidden="true" /> Add step</button>
              <button type="button" className={styles.secondaryButton} onClick={runSave} disabled={Boolean(working)}><FileCheck2 aria-hidden="true" /> Save draft</button>
              <button type="button" className={styles.primaryButton} onClick={publish} disabled={Boolean(working)}><Workflow aria-hidden="true" /> Publish revision</button>
            </div>
          </div>
        </section>

        <section className={styles.panel} aria-labelledby="plan-enroll-title">
          <header className={styles.panelHeader}><div><h4 id="plan-enroll-title">Manual enrollment</h4><p>Exact contacts + signed preview</p></div><UserRoundCheck aria-hidden="true" /></header>
          <div className={styles.panelBody}>
            <div className={styles.field}><label htmlFor="plan-contact-search">Search contacts</label><div className={styles.searchRow}><input id="plan-contact-search" value={contactQuery} onChange={(event) => setContactQuery(event.target.value)} placeholder="Name, email, or phone" /><button type="button" className={styles.iconButton} aria-label="Clear search" onClick={() => setContactQuery('')}><Search aria-hidden="true" /></button></div></div>
            <div className={styles.selectionSummary}><strong>{selectedContactIds.length}</strong><span>contacts selected manually</span></div>
          </div>
          <div className={styles.contactSelector}>
            {filteredContacts.map((contact) => (
              <label className={styles.checkRow} key={contact.id}>
                <input type="checkbox" checked={selectedContactIds.includes(contact.id)} onChange={() => toggleContact(contact.id)} />
                <span><strong>{contact.full_name}</strong><small>{contact.email || contact.phone || 'No delivery destination'} · {contact.state_code || 'state missing'}</small></span>
              </label>
            ))}
          </div>
          <div className={styles.panelBody}>
            <div className={styles.field}><label htmlFor="plan-start-at">Start at</label><input id="plan-start-at" type="datetime-local" value={startAt} onChange={(event) => { setStartAt(event.target.value); invalidatePreview(); }} /></div>
            <div className={styles.buttonRow}>
              <button type="button" className={styles.secondaryButton} onClick={previewEnrollment} disabled={Boolean(working) || !selectedPlan?.current_revision_id}><CalendarClock aria-hidden="true" /> Preview</button>
              <button type="button" className={styles.primaryButton} onClick={enroll} disabled={Boolean(working) || !preview?.can_enroll}><CheckCircle2 aria-hidden="true" /> Enroll exact selection</button>
            </div>
            {preview ? (
              <div className={styles.resultCard}>
                <h5>{preview.can_enroll ? 'Preview ready' : 'Enrollment blocked'}</h5>
                <p>Revision {preview.revision_number} · {preview.contact_count} contacts · expires {new Date(preview.expires_at).toLocaleTimeString()}</p>
                {preview.blockers?.length ? <ul>{preview.blockers.map((blocker, index) => <li key={`${blocker.contact_id}-${blocker.step_key}-${index}`}>{blocker.step_key}: {blocker.message}</li>)}</ul> : null}
                {preview.warnings?.length ? <ul>{preview.warnings.map((warning, index) => <li key={`${warning.contact_id}-${warning.step_key}-${index}`}>Warning: {warning.step_key}: {warning.message}</li>)}</ul> : null}
                <ol className={styles.scheduleList}>{preview.schedule?.map((item) => <li key={item.step_key}><Clock3 aria-hidden="true" /><span><strong>{item.step_key}</strong><small>{item.type.replaceAll('_', ' ')} · {new Date(item.scheduled_for).toLocaleString()}</small></span></li>)}</ol>
              </div>
            ) : null}
          </div>
        </section>
      </div>

      <section className={styles.panel} aria-labelledby="plan-enrollments-title">
        <header className={styles.panelHeader}><div><h4 id="plan-enrollments-title">Enrollment control</h4><p>Pause, resume, or cancel future work without changing revision history</p></div><Clock3 aria-hidden="true" /></header>
        {enrollments.length ? (
          <div className={styles.tableWrap}>
            <table className={styles.dataTable}>
              <thead><tr><th>Plan</th><th>Contact</th><th>Revision</th><th>Status</th><th>Next run</th><th>Controls</th></tr></thead>
              <tbody>
                {enrollments.map((enrollment) => (
                  <tr key={enrollment.id}>
                    <td>{enrollment.plan_name}</td>
                    <td>{contacts.find((contact) => contact.id === enrollment.contact_id)?.full_name || 'Contact'}</td>
                    <td>{enrollment.revision_number}</td>
                    <td><span className={styles.badge} data-tone={enrollment.status === 'active' ? 'good' : enrollment.status === 'cancelled' ? 'bad' : 'warn'}>{enrollment.status}</span></td>
                    <td>{enrollment.next_run_at ? new Date(enrollment.next_run_at).toLocaleString() : '—'}</td>
                    <td><div className={styles.tableActions}>{enrollment.status === 'active' ? <button type="button" aria-label="Pause enrollment" onClick={() => changeEnrollment(enrollment, 'pause')} disabled={Boolean(working)}><CirclePause aria-hidden="true" /></button> : null}{enrollment.status === 'paused' ? <button type="button" aria-label="Resume enrollment" onClick={() => changeEnrollment(enrollment, 'resume')} disabled={Boolean(working)}><CirclePlay aria-hidden="true" /></button> : null}{['active', 'paused'].includes(enrollment.status) ? <button type="button" aria-label="Cancel enrollment" onClick={() => changeEnrollment(enrollment, 'cancel')} disabled={Boolean(working)}><XCircle aria-hidden="true" /></button> : null}</div></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className={styles.empty}>No contacts are enrolled in Smart Plans yet.</div>}
      </section>
    </div>
  );
}
