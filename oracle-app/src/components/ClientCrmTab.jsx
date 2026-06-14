import { useCallback, useEffect, useRef, useState } from 'react';
import { crmGet, crmPost } from '../state/useCrmApi';
import styles from './ClientCrmTab.module.css';

// Inline stroke glyphs — currentColor, zero icon deps (house rule).
const GLYPHS = {
  refresh: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M20.5 12a8.5 8.5 0 1 1-2.49-6.01" />
      <path d="M20.5 3.5V8H16" />
    </svg>
  ),
  plus: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 5.5v13M5.5 12h13" />
    </svg>
  ),
  close: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m6 6 12 12M18 6 6 18" />
    </svg>
  ),
  clients: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="9" cy="8.5" r="3.2" />
      <path d="M3.5 19.5c0-3 2.5-5 5.5-5s5.5 2 5.5 5" />
      <circle cx="16.5" cy="9.5" r="2.4" />
      <path d="M16 14.7c2.6.3 4.5 2 4.5 4.3" />
    </svg>
  ),
  house: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 10.5 12 3l9 7.5" />
      <path d="M5.5 9.5V20h13V9.5" />
      <path d="M9.5 20v-6h5v6" />
    </svg>
  ),
  eye: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  ),
};

const SEGMENTS = [
  { id: 'seller', label: 'Sellers' },
  { id: 'buyer', label: 'Buyers' },
];

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const EMPTY_FORM = { full_name: '', email: '', phone: '', budget: '', beds: '', zips: '' };
const fmtInt = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });

// numeric / jsonb values may land as strings — parse defensively, never trust shape.
function toNum(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = typeof v === 'string' ? Number(v) : v;
  return Number.isFinite(n) ? n : null;
}

// $1.2M / $850K short-form for budget + pick rows.
function shortDollars(n) {
  const trim = (x) => {
    const s = x >= 100 ? String(Math.round(x)) : x.toFixed(1);
    return s.replace(/\.0$/, '');
  };
  const abs = Math.abs(n);
  if (abs >= 1e9) return `$${trim(n / 1e9)}B`;
  if (abs >= 1e6) return `$${trim(n / 1e6)}M`;
  if (abs >= 1e3) return `$${trim(n / 1e3)}K`;
  return `$${fmtInt.format(n)}`;
}

function initialsOf(name) {
  return (
    String(name || '')
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((w) => w[0].toUpperCase())
      .join('') || '·'
  );
}

// "now / 12m / 3h / 2d / 5w / 4mo / 1y" — instrument-cluster relative time.
function relTime(iso) {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return 'now';
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  const d = s / 86400;
  if (d < 7) return `${Math.floor(d)}d`;
  if (d < 30) return `${Math.floor(d / 7)}w`;
  if (d < 365) return `${Math.floor(d / 30)}mo`;
  return `${Math.floor(d / 365)}y`;
}

function normalizeType(t, fallback) {
  const v = String(t || '').toLowerCase();
  return v === 'seller' || v === 'buyer' || v === 'both' ? v : fallback;
}

// preferences is jsonb — could be an object, a JSON string, or junk. Never crash.
function parsePrefs(p) {
  if (!p) return null;
  if (typeof p === 'string') {
    try { p = JSON.parse(p); } catch { return null; }
  }
  return typeof p === 'object' && !Array.isArray(p) ? p : null;
}

function budgetChip(b) {
  if (b === null || b === undefined) return null;
  if (typeof b === 'object' && !Array.isArray(b)) {
    const min = toNum(b.min);
    const max = toNum(b.max);
    if (min !== null && max !== null) return `${shortDollars(min)}–${shortDollars(max)}`;
    if (max !== null) return `≤ ${shortDollars(max)}`;
    if (min !== null) return `≥ ${shortDollars(min)}`;
    return null;
  }
  const n = toNum(b);
  if (n !== null) return shortDollars(n);
  return typeof b === 'string' && b.trim() ? b.trim() : null;
}

