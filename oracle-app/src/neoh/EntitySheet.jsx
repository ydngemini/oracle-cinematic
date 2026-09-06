import { Suspense, lazy, useCallback, useEffect, useState } from 'react';

import { useAssistantRecord } from '../components/AssistantContext';
import { ErrorBoundary } from '../components/ErrorBoundary';
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
const TourViewer = lazy(() => import('../components/TourViewer'));

function useFetched(path) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  useEffect(() => {
    let live = true;
    crmGet(path).then(
      (data) => { if (live) setState({ path, data, error: null, loading: false }); },
      (error) => { if (live) setState({ path, data: null, error, loading: false }); },
    );
    return () => { live = false; };
  }, [path]);
  return state.path === path ? state : { data: null, error: null, loading: true };
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

function PropertySheet({ id, onClose, tourOpen, onOpenTour }) {
  // The address, from the same read the dossier below performs. A property
  // sheet whose header does not say which property it is was the gap this
  // frame exists to close.
  const dossier = useFetched(`/api/leads/${encodeURIComponent(id)}/dossier`);
  const record = dossier.data;
  const address = record?.payload?.address || record?.parcel_id || 'Property';
  useAssistantRecord('property', id, address, record?.dossier_status || '');
  const tour = useFetched(`/api/crm/property-tour?lead_id=${encodeURIComponent(id)}`);
  const walkable = Boolean(tour.data?.splat_url);
  const scenes = tour.data?.pano_scenes;
  const hasScenes = Array.isArray(scenes) && scenes.length > 0;
  const canExplore = walkable || hasScenes;
  const demo = walkable && tour.data?.is_this_property === false;
  const tourLabel = tour.loading ? 'Checking for a tour…'
    : tour.error ? 'Tour status unavailable'
      : walkable ? (demo ? 'Preview demo 3D space' : 'Explore in 3D')
        : hasScenes ? (scenes.length > 1 ? 'Explore 360° tour' : 'View 360° scene')
          : '3D tour not captured yet';

  const tourContent = tour.loading ? (
    <div className={styles.tourStatus} role="status">Loading tour…</div>
  ) : tour.error ? (
    <div className={styles.tourStatus} role="alert">The tour could not be loaded. Return to the property and try again later.</div>
  ) : !canExplore ? (
    <div className={styles.tourStatus} role="status">This property has no 3D capture or 360° scenes yet.</div>
  ) : (
    <ErrorBoundary
      label="property tour"
      fallback={() => <div className={styles.tourStatus} role="alert">The tour could not be opened. You can still return to the property.</div>}
    >
      <Suspense fallback={<div className={styles.tourStatus} role="status">Preparing tour…</div>}>
        <TourViewer
          embedded
          splatUrl={tour.data.splat_url}
          splatFormat={tour.data.splat_format}
          splatScene={tour.data.splat_scene}
          panoScenes={scenes}
          disclosure={tour.data.disclosure}
          floors={tour.data.floors}
          tourpoints={tour.data.tourpoints}
          isThisProperty={tour.data.is_this_property !== false}
          address={address}
          title={address}
          onClose={onClose}
        />
      </Suspense>
    </ErrorBoundary>
  );

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
      immersive={tourOpen ? tourContent : null}
      read={null}
      facts={[
        { label: 'Photos', value: tour.data?.photo_count ?? null },
        { label: '360 scenes', value: tour.data?.pano_scene_count ?? null },
        { label: '3D tour', value: tour.loading || tour.error ? null : walkable ? (demo ? 'Demo space' : 'Yes') : 'Not yet' },
      ]}
      actions={[
        {
          label: tourLabel,
          primary: canExplore,
          disabled: !canExplore || tour.loading || Boolean(tour.error),
          onClick: onOpenTour,
        },
      ]}
    >
      <Suspense fallback={<Fallback />}>
        <DossierPanel leadId={id} onClose={onClose} onOpenTour={onOpenTour} embedded />
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

export function EntitySheet({ entity, onClose, onOpenTour }) {
  const close = useCallback(() => onClose?.(), [onClose]);
  if (!entity?.kind || !entity?.id) return null;
  if (entity.kind === 'person') return <PersonSheet id={entity.id} onClose={close} />;
  if (entity.kind === 'property') return <PropertySheet key={entity.id} id={entity.id} tourOpen={entity.tour} onClose={close} onOpenTour={onOpenTour} />;
  if (entity.kind === 'deal') return <DealSheet id={entity.id} onClose={close} />;
  return null;
}

export default EntitySheet;
