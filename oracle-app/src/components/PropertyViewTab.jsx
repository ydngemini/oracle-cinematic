import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Building2, Camera, Check, Copy, Globe, Home, Link2, Loader2, MapPin, Plus, Search, Trash2, Upload, Video, X,
} from 'lucide-react';
import CaptureSessionPanel from './CaptureSessionPanel';
import { crmGet, crmPost, crmDelete, crmUpload } from '../state/useCrmApi';
import { useTour } from '../state/useTour';
import { tourOffer } from '../lib/tour/tourOffer';
import styles from './PropertyViewTab.module.css';

// Same entry point HouseWorkspace uses, so both surfaces pick the engine
// from VITE_TOUR_ENGINE and neither can drift onto its own viewer.
const TourViewer = lazy(() => import('./TourViewer'));

/**
 * Property View — one address, what public records say about it, and the media
 * the agent and their client have captured for it.
 *
 * Address lookup uses the existing /api/geocode + /api/enrich-property routes
 * (OpenStreetMap geocoding, then licensed/public enrichment). It deliberately
 * does NOT scrape listing portals: spatial_agent's Zillow/Redfin scrapers are
 * gated off for ToS reasons and stay that way.
 *
 * Media always attaches to a CRM record (lead or listing), never to a bare
 * address string — otherwise photos orphan themselves as soon as the deal
 * progresses. The resolve step below is what enforces that.
 */

const SURFACES = [
  { id: 'exterior', label: 'Exterior', Icon: Home },
  { id: 'interior', label: 'Interior', Icon: Camera },
  { id: 'aerial', label: 'Aerial', Icon: MapPin },
  { id: 'other', label: 'Other', Icon: Camera },
];

const MAX_PHOTO_MB = 25;
const MAX_VIDEO_MB = 512;

// Pull a 2-letter state out of a US address so a new lead can satisfy the NOT
// NULL `state` column. Falls back to null, which surfaces a prompt rather than
// guessing a state onto the record.
const STATE_RE = /,\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?\s*(?:,\s*USA?)?\s*$/i;
function stateFrom(address) {
  const match = STATE_RE.exec((address || '').trim());
  return match ? match[1].toUpperCase() : null;
}

function fmtMoney(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  return `$${Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 })}`;
}

function fmtNum(v, suffix = '') {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  return `${Number(v).toLocaleString('en-US')}${suffix}`;
}