// Buyer preference chips — defensively read budget / beds / zips keys.
function prefChipsOf(preferences) {
  const p = parsePrefs(preferences);
  if (!p) return [];
  const chips = [];
  const budget = budgetChip(p.budget ?? p.price ?? p.max_price);
  if (budget) chips.push(budget);
  const bedsRaw = p.beds ?? p.bedrooms;
  const beds = toNum(bedsRaw);
  if (beds !== null) chips.push(`${fmtInt.format(beds)} bd`);
  else if (typeof bedsRaw === 'string' && bedsRaw.trim()) chips.push(`${bedsRaw.trim()} bd`);
  const zipsRaw = p.zips ?? p.zip_codes ?? p.zip;
  const zips = Array.isArray(zipsRaw)
    ? zipsRaw.map((z) => String(z).trim()).filter(Boolean)
    : typeof zipsRaw === 'string'
      ? zipsRaw.split(/[\s,]+/).filter(Boolean)
      : [];
  chips.push(...zips.slice(0, 4));
  if (zips.length > 4) chips.push(`+${zips.length - 4}`);
  return chips;
}

/**
 * ClientCrmTab — the two-sided client book: SELLERS | BUYERS.
 * Sellers wear amber (their houses listed as address chips); buyers wear blue
 * (preference chips + a SEEN filmstrip of toured houses). The FAB raises the
 * Create Profile bottom-sheet; buyer cards log showings against real listings.
 * Renders only what /api/crm/clients returns — never simulates data.
 */
