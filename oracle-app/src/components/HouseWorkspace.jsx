import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { Building2, ExternalLink, Link2, MapPin, ScanLine } from 'lucide-react';
import HouseLinkDialog from './HouseLinkDialog';
import { crmPost } from '../state/useCrmApi';
import { friendlyError } from '../lib/errorMessages';
import { useTour } from '../state/useTour';
import { tourOffer } from '../lib/tour/tourOffer';
import styles from './HouseSelection.module.css';

const PropertyTour = lazy(() => import('./PropertyTour'));
// Tier-3 walkable interior. TourViewer picks the engine from VITE_TOUR_ENGINE
// (PlayCanvas by default since 2026-08-07, gsplat opt-in) so this surface stays
// engine-agnostic.
const TourViewer = lazy(() => import('./TourViewer'));

const PRICE_FORMAT = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
});
const NUMBER_FORMAT = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 });

// The badge names what the search matched and no more (AC11.04): an exact key
// is "not verified" until a person confirms the record is the linked client's
// house, and a partial hit is only a possible match. A browsed record, or one
// opened by its id, was not matched against anything, so it keeps the plain
// badge.
const IDENTITY_BADGE = {
  exact_parcel: 'Parcel number match · not verified',
  exact_address: 'Address match · not verified',
  possible_match: 'Possible match',
};
// Once confirmed (T11.03, `identity.verified`), an exact key drops "not
// verified". A possible match stays one: the decision is about the house, not
// about what the search matched, and is shown on its own line.
const VERIFIED_BADGE = {
  ...IDENTITY_BADGE,
  exact_parcel: 'Parcel number match',
  exact_address: 'Address match',
};

function decidedOn(confirmation) {
  return confirmation?.decided_at ? new Date(confirmation.decided_at).toLocaleDateString() : '';
}

// The house decision as its own fact, by `identity.confirmation.state`.
const CONFIRMATION_LINE = {
  confirmed: (c) => ["Confirmed as the linked client's house", decidedOn(c)].filter(Boolean).join(' · '),
  rejected: (c) => ["Marked not the linked client's house", decidedOn(c)].filter(Boolean).join(' · '),
  out_of_date: () => "Decision out of date: the house's client link changed",
};
const CANDIDATE_WARNING = 'Possible match: confirm the parcel before linking.';
const REJECTED_WARNING = "This record was marked not the linked client's house.";

// Mirrors the CHECK in migration 0168.
const MATCH_EVIDENCE = [
  ['parcel_number_checked', 'Parcel number checked'],
  ['deed_or_title', 'Deed or title'],
  ['tax_bill', 'Tax bill'],
  ['client_confirmed', 'Client confirmed'],
  ['site_visit', 'Site visit'],
  ['other', 'Other'],
];
const MATCH_DECISION = {
  confirmed: {
    open: 'Confirm parcel',
    title: "Confirm this record is the linked client's house",
    done: 'Confirmation recorded.',
  },
  rejected: {
    open: "Not this client's house",
    title: "Mark this record not the linked client's house",
    done: 'Rejection recorded.',
  },
  withdrawn: { open: 'Withdraw decision', title: 'Withdraw the current decision', done: 'Decision withdrawn.' },
};
// What may follow the current decision. An out-of-date one was made for a
// client the house is no longer linked to, so it may be decided again.
const MATCH_CHOICES = {
  none: ['confirmed', 'rejected'],
  confirmed: ['withdrawn'],
  rejected: ['withdrawn'],
  out_of_date: ['confirmed', 'rejected', 'withdrawn'],
};

// A display hint only (the PortfolioTab pattern): the server is the gate.
function canDecideMatch() {
  try {
    const role = sessionStorage.getItem('oracle_role');
    return role === 'broker_owner' || role === 'platform_admin';
  } catch {
    return false;
  }
}

/**
 * A broker owner's decision that this public record is, or is not, the linked
 * client's house (T11.03, F11.04). The server takes a reason of 8-500
 * characters once whitespace is collapsed, refuses an agent, and refuses a
 * decision made against a head someone else has since replaced (409). A
 * refusal stays until dismissed, and the badge keeps what the server last
 * said.
 */
