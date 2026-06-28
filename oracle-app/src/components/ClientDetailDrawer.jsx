import { useCallback, useEffect, useRef, useState } from 'react';
import { crmGet, crmPost, crmPatch, crmDelete } from '../state/useCrmApi';
import {
  GLYPHS, STAGES, stageLabel, normStage, normalizeType, clampScore,
  relTime, fmtDate, prefChipsOf, errMessage, shortDollars, toNum, fmtInt,
  Avatar, ScoreMeter,
} from './ClientShared';
import ClientTimeline from './ClientTimeline';
import ClientTaskList from './ClientTaskList';
import ClientNotes from './ClientNotes';
import styles from './ClientDetailDrawer.module.css';

const SUBTABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'timeline', label: 'Timeline' },
  { id: 'tasks', label: 'Tasks' },
  { id: 'notes', label: 'Notes' },
  { id: 'houses', label: 'Houses' },
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
 * controls stage / score / tags / assignee / identity (all PATCH /clients/{id}),
 * and the sub-tabs own their own data + graceful states. Keyboard-dismissable.
 */
export default function ClientDetailDrawer({ card, onClose, onClientChanged, getListings }) {
  const clientId = card?.id;
  const [detail, setDetail] = useState(card);   // seed with the row we already have
  const [loadErr, setLoadErr] = useState(null);
  const [tab, setTab] = useState('overview');
  const [toast, setToast] = useState('');
  const [tlKey, setTlKey] = useState(0);
  const sheetRef = useRef(null);
  const scoreTimer = useRef(null);

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

  // Esc to dismiss + focus the sheet on open.
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    if (sheetRef.current) sheetRef.current.focus();
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  useEffect(() => () => { if (scoreTimer.current) clearTimeout(scoreTimer.current); }, []);

  const flashToast = (msg) => { setToast(msg); setTimeout(() => setToast(''), 3200); };
  const bumpTimeline = useCallback(() => setTlKey((k) => k + 1), []);

  // Optimistic PATCH /clients/{id}; revert on failure, bubble the merged card up.
  const applyPatch = useCallback((patchBody) => {
    if (!clientId) return;
    const prev = detail;
    const optimistic = { ...detail, ...patchBody };
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

  // ── Tags ───────────────────────────────────────────────────────────────
  const [tagDraft, setTagDraft] = useState('');
  const tags = Array.isArray(detail?.tags) ? detail.tags : [];
  const addTag = () => {
    const t = tagDraft.trim().toLowerCase();
    if (!t || tags.includes(t)) { setTagDraft(''); return; }
    setTagDraft('');
    const optimistic = { ...detail, tags: [...tags, t] };
    setDetail(optimistic);
    onClientChanged?.(optimistic);
    crmPost(`/api/crm/clients/${clientId}/tags`, { tag: t }).then(
      (data) => { if (Array.isArray(data?.tags)) { const m = { ...optimistic, tags: data.tags }; setDetail(m); onClientChanged?.(m); } bumpTimeline(); },
      (err) => { setDetail(detail); onClientChanged?.(detail); flashToast(errMessage(err, 'tags')); }
    );
  };
  const removeTag = (t) => {
    const optimistic = { ...detail, tags: tags.filter((x) => x !== t) };
    setDetail(optimistic);
    onClientChanged?.(optimistic);
    crmDelete(`/api/crm/clients/${clientId}/tags/${encodeURIComponent(t)}`).then(
      (data) => { if (Array.isArray(data?.tags)) { const m = { ...optimistic, tags: data.tags }; setDetail(m); onClientChanged?.(m); } },
      (err) => { setDetail(detail); onClientChanged?.(detail); flashToast(errMessage(err, 'tags')); }
    );
  };

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
  const showings = Array.isArray(detail?.showings) ? detail.showings : [];
  const notesCount = Array.isArray(detail?.notes) ? detail.notes.length : null;
  const openTasks = toNum(detail?.open_tasks);

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

          {/* Stage pipeline */}
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
              <span className={styles.cellLabel}>Lead Score</span>
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

          {/* Tags editor */}
          <div className={styles.tagsEditor}>
            {tags.map((t) => (
              <span key={t} className={styles.tagEdit}>
                #{t}
                <button type="button" className={styles.tagX} aria-label={`Remove tag ${t}`} onClick={() => removeTag(t)}>{GLYPHS.close}</button>
              </span>
            ))}
            <input
              className={styles.tagAdd}
              value={tagDraft}
              onChange={(e) => setTagDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addTag(); } }}
              onBlur={addTag}
              placeholder="+ tag"
              aria-label="Add tag"
            />
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
            if (s.id === 'tasks' && openTasks !== null) count = openTasks;
            if (s.id === 'notes' && notesCount !== null) count = notesCount;
            if (s.id === 'houses' && houses.length) count = houses.length;
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
            <OverviewPane detail={detail} loadErr={loadErr} prefChips={prefChips} openTasks={openTasks} houses={houses} applyPatch={applyPatch} />
          )}
          {tab === 'timeline' && <ClientTimeline clientId={clientId} reloadKey={tlKey} />}
          {tab === 'tasks' && <ClientTaskList clientId={clientId} onChange={bumpTimeline} />}
          {tab === 'notes' && <ClientNotes clientId={clientId} onChange={bumpTimeline} />}
          {tab === 'houses' && (
            <HousesPane
              detail={detail}
              houses={houses}
              showings={showings}
              getListings={getListings}
              onShowingLogged={(listing) => {
                setDetail((d) => {
                  const hs = Array.isArray(d.houses) ? d.houses : [];
                  if (hs.some((h) => h.id === listing.id)) return d;
                  const next = { ...d, houses: [...hs, { id: listing.id, address: listing.address, kind: 'listing' }] };
                  onClientChanged?.(next);
                  return next;
                });
                bumpTimeline();
              }}
            />
          )}
        </div>
      </div>
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
function OverviewPane({ detail, loadErr, prefChips, openTasks, houses, applyPatch }) {
  const [contact, setContact] = useState({ email: detail?.email || '', phone: detail?.phone || '' });
  // Re-sync editable contact fields when the underlying record changes —
  // render-phase reset, not an effect (avoids the setState-in-effect cascade).
  const contactSig = `${detail?.email || ''} ${detail?.phone || ''}`;
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
        <div className={styles.stat}><span className={styles.statNum}>{openTasks === null ? '—' : openTasks}</span><span className={styles.statLabel}>Open Tasks</span></div>
        <div className={styles.stat}><span className={styles.statNum}>{houses.length}</span><span className={styles.statLabel}>Houses</span></div>
        <div className={styles.stat}><span className={styles.statNum}>{relTime(detail?.last_contacted_at || detail?.last_touch?.created_at) || '—'}</span><span className={styles.statLabel}>Last Contact</span></div>
      </div>

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

// ── Houses & showings pane (reuses the book's listing-picker idiom) ─────────
function HousesPane({ houses, getListings, onShowingLogged, detail }) {
  const [picker, setPicker] = useState(null); // {status, listings, error, postingId, postError}
  const clientId = detail?.id;

  const open = () => {
    if (picker) { setPicker(null); return; }
    setPicker({ status: 'loading', listings: [], error: '', postingId: null, postError: '' });
    getListings().then(
      (rows) => setPicker((p) => (p ? { ...p, status: 'ready', listings: rows } : p)),
      (err) => setPicker((p) => (p ? { ...p, status: 'error', error: errMessage(err, 'listings') } : p))
    );
  };

  const logShowing = (listing) => {
    setPicker((p) => (p ? { ...p, postingId: listing.id, postError: '' } : p));
    crmPost('/api/crm/showings', { client_id: clientId, listing_id: listing.id, outcome: 'pending' }).then(
      () => { onShowingLogged(listing); setPicker(null); },
      (err) => setPicker((p) => (p ? { ...p, postingId: null, postError: errMessage(err, 'showings') } : p))
    );
  };

  return (
    <div>
      {houses.length === 0 ? (
        <div className={styles.empty}>
          <span aria-hidden="true">{GLYPHS.house}</span>
          <p className={styles.emptyText}>No houses linked yet. Log a showing to attach a listing.</p>
        </div>
      ) : (
        <div className={styles.houseList}>
          {houses.map((h, i) => (
            <div key={h.id ?? `${h.address}-${i}`} className={styles.houseItem} data-kind={h.kind === 'lead' ? 'lead' : 'listing'}>
              {GLYPHS.house}
              <span className={styles.houseAddr}>{h.address || 'Address pending'}</span>
              <span className={styles.houseKind}>{h.kind === 'lead' ? 'Lead' : 'Listing'}</span>
            </div>
          ))}
        </div>
      )}

      <button type="button" className={styles.logBtn} aria-expanded={!!picker} onClick={open}>
        {GLYPHS.eye}{picker ? 'Close' : 'Log Showing'}
      </button>

      {picker && (
        <div className={styles.picker}>
          <span className={styles.pickerLabel}>Pick the listing shown</span>
          {picker.status === 'loading' && (
            <div className={styles.bodySkel} aria-hidden="true"><div className={styles.skelBar} /><div className={styles.skelBar} /></div>
          )}
          {picker.status === 'error' && (
            <div className={styles.errorStrip} role="alert"><span className={styles.errorTick} aria-hidden="true" /><p className={styles.errorText}>{picker.error}</p></div>
          )}
          {picker.status === 'ready' && picker.listings.length === 0 && (
            <p className={styles.emptyText}>No listings on file — stage a house first.</p>
          )}
          {picker.status === 'ready' && picker.listings.map((l, i) => {
            const seen = houses.some((h) => h.id === l.id);
            const posting = picker.postingId === l.id;
            const price = toNum(l.price);
            return (
              <button key={l.id ?? `${l.address}-${i}`} type="button" className={styles.pickRow} disabled={seen || picker.postingId !== null} onClick={() => logShowing(l)}>
                {GLYPHS.house}
                <span className={styles.pickAddr}>{l.address || 'Address pending'}</span>
                <span className={styles.pickMeta}>{posting ? 'Logging…' : seen ? 'Seen' : price !== null ? shortDollars(price) : ''}</span>
              </button>
            );
          })}
          {picker.postError && <span className={styles.err} role="alert">{picker.postError}</span>}
        </div>
      )}
    </div>
  );
}
