import { Fragment, Suspense, lazy, useCallback, useEffect, useMemo, useState } from 'react';
import { crmGet, crmPost } from '../state/useCrmApi';
import { ComplianceDashboard } from './ComplianceDashboard';
import MediaUploader from './MediaUploader';
import styles from './HouseProfileTab.module.css';

// The photoreal 3D tour pulls in the Google Maps loader on demand — keep it in
// its own chunk so the House form paints without it until the agent opens a tour.
const PropertyTour = lazy(() => import('./PropertyTour'));

// Inline stroke glyphs — same idiom as TabBar GLYPHS, zero icon deps.
const GLYPHS = {
  house: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 10.5 12 3l9 7.5" />
      <path d="M5.5 9.5V20h13V9.5" />
      <path d="M9.5 20v-6h5v6" />
    </svg>
  ),
  owner: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="8" r="3.5" />
      <path d="M5 20c0-3.6 3.1-6 7-6s7 2.4 7 6" />
    </svg>
  ),
  check: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 12.5 10 17.5 19 7" />
    </svg>
  ),
  search: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="11" cy="11" r="6.5" />
      <path d="m20 20-4.4-4.4" />
    </svg>
  ),
  plus: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 5.5v13M5.5 12h13" />
    </svg>
  ),
};

const STEPS = ['House', 'Seller', 'Review'];
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const EMPTY_HOUSE = { address: '', price: '', beds: '', baths: '', sqft: '' };
const EMPTY_OWNER = { full_name: '', email: '', phone: '' };

const usd = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
});

// numeric(12,2) can land as a string — format defensively, never crash.
function formatPrice(value) {
  if (value === '' || value == null) return '—';
  const n = Number(value);
  return Number.isFinite(n) ? usd.format(n) : '—';
}

function formatSqft(value) {
  if (value === '' || value == null) return '—';
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString('en-US') : '—';
}

const onlyDigits = (v) => v.replace(/[^\d]/g, '');
const onlyDecimal = (v) => {
  const cleaned = v.replace(/[^\d.]/g, '');
  const i = cleaned.indexOf('.');
  return i === -1 ? cleaned : cleaned.slice(0, i + 1) + cleaned.slice(i + 1).replace(/\./g, '');
};

/**
 * HouseProfileTab — "Create Profile for House".
 * Three steps on a filament rail: HOUSE → SELLER → REVIEW. The seller step
 * is the point of the product: attach the home owner so the AI knows this
 * house belongs to this person. Confirm POSTs /api/crm/listings.
 */
