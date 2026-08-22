import { useCallback, useEffect, useMemo, useState } from 'react';
import { House, ImageOff, Loader2, Plus, RefreshCw, UserPlus, Users } from 'lucide-react';
import { crmGet, crmPost } from '../state/useCrmApi';
import styles from './ListingsInventory.module.css';

/**
 * ListingsInventory — the tenant's own listing records.
 *
 * `GET` and `POST /api/crm/listings` had no caller anywhere in the frontend, so
 * a listing could not be created through the product at all — while
 * `AssistantRecordPicker` lets the assistant anchor to one and `update_listing`
 * is an allowlisted agent tool. The agent could edit records nobody could make.
 *
 * This is distinct from the Houses view, which browses `/api/mls/public-records`
 * — seven million public parcels the workspace does not own.
 *
 * Two shapes of the API are surfaced rather than smoothed over:
 *
 *  - **A seller is an existing client or a new one, never both.** The server
 *    422s when both arrive, so the form makes it structurally impossible
 *    instead of validating after the fact.
 *  - **Beds, baths and sqft do not live on a listing.** They ride a companion
 *    lead the server creates only when at least one is supplied (migration
 *    0012: the lead *is* the house record). A listing saved without them has no
 *    lead, so the card says so — an empty specs row would read as a studio.
 */

const STATUSES = ['draft', 'active', 'pending', 'sold', 'withdrawn'];

const EMPTY_FORM = {
  address: '', price: '', beds: '', baths: '', sqft: '', status: 'active',
  sellerMode: 'none', seller_client_id: '', full_name: '', email: '', phone: '',
};

function money(value) {
  if (value === null || value === undefined || value === '') return null;
  return Number(value).toLocaleString(undefined, {
    style: 'currency', currency: 'USD', maximumFractionDigits: 0,
  });
}

function specsOf(listing) {
  const parts = [];
  if (listing.beds !== null && listing.beds !== undefined) parts.push(`${listing.beds} bd`);
  if (listing.baths !== null && listing.baths !== undefined) parts.push(`${listing.baths} ba`);
  if (listing.sqft !== null && listing.sqft !== undefined) {
    parts.push(`${Number(listing.sqft).toLocaleString()} sqft`);
  }
  return parts;
}

function ListingCard({ listing }) {
  const specs = specsOf(listing);
  return (
    <article className={styles.card}>
      <div className={styles.cover}>
        {listing.cover_url ? (
          <img src={listing.cover_url} alt="" loading="lazy" />
        ) : (
          // A missing cover is stated, not papered over with a placeholder
          // house graphic that reads as a real photo of the property.
          <span className={styles.noCover}>
            <ImageOff aria-hidden="true" />
            <small>No photo uploaded</small>
          </span>
        )}
      </div>
      <div className={styles.cardBody}>
        <header>
          <strong>{listing.address}</strong>
          <span className={styles.status} data-status={listing.status}>{listing.status}</span>
        </header>
        <p className={styles.price}>{money(listing.price) ?? 'No price set'}</p>
        {specs.length > 0 ? (
          <p className={styles.specs}>{specs.join(' · ')}</p>
        ) : (
          <p className={styles.specsMissing}>
            No beds, baths or square footage recorded — this listing has no
            companion property record, so valuation tools cannot read it.
          </p>
        )}
        <footer>
          {listing.seller ? (
            <span className={styles.seller}>
              <Users aria-hidden="true" />
              {listing.seller.full_name}
            </span>
          ) : (
            <span className={styles.sellerMissing}>No seller linked</span>
          )}
        </footer>
      </div>
    </article>
  );
}