function MatchDecision({ house, onDecided }) {
  const confirmation = house.identity?.confirmation;
  const state = confirmation?.state || 'none';
  const [decision, setDecision] = useState(null);
  const [evidence, setEvidence] = useState([]);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState('');
  const cleaned = reason.split(/\s+/).filter(Boolean).join(' ');

  const close = () => {
    setDecision(null);
    setEvidence([]);
    setReason('');
  };
  const open = (next) => {
    close();
    setProblem('');
    setDecision(next);
  };
  const toggle = (key) => setEvidence((current) => (
    current.includes(key) ? current.filter((k) => k !== key) : [...current, key]
  ));
  const submit = async (event) => {
    event.preventDefault();
    if (cleaned.length < 8) {
      setProblem('Write a reason of at least 8 characters.');
      return;
    }
    if (decision === 'confirmed' && evidence.length === 0) {
      setProblem('Choose at least one piece of evidence for a confirmation.');
      return;
    }
    setBusy(true);
    setProblem('');
    try {
      const result = await crmPost(`/api/crm/houses/${encodeURIComponent(house.lead_id)}/record-match`, {
        public_record_id: String(house.public_record_id || house.id),
        decision,
        evidence: decision === 'withdrawn'
          ? []
          : MATCH_EVIDENCE.map(([key]) => key).filter((key) => evidence.includes(key)),
        reason: cleaned,
        supersedes_id: confirmation?.head_id ?? null,
      });
      close();
      onDecided(decision, result?.identity);
    } catch (failure) {
      setProblem(friendlyError(failure, { fallback: 'The decision was not saved.' }));
      // Decided by someone else, or gone: the open form is stale either way.
      if (failure?.status === 404 || failure?.status === 409) close();
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={styles.matchDecision} aria-labelledby="match-decision-title">
      <h3 id="match-decision-title">Is this record the linked client&apos;s house?</h3>
      <p>
        A decision is recorded with your reason. It does not change the search label, the source
        facts or the client link.
      </p>
      {!decision && (
        <div className={styles.matchActions}>
          {MATCH_CHOICES[state]?.map((choice) => (
            <button key={choice} type="button" className={styles.secondaryButton} onClick={() => open(choice)}>
              {MATCH_DECISION[choice].open}
            </button>
          ))}
        </div>
      )}
      {decision && (
        <form className={styles.matchForm} onSubmit={submit} noValidate>
          <strong>{MATCH_DECISION[decision].title}</strong>
          {decision !== 'withdrawn' && (
            <fieldset>
              <legend>{decision === 'confirmed' ? 'Evidence (at least one)' : 'Evidence'}</legend>
              {MATCH_EVIDENCE.map(([key, label]) => (
                <label key={key}>
                  <input type="checkbox" checked={evidence.includes(key)} onChange={() => toggle(key)} />
                  {label}
                </label>
              ))}
            </fieldset>
          )}
          <label htmlFor="match-decision-reason">Reason</label>
          <textarea
            id="match-decision-reason"
            rows={3}
            maxLength={500}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            aria-describedby="match-decision-reason-hint"
          />
          <small id="match-decision-reason-hint">8 to 500 characters. Kept encrypted with the decision.</small>
          <div>
            <button type="submit" className={styles.primaryButton} disabled={busy}>Save decision</button>
            <button type="button" className={styles.secondaryButton} onClick={close} disabled={busy}>
              Cancel
            </button>
          </div>
        </form>
      )}
      {problem && (
        <div className={styles.matchProblem} role="alert">
          <span>{problem}</span>
          <button type="button" className={styles.textButton} onClick={() => setProblem('')}>Dismiss</button>
        </div>
      )}
    </section>
  );
}

function sourcedValue(value, formatter) {
  if (value === null || value === undefined || value === '') return 'Not published';
  return formatter ? formatter.format(Number(value)) : String(value);
}

function trapFocus(event, root) {
  if (event.key !== 'Tab') return;
  const focusable = [...(root?.querySelectorAll(
    'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
  ) ?? [])];
  if (focusable.length === 0) {
    event.preventDefault();
    root?.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

export default function HouseWorkspace({ house, onHouseUpdate }) {
  const [linkOpen, setLinkOpen] = useState(false);
  // Linked in this session: the record's own `confirmable` predates the link.
  const [linkedHere, setLinkedHere] = useState(false);
  const [tourOpen, setTourOpen] = useState(false);
  const [walkOpen, setWalkOpen] = useState(false);
  // Honest tier resolver — only yields splat_url when a real capture exists.
  const { tour } = useTour({ leadId: house?.lead_id ?? null });
  // Shared with PropertyViewTab. The wording is a claim about evidence, not
  // a caption, so it lives in one tested place rather than in two copies.
  const offer = tourOffer(tour);
  const [notice, setNotice] = useState('');
  const tourDialogRef = useRef(null);
  const tourButtonRef = useRef(null);

  useEffect(() => {
    if (!tourOpen) return undefined;
    const opener = tourButtonRef.current;
    window.requestAnimationFrame(() => tourDialogRef.current?.focus());
    return () => opener?.focus({ preventScroll: true });
  }, [tourOpen]);

  if (!house) {
    return (
      <aside className={styles.workspaceEmpty} aria-labelledby="workspace-empty-title">
        <Building2 aria-hidden="true" />
        <span>Selected house</span>
        <h2 id="workspace-empty-title">Choose a property record</h2>
        <p>The property workspace will open here with sourced facts, 3D access, and client linking.</p>
      </aside>
    );
  }

  const locality = [house.city, house.state, house.zip].filter(Boolean).join(', ');
  const hasAddress = Boolean(house.address);
  const factCoverage = house.fact_coverage;
  const identity = house.identity || {};
  const confirmation = house.isManual ? null : identity.confirmation;
  const verified = !house.isManual && identity.verified === true;
  const possibleMatch = !house.isManual && identity.status === 'possible_match';
  // A confirmed record is no longer a candidate for that client; an
  // out-of-date or rejected one still is.
  const candidate = possibleMatch && !verified;
  const rejected = confirmation?.state === 'rejected';
  const warning = [candidate && CANDIDATE_WARNING, rejected && REJECTED_WARNING].filter(Boolean).join(' ');
  const confirmationLine = CONFIRMATION_LINE[confirmation?.state]?.(confirmation);
  const confirmable = !house.isManual && Boolean(house.lead_id)
    && (identity.confirmable === true || (linkedHere && canDecideMatch()));

  return (
    <aside className={styles.workspace} aria-labelledby="workspace-title">
      <div className={styles.workspaceTopline}>
        <span
          className={styles.sourceBadge}
          data-manual={house.isManual ? '' : undefined}
          data-possible={possibleMatch ? '' : undefined}
        >
          {house.isManual
            ? 'CRM manual · unverified'
            : (verified ? VERIFIED_BADGE : IDENTITY_BADGE)[identity.status] || 'Public record'}
        </span>
        {house.parcel_id && <span>Parcel {house.parcel_id}</span>}
      </div>
      {confirmationLine && (
        <p className={styles.identityConfirmation} data-state={confirmation.state}>{confirmationLine}</p>
      )}

      <div className={styles.workspaceTitle}>
        <MapPin aria-hidden="true" />
        <div>
          <h2 id="workspace-title">{house.address || 'Address unavailable'}</h2>
          <p>{locality || house.county || 'Property workspace'}</p>
        </div>
      </div>

      <dl className={styles.facts}>
        <div>
          <dt>Assessor value</dt>
          <dd>{sourcedValue(house.price, PRICE_FORMAT)}</dd>
        </div>
        <div>
          <dt>Last recorded sale</dt>
          <dd>{sourcedValue(house.last_sale_price, PRICE_FORMAT)}</dd>
        </div>
        <div>
          <dt>Bedrooms</dt>
          <dd>{sourcedValue(house.beds, NUMBER_FORMAT)}</dd>
        </div>
        <div>
          <dt>Bathrooms</dt>
          <dd>{sourcedValue(house.baths, NUMBER_FORMAT)}</dd>
        </div>
        <div>
          <dt>Square feet</dt>
          <dd>{sourcedValue(house.sqft, NUMBER_FORMAT)}</dd>
        </div>
        <div>
          <dt>Lot square feet</dt>
          <dd>{sourcedValue(house.lot_sqft, NUMBER_FORMAT)}</dd>
        </div>
        <div>
          <dt>Year built</dt>
          <dd>{sourcedValue(house.year_built)}</dd>
        </div>
        <div>
          <dt>Total rooms</dt>
          <dd>{sourcedValue(house.rooms, NUMBER_FORMAT)}</dd>
        </div>
        <div>
          <dt>Property class</dt>
          <dd>{sourcedValue(house.property_class || house.property_type)}</dd>
        </div>
        <div>
          <dt>County</dt>
          <dd>{sourcedValue(house.county)}</dd>
        </div>
        <div>
          <dt>Owner of record</dt>
          <dd>
            {sourcedValue(house.owner_name)}
            {/* The owner of a candidate parcel is never shown as this house's owner. */}
            {candidate && house.owner_name && (
              <small className={styles.factQualifier}>(possible match)</small>
            )}
          </dd>
        </div>
        <div>
          <dt>Sale date</dt>
          <dd>
            {house.last_sale_date
              ? new Date(`${String(house.last_sale_date).slice(0, 10)}T00:00:00`).toLocaleDateString()
              : 'Not published'}
          </dd>
        </div>
      </dl>

      {factCoverage && (
        <p className={styles.factCoverage} role="status">
          {factCoverage.complete
            ? 'All 8 core property facts are source-backed.'
            : `${factCoverage.observed_count} of ${factCoverage.required_count} core facts published by this source. Missing facts are never estimated.`}
        </p>
      )}

      <div className={styles.provenance}>
        <ScanLine aria-hidden="true" />
        <div>
          <strong>{house.isManual ? 'Manual CRM entry' : 'Source-backed property data'}</strong>
          <span>
            {house.isManual
              ? 'No public facts are inferred. Link this address to a client to save it as a draft house.'
              : `${house.source || 'Public source'} · ${
                house.last_updated
                  ? `refreshed ${new Date(house.last_updated).toLocaleDateString()}`
                  : 'refresh date unavailable'
              }. Verify source facts before relying on them.`}
          </span>
        </div>
      </div>

      {notice && <div className={styles.successState} role="status">{notice}</div>}

      {warning && (
        <p id="workspace-identity-warning" className={styles.identityWarning}>
          {warning}
        </p>
      )}

      <div className={styles.workspaceActions}>
        <button
          ref={tourButtonRef}
          type="button"
          className={styles.secondaryButton}
          onClick={() => setTourOpen(true)}
          disabled={!hasAddress}
        >
          <ExternalLink aria-hidden="true" />
          Open 3D tour
        </button>
        {offer.kind === 'walkable' && (
          <button
            type="button"
            className={styles.secondaryButton}
            onClick={() => setWalkOpen(true)}
          >
            <Building2 aria-hidden="true" />
            {offer.label}
          </button>
        )}
        <button
          type="button"
          className={styles.primaryButton}
          onClick={() => setLinkOpen(true)}
          disabled={!hasAddress}
          aria-describedby={warning ? 'workspace-identity-warning' : undefined}
        >
          <Link2 aria-hidden="true" />
          Link to client
        </button>
      </div>

      {!hasAddress && (
        <p className={styles.addressWarning} role="status">
          This public record needs a usable address before it can open or link.
        </p>
      )}

      {confirmable && (
        <MatchDecision
          house={house}
          onDecided={(decision, decided) => {
            setNotice(MATCH_DECISION[decision].done);
            // Only the house decision changes; the search's status and basis
            // stay as they are.
            if (decided) onHouseUpdate?.({ identity: { ...house.identity, ...decided } });
          }}
        />
      )}

      {linkOpen && (
        <HouseLinkDialog
          house={house}
          onClose={() => setLinkOpen(false)}
          onLinked={(client, result) => {
            setNotice(
              result?.created
                ? `Linked to ${client.full_name}.`
                : `Already linked to ${client.full_name}.`,
            );
            // The link created (or found) this tenant's lead for the parcel and
            // returned its id. Everything interior hangs off that id — the tour
            // resolver, the photo filmstrip, the tier badge — so hand it up
            // rather than discarding it and waiting for a refetch.
            const linked = result?.house;
            if (linked?.lead_id) setLinkedHere(true);
            if (linked?.lead_id || linked?.listing_id) {
              onHouseUpdate?.({
                lead_id: linked.lead_id ?? house?.lead_id ?? null,
                listing_id: linked.listing_id ?? house?.listing_id ?? null,
              });
            }
            setLinkOpen(false);
          }}
        />
      )}

      {tourOpen && (
        <div
          ref={tourDialogRef}
          className={styles.tourLayer}
          role="dialog"
          aria-modal="true"
          aria-label={`3D tour for ${house.address}`}
          tabIndex={-1}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.preventDefault();
              setTourOpen(false);
            } else {
              trapFocus(event, tourDialogRef.current);
            }
          }}
        >
          <Suspense fallback={<div className={styles.tourLoading} role="status">Opening property tour…</div>}>
            <PropertyTour
              address={house.address}
              lat={house.latitude == null ? undefined : Number(house.latitude)}
              lng={house.longitude == null ? undefined : Number(house.longitude)}
              leadId={house.lead_id || undefined}
              onClose={() => setTourOpen(false)}
              title={house.address}
            />
          </Suspense>
        </div>
      )}
      {walkOpen && offer.kind === 'walkable' && (
        <Suspense fallback={null}>
          <TourViewer
            splatUrl={tour.splat_url}
            panoScenes={tour.pano_scenes}
            tourpoints={tour.tourpoints}
            disclosure={tour.disclosure}
            floors={tour.floors}
            address={house.address}
            title={house.address}
            // The badge has to persist inside the viewer, not just on the button
            // that opened it — once someone is walking around, the entry point
            // is off screen and the space looks exactly like a real capture.
            isThisProperty={tour.is_this_property !== false}
            onClose={() => setWalkOpen(false)}
          />
        </Suspense>
      )}
    </aside>
  );
}
