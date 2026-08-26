import { useCallback, useEffect, useRef, useState } from 'react';
import { crmGet, crmPost, crmPatch } from '../state/useCrmApi';
import {
  GLYPHS, STAGES, stageLabel, normStage, normalizeType, clampScore,
  relTime, fmtDate, prefChipsOf, errMessage, fmtInt,
  PORTAL_LINK_KINDS, PORTAL_ASSET_SCOPES, portalKindLabel, portalScopeLabel,
  defaultPortalAssetScope, Avatar, ScoreMeter,
} from './ClientShared';
import ClientTimeline from './ClientTimeline';
// ClientTaskList shares ClientPanes.module.css with Timeline and Notes — the
// stylesheet names all three as the drawer's sub-panes — but unlike them it was
// never imported anywhere, so the CRM task endpoints it calls had no way in.
import ClientTaskList from './ClientTaskList';
import ClientNotes from './ClientNotes';
import StateDocumentChecklist from './StateDocumentChecklist';
import { useAssistantRecord } from './AssistantContext';
import styles from './ClientDetailDrawer.module.css';

const SUBTABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'timeline', label: 'Timeline' },
  { id: 'tasks', label: 'Tasks' },
  { id: 'notes', label: 'Notes' },
  { id: 'documents', label: 'Documents' },
  { id: 'dossier', label: 'Dossier' },
];

// Inline-editable text — click to edit, commit on Enter/blur, Esc cancels.
function InlineEdit({ value, placeholder, onCommit, className, ariaLabel }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value || '');
  const ref = useRef(null);
  useEffect(() => { if (editing && ref.current) ref.current.focus(); }, [editing]);
  const commit = () => {
    setEditing(false);
    const v = draft.trim();
    if (v !== (value || '')) onCommit(v);
  };
  if (editing) {
    return (
      <input
        ref={ref}
        className={styles.inlineInput}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') { e.preventDefault(); commit(); }
          if (e.key === 'Escape') { setDraft(value || ''); setEditing(false); }
        }}
        aria-label={ariaLabel}
      />
    );
  }
  return (
    <button type="button" className={`${styles.inlineEditBtn} ${className || ''}`} onClick={() => { setDraft(value || ''); setEditing(true); }} aria-label={`Edit ${ariaLabel}`}>
      <span>{value || <span className={styles.subGhost}>{placeholder}</span>}</span>
      <span className={styles.editHint} aria-hidden="true">{GLYPHS.edit}</span>
    </button>
  );
}

/**
 * ClientDetailDrawer — the full client sheet. Receives the list card as a seed
 * (real data already in hand), then deep-fetches GET /clients/{id}. Header
 * controls stage / score / assignee / identity (all PATCH /clients/{id}),
 * and the sub-tabs own their own data + graceful states. Keyboard-dismissable.
 */
