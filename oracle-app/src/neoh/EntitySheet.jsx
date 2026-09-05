import { Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react';

import { useAssistantRecord } from '../components/AssistantContext';
import { crmGet } from '../state/useCrmApi';
import { dealRead, entityTitle, humanState, personRead } from './entityModel';
import { NeohRead } from './NeohRead';
import { LivingStrip } from './LivingObject';
import { composeLiving } from './livingModel';
import { useCallPresence } from './callPresence';
import styles from './EntitySheet.module.css';

/**
 * EntitySheet — one address, one sheet, over whatever was beneath.
 *
 * /p/:id, /property/:key and /deal/:id each open here. The shell keeps the
 * view underneath mounted, so Back closes the sheet without a re-fetch of the
 * work that was in progress — the reason entity routes are parsed
 * independently of the view.
 *
 * The three bodies are the components the app already had, not rewrites:
 * the client drawer (which is already a sheet, and gains a `read` slot), the
 * asset dossier (an aside with no scrim or Escape of its own, so this wraps
 * it in both), and the deal room (which had no single-deal surface at all).
 * What is new on every one is the first thing on it: Neoh's read.
 */

const ClientDetailDrawer = lazy(() => import('../components/ClientDetailDrawer'));
const DossierPanel = lazy(() =>
  import('../components/DossierPanel').then((m) => ({ default: m.DossierPanel })));
const DealRoomPanel = lazy(() => import('../components/DealRoomPanel'));

/** Escape closes; focus lands in the sheet on open and returns on close. */
function useSheetChrome(onClose) {
  const sheetRef = useRef(null);
  useEffect(() => {
    const opener = document.activeElement;
    const frame = window.requestAnimationFrame(() => sheetRef.current?.focus());
    const onKey = (event) => {
      if (event.key === 'Escape') { event.stopPropagation(); onClose?.(); }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener('keydown', onKey);
      if (opener && typeof opener.focus === 'function') opener.focus();
    };
  }, [onClose]);
  return sheetRef;
}

function useFetched(path) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  useEffect(() => {
    let live = true;
    const frame = window.requestAnimationFrame(() => {
      if (live) setState({ data: null, error: null, loading: true });
    });
    crmGet(path).then(
      (data) => { if (live) setState({ data, error: null, loading: false }); },
      (error) => { if (live) setState({ data: null, error, loading: false }); },
    );
    return () => { live = false; window.cancelAnimationFrame(frame); };
  }, [path]);
  return state;
}

function Fallback() {
  return <div className={styles.fallback} aria-hidden="true" />;
}

/* ── Person ─────────────────────────────────────────────────────────────── */

function PersonSheet({ id, onClose }) {
  const intent = useFetched(`/api/clients/${id}/intent`);
  const read = personRead(intent.data);
  // The server derives the state; the browser adds only its own softphone.
  const presence = useCallPresence({ clientId: id });
  const living = composeLiving(intent.data?.living, presence);
  return (
    <Suspense fallback={<Fallback />}>
      <ClientDetailDrawer
        card={{ id }}
        onClose={onClose}
        read={(
          <>
            {living && <LivingStrip living={living} />}
            <NeohRead read={read} loading={intent.loading} error={intent.error} />
          </>
        )}
      />
    </Suspense>
  );
}

/* ── Property ───────────────────────────────────────────────────────────── */

function PropertySheet({ id, onClose }) {
  const sheetRef = useSheetChrome(onClose);
  useAssistantRecord('property', id, 'Property', '');
  return (
    <div className={styles.layer} ref={sheetRef} tabIndex={-1}>
      <button type="button" className={styles.scrim} aria-label="Close property" onClick={onClose} />
      <Suspense fallback={<Fallback />}>
        <DossierPanel leadId={id} onClose={onClose} />
      </Suspense>
    </div>
  );
}

/* ── Deal ───────────────────────────────────────────────────────────────── */

function money(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return null;
  return n.toLocaleString(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
}

function DealSheet({ id, onClose }) {
  const sheetRef = useSheetChrome(onClose);
  const detail = useFetched(`/api/portfolio/transactions/${id}`);
  const transaction = detail.data?.transaction || null;
  const title = entityTitle('deal', transaction);
  useAssistantRecord('transaction', id, title, transaction?.status || '');
  const read = detail.data ? dealRead(transaction, detail.data.milestones) : null;
  const price = money(transaction?.purchase_price ?? transaction?.list_price);

  return (
    <div className={styles.layer}>
      <button type="button" className={styles.scrim} aria-label="Close deal" onClick={onClose} />
      <section
        className={styles.sheet}
        role="dialog"
        aria-modal="true"
        aria-label={`Deal — ${title}`}
        ref={sheetRef}
        tabIndex={-1}
      >
        <header className={styles.head}>
          <div className={styles.headText}>
            <span className={styles.kicker}>Deal</span>
            <h1 className={styles.title}>{title}</h1>
            <span className={styles.subline}>
              {transaction?.status && <span>{humanState(transaction.status)}</span>}
              {price && <span>{price}</span>}
              {transaction?.closing_date && <span>closes {new Date(transaction.closing_date).toLocaleDateString()}</span>}
            </span>
          </div>
          <button type="button" className={styles.close} onClick={onClose} aria-label="Close">×</button>
        </header>
        <NeohRead read={read} loading={detail.loading} error={detail.error} />
        <div className={styles.body}>
          {detail.error && !detail.data ? (
            <p className={styles.error}>This deal could not be loaded. It may have been removed, or belong to another workspace.</p>
          ) : (
            <Suspense fallback={<Fallback />}>
              <DealRoomPanel transactionId={id} />
            </Suspense>
          )}
        </div>
      </section>
    </div>
  );
}

/* ── Dispatch ───────────────────────────────────────────────────────────── */

export function EntitySheet({ entity, onClose }) {
  const close = useCallback(() => onClose?.(), [onClose]);
  if (!entity?.kind || !entity?.id) return null;
  if (entity.kind === 'person') return <PersonSheet id={entity.id} onClose={close} />;
  if (entity.kind === 'property') return <PropertySheet id={entity.id} onClose={close} />;
  if (entity.kind === 'deal') return <DealSheet id={entity.id} onClose={close} />;
  return null;
}

export default EntitySheet;
