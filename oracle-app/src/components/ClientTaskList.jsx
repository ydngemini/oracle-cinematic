import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPost, crmPatch } from '../state/useCrmApi';
import { GLYPHS, relTime, fmtDate, errMessage } from './ClientShared';
import styles from './ClientPanes.module.css';

const PRIORITIES = ['low', 'medium', 'high'];
const isDone = (t) => String(t?.status || '').toLowerCase() === 'done';
const isOverdue = (t) => {
  if (isDone(t) || !t?.due_at) return false;
  const due = new Date(t.due_at).getTime();
  return Number.isFinite(due) && due < Date.now();
};

/**
 * ClientTaskList — tasks for one client. Lists via /tasks?client_id, quick-adds
 * via POST /clients/{id}/tasks, completes/reopens via PATCH /tasks/{id}.
 * Every mutation bubbles up through onChange so the drawer can refresh the
 * timeline + open-task count.
 */
export default function ClientTaskList({ clientId, onChange }) {
  const [tasks, setTasks] = useState(null); // null = loading
  const [error, setError] = useState(null);
  const [title, setTitle] = useState('');
  const [due, setDue] = useState('');
  const [priority, setPriority] = useState('medium');
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState('');
  const [busyId, setBusyId] = useState(null);

  const load = useCallback(() => {
    if (!clientId) return;
    setError(null);
    crmGet(`/api/crm/tasks?client_id=${clientId}`).then(
      (data) => setTasks(Array.isArray(data?.tasks) ? data.tasks : []),
      (err) => { setError(err); setTasks([]); }
    );
  }, [clientId]);

  useEffect(() => { setTasks(null); load(); }, [load]);

  const add = () => {
    const t = title.trim();
    if (!t) { setAddError('A task title is required.'); return; }
    const body = { title: t, priority };
    if (due) body.due_at = new Date(due).toISOString();
    setAdding(true);
    setAddError('');
    crmPost(`/api/crm/clients/${clientId}/tasks`, body).then(
      (data) => {
        const made = data?.task ?? { id: `tmp-${Date.now()}`, title: t, priority, status: 'open', due_at: body.due_at || null, created_at: new Date().toISOString() };
        setTasks((prev) => [made, ...(Array.isArray(prev) ? prev : [])]);
        setTitle(''); setDue(''); setPriority('medium');
        setAdding(false);
        onChange?.();
      },
      (err) => { setAdding(false); setAddError(errMessage(err, 'tasks')); }
    );
  };

  const toggle = (task) => {
    const next = isDone(task) ? 'open' : 'done';
    setBusyId(task.id);
    crmPatch(`/api/crm/tasks/${task.id}`, { status: next }).then(
      (data) => {
        const updated = data?.task ?? { ...task, status: next, completed_at: next === 'done' ? new Date().toISOString() : null };
        setTasks((prev) => (Array.isArray(prev) ? prev.map((t) => (t.id === task.id ? { ...t, ...updated } : t)) : prev));
        setBusyId(null);
        onChange?.();
      },
      () => setBusyId(null)
    );
  };

  const onKey = (e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } };

  const open = Array.isArray(tasks) ? tasks.filter((t) => !isDone(t)) : [];
  const done = Array.isArray(tasks) ? tasks.filter(isDone) : [];

  const renderRow = (t) => (
    <li key={t.id} className={styles.row}>
      <div className={styles.rowTop}>
        <button
          type="button"
          className={styles.checkBtn}
          data-done={isDone(t)}
          disabled={busyId === t.id}
          aria-pressed={isDone(t)}
          aria-label={isDone(t) ? `Reopen task ${t.title}` : `Complete task ${t.title}`}
          onClick={() => toggle(t)}
        >
          {GLYPHS.check}
        </button>
        <div className={styles.rowBody}>
          <span className={styles.rowTitle} data-done={isDone(t)}>{t.title || 'Untitled task'}</span>
          {t.details && <span className={styles.rowDetails}>{t.details}</span>}
          <span className={styles.rowMeta}>
            {t.priority && <span className={styles.prio} data-p={String(t.priority).toLowerCase()}>{t.priority}</span>}
            {t.due_at && (
              <span className={styles.metaDue} data-overdue={isOverdue(t)}>
                {GLYPHS.clock} {isOverdue(t) ? 'Overdue ' : 'Due '}{fmtDate(t.due_at)}
              </span>
            )}
            {isDone(t) && t.completed_at && <span>Done {relTime(t.completed_at)}</span>}
          </span>
        </div>
      </div>
    </li>
  );

  return (
    <div className={styles.pane}>
      <div className={styles.composer}>
        <div className={styles.composerRow}>
          <input
            className={styles.input}
            value={title}
            onChange={(e) => { setTitle(e.target.value); if (addError) setAddError(''); }}
            onKeyDown={onKey}
            placeholder="Add a task…"
            aria-label="Task title"
          />
        </div>
        <div className={styles.composerMeta}>
          <input
            className={styles.metaInput}
            type="date"
            value={due}
            onChange={(e) => setDue(e.target.value)}
            aria-label="Due date"
          />
          <select
            className={styles.selectChip}
            value={priority}
            onChange={(e) => setPriority(e.target.value)}
            aria-label="Priority"
          >
            {PRIORITIES.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
          <button type="button" className={styles.addBtn} onClick={add} disabled={adding}>
            {GLYPHS.plus}{adding ? 'Adding…' : 'Add Task'}
          </button>
        </div>
        {addError && <span className={styles.err} role="alert">{addError}</span>}
      </div>

      {tasks === null ? (
        <div className={styles.skel} aria-hidden="true"><div className={styles.skelRow} /><div className={styles.skelRow} /></div>
      ) : error && tasks.length === 0 ? (
        <div className={styles.errorStrip} role="alert">
          <span className={styles.errorTick} aria-hidden="true" />
          <p className={styles.errorText}>{errMessage(error, 'tasks')}</p>
          <button type="button" className={styles.retrySm} onClick={() => { setTasks(null); load(); }}>Retry</button>
        </div>
      ) : open.length === 0 && done.length === 0 ? (
        <div className={styles.empty}>
          <span className={styles.emptyGlyph} aria-hidden="true">{GLYPHS.task}</span>
          <p className={styles.emptyText}>No tasks yet. Add a follow-up to keep this client moving.</p>
        </div>
      ) : (
        <>
          {open.length > 0 && (
            <>
              <span className={styles.groupLabel}>Open <span className={styles.groupCount}>{open.length}</span></span>
              <ul className={styles.list}>{open.map(renderRow)}</ul>
            </>
          )}
          {done.length > 0 && (
            <>
              <span className={styles.groupLabel}>Done <span className={styles.groupCount}>{done.length}</span></span>
              <ul className={styles.list}>{done.map(renderRow)}</ul>
            </>
          )}
        </>
      )}
    </div>
  );
}