export default function PropertyViewTab() {
  const [query, setQuery] = useState('');
  const [lookup, setLookup] = useState(null);
  const [lookupState, setLookupState] = useState('idle'); // idle|loading|error|done
  const [lookupError, setLookupError] = useState(null);

  const [candidates, setCandidates] = useState(null); // { leads, listings }
  const [subject, setSubject] = useState(null);       // { leadId } | { listingId }
  const [view, setView] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  // Which subject the walk was opened for. Storing the subject rather than a
  // bare boolean makes "close it when the property changes" a derivation
  // instead of an effect that fires a second render.
  const [walkOpenFor, setWalkOpenFor] = useState(null);
  // 'auto' = photo/video sniffed by the server; 'pano' = the agent asserting
  // these are equirectangular 360s. Deliberately a choice, never inferred —
  // the server validates the claim and refuses a flat photo rather than
  // wrapping it onto a sphere and calling it a room.
  const [captureMode, setCaptureMode] = useState('auto');
  const [floorIndex, setFloorIndex] = useState(0);
  const [mintedLink, setMintedLink] = useState(null);
  const [activeSurface, setActiveSurface] = useState('exterior');

  const fileInputRef = useRef(null);
  // Guards against a slow earlier lookup overwriting a newer one.
  const lookupSeq = useRef(0);

  const subjectQs = useMemo(() => {
    if (!subject) return null;
    return subject.leadId ? `lead_id=${subject.leadId}` : `listing_id=${subject.listingId}`;
  }, [subject]);

  // --- tour ----------------------------------------------------------------
  // This surface is where a capture is STARTED (CaptureSessionPanel below), so
  // it is where a user looks for the result. It previously had no way to show
  // one: the tour existed, the viewer existed, and nothing on this page
  // reached them.
  const { tour } = useTour({
    leadId: subject?.leadId ?? null,
    listingId: subject?.listingId ?? null,
  });
  const offer = tourOffer(tour);
  const panoCount = Number(tour?.pano_scene_count) || 0;

  // Closes itself when the subject changes: a walk through the previous
  // property can never stay open over the new one's record.
  const walkOpen = walkOpenFor != null && walkOpenFor === subjectQs;

  // --- address lookup ------------------------------------------------------
  const runLookup = useCallback(async (event) => {
    event?.preventDefault();
    const address = query.trim();
    if (!address) return;

    const seq = ++lookupSeq.current;
    setLookupState('loading');
    setLookupError(null);
    setSubject(null);
    setCandidates(null);
    setView(null);
    setMintedLink(null);

    try {
      const geo = await crmGet(`/api/geocode?address=${encodeURIComponent(address)}`);
      if (seq !== lookupSeq.current) return;
      if (!geo?.lat || !geo?.lng) {
        setLookupState('error');
        setLookupError('That address could not be located. Try adding city and state.');
        return;
      }

      // Enrichment and record-matching are independent; run them together and
      // let either fail without taking the other down.
      const [enrichment, matches] = await Promise.all([
        crmGet(`/api/enrich-property?address=${encodeURIComponent(address)}&lat=${geo.lat}&lng=${geo.lng}`)
          .catch(() => null),
        crmGet(`/api/crm/property-view/resolve?address=${encodeURIComponent(address)}`)
          .catch(() => ({ leads: [], listings: [] })),
      ]);
      if (seq !== lookupSeq.current) return;

      setLookup({
        address: geo.display_name || address,
        typed: address,
        lat: geo.lat,
        lng: geo.lng,
        enrichment,
      });
      setCandidates(matches);
      setLookupState('done');

      // Exactly one match is unambiguous — select it so the agent can start
      // capturing immediately instead of confirming the obvious.
      const only = (matches?.leads?.length === 1 && !matches?.listings?.length)
        ? { leadId: matches.leads[0].id }
        : (matches?.listings?.length === 1 && !matches?.leads?.length)
          ? { listingId: matches.listings[0].id }
          : null;
      if (only) setSubject(only);
    } catch (err) {
      if (seq !== lookupSeq.current) return;
      setLookupState('error');
      setLookupError(err?.message || 'Address lookup failed.');
    }
  }, [query]);

  // --- property media ------------------------------------------------------
  // Bumped after every mutation to re-run the fetch effect below. Using a
  // counter rather than calling a fetch function directly from the effect body
  // keeps every setState inside an async callback with a cancellation guard,
  // so a response for a previous subject can never land on the current one.
  const [reloadToken, setReloadToken] = useState(0);
  const refreshView = useCallback(() => setReloadToken((n) => n + 1), []);

  useEffect(() => {
    if (!subjectQs) return undefined;
    let cancelled = false;

    (async () => {
      try {
        const payload = await crmGet(`/api/crm/property-view?${subjectQs}`);
        if (!cancelled) setView(payload);
      } catch (err) {
        if (!cancelled) {
          setNotice({ tone: 'error', text: err?.message || 'Could not load property media.' });
        }
      }
    })();

    return () => { cancelled = true; };
  }, [subjectQs, reloadToken]);

  const createSubject = useCallback(async () => {
    const address = lookup?.typed || query.trim();
    const state = stateFrom(lookup?.address) || stateFrom(address);
    if (!state) {
      setNotice({
        tone: 'error',
        text: 'Add the two-letter state to the address (e.g. "…, DE 19801") so the record can be created.',
      });
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      const created = await crmPost('/api/crm/property-view/subject', { address, state });
      setSubject({ leadId: created.lead_id });
      setNotice({
        tone: 'ok',
        text: created.created ? 'Property record created.' : 'Matched an existing record.',
      });
    } catch (err) {
      setNotice({ tone: 'error', text: err?.message || 'Could not create the record.' });
    } finally {
      setBusy(false);
    }
  }, [lookup, query]);

  const onPickFiles = useCallback(async (event) => {
    const files = Array.from(event.target.files || []);
    event.target.value = ''; // allow re-picking the same file after a failure
    if (!files.length || !subjectQs) return;

    // The route refuses a video labelled 360° anyway; saying so here names the
    // offending file instead of failing the whole batch with a server error.
    if (captureMode === 'pano') {
      const video = files.find((f) => f.type.startsWith('video/'));
      if (video) {
        setNotice({
          tone: 'error',
          text: `${video.name} is a video. A 360° scene must be an equirectangular photo.`,
        });
        return;
      }
    }

    const tooBig = files.find((f) => {
      const capMb = f.type.startsWith('video/') ? MAX_VIDEO_MB : MAX_PHOTO_MB;
      return f.size > capMb * 1024 * 1024;
    });
    if (tooBig) {
      setNotice({
        tone: 'error',
        text: `${tooBig.name} is too large (limit ${tooBig.type.startsWith('video/') ? MAX_VIDEO_MB : MAX_PHOTO_MB} MB).`,
      });
      return;
    }

    setBusy(true);
    setNotice(null);
    // One multipart request for the batch — the route takes `files` (plural)
    // and computes sort_order once, so per-file requests would interleave.
    const form = new FormData();
    form.append('surface', activeSurface);
    form.append('capture', captureMode);
    if (captureMode === 'pano') form.append('floor_index', String(floorIndex));
    for (const file of files) form.append('files', file);

    try {
      const result = await crmUpload(`/api/crm/property-view/media?${subjectQs}`, form);
      const count = result?.media?.length ?? files.length;
      setNotice({ tone: 'ok', text: `${count} file${count === 1 ? '' : 's'} uploaded.` });
      refreshView();
    } catch (err) {
      setNotice({ tone: 'error', text: err?.message || 'Upload failed.' });
    } finally {
      setBusy(false);
    }
  }, [subjectQs, activeSurface, captureMode, floorIndex, refreshView]);

  const mintLink = useCallback(async () => {
    if (!subjectQs) return;
    setBusy(true);
    setNotice(null);
    try {
      const created = await crmPost(`/api/crm/property-view/upload-links?${subjectQs}`, {
        label: 'Client capture link',
        ttl_hours: 72,
        max_uploads: 40,
      });
      setMintedLink(created);
      refreshView();
    } catch (err) {
      setNotice({ tone: 'error', text: err?.message || 'Could not create the link.' });
    } finally {
      setBusy(false);
    }
  }, [subjectQs, refreshView]);

  const revokeLink = useCallback(async (linkId) => {
    try {
      await crmDelete(`/api/crm/property-view/upload-links/${linkId}`);
      refreshView();
    } catch (err) {
      setNotice({ tone: 'error', text: err?.message || 'Could not revoke the link.' });
    }
  }, [refreshView]);

  const reviewMedia = useCallback(async (mediaId, decision) => {
    try {
      await crmPost(`/api/crm/property-view/media/${mediaId}/review`, { decision });
      refreshView();
    } catch (err) {
      setNotice({ tone: 'error', text: err?.message || 'Review failed.' });
    }
  }, [refreshView]);

  const surfaceMedia = view?.by_surface?.[activeSurface] || [];
  // The reconstruction worker reads every photo on the property, not just the
  // surface being viewed, so the readiness count has to match what it will see.
  const photoTotal = Object.values(view?.by_surface || {})
    .flat()
    .filter((item) => item?.kind === 'photo').length;
  const enrich = lookup?.enrichment;
  const hasCandidates = Boolean(candidates?.leads?.length || candidates?.listings?.length);

  return (
    <section className={styles.wrap} aria-labelledby="property-view-title">
      <header className={styles.head}>
        {/* h2, not h1: PropertiesTab already renders the tab's h1. Two h1 on one
            page is an a11y violation, and repeating the tab title verbatim made
            "Property View" appear twice on screen. Names the sub-view instead,
            matching ListingsInventory's h2. */}
        <h2 id="property-view-title" className={styles.title}>Address lookup</h2>
        <p className={styles.sub}>
          Enter an address to pull what public records say about it, then capture
          exterior and interior media yourself or invite the owner to send theirs.
        </p>
      </header>

      <form className={styles.searchRow} onSubmit={runLookup} role="search">
        <label htmlFor="pv-address" className={styles.srOnly}>Property address</label>
        <div className={styles.searchField}>
          <MapPin aria-hidden="true" />
          <input
            id="pv-address"
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="123 Main St, Wilmington, DE 19801"
            autoComplete="street-address"
          />
        </div>
        <button type="submit" className={styles.searchBtn} disabled={lookupState === 'loading' || !query.trim()}>
          {lookupState === 'loading'
            ? <><Loader2 className={styles.spin} aria-hidden="true" /> Locating…</>
            : <><Search aria-hidden="true" /> Look up</>}
        </button>
      </form>

      {lookupState === 'error' ? (
        <p className={styles.error} role="alert">{lookupError}</p>
      ) : null}

      {lookup ? (
        <div className={styles.resultCard}>
          <div className={styles.resultHead}>
            <Home aria-hidden="true" />
            <div>
              <h2 className={styles.resultAddress}>{lookup.address}</h2>
              <p className={styles.coords}>
                {Number(lookup.lat).toFixed(5)}, {Number(lookup.lng).toFixed(5)}
              </p>
            </div>
          </div>

          {enrich ? (
            <dl className={styles.facts}>
              <div><dt>Flood zone</dt><dd>{enrich.flood_zone?.zone || '—'}</dd></div>
              <div><dt>Walk Score</dt><dd>{fmtNum(enrich.walkscore?.walkscore)}</dd></div>
              <div><dt>Est. value</dt><dd>{fmtMoney(enrich.valuation?.estimatedValue)}</dd></div>
              <div><dt>Nearby POIs</dt><dd>{fmtNum(enrich.pois?.length)}</dd></div>
            </dl>
          ) : (
            <p className={styles.muted}>
              Public-record enrichment is unavailable for this address right now.
              Media capture still works.
            </p>
          )}

          <p className={styles.provenance}>
            Sourced from public and licensed data providers. Neoh does not scrape
            listing portals — figures are estimates, not an appraisal.
          </p>
        </div>
      ) : null}

      {/* Subject selection: media must attach to a real CRM record. */}
      {lookupState === 'done' && !subject ? (
        <div className={styles.subjectPicker}>
          <h3>Attach media to a record</h3>
          {hasCandidates ? (
            <ul className={styles.candidateList}>
              {(candidates.leads || []).map((lead) => (
                <li key={`lead-${lead.id}`}>
                  <button type="button" onClick={() => setSubject({ leadId: lead.id })}>
                    <span className={styles.candidateKind}>Lead</span>
                    <span>{lead.address}</span>
                  </button>
                </li>
              ))}
              {(candidates.listings || []).map((listing) => (
                <li key={`listing-${listing.id}`}>
                  <button type="button" onClick={() => setSubject({ listingId: listing.id })}>
                    <span className={styles.candidateKind}>Listing</span>
                    <span>{listing.address}</span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className={styles.muted}>No existing record matches this address.</p>
          )}
          <button type="button" className={styles.secondary} onClick={createSubject} disabled={busy}>
            <Plus aria-hidden="true" /> Create a new property record
          </button>
        </div>
      ) : null}

      {subject ? (
        <>
          <div className={styles.surfaceTabs} role="tablist" aria-label="Media surface">
            {SURFACES.map(({ id, label, Icon }) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={activeSurface === id}
                className={activeSurface === id ? styles.surfaceActive : styles.surfaceTab}
                onClick={() => setActiveSurface(id)}
              >
                <Icon aria-hidden="true" /> {label}
                <span className={styles.count}>{view?.by_surface?.[id]?.length || 0}</span>
              </button>
            ))}
          </div>

          {/* Capture mode. The 360 route has existed on the server since the
              pano work landed — it validates the equirect claim and writes
              property_pano_scenes — but nothing in the UI ever sent
              `capture`, so every upload defaulted to "auto" and the no-GPU
              route to a walkable tour was unreachable. */}
          <div className={styles.captureModes} role="radiogroup" aria-label="What you are uploading">
            <button
              type="button"
              role="radio"
              aria-checked={captureMode === 'auto'}
              className={captureMode === 'auto' ? styles.modeActive : styles.mode}
              onClick={() => setCaptureMode('auto')}
            >
              Photos &amp; video
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={captureMode === 'pano'}
              className={captureMode === 'pano' ? styles.modeActive : styles.mode}
              onClick={() => setCaptureMode('pano')}
            >
              <Globe aria-hidden="true" /> 360° scenes
            </button>
          </div>

          {captureMode === 'pano' ? (
            <div className={styles.panoHint}>
              <p>
                Equirectangular photos only — close to <strong>2:1</strong> (the server
                rejects anything outside 1.94–2.06 rather than smearing a flat photo
                onto a sphere). Any 360 camera or a phone&apos;s panorama-sphere mode works.
              </p>
              <p className={styles.panoProgress}>
                {/* Two is the walkable threshold, server-side and in panoGraph.
                    One scene is a view — there is no route from a place to itself. */}
                {panoCount === 0
                  ? 'No 360° scenes yet. Two or more make a walkable tour — no GPU needed.'
                  : panoCount === 1
                    ? '1 scene uploaded — one more makes it walkable.'
                    : `${panoCount} scenes — this property has a walkable 360° tour.`}
              </p>
              <label className={styles.floorLabel}>
                Floor
                <input
                  type="number"
                  min="0"
                  max="200"
                  value={floorIndex}
                  onChange={(e) => setFloorIndex(Math.max(0, Math.min(200, Number(e.target.value) || 0)))}
                />
                <span>0 = ground. Scenes group by storey in the viewer.</span>
              </label>
            </div>
          ) : null}

          <div className={styles.actions}>
            <button type="button" className={styles.primary} onClick={() => fileInputRef.current?.click()} disabled={busy}>
              <Upload aria-hidden="true" />{' '}
              {captureMode === 'pano'
                ? `Upload 360° scenes${floorIndex ? ` (floor ${floorIndex})` : ''}`
                : `Upload ${activeSurface} photos or video`}
            </button>
            <button type="button" className={styles.secondary} onClick={mintLink} disabled={busy}>
              <Link2 aria-hidden="true" /> Invite client to upload
            </button>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={captureMode === 'pano' ? 'image/*' : 'image/*,video/mp4,video/quicktime,video/webm'}
              className={styles.srOnly}
              onChange={onPickFiles}
            />
          </div>

          {notice ? (
            <p className={notice.tone === 'error' ? styles.error : styles.ok} role="status">{notice.text}</p>
          ) : null}

          {mintedLink ? (
            <div className={styles.linkCard}>
              <p className={styles.linkNotice}>{mintedLink.notice}</p>
              <div className={styles.linkRow}>
                <code>{mintedLink.share_url || mintedLink.token}</code>
                <button
                  type="button"
                  onClick={() => navigator.clipboard?.writeText(mintedLink.share_url || mintedLink.token)}
                >
                  <Copy aria-hidden="true" /> Copy
                </button>
                <button type="button" onClick={() => setMintedLink(null)} aria-label="Dismiss link">
                  <X aria-hidden="true" />
                </button>
              </div>
            </div>
          ) : null}

          {view?.pending_review?.length ? (
            <section className={styles.reviewQueue} aria-label="Client uploads awaiting review">
              <h3>Client uploads awaiting your review ({view.pending_review.length})</h3>
              <ul className={styles.reviewList}>
                {view.pending_review.map((item) => (
                  <li key={item.id}>
                    <span className={styles.reviewKind}>
                      {item.kind === 'video' ? <Video aria-hidden="true" /> : <Camera aria-hidden="true" />}
                      {item.surface || 'other'}
                    </span>
                    <button type="button" onClick={() => reviewMedia(item.id, 'approved')}>
                      <Check aria-hidden="true" /> Approve
                    </button>
                    <button type="button" onClick={() => reviewMedia(item.id, 'rejected')}>
                      <X aria-hidden="true" /> Reject
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          <div className={styles.gallery}>
            {surfaceMedia.length === 0 ? (
              <p className={styles.muted}>No {activeSurface} media yet.</p>
            ) : (
              surfaceMedia.map((item) => (
                <figure key={item.id} className={styles.tile}>
                  {item.kind === 'video' ? (
                    <video src={item.url} controls preload="metadata" />
                  ) : (
                    <img src={item.url} alt={`${item.surface || 'property'} media`} loading="lazy" />
                  )}
                  <figcaption>
                    {item.uploaded_via === 'client_link' ? 'From client' : 'From you'}
                  </figcaption>
                </figure>
              ))
            )}
          </div>

          {/* Reconstruction lives beside the photos it consumes. It used to be
              a self-contained wizard with its own uploader and no importer —
              so nothing could reach it, and the only way to start a capture was
              unreachable. */}
          {/* The result of a capture, beside the control that starts one. When
              there is nothing to walk this renders the reason rather than
              rendering nothing — an empty space is indistinguishable from a
              missing feature, which is how this surface read before. */}
          <section className={styles.tourSection} aria-label="3D tour">
            <h3>3D tour</h3>
            {offer.kind === 'walkable' ? (
              <>
                <button
                  type="button"
                  className={styles.primary}
                  onClick={() => setWalkOpenFor(subjectQs)}
                >
                  <Building2 aria-hidden="true" /> {offer.label}
                </button>
                {offer.isDemo ? (
                  <p className={styles.tourWarn}>
                    This walkable space is a stand-in, not a capture of this address.
                  </p>
                ) : null}
              </>
            ) : (
              <p className={styles.tourEmpty}>{offer.reason}</p>
            )}
          </section>

          <CaptureSessionPanel
            leadId={subject?.leadId}
            listingId={subject?.listingId}
            photoCount={photoTotal}
            onComplete={refreshView}
          />

          {walkOpen && offer.kind === 'walkable' ? (
            <Suspense fallback={null}>
              <TourViewer
                splatUrl={tour.splat_url}
                splatFormat={tour.splat_format}
                splatScene={tour.splat_scene}
                panoScenes={tour.pano_scenes}
                disclosure={tour.disclosure}
                floors={tour.floors}
                address={lookup?.address || ''}
                title={lookup?.address || 'Property tour'}
                // The badge has to survive into the viewer. Once someone is
                // walking around, the button that carried the caveat is off
                // screen and a stand-in space looks like a real capture.
                isThisProperty={tour.is_this_property !== false}
                onClose={() => setWalkOpenFor(null)}
              />
            </Suspense>
          ) : null}

          {view?.upload_links?.length ? (
            <section className={styles.linksSection} aria-label="Client upload links">
              <h3>Client upload links</h3>
              <ul className={styles.linkList}>
                {view.upload_links.map((link) => (
                  <li key={link.id} data-inactive={link.revoked || link.expired}>
                    <span>{link.label || 'Upload link'}</span>
                    <span className={styles.linkMeta}>
                      {link.upload_count}/{link.max_uploads} used
                      {link.revoked ? ' · revoked' : link.expired ? ' · expired' : ''}
                    </span>
                    {!link.revoked && !link.expired ? (
                      <button type="button" onClick={() => revokeLink(link.id)}>
                        <Trash2 aria-hidden="true" /> Revoke
                      </button>
                    ) : null}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
