import { Suspense, lazy, useCallback, useEffect, useState } from 'react';

import { useAssistantRecord } from '../components/AssistantContext';
import { crmGet } from '../state/useCrmApi';
import { dealRead, entityTitle, humanState, personRead } from './entityModel';
import { NeohRead } from './NeohRead';
import { EntityFrame } from './EntityFrame';
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
  // The address, from the same read the dossier below performs. A property
  // sheet whose header does not say which property it is was the gap this
  // frame exists to close.
  const dossier = useFetched(`/api/leads/${id}/dossier`);
  const record = dossier.data;
  const address = record?.payload?.address || record?.parcel_id || 'Property';
  useAssistantRecord('property', id, address, record?.dossier_status || '');
  const tour = useFetched(`/api/crm/property-tour?lead_id=${id}`);
  const walkable = Boolean(tour.data?.splat_url);

  return (
    <EntityFrame
      kind="Property"
      title={address}
      subline={[
        record?.payload?.city,
        record?.state,
        record?.dossier_status && humanState(record.dossier_status),
      ]}
      onClose={onClose}
      read={null}
      facts={[
        { label: 'Photos', value: tour.data?.photo_count ?? null },
        { label: '360 scenes', value: tour.data?.pano_scene_count ?? null },
        { label: '3D tour', value: walkable ? 'Yes' : 'Not yet' },
      ]}
      actions={[
        {
          label: walkable ? 'Explore in 3D' : '3D tour not captured yet',
          primary: walkable,
          disabled: !walkable,
          onClick: () => window.dispatchEvent(new CustomEvent('neoh:open-tour', { detail: { leadId: id } })),
        },
      ]}
    >
      <Suspense fallback={<Fallback />}>
        <DossierPanel leadId={id} onClose={onClose} embedded />
      </Suspense>
    </EntityFrame>
  );
}

/* ── Deal ───────────────────────────────────────────────────────────────── */

function money(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return null;
  return n.toLocaleString(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
}

function DealSheet({ id, onClose }) {
  const detail = useFetched(`/api/portfolio/transactions/${id}`);
  const transaction = detail.data?.transaction || null;
  const title = entityTitle('deal', transaction);
  useAssistantRecord('transaction', id, title, transaction?.status || '');
  const read = detail.data ? dealRead(transaction, detail.data.milestones) : null;
  const price = money(transaction?.purchase_price ?? transaction?.list_price);
  const milestones = detail.data?.milestones || [];
  const done = milestones.filter((m) => m.completed_at || m.status === 'completed').length;

  return (
    <EntityFrame
      kind="Deal"
      title={title}
      subline={[
        transaction?.status && humanState(transaction.status),
        price,
        transaction?.closing_date && `closes ${new Date(transaction.closing_date).toLocaleDateString()}`,
      ]}
      read={read}
      readLoading={detail.loading}
      readError={detail.error}
      facts={[
        { label: 'Milestones', value: milestones.length ? `${done} of ${milestones.length}` : null },
        { label: 'Price', value: price },
      ]}
      onClose={onClose}
    >
      {detail.error && !detail.data ? (
        <p className={styles.error}>This deal could not be loaded. It may have been removed, or belong to another workspace.</p>
      ) : (
        <Suspense fallback={<Fallback />}>
          <DealRoomPanel transactionId={id} />
        </Suspense>
      )}
    </EntityFrame>
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
