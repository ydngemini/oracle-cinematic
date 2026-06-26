import { useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import { GLYPHS, relTime, fmtDate, errMessage } from './ClientShared';
import styles from './ClientPanes.module.css';

// One glyph per activity kind — falls back to the bolt for anything new the
// backend emits, so the feed never breaks on an unknown kind.
const KIND_GLYPH = {
  created: GLYPHS.plus,
  stage_change: GLYPHS.bolt,
  score_change: GLYPHS.bolt,
  note: GLYPHS.note,
  note_added: GLYPHS.note,
  task: GLYPHS.task,
  task_added: GLYPHS.task,
  task_completed: GLYPHS.check,
  message: GLYPHS.mail,
  showing: GLYPHS.eye,
  tag: GLYPHS.tag,
  assign: GLYPHS.assign,
  archive: GLYPHS.archive,
};

const kindLabel = (k) => String(k || 'event').replace(/_/g, ' ');

/**
 * ClientTimeline — the activity spine for one client. Reads
 * /clients/{id}/timeline (client_activities, newest first). Read-only;
 * every mutation elsewhere in the drawer writes a row the backend returns here.
 */
export default function ClientTimeline({ clientId, reloadKey = 0 }) {
  const [feed, setFeed] = useState(null); // null = loading
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!clientId) return undefined;
    let live = true;
    setFeed(null);
    setError(null);
    crmGet(`/api/crm/clients/${clientId}/timeline?limit=50`).then(
      (data) => {
        if (!live) return;
        setFeed(Array.isArray(data?.timeline) ? data.timeline : []);
      },
      (err) => {
        if (!live) return;
        setError(err);
        setFeed([]);
      }
    );
    return () => { live = false; };
  }, [clientId, reloadKey]);

  if (feed === null) {
    return (
      <div className={styles.skel} aria-hidden="true">
        <div className={styles.skelRow} /><div className={styles.skelRow} /><div className={styles.skelRow} />
      </div>
    );
  }

  if (error && feed.length === 0) {
    return (
      <div className={styles.empty}>
        <span className={styles.emptyGlyph} aria-hidden="true">{GLYPHS.clock}</span>
        <p className={styles.emptyText}>{errMessage(error, 'activity timeline')}</p>
      </div>
    );
  }

  if (feed.length === 0) {
    return (
      <div className={styles.empty}>
        <span className={styles.emptyGlyph} aria-hidden="true">{GLYPHS.clock}</span>
        <p className={styles.emptyText}>No activity yet. Stage changes, notes, tasks and messages land here.</p>
      </div>
    );
  }

  return (
    <ol className={styles.feed}>
      {feed.map((ev, i) => (
        <li key={ev.id ?? `${ev.kind}-${i}`} className={styles.event} style={{ animationDelay: `${Math.min(i, 8) * 45}ms` }}>
          <span className={styles.eventSpine} aria-hidden="true">
            <span className={styles.eventNode}>{KIND_GLYPH[ev.kind] || GLYPHS.bolt}</span>
          </span>
          <div className={styles.eventBody}>
            <p className={styles.eventSummary}>{ev.summary || kindLabel(ev.kind)}</p>
            <span className={styles.eventMeta}>
              <span className={styles.eventKind}>{kindLabel(ev.kind)}</span>
              {ev.actor && <span>· {ev.actor}</span>}
              <span>· {relTime(ev.created_at) || fmtDate(ev.created_at) || ''}</span>
            </span>
          </div>
        </li>
      ))}
    </ol>
  );
}