export default function HouseProfileTab() {
  const [step, setStep] = useState(0);

  // Step 1 — the house.
  const [house, setHouse] = useState(EMPTY_HOUSE);
  const [houseError, setHouseError] = useState('');

  // Step 2 — the owner. Real sellers from the CRM; no simulated rows.
  const [sellers, setSellers] = useState([]);
  const [sellersStatus, setSellersStatus] = useState('idle'); // idle|loading|error|ready
  const [sellersError, setSellersError] = useState('');
  const [query, setQuery] = useState('');
  const [picked, setPicked] = useState(null);
  const [creating, setCreating] = useState(false);
  const [owner, setOwner] = useState(EMPTY_OWNER);
  const [sellerError, setSellerError] = useState('');

  // Step 3 — submit.
  const [submitStatus, setSubmitStatus] = useState('idle'); // idle|busy|error|done
  const [submitError, setSubmitError] = useState('');
  const [listed, setListed] = useState(null);

  // Photoreal 3D flyover overlay — opens against whatever address is on file.
  const [tourOpen, setTourOpen] = useState(false);

  const loadSellers = useCallback(() => {
    crmGet('/api/crm/clients?type=seller').then(
      (data) => {
        setSellers(Array.isArray(data?.clients) ? data.clients : []);
        setSellersStatus('ready');
      },
      (err) => {
        // The route may 404 until the backend ships — quiet inline state.
        setSellersError(
          err.status === 404
            ? 'Seller directory is not online yet.'
            : err.message || 'Could not reach the CRM.'
        );
        setSellersStatus('error');
      }
    );
  }, []);

  // Flip idle→loading during render when the seller step opens, then let the
  // effect run the async fetch — keeps the effect free of synchronous setState.
  if (step === 1 && sellersStatus === 'idle') {
    setSellersStatus('loading');
    setSellersError('');
  }

  useEffect(() => {
    if (sellersStatus === 'loading') loadSellers();
  }, [sellersStatus, loadSellers]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return sellers;
    return sellers.filter((c) =>
      `${c.full_name ?? ''} ${c.email ?? ''}`.toLowerCase().includes(q)
    );
  }, [sellers, query]);

  const setHouseField = (key, value) => setHouse((h) => ({ ...h, [key]: value }));
  const setOwnerField = (key, value) => setOwner((o) => ({ ...o, [key]: value }));

  const inlineOwnerValid =
    creating && owner.full_name.trim() !== '' && EMAIL_RE.test(owner.email.trim());

  const ownerForReview = picked
    ? { full_name: picked.full_name, email: picked.email }
    : inlineOwnerValid
      ? { full_name: owner.full_name.trim(), email: owner.email.trim() }
      : null;

  const nextFromHouse = () => {
    if (!house.address.trim()) {
      setHouseError('An address is required.');
      return;
    }
    setHouseError('');
    setStep(1);
  };

  const nextFromSeller = () => {
    if (picked) {
      setSellerError('');
      setStep(2);
      return;
    }
    if (creating) {
      if (!owner.full_name.trim()) {
        setSellerError('The owner needs a name.');
        return;
      }
      if (!EMAIL_RE.test(owner.email.trim())) {
        setSellerError('A valid email is required — the AI reaches the owner through it.');
        return;
      }
      setSellerError('');
      setStep(2);
      return;
    }
    setSellerError('Pick an owner, add a new one, or continue without.');
  };

  const continueWithoutOwner = () => {
    setPicked(null);
    setCreating(false);
    setOwner(EMPTY_OWNER);
    setSellerError('');
    setStep(2);
  };

  const togglePick = (client) => {
    setSellerError('');
    setCreating(false);
    setPicked((p) => (p && p.id === client.id ? null : client));
  };

  const toggleCreate = () => {
    setSellerError('');
    setPicked(null);
    setCreating((v) => !v);
  };

  const submit = async () => {
    setSubmitStatus('busy');
    setSubmitError('');
    const body = { address: house.address.trim() };
    const putNum = (key, raw) => {
      if (raw === '') return;
      const n = Number(raw);
      if (Number.isFinite(n)) body[key] = n;
    };
    putNum('price', house.price);
    putNum('beds', house.beds);
    putNum('baths', house.baths);
    putNum('sqft', house.sqft);
    if (picked) {
      body.seller_client_id = picked.id;
    } else if (inlineOwnerValid) {
      body.seller = { full_name: owner.full_name.trim(), email: owner.email.trim() };
      if (owner.phone.trim()) body.seller.phone = owner.phone.trim();
    }
    try {
      const data = await crmPost('/api/crm/listings', body);
      setListed(data?.listing ?? null);
      setSubmitStatus('done');
    } catch (err) {
      setSubmitError(
        err.status === 404
          ? 'The listing service is not deployed yet — nothing was saved.'
          : err.message || 'The listing could not be created.'
      );
      setSubmitStatus('error');
    }
  };

  const reset = () => {
    setStep(0);
    setHouse(EMPTY_HOUSE);
    setHouseError('');
    setQuery('');
    setPicked(null);
    setCreating(false);
    setOwner(EMPTY_OWNER);
    setSellerError('');
    setSubmitStatus('idle');
    setSubmitError('');
    setListed(null);
  };

  // ── Success ──────────────────────────────────────────────────────────────
  if (submitStatus === 'done') {
    const ownerName = listed?.seller?.full_name || ownerForReview?.full_name || null;
    return (
      <div className={styles.wrap}>
        <div className={styles.done} role="status">
          <span className={styles.doneCheck} aria-hidden="true">{GLYPHS.check}</span>
          <span className={styles.doneTitle}>Listed</span>
          <span className={styles.doneAddress}>{listed?.address || house.address.trim()}</span>
          {ownerName && (
            <span className={styles.doneOwner}>Owner on file — {ownerName}</span>
          )}
          <button type="button" className={styles.primaryBtn} onClick={reset}>
            Add another
          </button>
        </div>
        {listed?.id && (
          <MediaUploader
            listingId={listed.id}
            token={typeof sessionStorage !== 'undefined' ? sessionStorage.getItem('oracle_token') : null}
            title="Property photos"
          />
        )}
        <ComplianceDashboard listingId={listed?.id} />
      </div>
    );
  }

  const busy = submitStatus === 'busy';

  return (
    <div className={styles.wrap}>
      <header className={styles.heading}>
        <span className={styles.kicker}>New Profile</span>
        <h2 className={styles.title}>Create Profile for House</h2>
      </header>

      {/* Filament progress rail */}
      <nav className={styles.rail} aria-label={`Progress — step ${step + 1} of ${STEPS.length}`}>
        {STEPS.map((label, i) => (
          <Fragment key={label}>
            {i > 0 && (
              <span
                className={`${styles.railLine}${i <= step ? ` ${styles.railLineLit}` : ''}`}
                aria-hidden="true"
              />
            )}
            <span
              className={`${styles.railNode}${i === step ? ` ${styles.railNodeActive}` : ''}${i < step ? ` ${styles.railNodeDone}` : ''}`}
              aria-current={i === step ? 'step' : undefined}
            >
              <span className={styles.railDot}>{i < step ? GLYPHS.check : i + 1}</span>
              <span className={styles.railLabel}>{label}</span>
            </span>
          </Fragment>
        ))}
      </nav>

      {/* ── Step 1 · HOUSE ─────────────────────────────────────────────── */}
      {step === 0 && (
        <section key="house" className={styles.stage} aria-label="House details">
          <label className={styles.field}>
            <span className={styles.microLabel}>Address · Required</span>
            <input
              className={styles.input}
              value={house.address}
              onChange={(e) => {
                setHouseField('address', e.target.value);
                if (houseError) setHouseError('');
              }}
              placeholder="Street, city, state"
              autoComplete="street-address"
              aria-invalid={houseError ? true : undefined}
            />
            {houseError && (
              <span className={styles.err} role="alert">{houseError}</span>
            )}
          </label>

          {house.address.trim() && (
            <button
              type="button"
              className={styles.tourLaunch}
              onClick={() => setTourOpen(true)}
            >
              <span className={styles.tourLaunchGlyph} aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M3 10.5 12 3l9 7.5" />
                  <path d="M5.5 9.5V20h13V9.5" />
                  <circle cx="12" cy="13.5" r="2" />
                </svg>
              </span>
              Preview the 3D tour
            </button>
          )}

          <label className={styles.field}>
            <span className={styles.microLabel}>Asking Price · USD</span>
            <input
              className={`${styles.input} ${styles.inputMono}`}
              value={house.price}
              onChange={(e) => setHouseField('price', onlyDecimal(e.target.value))}
              inputMode="decimal"
              placeholder="0"
            />
          </label>

          <div className={styles.specGrid}>
            <label className={styles.field}>
              <span className={styles.microLabel}>Beds</span>
              <input
                className={`${styles.input} ${styles.inputMono}`}
                value={house.beds}
                onChange={(e) => setHouseField('beds', onlyDigits(e.target.value))}
                inputMode="numeric"
                placeholder="0"
              />
            </label>
            <label className={styles.field}>
              <span className={styles.microLabel}>Baths</span>
              <input
                className={`${styles.input} ${styles.inputMono}`}
                value={house.baths}
                onChange={(e) => setHouseField('baths', onlyDecimal(e.target.value))}
                inputMode="decimal"
                placeholder="0"
              />
            </label>
            <label className={styles.field}>
              <span className={styles.microLabel}>Sqft</span>
              <input
                className={`${styles.input} ${styles.inputMono}`}
                value={house.sqft}
                onChange={(e) => setHouseField('sqft', onlyDigits(e.target.value))}
                inputMode="numeric"
                placeholder="0"
              />
            </label>
          </div>

          <div className={styles.actions}>
            <button type="button" className={styles.primaryBtn} onClick={nextFromHouse}>
              Next Step
            </button>
          </div>
        </section>
      )}

      {/* ── Step 2 · SELLER ────────────────────────────────────────────── */}
      {step === 1 && (
        <section key="seller" className={styles.stage} aria-label="Attach the owner">
          <p className={styles.stepHint}>
            Attach the home owner — the AI knows this house belongs to this person.
          </p>

          <div className={styles.searchWrap}>
            <span className={styles.searchGlyph} aria-hidden="true">{GLYPHS.search}</span>
            <input
              className={`${styles.input} ${styles.searchInput}`}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search sellers"
              aria-label="Search sellers"
              type="search"
            />
          </div>

          {sellersStatus === 'loading' && (
            <div className={styles.skeletons} aria-hidden="true">
              <div className={styles.skeletonRow} />
              <div className={styles.skeletonRow} />
              <div className={styles.skeletonRow} />
            </div>
          )}

          {sellersStatus === 'error' && (
            <div className={styles.errorBox}>
              <span className={styles.errorText}>{sellersError}</span>
              <button type="button" className={styles.retryBtn} onClick={loadSellers}>
                Retry
              </button>
            </div>
          )}

          {sellersStatus === 'ready' && filtered.length === 0 && (
            <p className={styles.empty}>
              {sellers.length === 0
                ? 'No sellers on file yet — add the owner below.'
                : 'No match — refine the search or add the owner below.'}
            </p>
          )}

          {sellersStatus === 'ready' && filtered.length > 0 && (
            <ul className={styles.sellerList}>
              {filtered.map((c) => {
                const isPicked = picked?.id === c.id;
                const homes = c.houses?.length ?? 0;
                return (
                  <li key={c.id}>
                    <button
                      type="button"
                      className={`${styles.sellerRow}${isPicked ? ` ${styles.sellerRowPicked}` : ''}`}
                      aria-pressed={isPicked}
                      onClick={() => togglePick(c)}
                    >
                      <span className={styles.rowGlyph} aria-hidden="true">{GLYPHS.owner}</span>
                      <span className={styles.rowText}>
                        <span className={styles.rowName}>{c.full_name}</span>
                        {c.email && <span className={styles.rowMail}>{c.email}</span>}
                      </span>
                      <span className={styles.rowCount}>
                        {homes} {homes === 1 ? 'home' : 'homes'}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}

          <button
            type="button"
            className={styles.newOwnerBtn}
            onClick={toggleCreate}
            aria-expanded={creating}
          >
            <span className={styles.btnGlyph} aria-hidden="true">{GLYPHS.plus}</span>
            {creating ? 'Cancel new owner' : 'New owner'}
          </button>

          {creating && (
            <div className={styles.ownerForm}>
              <label className={styles.field}>
                <span className={styles.microLabel}>Full Name · Required</span>
                <input
                  className={styles.input}
                  value={owner.full_name}
                  onChange={(e) => setOwnerField('full_name', e.target.value)}
                  autoComplete="name"
                  placeholder="Owner of record"
                />
              </label>
              <label className={styles.field}>
                <span className={styles.microLabel}>Email · Required</span>
                <input
                  className={styles.input}
                  value={owner.email}
                  onChange={(e) => setOwnerField('email', e.target.value)}
                  type="email"
                  inputMode="email"
                  autoComplete="email"
                  placeholder="owner@email.com"
                />
              </label>
              <label className={styles.field}>
                <span className={styles.microLabel}>Phone</span>
                <input
                  className={`${styles.input} ${styles.inputMono}`}
                  value={owner.phone}
                  onChange={(e) => setOwnerField('phone', e.target.value)}
                  type="tel"
                  inputMode="tel"
                  autoComplete="tel"
                  placeholder="555 000 0000"
                />
              </label>
              <p className={styles.hint}>The AI emails the owner using this contact.</p>
            </div>
          )}

          {sellerError && (
            <span className={styles.err} role="alert">{sellerError}</span>
          )}

          <div className={styles.actions}>
            <button type="button" className={styles.ghostBtn} onClick={() => setStep(0)}>
              Back
            </button>
            <button type="button" className={styles.primaryBtn} onClick={nextFromSeller}>
              Next Step
            </button>
          </div>

          <button type="button" className={styles.quietLink} onClick={continueWithoutOwner}>
            Continue without an owner
          </button>
        </section>
      )}

      {/* ── Step 3 · REVIEW ────────────────────────────────────────────── */}
      {step === 2 && (
        <section key="review" className={styles.stage} aria-label="Review and confirm">
          <div className={styles.linkage}>
            <div className={styles.houseCard}>
              <span className={styles.houseGlyph} aria-hidden="true">{GLYPHS.house}</span>
              <span className={styles.houseAddress}>{house.address.trim()}</span>
              <span className={styles.housePrice}>{formatPrice(house.price)}</span>
              <div className={styles.houseSpecs}>
                <span className={styles.spec}>
                  <span className={styles.specValue}>{house.beds || '—'}</span>
                  <span className={styles.specLabel}>Beds</span>
                </span>
                <span className={styles.spec}>
                  <span className={styles.specValue}>{house.baths || '—'}</span>
                  <span className={styles.specLabel}>Baths</span>
                </span>
                <span className={styles.spec}>
                  <span className={styles.specValue}>{formatSqft(house.sqft)}</span>
                  <span className={styles.specLabel}>Sqft</span>
                </span>
              </div>
            </div>

            <span
              className={`${styles.linkFilament}${ownerForReview ? '' : ` ${styles.linkFilamentGhost}`}`}
              aria-hidden="true"
            />

            {ownerForReview ? (
              <div className={styles.ownerChip}>
                <span className={styles.rowGlyph} aria-hidden="true">{GLYPHS.owner}</span>
                <span className={styles.rowText}>
                  <span className={styles.rowName}>{ownerForReview.full_name}</span>
                  {ownerForReview.email && (
                    <span className={styles.rowMail}>{ownerForReview.email}</span>
                  )}
                </span>
                <span className={styles.ownerBadge}>Owner</span>
              </div>
            ) : (
              <div className={`${styles.ownerChip} ${styles.ownerChipGhost}`}>
                <span className={styles.rowMail}>No owner attached</span>
              </div>
            )}
          </div>

          {ownerForReview?.email && (
            <p className={styles.hint}>The AI emails the owner using this contact.</p>
          )}

          {submitStatus === 'error' && (
            <div className={styles.errorBox} role="alert">
              <span className={styles.errorText}>{submitError}</span>
            </div>
          )}

          <div className={styles.actions}>
            <button
              type="button"
              className={styles.ghostBtn}
              onClick={() => setStep(1)}
              disabled={busy}
            >
              Back
            </button>
            <button
              type="button"
              className={styles.primaryBtn}
              onClick={submit}
              disabled={busy}
            >
              {busy ? 'Listing…' : submitStatus === 'error' ? 'Retry' : 'Confirm & List'}
            </button>
          </div>
        </section>
      )}

      {/* Photoreal 3D flyover — full-screen overlay; degrades cleanly w/o a key. */}
      {tourOpen && (
        <div
          className={styles.tourOverlay}
          role="dialog"
          aria-modal="true"
          aria-label="3D property tour"
          onClick={(e) => { if (e.target === e.currentTarget) setTourOpen(false); }}
        >
          <div className={styles.tourFrame}>
            <Suspense fallback={null}>
              <PropertyTour
                address={house.address.trim()}
                listingId={listed?.id}
                onClose={() => setTourOpen(false)}
              />
            </Suspense>
          </div>
        </div>
      )}
    </div>
  );
}
