import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPost, crmPatch, crmDelete } from '../state/useCrmApi';
import { GLYPHS, relTime, fmtDate, errMessage } from './ClientShared';
import styles from './ClientPanes.module.css';

// pinned first, then newest first — mirrors the contract's ordering so the
// optimistic list matches what a reload would return.
function order(notes) {
  return [...notes].sort((a, b) => {
    if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1;
    return new Date(b.created_at || 0) - new Date(a.created_at || 0);
  });
}

/**
 * ClientNotes — the note book for one client. Compose (+pin) via POST, pin/edit
 * via PATCH, remove via DELETE. Pinned notes float to the top.
 */
export default function ClientNotes({ clientId, onChange }) {
  const [notes, setNotes] = useState(null); // null = loading
  const [error, setError] = useState(null);
  const [draft, setDraft] = useState('');
  const [pinNew, setPinNew] = useState(false);
  const [saving, setSaving] = useState(false);
  const [composeError, setComposeError] = useState('');
  const [editId, setEditId] = useState(null);
  const [editBody, setEditBody] = useState('');
  const [busyId, setBusyId] = useState(null);

  const load = useCallback(() => {
    if (!clientId) return;
    setError(null);
    crmGet(`/api/crm/clients/${clientId}/notes`).then(
      (data) => setNotes(order(Array.isArray(data?.notes) ? data.notes : [])),
      (err) => { setError(err); setNotes([]); }
    );
  }, [clientId]);

  useEffect(() => { setNotes(null); load(); }, [load]);

  const create = () => {
    const body = draft.trim();
    if (!body) { setComposeError('Write something first.'); return; }
    setSaving(true);
    setComposeError('');
    crmPost(`/api/crm/clients/${clientId}/notes`, { body, pinned: pinNew }).then(
      (made) => {
        const note = made?.id ? made : { id: `tmp-${Date.now()}`, body, pinned: pinNew, created_at: new Date().toISOString() };
        setNotes((prev) => order([...(Array.isArray(prev) ? prev : []), note]));
        setDraft(''); setPinNew(false); setSaving(false);
        onChange?.();
      },
      (err) => { setSaving(false); setComposeError(errMessage(err, 'notes')); }
    );
  };

  const patch = (note, patchBody) => {
    setBusyId(note.id);
    crmPatch(`/api/crm/clients/${clientId}/notes/${note.id}`, patchBody).then(
      (updated) => {
        const merged = updated?.id ? updated : { ...note, ...patchBody, updated_at: new Date().toISOString() };
        setNotes((prev) => order((Array.isArray(prev) ? prev : []).map((n) => (n.id === note.id ? { ...n, ...merged } : n))));
        setBusyId(null);
        setEditId(null);
        onChange?.();
      },
      () => setBusyId(null)
    );
  };

  const remove = (note) => {
    setBusyId(note.id);
    crmDelete(`/api/crm/clients/${clientId}/notes/${note.id}`).then(
      () => {
        setNotes((prev) => (Array.isArray(prev) ? prev.filter((n) => n.id !== note.id) : prev));
        setBusyId(null);
        onChange?.();
      },
      () => setBusyId(null)
    );
  };

  const startEdit = (note) => { setEditId(note.id); setEditBody(note.body || ''); };

  return (
    <div className={styles.pane}>
      <div className={styles.composer}>
        <textarea
          className={styles.textarea}
          value={draft}
          onChange={(e) => { setDraft(e.target.value); if (composeError) setComposeError(''); }}
          placeholder="Log a note — calls, context, next steps…"
          aria-label="New note"
        />
        <div className={styles.composerMeta}>
          <button
            type="button"
            className={styles.pinToggle}
            aria-pressed={pinNew}
            onClick={() => setPinNew((v) => !v)}
          >
            {GLYPHS.pin}{pinNew ? 'Pinned' : 'Pin'}
          </button>
          <button type="button" className={styles.addBtn} onClick={create} disabled={saving} style={{ marginLeft: 'auto' }}>
            {GLYPHS.plus}{saving ? 'Saving…' : 'Add Note'}
          </button>
        </div>
        {composeError && <span className={styles.err} role="alert">{composeError}</span>}
      </div>

      {notes === null ? (
        <div className={styles.skel} aria-hidden="true"><div className={styles.skelRow} /><div className={styles.skelRow} /></div>
      ) : error && notes.length === 0 ? (
        <div className={styles.errorStrip} role="alert">
          <span className={styles.errorTick} aria-hidden="true" />
          <p className={styles.errorText}>{errMessage(error, 'notes')}</p>
          <button type="button" className={styles.retrySm} onClick={() => { setNotes(null); load(); }}>Retry</button>
        </div>
      ) : notes.length === 0 ? (
        <div className={styles.empty}>
          <span className={styles.emptyGlyph} aria-hidden="true">{GLYPHS.note}</span>
          <p className={styles.emptyText}>No notes yet. The first one you write pins the relationship’s context.</p>
        </div>
      ) : (
        <ul className={styles.list}>
          {notes.map((n) => (
            <li key={n.id} className={styles.row} data-pinned={!!n.pinned}>
              {editId === n.id ? (
                <>
                  <textarea
                    className={styles.textarea}
                    value={editBody}
                    onChange={(e) => setEditBody(e.target.value)}
                    aria-label="Edit note"
                  />
                  <div className={styles.composerMeta}>
                    <button type="button" className={styles.retrySm} onClick={() => setEditId(null)} style={{ background: 'transparent', color: 'var(--oracle-text-dim)', borderColor: 'var(--oracle-border-bright)' }}>Cancel</button>
                    <button type="button" className={styles.addBtn} onClick={() => patch(n, { body: editBody.trim() })} disabled={busyId === n.id || !editBody.trim()}>
                      {GLYPHS.check}Save
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <div className={styles.rowTop}>
                    <div className={styles.rowBody}>
                      <p className={styles.noteBody}>{n.body}</p>
                      <span className={styles.rowMeta}>
                        {n.pinned && <span className={styles.pinnedFlag}>{GLYPHS.pin} Pinned</span>}
                        <span>{relTime(n.created_at) || fmtDate(n.created_at)}</span>
                        {n.updated_at && n.updated_at !== n.created_at && <span>· edited</span>}
                      </span>
                    </div>
                    <div className={styles.rowActions}>
                      <button type="button" className={styles.iconBtn} data-active={!!n.pinned} disabled={busyId === n.id} aria-label={n.pinned ? 'Unpin note' : 'Pin note'} onClick={() => patch(n, { pinned: !n.pinned })}>{GLYPHS.pin}</button>
                      <button type="button" className={styles.iconBtn} disabled={busyId === n.id} aria-label="Edit note" onClick={() => startEdit(n)}>{GLYPHS.edit}</button>
                      <button type="button" className={styles.iconBtn} data-danger="true" disabled={busyId === n.id} aria-label="Delete note" onClick={() => remove(n)}>{GLYPHS.trash}</button>
                    </div>
                  </div>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