export default function ListingsInventory() {
  const [listings, setListings] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [form, setForm] = useState(EMPTY_FORM);
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');

  const load = useCallback(async (signal) => {
    setLoading(true);
    setLoadError('');
    try {
      const data = await crmGet('/api/crm/listings', { signal, retries: 1 });
      setListings(Array.isArray(data?.listings) ? data.listings : []);
    } catch (error) {
      if (error?.name === 'AbortError') return;
      // "Nothing loaded" and "nothing exists" are different facts; the empty
      // state below only claims the second when this is clear.
      setLoadError(error?.message || 'Listings could not be loaded.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    // Mirrors an external request lifecycle, not derived render data — same
    // reasoning (and same disable) as HouseSelection's catalog fetch and
    // MarketplaceBrowse's browse fetch.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const set = (key) => (event) => {
    const { value } = event.target;
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const canSave = useMemo(() => {
    if (form.address.trim().length < 3) return false;
    if (form.sellerMode === 'existing' && !form.seller_client_id.trim()) return false;
    if (form.sellerMode === 'new') {
      return form.full_name.trim().length > 0 && form.email.trim().length > 0;
    }
    return true;
  }, [form]);

  async function submit(event) {
    event.preventDefault();
    if (!canSave || saving) return;
    setSaving(true);
    setSaveError('');

    const payload = { address: form.address.trim(), status: form.status };
    for (const key of ['price', 'beds', 'baths', 'sqft']) {
      if (form[key] !== '') payload[key] = Number(form[key]);
    }
    // Exactly one seller shape reaches the server. Sending both is a 422, and
    // the radio makes the third state unreachable rather than merely invalid.
    if (form.sellerMode === 'existing') {
      payload.seller_client_id = form.seller_client_id.trim();
    } else if (form.sellerMode === 'new') {
      payload.seller = {
        full_name: form.full_name.trim(),
        email: form.email.trim(),
        ...(form.phone.trim() ? { phone: form.phone.trim() } : {}),
      };
    }

    try {
      await crmPost('/api/crm/listings', payload);
      setForm(EMPTY_FORM);
      setOpen(false);
      await load();
    } catch (error) {
      setSaveError(error?.message || 'The listing could not be created.');
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className={styles.wrap} aria-labelledby="listings-title">
      <header className={styles.head}>
        <div>
          <h2 id="listings-title">Listings</h2>
          <p>
            Property records this workspace owns. Distinct from Houses, which
            browses public parcel data the workspace has not listed.
          </p>
        </div>
        <div className={styles.headActions}>
          <button type="button" onClick={() => load()} disabled={loading}>
            <RefreshCw aria-hidden="true" />
            <span>Refresh</span>
          </button>
          <button
            type="button"
            className={styles.primary}
            onClick={() => setOpen((value) => !value)}
            aria-expanded={open}
          >
            <Plus aria-hidden="true" />
            <span>New listing</span>
          </button>
        </div>
      </header>

      {open && (
        <form className={styles.form} onSubmit={submit}>
          <label>
            <span>Address</span>
            <input
              value={form.address}
              onChange={set('address')}
              placeholder="15 Main St, Dover DE"
              required
              minLength={3}
              maxLength={300}
            />
          </label>

          <div className={styles.row}>
            <label>
              <span>Price</span>
              <input type="number" min="0" step="1000" value={form.price} onChange={set('price')} />
            </label>
            <label>
              <span>Status</span>
              <select value={form.status} onChange={set('status')}>
                {STATUSES.map((status) => <option key={status} value={status}>{status}</option>)}
              </select>
            </label>
          </div>

          <fieldset className={styles.specsFieldset}>
            <legend>
              Property specs
              <small>
                Saved to a companion property record. Leave all three blank and
                no record is created, which valuation tools read as unknown.
              </small>
            </legend>
            <div className={styles.row}>
              <label>
                <span>Beds</span>
                <input type="number" min="0" max="200" value={form.beds} onChange={set('beds')} />
              </label>
              <label>
                <span>Baths</span>
                <input type="number" min="0" max="99" step="0.5" value={form.baths} onChange={set('baths')} />
              </label>
              <label>
                <span>Sqft</span>
                <input type="number" min="0" value={form.sqft} onChange={set('sqft')} />
              </label>
            </div>
          </fieldset>

          <fieldset className={styles.sellerFieldset}>
            <legend>Seller</legend>
            <div className={styles.modes} role="radiogroup" aria-label="Seller">
              {[
                ['none', 'No seller yet'],
                ['existing', 'Existing client'],
                ['new', 'New contact'],
              ].map(([mode, label]) => (
                <label key={mode} className={styles.mode}>
                  <input
                    type="radio"
                    name="sellerMode"
                    value={mode}
                    checked={form.sellerMode === mode}
                    onChange={set('sellerMode')}
                  />
                  <span>{label}</span>
                </label>
              ))}
            </div>

            {form.sellerMode === 'existing' && (
              <label>
                <span>Client id</span>
                <input
                  value={form.seller_client_id}
                  onChange={set('seller_client_id')}
                  placeholder="UUID of an existing client"
                />
              </label>
            )}

            {form.sellerMode === 'new' && (
              <div className={styles.row}>
                <label>
                  <span>Full name</span>
                  <input value={form.full_name} onChange={set('full_name')} maxLength={160} />
                </label>
                <label>
                  <span>Email</span>
                  <input type="email" value={form.email} onChange={set('email')} maxLength={254} />
                </label>
                <label>
                  <span>Phone</span>
                  <input value={form.phone} onChange={set('phone')} maxLength={40} />
                </label>
              </div>
            )}
          </fieldset>

          {saveError && <p className={styles.error} role="alert">{saveError}</p>}

          <div className={styles.formActions}>
            <button type="button" onClick={() => { setOpen(false); setSaveError(''); }}>
              Cancel
            </button>
            <button type="submit" className={styles.primary} disabled={!canSave || saving}>
              {saving ? <Loader2 className={styles.spin} aria-hidden="true" /> : <UserPlus aria-hidden="true" />}
              <span>{saving ? 'Creating…' : 'Create listing'}</span>
            </button>
          </div>
        </form>
      )}

      {loading && <p className={styles.muted}>Loading listings…</p>}

      {!loading && loadError && (
        <p className={styles.error} role="alert">
          {loadError} This is a failure to load, not a finding that you have no listings.
        </p>
      )}

      {!loading && !loadError && listings.length === 0 && (
        <p className={styles.muted}>
          <House aria-hidden="true" /> No listings in this workspace yet.
        </p>
      )}

      {listings.length > 0 && (
        <div className={styles.grid}>
          {listings.map((listing) => <ListingCard key={listing.id} listing={listing} />)}
        </div>
      )}
    </section>
  );
}