export default function ClientDetailDrawer({ card, onClose, onClientChanged }) {
  const clientId = card?.id;
  const [detail, setDetail] = useState(card);   // seed with the row we already have
  const [loadErr, setLoadErr] = useState(null);
  const [tab, setTab] = useState('overview');
  const [toast, setToast] = useState('');
  const [automationBusy, setAutomationBusy] = useState(false);
  const [tlKey, setTlKey] = useState(0);
  const sheetRef = useRef(null);
  const scoreTimer = useRef(null);
  const onCloseRef = useRef(onClose);
  useAssistantRecord('client', clientId, detail?.full_name || card?.full_name || 'Client', detail?.stage || '');

  useEffect(() => { onCloseRef.current = onClose; }, [onClose]);

  // Deep-fetch full detail; merge over the seed so nothing flickers to empty.
  useEffect(() => {
    if (!clientId) return undefined;
    let live = true;
    crmGet(`/api/crm/clients/${clientId}`).then(
      (data) => {
        if (!live) return;
        const full = data?.client ?? data;
        if (full && typeof full === 'object') setDetail((d) => ({ ...d, ...full }));
        setLoadErr(null);
      },
      (err) => { if (live) setLoadErr(err); }
    );
    return () => { live = false; };
  }, [clientId]);

  // Treat the sheet as a real modal: focus enters on open, cannot tab behind
  // the scrim, Escape dismisses, and the opener regains focus on close.
  useEffect(() => {
    const previouslyFocused = document.activeElement;
    const onKey = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onCloseRef.current?.();
        return;
      }
      if (e.key !== 'Tab') return;
      const focusable = [...(sheetRef.current?.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      ) ?? [])];
      if (focusable.length === 0) {
        e.preventDefault();
        sheetRef.current?.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!sheetRef.current?.contains(document.activeElement)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    if (sheetRef.current) sheetRef.current.focus();
    return () => {
      window.removeEventListener('keydown', onKey);
      previouslyFocused?.focus?.({ preventScroll: true });
    };
  }, [clientId]);

  useEffect(() => () => { if (scoreTimer.current) clearTimeout(scoreTimer.current); }, []);

  // A reconciliation is durable and asynchronous. Poll only while this drawer
  // is open and the client is actually queued/running; terminal states stop the
  // loop, so idle profiles cost no network or animation work.
  useEffect(() => {
    const status = detail?.automation?.status;
    if (!clientId || !['queued', 'running'].includes(status)) return undefined;
    let live = true;
    let attempts = 0;
    let timer;
    const poll = () => {
      timer = window.setTimeout(() => {
        crmGet(`/api/crm/clients/${clientId}/automation`).then(
          (data) => {
            if (!live) return;
            const automation = data?.automation;
            if (automation) setDetail((current) => ({ ...current, automation }));
            attempts += 1;
            if (attempts < 12 && ['queued', 'running'].includes(automation?.status)) poll();
          },
          () => { attempts += 1; if (live && attempts < 4) poll(); },
        );
      }, 1500);
    };
    poll();
    return () => { live = false; if (timer) window.clearTimeout(timer); };
  }, [clientId, detail?.automation?.status]);

  const flashToast = (msg) => { setToast(msg); setTimeout(() => setToast(''), 3200); };
  const bumpTimeline = useCallback(() => setTlKey((k) => k + 1), []);

  // Optimistic PATCH /clients/{id}; revert on failure, bubble the merged card up.
  const applyPatch = useCallback((patchBody) => {
    if (!clientId) return;
    const prev = detail;
    const automationPatch = { ...(detail?.automation || {}) };
    if (Object.hasOwn(patchBody, 'lead_score')) automationPatch.score_mode = 'manual';
    if (Object.hasOwn(patchBody, 'stage')) automationPatch.stage_mode = 'manual';
    const optimistic = {
      ...detail,
      ...patchBody,
      ...(detail?.automation ? { automation: automationPatch } : {}),
    };
    setDetail(optimistic);
    onClientChanged?.(optimistic);
    crmPatch(`/api/crm/clients/${clientId}`, patchBody).then(
      (data) => {
        const full = data?.client ?? data;
        if (full && typeof full === 'object') {
          const merged = { ...optimistic, ...full };
          setDetail(merged);
          onClientChanged?.(merged);
        }
        bumpTimeline();
      },
      (err) => {
        setDetail(prev);
        onClientChanged?.(prev);
        flashToast(errMessage(err, 'client'));
      }
    );
  }, [clientId, detail, onClientChanged, bumpTimeline]);

  const updateAutomation = useCallback((patchBody, successMessage) => {
    if (!clientId || automationBusy) return;
    setAutomationBusy(true);
    crmPatch(`/api/crm/clients/${clientId}/automation`, patchBody).then(
      (data) => {
        if (data?.automation) {
          setDetail((current) => ({ ...current, automation: data.automation }));
        }
        if (successMessage) flashToast(successMessage);
        bumpTimeline();
      },
      (error) => flashToast(errMessage(error, 'AI automation')),
    ).finally(() => setAutomationBusy(false));
  }, [clientId, automationBusy, bumpTimeline]);

  const refreshAutomation = useCallback(() => {
    if (!clientId || automationBusy) return;
    setAutomationBusy(true);
    crmPost(`/api/crm/clients/${clientId}/automation/reconcile`, {}).then(
      () => {
        setDetail((current) => ({
          ...current,
          automation: { ...(current?.automation || {}), enabled: true, status: 'queued' },
        }));
        flashToast('AI reconciliation queued');
      },
      (error) => flashToast(errMessage(error, 'AI reconciliation')),
    ).finally(() => setAutomationBusy(false));
  }, [clientId, automationBusy]);

  // ── Score stepper (debounced commit) ─────────────────────────────────────
  const score = clampScore(detail?.lead_score);
  const nudgeScore = (delta) => {
    const base = score === null ? 50 : score;
    const next = Math.max(0, Math.min(100, base + delta));
    setDetail((d) => ({ ...d, lead_score: next }));
    if (scoreTimer.current) clearTimeout(scoreTimer.current);
    scoreTimer.current = setTimeout(() => applyPatch({ lead_score: next }), 450);
  };

  const type = normalizeType(detail?.client_type, 'both');
  const stage = normStage(detail?.stage);
  const prefChips = prefChipsOf(detail?.preferences);
  const houses = Array.isArray(detail?.houses) ? detail.houses : [];
  const notesCount = Array.isArray(detail?.notes) ? detail.notes.length : null;
  const automation = detail?.automation || {};
  const automationStatus = automation.enabled === false ? 'disabled' : (automation.status || 'queued');
  const automationStatusLabel = {
    queued: 'Queued', running: 'Reconciling', complete: 'Current', degraded: 'Rules only',
    failed: 'Needs retry', disabled: 'Paused',
  }[automationStatus] || 'Queued';

  return (
    <div className={styles.layer}>
      <button type="button" className={styles.scrim} aria-label="Close client" onClick={onClose} />
      <div
        className={styles.sheet}
        role="dialog"
        aria-modal="true"
        aria-label={`Client — ${detail?.full_name || 'profile'}`}
        ref={sheetRef}
        tabIndex={-1}
      >
        <span className={styles.grip} aria-hidden="true" />

        {/* ── Header ─────────────────────────────────────────────────────── */}
        <header className={styles.header}>
          <div className={styles.idRow}>
            <Avatar name={detail?.full_name} type={type} size="lg" />
            <div className={styles.idMain}>
              <span className={styles.nameLine}>
                <span className={styles.name}>
                  <InlineEdit value={detail?.full_name} placeholder="Unnamed" ariaLabel="name" onCommit={(v) => v && applyPatch({ full_name: v })} />
                </span>
                <span className={styles.typeTag}>{type}</span>
              </span>
              <span className={styles.subLine}>
                <span className={styles.subBit}>{GLYPHS.briefcase}
                  <InlineEdit value={detail?.company} placeholder="Add company" ariaLabel="company" onCommit={(v) => applyPatch({ company: v })} />
                </span>
              </span>
              <span className={styles.subLine}>
                {detail?.email && <span className={styles.subBit}>{GLYPHS.mail}{detail.email}</span>}
                {detail?.phone && <span className={styles.subBit}>{GLYPHS.phone}{detail.phone}</span>}
                {!detail?.email && !detail?.phone && <span className={styles.subGhost}>No contact on file</span>}
              </span>
            </div>
            <button type="button" className={styles.closeBtn} onClick={onClose} aria-label="Close">{GLYPHS.close}</button>
          </div>

          <div className={styles.automationBar} aria-live="polite">
            <span className={styles.automationIdentity}>
              <span className={styles.automationDot} data-status={automationStatus} aria-hidden="true" />
              <span>AI steward</span>
              <span className={styles.automationState}>{automationStatusLabel}</span>
            </span>
            <span className={styles.automationTime}>
              {automation.last_evaluated_at ? `Updated ${relTime(automation.last_evaluated_at)}` : 'Awaiting first review'}
            </span>
            <button type="button" className={styles.automationAction} onClick={refreshAutomation} disabled={automationBusy || automation.enabled === false}>
              Refresh
            </button>
            <button
              type="button"
              className={styles.automationAction}
              onClick={() => updateAutomation({ enabled: automation.enabled === false }, automation.enabled === false ? 'AI steward resumed' : 'AI steward paused')}
              disabled={automationBusy}
            >
              {automation.enabled === false ? 'Resume' : 'Pause'}
            </button>
          </div>

          {/* Stage pipeline */}
          <div className={styles.modeRow}>
            <span>Pipeline stage {automation.stage_mode === 'manual' ? '· Manual' : '· AI managed'}</span>
            {automation.stage_mode === 'manual' && (
              <button type="button" onClick={() => updateAutomation({ stage_mode: 'auto' }, 'AI stage management resumed')} disabled={automationBusy}>Use AI stage</button>
            )}
          </div>
          <div className={styles.pipeline} role="group" aria-label="Pipeline stage">
            {STAGES.map((s) => (
              <button
                key={s.id}
                type="button"
                className={styles.stageKey}
                data-stage={s.id}
                aria-pressed={stage === s.id}
                onClick={() => stage !== s.id && applyPatch({ stage: s.id })}
              >
                {s.label}
              </button>
            ))}
          </div>

          {/* Score + assignee */}
          <div className={styles.metaGrid}>
            <div className={styles.metaCell}>
              <span className={styles.cellLabelRow}>
                <span className={styles.cellLabel}>Lead Score {automation.score_mode === 'manual' ? '· Manual' : '· AI'}</span>
                {automation.score_mode === 'manual' && (
                  <button type="button" onClick={() => updateAutomation({ score_mode: 'auto' }, 'AI scoring resumed')} disabled={automationBusy}>Use AI</button>
                )}
              </span>
              <div className={styles.scoreCtl}>
                <button type="button" className={styles.stepBtn} onClick={() => nudgeScore(-5)} disabled={score === 0} aria-label="Lower score">{GLYPHS.minus}</button>
                <span className={styles.scoreNum}>{score === null ? '—' : score}</span>
                <button type="button" className={styles.stepBtn} onClick={() => nudgeScore(5)} disabled={score === 100} aria-label="Raise score">{GLYPHS.plus}</button>
                <span style={{ flex: 1, display: 'flex', minWidth: 0 }}><ScoreMeter score={score === null ? 0 : score} showVal={false} /></span>
              </div>
            </div>
            <div className={styles.metaCell}>
              <span className={styles.cellLabel}>Assignee</span>
              <InlineAssignee value={detail?.assignee_id} onCommit={(v) => applyPatch({ assignee_id: v || null })} />
            </div>
          </div>

        </header>

        {toast && (
          <div className={styles.toast} role="alert">
            <span className={styles.errorTick} aria-hidden="true" />{toast}
          </div>
        )}

        {/* ── Sub-tab nav ────────────────────────────────────────────────── */}
        <nav className={styles.subnav} role="tablist" aria-label="Client sections">
          {SUBTABS.map((s) => {
            let count = null;
            if (s.id === 'notes' && notesCount !== null) count = notesCount;
            return (
              <button
                key={s.id}
                type="button"
                role="tab"
                aria-selected={tab === s.id}
                className={styles.subKey}
                onClick={() => setTab(s.id)}
              >
                {s.label}
                {count !== null && count > 0 && <span className={styles.subCount}>{fmtInt.format(count)}</span>}
              </button>
            );
          })}
        </nav>

        {/* ── Body ───────────────────────────────────────────────────────── */}
        <div className={styles.body}>
          {tab === 'overview' && (
            <OverviewPane
              detail={detail}
              loadErr={loadErr}
              prefChips={prefChips}
              houses={houses}
              applyPatch={applyPatch}
              automation={automation}
            />
          )}
          {tab === 'timeline' && <ClientTimeline clientId={clientId} reloadKey={tlKey} />}
          {tab === 'tasks' && <ClientTaskList clientId={clientId} onChange={bumpTimeline} />}
          {tab === 'notes' && <ClientNotes clientId={clientId} onChange={bumpTimeline} />}
          {tab === 'documents' && (
            <StateDocumentChecklist
              clientId={clientId}
              stateCode={detail?.state_code || detail?.preferences?.state || ''}
              compact
            />
          )}
          {tab === 'dossier' && <DossierLinksPane detail={detail} houses={houses} automation={automation} />}
        </div>
      </div>
    </div>
  );
}