export default function ClientCrmTab() {
  const [segment, setSegment] = useState('seller');
  const [book, setBook] = useState({ seller: null, buyer: null }); // null = not loaded
  const [errors, setErrors] = useState({ seller: null, buyer: null });
  const [refreshing, setRefreshing] = useState(false);

  // Create-profile bottom sheet.
  const [sheetOpen, setSheetOpen] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [formType, setFormType] = useState('seller');
  const [formError, setFormError] = useState('');
  const [saving, setSaving] = useState(false);

  // Inline showing picker — one open at a time, listings cached across opens.
  const [picker, setPicker] = useState(null);
  const listingsCache = useRef(null);

  // All setState lands in async continuations — never synchronously inside
  // the effect's call stack (react-hooks/set-state-in-effect compliant).
  const fetchSegment = useCallback((seg) => {
    return crmGet(`/api/crm/clients?type=${seg}`).then(
      (data) => {
        setBook((b) => ({ ...b, [seg]: Array.isArray(data?.clients) ? data.clients : [] }));
        setErrors((e) => ({ ...e, [seg]: null }));
        setRefreshing(false);
      },
      (err) => {
        setErrors((e) => ({ ...e, [seg]: err })); // 404 until the route deploys — handled below
        setRefreshing(false);
      }
    );
  }, []);

  useEffect(() => {
    if (book[segment] === null && !errors[segment]) fetchSegment(segment);
  }, [segment, book, errors, fetchSegment]);

  useEffect(() => {
    if (!sheetOpen) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') setSheetOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [sheetOpen]);

  const refresh = () => {
    setRefreshing(true);
    setErrors((e) => ({ ...e, [segment]: null }));
    fetchSegment(segment);
  };

  const retry = () => {
    setErrors((e) => ({ ...e, [segment]: null })); // back to skeletons while in flight
    fetchSegment(segment);
  };

  // ── Listings — fetched once, shared by every picker open ─────────────────
  const getListings = useCallback(() => {
    if (!listingsCache.current) {
      const p = crmGet('/api/crm/listings').then((d) =>
        Array.isArray(d?.listings) ? d.listings : []
      );
      p.catch(() => {
        if (listingsCache.current === p) listingsCache.current = null; // allow retry
      });
      listingsCache.current = p;
    }
    return listingsCache.current;
  }, []);

  const loadPickerListings = useCallback(
    (clientId) => {
      getListings().then(
        (rows) =>
          setPicker((p) => (p && p.clientId === clientId ? { ...p, status: 'ready', listings: rows } : p)),
        (err) =>
          setPicker((p) =>
            p && p.clientId === clientId
              ? {
                  ...p,
                  status: 'error',
                  error:
                    err.status === 404
                      ? 'Listings service isn’t online yet.'
                      : err.message || 'Could not load listings.',
                }
              : p
          )
      );
    },
    [getListings]
  );

  const togglePicker = (client) => {
    if (picker?.clientId === client.id) {
      setPicker(null);
      return;
    }
    setPicker({ clientId: client.id, status: 'loading', listings: [], error: '', postingId: null, postError: '' });
    loadPickerListings(client.id);
  };

  const retryPicker = (clientId) => {
    setPicker((p) => (p && p.clientId === clientId ? { ...p, status: 'loading', error: '' } : p));
    loadPickerListings(clientId);
  };

  const logShowing = (client, listing) => {
    setPicker((p) => (p && p.clientId === client.id ? { ...p, postingId: listing.id, postError: '' } : p));
    crmPost('/api/crm/showings', { client_id: client.id, listing_id: listing.id, outcome: 'pending' }).then(
      () => {
        // The POST landed — reflect the real event: house joins the SEEN
        // filmstrip, last_touch becomes the showing we just recorded.
        const touch = { interaction_type: 'showing', created_at: new Date().toISOString() };
        setBook((b) => {
          const stamp = (list) =>
            Array.isArray(list)
              ? list.map((c) => {
                  if (c.id !== client.id) return c;
                  const houses = Array.isArray(c.houses) ? c.houses : [];
                  const already = houses.some((h) => h.id === listing.id);
                  return {
                    ...c,
                    last_touch: touch,
                    houses: already
                      ? houses
                      : [...houses, { id: listing.id, address: listing.address, kind: 'listing' }],
                  };
                })
              : list;
          return { seller: stamp(b.seller), buyer: stamp(b.buyer) };
        });
        setPicker(null);
      },
      (err) => {
        setPicker((p) =>
          p && p.clientId === client.id
            ? {
                ...p,
                postingId: null,
                postError:
                  err.status === 404
                    ? 'Showings service isn’t online yet — nothing was saved.'
                    : err.message || 'Could not log the showing.',
              }
            : p
        );
      }
    );
  };

  // ── Create Profile sheet ──────────────────────────────────────────────────
  const openSheet = () => {
    setFormType(segment);
    setFormError('');
    setSheetOpen(true);
  };

  const closeSheet = () => {
    if (!saving) setSheetOpen(false);
  };

  const setField = (key, value) => setForm((f) => ({ ...f, [key]: value }));

  const submitProfile = () => {
    const name = form.full_name.trim();
    if (!name) {
      setFormError('A name is required.');
      return;
    }
    const email = form.email.trim();
    if (email && !EMAIL_RE.test(email)) {
      setFormError('That email doesn’t look right.');
      return;
    }
    const body = { full_name: name, client_type: formType };
    if (email) body.email = email;
    if (form.phone.trim()) body.phone = form.phone.trim();
    const prefs = {};
    const budget = toNum(form.budget.trim());
    if (budget !== null) prefs.budget = budget;
    const beds = toNum(form.beds.trim());
    if (beds !== null) prefs.beds = beds;
    const zips = form.zips.split(/[\s,]+/).map((z) => z.trim()).filter(Boolean);
    if (zips.length) prefs.zips = zips;
    if (Object.keys(prefs).length) body.preferences = prefs;

    setSaving(true);
    setFormError('');
    crmPost('/api/crm/clients', body).then(
      (data) => {
        const made = {
          houses: [],
          last_touch: null,
          created_at: new Date().toISOString(),
          ...(data?.client ?? body),
        };
        const type = normalizeType(made.client_type, formType);
        const targets = type === 'both' ? ['seller', 'buyer'] : [type];
        // Optimistic prepend into every loaded segment the new client belongs
        // to; unloaded segments stay null and fetch fresh on first open.
        setBook((b) => {
          const next = { ...b };
          for (const t of targets) {
            if (Array.isArray(next[t])) next[t] = [made, ...next[t]];
          }
          return next;
        });
        setSaving(false);
        setSheetOpen(false);
        setForm(EMPTY_FORM);
        if (type === 'seller' || type === 'buyer') setSegment(type);
      },
      (err) => {
        setSaving(false);
        setFormError(
          err.status === 404
            ? 'The client service isn’t deployed yet — nothing was saved.'
            : err.message || 'The profile could not be created.'
        );
      }
    );
  };

  // ── Render ────────────────────────────────────────────────────────────────
  const clients = book[segment];
  const error = errors[segment];
  const isLoading = clients === null && !error;
  const errMsg =
    error?.status === 404
      ? 'Client book isn’t online yet.'
      : error?.message || 'Couldn’t reach the client book.';

  return (
    <section className={styles.wrap} aria-label="Clients — the two-sided book" aria-busy={isLoading || refreshing}>
      <header className={styles.topRow}>
        <span className={styles.kicker}>Client Book</span>
        <button
          type="button"
          className={`${styles.refreshBtn} ${refreshing ? styles.refreshing : ''}`}
          onClick={refresh}
          disabled={refreshing || isLoading}
          aria-label={`Refresh ${segment === 'seller' ? 'sellers' : 'buyers'}`}
        >
          {GLYPHS.refresh}
        </button>
      </header>

      {/* Two-port segmented control — amber for sellers, blue for buyers */}
      <div className={styles.segments}>
        {SEGMENTS.map((s) => {
          const active = segment === s.id;
          const count = Array.isArray(book[s.id]) ? book[s.id].length : null;
          return (
            <button
              key={s.id}
              type="button"
              data-seg={s.id}
              aria-pressed={active}
              className={`${styles.segKey}${active ? ` ${styles.segKeyActive}` : ''}`}
              onClick={() => setSegment(s.id)}
            >
              <span className={styles.segLabel}>{s.label}</span>
              {count !== null && <span className={styles.segCount}>{fmtInt.format(count)}</span>}
            </button>
          );
        })}
      </div>

      {isLoading ? (
        <div className={styles.skeleton} aria-hidden="true">
          <div className={styles.skelCard} />
          <div className={styles.skelCard} />
          <div className={styles.skelCard} />
        </div>
      ) : error && clients === null ? (
        <div className={styles.errorBox} role="alert">
          <span className={styles.errorTick} aria-hidden="true" />
          <p className={styles.errorText}>{errMsg}</p>
          <button type="button" className={styles.retryBtn} onClick={retry}>
            Retry
          </button>
        </div>
      ) : (
        <>
          {error && (
            <div className={styles.errorStrip} role="alert">
              <span className={styles.errorTick} aria-hidden="true" />
              <p className={styles.errorText}>{errMsg}</p>
              <button type="button" className={styles.retrySm} onClick={refresh}>
                Retry
              </button>
            </div>
          )}

          {clients.length === 0 ? (
            <div className={styles.empty}>
              <span className={styles.emptyGlyph} aria-hidden="true">{GLYPHS.clients}</span>
              <p className={styles.emptyTitle}>
                {segment === 'seller' ? 'No sellers on file' : 'No buyers on file'}
              </p>
              <p className={styles.emptyHint}>
                {segment === 'seller'
                  ? 'Tap + to create a seller profile, or attach an owner from the House tab.'
                  : 'Tap + to create a buyer profile — preferences feed the match engine.'}
              </p>
            </div>
          ) : (
            <ul key={segment} className={styles.list} role="list">
              {clients.map((c, i) => {
                const tint = normalizeType(c.client_type, segment);
                const houses = Array.isArray(c.houses) ? c.houses : [];
                const prefChips = segment === 'buyer' ? prefChipsOf(c.preferences) : [];
                const touched = relTime(c.last_touch?.created_at);
                const pickerOpen = picker?.clientId === c.id;

                return (
                  <li
                    key={c.id ?? `${c.full_name}-${i}`}
                    className={styles.card}
                    data-type={tint}
                    style={{ animationDelay: `${Math.min(i, 8) * 70}ms` }}
                  >
                    <div className={styles.cardHead}>
                      <span className={styles.ring} data-tint={tint} aria-hidden="true">
                        {initialsOf(c.full_name)}
                      </span>
                      <div className={styles.idBlock}>
                        <span className={styles.name}>{c.full_name || 'Unnamed'}</span>
                        {(c.email || c.phone) && (
                          <span className={styles.contact}>
                            {c.email && <span className={styles.contactBit}>{c.email}</span>}
                            {c.email && c.phone && (
                              <span className={styles.dotSep} aria-hidden="true">·</span>
                            )}
                            {c.phone && <span className={styles.contactBit}>{c.phone}</span>}
                          </span>
                        )}
                      </div>
                      <div className={styles.touch}>
                        {c.last_touch ? (
                          <>
                            <span className={styles.touchTime}>{touched ?? '—'}</span>
                            <span className={styles.touchType}>
                              {c.last_touch.interaction_type || 'touch'}
                            </span>
                          </>
                        ) : (
                          <span className={styles.touchGhost}>No touch</span>
                        )}
                      </div>
                    </div>

                    {/* Sellers — their houses, the property↔person graph */}
                    {segment === 'seller' && houses.length > 0 && (
                      <div className={styles.houseRow}>
                        {houses.map((h, j) => (
                          <span
                            key={h.id ?? `${h.address}-${j}`}
                            className={styles.houseChip}
                            data-kind={h.kind === 'lead' ? 'lead' : 'listing'}
                          >
                            <span className={styles.chipGlyph} aria-hidden="true">{GLYPHS.house}</span>
                            <span className={styles.chipText}>{h.address || 'Address pending'}</span>
                            {h.kind === 'lead' && <span className={styles.kindTag}>Lead</span>}
                          </span>
                        ))}
                      </div>
                    )}

                    {/* Buyers — preference chips + SEEN filmstrip + log showing */}
                    {segment === 'buyer' && (
                      <>
                        {prefChips.length > 0 && (
                          <div className={styles.prefRow}>
                            {prefChips.map((p, j) => (
                              <span key={`${p}-${j}`} className={styles.prefChip}>{p}</span>
                            ))}
                          </div>
                        )}

                        {houses.length > 0 && (
                          <div className={styles.seenBlock}>
                            <span className={styles.seenLabel}>
                              <span className={styles.seenGlyph} aria-hidden="true">{GLYPHS.eye}</span>
                              Seen · {fmtInt.format(houses.length)}
                            </span>
                            <div className={styles.filmstrip}>
                              {houses.map((h, j) => (
                                <span
                                  key={h.id ?? `${h.address}-${j}`}
                                  className={styles.frameChip}
                                  data-kind={h.kind === 'lead' ? 'lead' : 'listing'}
                                >
                                  {h.address || 'Address pending'}
                                </span>
                              ))}
                            </div>
                          </div>
                        )}

                        <div className={styles.cardActions}>
                          <button
                            type="button"
                            className={styles.logBtn}
                            aria-expanded={pickerOpen}
                            onClick={() => togglePicker(c)}
                          >
                            <span className={styles.btnGlyph} aria-hidden="true">{GLYPHS.eye}</span>
                            {pickerOpen ? 'Close' : 'Log Showing'}
                          </button>
                        </div>

                        {pickerOpen && (
                          <div className={styles.picker}>
                            <span className={styles.pickerLabel}>Pick the listing shown</span>

                            {picker.status === 'loading' && (
                              <div className={styles.pickSkels} aria-hidden="true">
                                <div className={styles.pickSkel} />
                                <div className={styles.pickSkel} />
                              </div>
                            )}

                            {picker.status === 'error' && (
                              <div className={styles.errorStrip} role="alert">
                                <span className={styles.errorTick} aria-hidden="true" />
                                <p className={styles.errorText}>{picker.error}</p>
                                <button
                                  type="button"
                                  className={styles.retrySm}
                                  onClick={() => retryPicker(c.id)}
                                >
                                  Retry
                                </button>
                              </div>
                            )}

                            {picker.status === 'ready' && picker.listings.length === 0 && (
                              <p className={styles.pickerEmpty}>
                                No listings on file — stage a house first.
                              </p>
                            )}

                            {picker.status === 'ready' &&
                              picker.listings.map((l, j) => {
                                const seen = houses.some((h) => h.id === l.id);
                                const posting = picker.postingId === l.id;
                                const price = toNum(l.price);
                                return (
                                  <button
                                    key={l.id ?? `${l.address}-${j}`}
                                    type="button"
                                    className={styles.pickRow}
                                    disabled={seen || picker.postingId !== null}
                                    onClick={() => logShowing(c, l)}
                                  >
                                    <span className={styles.chipGlyph} aria-hidden="true">{GLYPHS.house}</span>
                                    <span className={styles.pickAddress}>
                                      {l.address || 'Address pending'}
                                    </span>
                                    <span className={styles.pickMeta}>
                                      {posting
                                        ? 'Logging…'
                                        : seen
                                          ? 'Seen'
                                          : price !== null
                                            ? shortDollars(price)
                                            : ''}
                                    </span>
                                  </button>
                                );
                              })}

                            {picker.postError && (
                              <span className={styles.err} role="alert">{picker.postError}</span>
                            )}
                          </div>
                        )}
                      </>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}

      {/* FAB — floats above the Deck, raises the Create Profile sheet */}
      <button type="button" className={styles.fab} aria-label="Create client profile" onClick={openSheet}>
        {GLYPHS.plus}
      </button>

      {sheetOpen && (
        <div className={styles.sheetLayer}>
          <button type="button" className={styles.scrim} aria-label="Close" onClick={closeSheet} />
          <div className={styles.sheet} role="dialog" aria-modal="true" aria-label="Create client profile">
            <span className={styles.sheetGrip} aria-hidden="true" />
            <header className={styles.sheetHead}>
              <div className={styles.sheetHeading}>
                <span className={styles.sheetKicker}>New Client</span>
                <h3 className={styles.sheetTitle}>Create Profile</h3>
              </div>
              <button
                type="button"
                className={styles.sheetClose}
                onClick={closeSheet}
                disabled={saving}
                aria-label="Close"
              >
                {GLYPHS.close}
              </button>
            </header>

            {/* Seller ⇄ buyer toggle — same two-port control as the book */}
            <div className={`${styles.segments} ${styles.typeToggle}`}>
              {SEGMENTS.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  data-seg={s.id}
                  aria-pressed={formType === s.id}
                  className={`${styles.segKey}${formType === s.id ? ` ${styles.segKeyActive}` : ''}`}
                  onClick={() => setFormType(s.id)}
                >
                  <span className={styles.segLabel}>{s.id === 'seller' ? 'Seller' : 'Buyer'}</span>
                </button>
              ))}
            </div>

            <label className={styles.field}>
              <span className={styles.microLabel}>Full Name · Required</span>
              <input
                className={styles.input}
                value={form.full_name}
                onChange={(e) => {
                  setField('full_name', e.target.value);
                  if (formError) setFormError('');
                }}
                autoComplete="name"
                placeholder="Client name"
              />
            </label>

            <div className={styles.formSection}>
              <span className={styles.sectionLabel}>Contact Info</span>
              <label className={styles.field}>
                <span className={styles.microLabel}>Email</span>
                <input
                  className={styles.input}
                  value={form.email}
                  onChange={(e) => setField('email', e.target.value)}
                  type="email"
                  inputMode="email"
                  autoComplete="email"
                  placeholder="client@email.com"
                />
              </label>
              <label className={styles.field}>
                <span className={styles.microLabel}>Phone</span>
                <input
                  className={`${styles.input} ${styles.inputMono}`}
                  value={form.phone}
                  onChange={(e) => setField('phone', e.target.value)}
                  type="tel"
                  inputMode="tel"
                  autoComplete="tel"
                  placeholder="555 000 0000"
                />
              </label>
            </div>

            <div className={styles.formSection}>
              <span className={styles.sectionLabel}>Preferences</span>
              <div className={styles.fieldGrid}>
                <label className={styles.field}>
                  <span className={styles.microLabel}>Budget · USD</span>
                  <input
                    className={`${styles.input} ${styles.inputMono}`}
                    value={form.budget}
                    onChange={(e) => setField('budget', e.target.value.replace(/[^\d.]/g, ''))}
                    inputMode="decimal"
                    placeholder="0"
                  />
                </label>
                <label className={styles.field}>
                  <span className={styles.microLabel}>Beds</span>
                  <input
                    className={`${styles.input} ${styles.inputMono}`}
                    value={form.beds}
                    onChange={(e) => setField('beds', e.target.value.replace(/[^\d]/g, ''))}
                    inputMode="numeric"
                    placeholder="0"
                  />
                </label>
              </div>
              <label className={styles.field}>
                <span className={styles.microLabel}>Zip Codes · Comma separated</span>
                <input
                  className={`${styles.input} ${styles.inputMono}`}
                  value={form.zips}
                  onChange={(e) => setField('zips', e.target.value)}
                  inputMode="numeric"
                  placeholder="33101, 33139"
                />
              </label>
            </div>

            {formError && (
              <span className={styles.err} role="alert">{formError}</span>
            )}

            <button
              type="button"
              className={styles.primaryBtn}
              onClick={submitProfile}
              disabled={saving}
            >
              {saving ? 'Saving…' : 'Create Profile'}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