function DossierLinksPane({ detail, houses, automation }) {
  const leadHouses = houses
    .map((house) => ({ ...house, lead_id: house.lead_id || (house.kind === 'lead' ? house.id : null) }))
    .filter((house) => house.lead_id);
  const [leadId, setLeadId] = useState(leadHouses[0]?.lead_id || '');
  const [links, setLinks] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState(null);
  const [kind, setKind] = useState('seller');
  const [expiryDays, setExpiryDays] = useState(7);
  const [scope, setScope] = useState(defaultPortalAssetScope);

  const load = useCallback(() => {
    if (!leadId) return Promise.resolve().then(() => setLinks([]));
    return crmGet(`/portal/links?lead_id=${encodeURIComponent(leadId)}`).then(
      (data) => { setLinks(Array.isArray(data?.links) ? data.links : []); setError(''); },
      (reason) => setError(errMessage(reason, 'dossier links')),
    );
  }, [leadId]);

  useEffect(() => { load(); }, [load]);

  const create = (event) => {
    event.preventDefault();
    if (!leadId) return;
    setBusy(true);
    setCreated(null);
    crmPost('/portal/links', {
      lead_id: leadId,
      expiry_days: Number(expiryDays),
      link_kind: kind,
      asset_scope: scope,
      issued_to_label: detail?.full_name || null,
      watermark_text: detail?.full_name ? `CONFIDENTIAL — ${detail.full_name}` : null,
    }).then((result) => {
      setCreated(result);
      return load();
    }).catch((reason) => setError(errMessage(reason, 'dossier link'))).finally(() => setBusy(false));
  };

  const revoke = (id) => {
    setBusy(true);
    crmPost(`/portal/links/${id}/revoke`, {}).then(load)
      .catch((reason) => setError(errMessage(reason, 'dossier link')))
      .finally(() => setBusy(false));
  };

  if (leadHouses.length === 0) {
    return (
      <div className={styles.dossierPane}>
        <AutomationEvidence automation={automation} />
        <div className={styles.empty}><span aria-hidden="true">{GLYPHS.house}</span><p className={styles.emptyText}>Link this client to a lead before issuing a read-only dossier.</p></div>
      </div>
    );
  }

  return (
    <div className={styles.dossierPane}>
      <AutomationEvidence automation={automation} />
      <section className={styles.section}>
        <span className={styles.sectionLabel}>Issue revocable dossier</span>
        <form onSubmit={create} className={styles.dossierForm}>
          <label className={styles.field}>
            <span className={styles.microLabel}>Property</span>
            <select className={styles.input} value={leadId} onChange={(event) => { setLeadId(event.target.value); setCreated(null); }}>
              {leadHouses.map((house) => <option key={`${house.id}:${house.lead_id}`} value={house.lead_id}>{house.address || house.lead_id}</option>)}
            </select>
          </label>
          <div className={styles.fieldGrid}>
            <label className={styles.field}>
              <span className={styles.microLabel}>Audience</span>
              <select className={styles.input} value={kind} onChange={(event) => setKind(event.target.value)}>
                {PORTAL_LINK_KINDS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
              </select>
            </label>
            <label className={styles.field}>
              <span className={styles.microLabel}>Expires</span>
              <select className={styles.input} value={expiryDays} onChange={(event) => setExpiryDays(Number(event.target.value))}>
                <option value={1}>1 day</option><option value={7}>7 days</option><option value={14}>14 days</option><option value={30}>30 days</option><option value={90}>90 days</option>
              </select>
            </label>
          </div>
          <fieldset className={styles.scopeFieldset}>
            <legend>Read-only assets</legend>
            {PORTAL_ASSET_SCOPES.map((item) => (
              <label key={item.id}>
                <input type="checkbox" checked={Boolean(scope[item.id])} onChange={(event) => setScope((current) => ({ ...current, [item.id]: event.target.checked }))} />
                <span>{item.label}</span>
              </label>
            ))}
          </fieldset>
          <p className={styles.dossierWarning}>Title, zoning, underwriting, and legal documents remain professional-review artifacts. Links are watermarked, audited, expiring, and read-only.</p>
          <button type="submit" className={styles.logBtn} disabled={busy || !scope.summary}>{busy ? 'Issuing…' : 'Issue secure link'}</button>
        </form>
      </section>

      {created && (
        <section className={styles.createdLink} aria-live="polite">
          <strong>Copy this link now</strong>
          <p>The bearer token is shown once and cannot be recovered later.</p>
          <input aria-label="New secure dossier URL" value={created.secure_url} readOnly onFocus={(event) => event.currentTarget.select()} />
          <button type="button" onClick={() => navigator.clipboard?.writeText(created.secure_url)}>Copy link</button>
        </section>
      )}

      {error && <div className={styles.errorStrip} role="alert"><span className={styles.errorTick} aria-hidden="true" /><p className={styles.errorText}>{error}</p></div>}

      <section className={styles.section}>
        <span className={styles.sectionLabel}>Issued links</span>
        {links === null ? <div className={styles.bodySkel} aria-hidden="true"><div className={styles.skelBar} /></div> : links.length === 0 ? (
          <p className={styles.emptyText}>No dossier links issued for this property.</p>
        ) : (
          <ul className={styles.portalLinks}>
            {links.map((link) => {
              const active = link.active === true;
              const scopes = Object.entries(link.asset_scope || {}).filter(([, enabled]) => enabled).map(([key]) => portalScopeLabel(key));
              return (
                <li key={link.id}>
                  <div><strong>{portalKindLabel(link.link_kind)} dossier</strong><small>{active ? `Expires ${fmtDate(link.access_expires_at)}` : link.revoked_at ? 'Revoked' : 'Expired'} · {link.access_count || 0} views</small><small>{scopes.join(' · ')}</small></div>
                  {active && <button type="button" onClick={() => revoke(link.id)} disabled={busy}>Revoke</button>}
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}

// Assignee inline field — separate so it owns its own draft state.
function InlineAssignee({ value, onCommit }) {
  const [draft, setDraft] = useState(value || '');
  // Re-sync the draft when the committed value changes — render-phase, the
  // documented alternative to a setState-in-effect (which the linter flags).
  const [prevValue, setPrevValue] = useState(value);
  if (value !== prevValue) { setPrevValue(value); setDraft(value || ''); }
  return (
    <input
      className={styles.assigneeInput}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => { if ((draft.trim() || null) !== (value || null)) onCommit(draft.trim()); }}
      onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur(); }}
      placeholder="Unassigned"
      aria-label="Assignee id"
    />
  );
}

// ── Overview pane ─────────────────────────────────────────────────────────
function OverviewPane({ detail, loadErr, prefChips, houses, applyPatch, automation }) {
  const [contact, setContact] = useState({ email: detail?.email || '', phone: detail?.phone || '' });
  // Re-sync editable contact fields when the underlying record changes —
  // render-phase reset, not an effect (avoids the setState-in-effect cascade).
  const contactSig = `${detail?.email || ''}${detail?.phone || ''}`;
  const [prevContactSig, setPrevContactSig] = useState(contactSig);
  if (contactSig !== prevContactSig) {
    setPrevContactSig(contactSig);
    setContact({ email: detail?.email || '', phone: detail?.phone || '' });
  }
  const commit = (key) => {
    const v = contact[key].trim();
    if (v !== (detail?.[key] || '')) applyPatch({ [key]: v || null });
  };
  return (
    <div className={styles.overview}>
      {loadErr && loadErr.status === 404 && (
        <div className={styles.errorStrip} role="status">
          <span className={styles.errorTick} aria-hidden="true" />
          <p className={styles.errorText}>Extended profile service isn’t online yet — showing what’s on the card.</p>
        </div>
      )}

      <div className={styles.statRow}>
        <div className={styles.stat}><span className={styles.statNum}>{houses.length}</span><span className={styles.statLabel}>Properties</span></div>
        <div className={styles.stat}><span className={styles.statNum}>{relTime(detail?.last_contacted_at || detail?.last_touch?.created_at) || '—'}</span><span className={styles.statLabel}>Last Contact</span></div>
      </div>

      <AutomationBrief automation={automation} />

      <div className={styles.section}>
        <span className={styles.sectionLabel}>Contact</span>
        <div className={styles.fieldGrid}>
          <label className={styles.field}>
            <span className={styles.microLabel}>Email</span>
            <input className={styles.input} type="email" value={contact.email} onChange={(e) => setContact((c) => ({ ...c, email: e.target.value }))} onBlur={() => commit('email')} placeholder="client@email.com" />
          </label>
          <label className={styles.field}>
            <span className={styles.microLabel}>Phone</span>
            <input className={styles.input} type="tel" value={contact.phone} onChange={(e) => setContact((c) => ({ ...c, phone: e.target.value }))} onBlur={() => commit('phone')} placeholder="555 000 0000" />
          </label>
        </div>
      </div>

      {prefChips.length > 0 && (
        <div className={styles.section}>
          <span className={styles.sectionLabel}>Preferences</span>
          <div className={styles.prefRow}>{prefChips.map((p, i) => <span key={`${p}-${i}`} className={styles.prefChip}>{p}</span>)}</div>
        </div>
      )}

      <div className={styles.section}>
        <span className={styles.sectionLabel}>Record</span>
        <div className={styles.dataList}>
          <div className={styles.dataRow}><span className={styles.dataKey}>Stage</span><span className={styles.dataVal}>{stageLabel(detail?.stage)}</span></div>
          <div className={styles.dataRow}><span className={styles.dataKey}>Source</span><span className={styles.dataVal}>{detail?.source || '—'}</span></div>
          <div className={styles.dataRow}><span className={styles.dataKey}>Created</span><span className={styles.dataVal}>{fmtDate(detail?.created_at) || '—'}</span></div>
          <div className={styles.dataRow}><span className={styles.dataKey}>Last Contact</span><span className={styles.dataVal}>{fmtDate(detail?.last_contacted_at) || relTime(detail?.last_touch?.created_at) || '—'}</span></div>
        </div>
      </div>
    </div>
  );
}

function AutomationEvidence({ automation }) {
  const evidence = Array.isArray(automation?.evidence) ? automation.evidence : [];
  return (
    <section className={`${styles.section} ${styles.automationEvidence}`}>
      <span className={styles.sectionLabel}>Evidence brief</span>
      <p>{automation?.summary || 'The AI steward has not completed its first evidence-backed review.'}</p>
      {evidence.length > 0 && (
        <ul>{evidence.slice(0, 8).map((item) => <li key={`${item.ref}:${item.label}`}>{item.label}</li>)}</ul>
      )}
      <small>Unknown fields stay unknown. Suggested public-record matches require human confirmation.</small>
    </section>
  );
}

function AutomationBrief({ automation }) {
  const factors = Array.isArray(automation?.score_breakdown) ? automation.score_breakdown : [];
  const nextActions = Array.isArray(automation?.next_actions) ? automation.next_actions : [];
  const gaps = Array.isArray(automation?.data_gaps) ? automation.data_gaps : [];
  const candidates = Array.isArray(automation?.property_candidates) ? automation.property_candidates : [];
  return (
    <section className={`${styles.section} ${styles.automationBrief}`} aria-labelledby="ai-brief-title">
      <div className={styles.briefHeader}>
        <div>
          <span className={styles.sectionLabel}>Automatic CRM steward</span>
          <h3 id="ai-brief-title">Evidence-backed next step</h3>
        </div>
        <span className={styles.briefMode}>{automation?.enabled === false ? 'Paused' : 'Internal only'}</span>
      </div>
      <p className={styles.briefSummary}>{automation?.summary || 'Waiting for the first CRM reconciliation.'}</p>
      {factors.length > 0 && (
        <div className={styles.briefBlock}>
          <span className={styles.microLabel}>Score factors</span>
          <ul className={styles.factorList}>
            {factors.map((factor) => (
              <li key={factor.code}><span>{factor.label}</span><strong>+{factor.points}</strong></li>
            ))}
          </ul>
        </div>
      )}
      <div className={styles.briefColumns}>
        <div className={styles.briefBlock}>
          <span className={styles.microLabel}>Next actions</span>
          {nextActions.length > 0 ? (
            <ul className={styles.actionList}>{nextActions.map((action) => <li key={action.code}><strong>{action.title}</strong><span>{action.reason}</span></li>)}</ul>
          ) : <p className={styles.briefEmpty}>No internal follow-up needed.</p>}
        </div>
        <div className={styles.briefBlock}>
          <span className={styles.microLabel}>Data gaps</span>
          {gaps.length > 0 ? (
            <ul className={styles.gapList}>{gaps.map((gap) => <li key={gap.code}>{gap.label}</li>)}</ul>
          ) : <p className={styles.briefEmpty}>No verified gaps flagged.</p>}
        </div>
      </div>
      {candidates.length > 0 && (
        <div className={styles.candidateBlock}>
          <span className={styles.microLabel}>Review-only property candidates</span>
          <ul>{candidates.map((candidate) => (
            <li key={candidate.public_record_id}>
              <strong>{candidate.address || candidate.parcel_id || 'Public record'}</strong>
              <span>{[candidate.city, candidate.state, candidate.zip_code].filter(Boolean).join(', ')} · {candidate.match_basis}</span>
            </li>
          ))}</ul>
          <small>No property is linked until an address or parcel relationship is confirmed.</small>
        </div>
      )}
    </section>
  );
}
