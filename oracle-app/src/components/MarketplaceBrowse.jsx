import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  FileCheck2, Gavel, Loader2, RefreshCw, Search, ShieldCheck, Store, UserPlus, Users,
} from 'lucide-react';
import { crmGet, crmPost, crmPut } from '../state/useCrmApi';
import styles from './MarketplaceBrowse.module.css';

/**
 * MarketplaceBrowse — the disposition surface for properties already under a
 * signed contract, plus objective buyer matching against them.
 *
 * Eight backend routes existed with no UI at all. Three properties of that API
 * are deliberately visible here rather than smoothed over, because each one is
 * a claim the platform refuses to make loosely:
 *
 *  - **Nothing here ever sends.** Drafting a bidding message returns
 *    `send_state: "not_sent"`, and approving it returns
 *    `"approved_not_sent"` — an approved draft is still not a delivered
 *    message, and the UI says so at both stages.
 *  - **Competition claims are evidence-gated.** The server 422s a message
 *    mentioning "multiple offers" unless at least two offers are actually
 *    recorded. That rejection is surfaced verbatim, not retried or softened.
 *  - **Match scores carry their reasoning.** `criteria_trace` explains why a
 *    buyer scored what they scored; a bare number would invite trusting it.
 *
 * Three views, because the domain has three distinct jobs: browse the shared
 * marketplace, publish your own inventory into it, and maintain the buyer
 * profiles that matching runs against. `Browse` reads the platform-wide
 * published set; `My listings` reads this tenant's own rows in every state
 * (drafts included) — those are deliberately different endpoints, since a
 * draft must never cross the tenant wall.
 */

const currency = (v) =>
  v === null || v === undefined || Number.isNaN(Number(v))
    ? '—'
    : `$${Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 })}`;

const PUBLICATION_STATE = {
  draft: { label: 'Draft', tone: 'neutral' },
  published: { label: 'Published', tone: 'good' },
  under_offer: { label: 'Under offer', tone: 'warn' },
};

const VIEWS = [
  { id: 'browse', label: 'Browse', Icon: Store },
  { id: 'listings', label: 'My listings', Icon: FileCheck2 },
  { id: 'buyers', label: 'Buyers', Icon: Users },
];

// Verification is the buyer's, not ours to assert — show exactly what the
// profile claimed and nothing stronger.
const VERIFICATION = {
  funds_verified: { label: 'Funds verified', tone: 'good' },
  identity_verified: { label: 'Identity verified', tone: 'warn' },
  unverified: { label: 'Unverified', tone: 'neutral' },
};

function StateBadge({ state }) {
  const meta = PUBLICATION_STATE[state] || { label: state || 'Draft', tone: 'neutral' };
  return <span className={styles.badge} data-tone={meta.tone}>{meta.label}</span>;
}

/**
 * My listings — this tenant's own publications in every state, and the two
 * write routes that move a property into the marketplace.
 *
 * Publishing is approval-gated on the server: `from-contract` creates the
 * draft AND a FINANCIAL-risk approval in one call, and `publish` decides that
 * approval. The reason text is not decoration — `decide_approval` requires at
 * least 8 characters and it lands in the audit ledger.
 */
function MyListings() {
  const [publications, setPublications] = useState(null);
  const [contracts, setContracts] = useState(null);
  const [error, setError] = useState('');
  const [refreshKey, setRefreshKey] = useState(0);
  const [busyId, setBusyId] = useState(null);
  const [notice, setNotice] = useState(null);
  const [publishReason, setPublishReason] = useState({});

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      crmGet('/api/marketplace/publications?limit=100').then(
        (p) => p?.publications ?? [],
        (reason) => { throw reason; },
      ),
      // Contracts are the source of a publication, so the eligible ones are
      // listed alongside. A failure here is not fatal to the view — the
      // existing publications still render.
      crmGet('/api/contracts/documents?limit=100').then(
        (p) => p?.documents ?? [],
        () => [],
      ),
    ]).then(
      ([pubs, docs]) => {
        if (cancelled) return;
        setPublications(pubs);
        setContracts(docs);
        setError('');
      },
      (reason) => {
        if (cancelled) return;
        setError(
          reason?.status === 404
            ? 'The marketplace is not enabled for this deployment.'
            : reason?.message || 'Could not load your listings.',
        );
        setPublications((current) => current ?? []);
        setContracts((current) => current ?? []);
      },
    );
    return () => { cancelled = true; };
  }, [refreshKey]);

  // Only a SIGNED assignment or seller-purchase contract linked to a lead can
  // become a publication — the server enforces all three and 409s otherwise,
  // so filtering here keeps the UI from offering a button that cannot work.
  const eligibleContracts = useMemo(() => {
    const publishedContractIds = new Set(
      (publications || []).map((p) => p.contract_document_id).filter(Boolean),
    );
    return (contracts || []).filter(
      (doc) => doc.status === 'signed'
        && ['assignment', 'seller_purchase'].includes(doc.document_type)
        && doc.lead_id
        && !publishedContractIds.has(doc.id),
    );
  }, [contracts, publications]);

  const createFromContract = useCallback(async (contractId) => {
    setBusyId(contractId);
    setNotice(null);
    try {
      const result = await crmPost(
        `/api/marketplace/publications/from-contract/${contractId}`,
        { visibility: 'platform' },
      );
      setNotice({
        tone: 'ok',
        text: `Draft created — not yet visible to other brokerages. `
          + `Approval ${result?.approval?.id ? 'pending' : 'created'}; publish below to make it live.`,
      });
      setRefreshKey((k) => k + 1);
    } catch (err) {
      setNotice({ tone: 'error', text: err?.message || 'Could not create the publication.' });
    } finally {
      setBusyId(null);
    }
  }, []);

  const publish = useCallback(async (publicationId) => {
    const reason = (publishReason[publicationId] || '').trim();
    if (reason.length < 8) return;
    setBusyId(publicationId);
    setNotice(null);
    try {
      await crmPost(`/api/marketplace/publications/${publicationId}/publish`, { reason });
      setNotice({ tone: 'ok', text: 'Published — now visible to other brokerages.' });
      setPublishReason((current) => ({ ...current, [publicationId]: '' }));
      setRefreshKey((k) => k + 1);
    } catch (err) {
      setNotice({ tone: 'error', text: err?.message || 'Publish failed.' });
    } finally {
      setBusyId(null);
    }
  }, [publishReason]);

  return (
    <div className={styles.viewBody}>
      {error ? <p className={styles.error} role="alert">{error}</p> : null}
      {notice ? (
        <p className={notice.tone === 'error' ? styles.error : styles.sendState} role="status">
          {notice.text}
        </p>
      ) : null}

      <div className={styles.section}>
        <div className={styles.sectionHead}><span>Your publications</span></div>
        {publications === null ? (
          <p className={styles.muted}><Loader2 className={styles.spin} aria-hidden="true" /> Loading…</p>
        ) : publications.length === 0 ? (
          <p className={styles.muted}>No publications yet.</p>
        ) : (
          <ul className={styles.list}>
            {publications.map((publication) => (
              <li key={publication.id}>
                <div className={styles.listRow}>
                  <span className={styles.listHead}>
                    <span className={styles.address}>
                      {publication.truthful_summary?.address || 'Address withheld'}
                    </span>
                    <StateBadge state={publication.state} />
                  </span>
                  <span className={styles.listMeta}>
                    <span>{publication.truthful_summary?.state || '—'}</span>
                    <span>{currency(publication.asking_price)}</span>
                  </span>
                  {publication.state === 'draft' ? (
                    <div className={styles.publishRow}>
                      <input
                        type="text"
                        placeholder="Reason for the audit trail (min 8 chars)"
                        value={publishReason[publication.id] || ''}
                        onChange={(e) => setPublishReason((current) => ({
                          ...current, [publication.id]: e.target.value,
                        }))}
                      />
                      <button
                        type="button"
                        className={styles.primary}
                        onClick={() => publish(publication.id)}
                        disabled={
                          busyId === publication.id
                          || (publishReason[publication.id] || '').trim().length < 8
                        }
                      >
                        {busyId === publication.id
                          ? <Loader2 className={styles.spin} aria-hidden="true" /> : null}
                        Publish
                      </button>
                    </div>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className={styles.section}>
        <div className={styles.sectionHead}><span>Ready to publish</span></div>
        <p className={styles.muted}>
          Only a signed assignment or seller-purchase contract linked to a
          property can become a publication.
        </p>
        {contracts === null ? (
          <p className={styles.muted}><Loader2 className={styles.spin} aria-hidden="true" /> Loading…</p>
        ) : eligibleContracts.length === 0 ? (
          <p className={styles.muted}>No eligible signed contracts.</p>
        ) : (
          <ul className={styles.list}>
            {eligibleContracts.map((doc) => (
              <li key={doc.id}>
                <div className={styles.listRow}>
                  <span className={styles.listHead}>
                    <span className={styles.address}>{doc.title || doc.document_type}</span>
                    <span className={styles.badge} data-tone="good">Signed</span>
                  </span>
                  <div className={styles.actions}>
                    <button
                      type="button"
                      onClick={() => createFromContract(doc.id)}
                      disabled={busyId === doc.id}
                    >
                      {busyId === doc.id ? <Loader2 className={styles.spin} aria-hidden="true" /> : null}
                      Create draft publication
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/**
 * Buyers — profiles and the active requests that make them eligible to match.
 *
 * A profile without an active request participates in no matching at all, so
 * the count is shown on every row: "matched nothing" and "was never in the
 * running" are different answers and the agent needs to tell them apart.
 */
function BuyersView() {
  const [profiles, setProfiles] = useState(null);
  const [clients, setClients] = useState([]);
  const [error, setError] = useState('');
  const [refreshKey, setRefreshKey] = useState(0);
  const [notice, setNotice] = useState(null);
  const [saving, setSaving] = useState(false);

  const [form, setForm] = useState({ client_id: '', states: '', min_price: '', max_price: '' });
  const [requestFor, setRequestFor] = useState(null);
  const [requestName, setRequestName] = useState('');

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      crmGet('/api/marketplace/buyers/profiles?limit=100').then(
        (p) => p?.profiles ?? [],
        (reason) => { throw reason; },
      ),
      // Only buyer-type clients can hold a profile — the server 409s on
      // anything else, so the picker offers only what can succeed.
      crmGet('/api/crm/clients?type=all&sort=recent').then(
        (p) => (p?.clients ?? []).filter((c) => ['buyer', 'both'].includes(c.client_type)),
        () => [],
      ),
    ]).then(
      ([loadedProfiles, loadedClients]) => {
        if (cancelled) return;
        setProfiles(loadedProfiles);
        setClients(loadedClients);
        setError('');
      },
      (reason) => {
        if (cancelled) return;
        setError(
          reason?.status === 404
            ? 'The marketplace is not enabled for this deployment.'
            : reason?.message || 'Could not load buyer profiles.',
        );
        setProfiles((current) => current ?? []);
      },
    );
    return () => { cancelled = true; };
  }, [refreshKey]);

  const saveProfile = useCallback(async () => {
    if (!form.client_id) return;
    setSaving(true);
    setNotice(null);
    try {
      await crmPut('/api/marketplace/buyers/profile', {
        client_id: form.client_id,
        states: form.states
          .split(',').map((s) => s.trim().toUpperCase()).filter((s) => s.length === 2),
        counties: [],
        property_types: [],
        strategies: [],
        min_price: form.min_price ? Number(form.min_price) : null,
        max_price: form.max_price ? Number(form.max_price) : null,
        // Verification is the buyer's status to earn, never something this
        // form asserts on their behalf — it stays at the server default.
        verification_status: 'unverified',
        acquisition_history_verified: false,
        explicit_preferences: {},
      });
      setNotice({ tone: 'ok', text: 'Buyer profile saved.' });
      setForm({ client_id: '', states: '', min_price: '', max_price: '' });
      setRefreshKey((k) => k + 1);
    } catch (err) {
      setNotice({ tone: 'error', text: err?.message || 'Could not save the profile.' });
    } finally {
      setSaving(false);
    }
  }, [form]);

  const createRequest = useCallback(async (profile) => {
    if (!requestName.trim()) return;
    setSaving(true);
    setNotice(null);
    try {
      await crmPost('/api/marketplace/buyers/requests', {
        buyer_profile_id: profile.id,
        request_name: requestName.trim(),
        // The criteria the profile already carries — the request names a
        // search, it does not invent constraints the buyer never gave.
        criteria: {
          states: profile.states || [],
          min_price: profile.min_price ?? null,
          max_price: profile.max_price ?? null,
        },
      });
      setNotice({ tone: 'ok', text: 'Buyer request created — now eligible for matching.' });
      setRequestFor(null);
      setRequestName('');
      setRefreshKey((k) => k + 1);
    } catch (err) {
      setNotice({ tone: 'error', text: err?.message || 'Could not create the request.' });
    } finally {
      setSaving(false);
    }
  }, [requestName]);

  return (
    <div className={styles.viewBody}>
      {error ? <p className={styles.error} role="alert">{error}</p> : null}
      {notice ? (
        <p className={notice.tone === 'error' ? styles.error : styles.sendState} role="status">
          {notice.text}
        </p>
      ) : null}

      <div className={styles.section}>
        <div className={styles.sectionHead}><span><UserPlus aria-hidden="true" /> Add or update a buyer profile</span></div>
        {clients.length === 0 ? (
          <p className={styles.muted}>
            No buyer-type clients yet — a profile can only attach to a client marked
            as a buyer.
          </p>
        ) : (
          <div className={styles.form}>
            <label className={styles.field}>
              <span className={styles.srOnly}>Buyer client</span>
              <select
                value={form.client_id}
                onChange={(e) => setForm((f) => ({ ...f, client_id: e.target.value }))}
              >
                <option value="">Select a buyer client…</option>
                {clients.map((client) => (
                  <option key={client.id} value={client.id}>{client.full_name}</option>
                ))}
              </select>
            </label>
            <input
              type="text"
              placeholder="States (e.g. DE, MD)"
              value={form.states}
              onChange={(e) => setForm((f) => ({ ...f, states: e.target.value }))}
            />
            <input
              type="number"
              min="0"
              placeholder="Min price"
              value={form.min_price}
              onChange={(e) => setForm((f) => ({ ...f, min_price: e.target.value }))}
            />
            <input
              type="number"
              min="0"
              placeholder="Max price"
              value={form.max_price}
              onChange={(e) => setForm((f) => ({ ...f, max_price: e.target.value }))}
            />
            <button
              type="button"
              className={styles.primary}
              onClick={saveProfile}
              disabled={!form.client_id || saving}
            >
              {saving ? <Loader2 className={styles.spin} aria-hidden="true" /> : null}
              Save profile
            </button>
          </div>
        )}
      </div>

      <div className={styles.section}>
        <div className={styles.sectionHead}><span>Buyer profiles</span></div>
        {profiles === null ? (
          <p className={styles.muted}><Loader2 className={styles.spin} aria-hidden="true" /> Loading…</p>
        ) : profiles.length === 0 ? (
          <p className={styles.muted}>No buyer profiles yet.</p>
        ) : (
          <ul className={styles.list}>
            {profiles.map((profile) => {
              const verification = VERIFICATION[profile.verification_status] || VERIFICATION.unverified;
              const active = Number(profile.active_request_count) || 0;
              return (
                <li key={profile.id}>
                  <div className={styles.listRow}>
                    <span className={styles.listHead}>
                      <span className={styles.address}>{profile.client_name || 'Unnamed buyer'}</span>
                      <span className={styles.badge} data-tone={verification.tone}>
                        {verification.label}
                      </span>
                    </span>
                    <span className={styles.listMeta}>
                      <span>{(profile.states || []).join(', ') || 'Any state'}</span>
                      <span>{currency(profile.min_price)} – {currency(profile.max_price)}</span>
                    </span>
                    {/* Zero active requests means this buyer is not in the
                        running at all — distinct from matching and scoring low. */}
                    <span className={styles.listMeta}>
                      <span className={active === 0 ? styles.warnText : undefined}>
                        {active === 0
                          ? 'No active request — not eligible for matching'
                          : `${active} active request${active === 1 ? '' : 's'}`}
                      </span>
                    </span>
                    {requestFor === profile.id ? (
                      <div className={styles.publishRow}>
                        <input
                          type="text"
                          placeholder="Request name"
                          value={requestName}
                          onChange={(e) => setRequestName(e.target.value)}
                        />
                        <button
                          type="button"
                          className={styles.primary}
                          onClick={() => createRequest(profile)}
                          disabled={!requestName.trim() || saving}
                        >
                          Create
                        </button>
                      </div>
                    ) : (
                      <div className={styles.actions}>
                        <button type="button" onClick={() => setRequestFor(profile.id)}>
                          Add request
                        </button>
                      </div>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}

export default function MarketplaceBrowse() {
  const [view, setView] = useState('browse');
  const [stateFilter, setStateFilter] = useState('');
  const [publications, setPublications] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);

  const [selected, setSelected] = useState(null);
  const [matches, setMatches] = useState(null);
  const [matching, setMatching] = useState(false);
  const [matchError, setMatchError] = useState('');

  const [draft, setDraft] = useState('');
  const [channel, setChannel] = useState('email');
  const [drafting, setDrafting] = useState(false);
  const [pendingApproval, setPendingApproval] = useState(null);
  const [draftError, setDraftError] = useState('');
  const [approving, setApproving] = useState(false);
  const [sendState, setSendState] = useState(null);

  const requestPath = useMemo(() => {
    const params = new URLSearchParams({ limit: '100' });
    if (stateFilter.trim()) params.set('state_code', stateFilter.trim().toUpperCase());
    return `/api/marketplace?${params.toString()}`;
  }, [stateFilter]);

  useEffect(() => {
    let cancelled = false;
    // Mirrors an external request lifecycle, not derived render data — same
    // reasoning (and same disable) as HouseSelection's catalog fetch.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setRefreshing(true);
    crmGet(requestPath).then(
      (payload) => {
        if (cancelled) return;
        setPublications(payload?.publications ?? []);
        setLoadError('');
        setRefreshing(false);
      },
      (reason) => {
        if (cancelled) return;
        // 404 is the feature gate, not a failure — say which it is.
        setLoadError(
          reason?.status === 404
            ? 'The marketplace is not enabled for this deployment.'
            : reason?.message || 'Could not load the marketplace.',
        );
        setPublications((current) => current ?? []);
        setRefreshing(false);
      },
    );
    return () => { cancelled = true; };
  }, [requestPath, refreshKey]);

  const selectPublication = useCallback((publication) => {
    setSelected(publication);
    setMatches(null);
    setMatchError('');
    setDraft('');
    setPendingApproval(null);
    setDraftError('');
    setSendState(null);
  }, []);

  const runMatch = useCallback(async () => {
    if (!selected) return;
    setMatching(true);
    setMatchError('');
    try {
      const result = await crmPost(`/api/marketplace/publications/${selected.id}/match`);
      setMatches(result?.matches ?? []);
    } catch (err) {
      setMatchError(err?.message || 'Buyer matching failed.');
    } finally {
      setMatching(false);
    }
  }, [selected]);

  const submitDraft = useCallback(async () => {
    if (!selected || !draft.trim()) return;
    setDrafting(true);
    setDraftError('');
    try {
      const result = await crmPost(
        `/api/marketplace/publications/${selected.id}/bidding-message`,
        { message: draft.trim(), channel },
      );
      setPendingApproval(result?.approval ?? null);
      setSendState(result?.send_state ?? 'not_sent');
    } catch (err) {
      // A 422 here is the competition-claim guard: the message asserted
      // competing offers the record cannot support. Show the server's own
      // reason rather than a generic failure — the agent needs to know it is
      // the CLAIM that was refused, not the request that broke.
      setDraftError(err?.message || 'Could not draft this message.');
    } finally {
      setDrafting(false);
    }
  }, [selected, draft, channel]);

  const approveDraft = useCallback(async () => {
    if (!pendingApproval) return;
    setApproving(true);
    setDraftError('');
    try {
      const result = await crmPost(
        `/api/marketplace/bidding-messages/${pendingApproval.id}/approve`,
        { reason: 'Reviewed and approved for outreach.' },
      );
      setSendState(result?.send_state ?? 'approved_not_sent');
      setPendingApproval(null);
    } catch (err) {
      setDraftError(err?.message || 'Approval failed.');
    } finally {
      setApproving(false);
    }
  }, [pendingApproval]);

  return (
    <section className={styles.wrap} aria-labelledby="marketplace-title">
      <header className={styles.head}>
        <div>
          <span className={styles.kicker}>Disposition</span>
          <h2 id="marketplace-title">Marketplace</h2>
          <p className={styles.sub}>
            Properties under a signed contract, offered to matched buyers.
          </p>
        </div>
        <div className={styles.headActions}>
          {view === 'browse' ? (
            <>
              <label className={styles.filter}>
                <Search aria-hidden="true" />
                <span className={styles.srOnly}>Filter by state</span>
                <input
                  type="text"
                  maxLength={2}
                  placeholder="ST"
                  value={stateFilter}
                  onChange={(e) => setStateFilter(e.target.value)}
                />
              </label>
              <button
                type="button"
                className={styles.iconButton}
                onClick={() => setRefreshKey((k) => k + 1)}
                disabled={refreshing}
                aria-label="Refresh marketplace"
              >
                <RefreshCw aria-hidden="true" />
              </button>
            </>
          ) : null}
        </div>
      </header>

      <nav className={styles.switcher} aria-label="Marketplace view">
        {VIEWS.map(({ id, label, Icon }) => (
          <button
            key={id}
            type="button"
            aria-pressed={view === id}
            onClick={() => setView(id)}
          >
            <Icon aria-hidden="true" />
            <span>{label}</span>
          </button>
        ))}
      </nav>

      {view === 'listings' ? <MyListings /> : null}
      {view === 'buyers' ? <BuyersView /> : null}
      {view !== 'browse' ? null : (
      <>
      {loadError ? <p className={styles.error} role="alert">{loadError}</p> : null}

      <div className={styles.layout}>
        <div className={styles.listPane}>
          {publications === null ? (
            <p className={styles.muted}><Loader2 className={styles.spin} aria-hidden="true" /> Loading…</p>
          ) : publications.length === 0 ? (
            <div className={styles.empty}>
              <strong>No published properties.</strong>
              <span>
                A publication is created from a signed assignment or seller
                contract — nothing appears here until a contract is executed.
              </span>
            </div>
          ) : (
            <ul className={styles.list}>
              {publications.map((publication) => {
                const summary = publication.truthful_summary || {};
                return (
                  <li key={publication.id}>
                    <button
                      type="button"
                      onClick={() => selectPublication(publication)}
                      aria-pressed={selected?.id === publication.id}
                    >
                      <span className={styles.listHead}>
                        <span className={styles.address}>{summary.address || 'Address withheld'}</span>
                        <StateBadge state={publication.state} />
                      </span>
                      <span className={styles.listMeta}>
                        <span>{summary.state || '—'}</span>
                        <span>{currency(publication.asking_price)}</span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <aside className={styles.detailPane} aria-label="Publication detail">
          {!selected ? (
            <p className={styles.muted}>Select a property to match buyers and draft outreach.</p>
          ) : (
            <>
              <div className={styles.detailHead}>
                <h3>{selected.truthful_summary?.address || 'Address withheld'}</h3>
                <StateBadge state={selected.state} />
              </div>

              <dl className={styles.facts}>
                <div><dt>Asking</dt><dd>{currency(selected.asking_price)}</dd></div>
                <div><dt>ARV</dt><dd>{currency(selected.truthful_summary?.arv)}</dd></div>
                <div><dt>Rehab</dt><dd>{currency(selected.truthful_summary?.rehab)}</dd></div>
                <div><dt>Beds / baths</dt><dd>
                  {selected.truthful_summary?.beds ?? '—'} / {selected.truthful_summary?.baths ?? '—'}
                </dd></div>
              </dl>

              {/* ── buyer matching ── */}
              <div className={styles.section}>
                <div className={styles.sectionHead}>
                  <span><Users aria-hidden="true" /> Matched buyers</span>
                  <button type="button" onClick={runMatch} disabled={matching}>
                    {matching ? <Loader2 className={styles.spin} aria-hidden="true" /> : null}
                    {matching ? 'Matching…' : 'Run match'}
                  </button>
                </div>
                {matchError ? <p className={styles.error} role="alert">{matchError}</p> : null}
                {matches === null ? null : matches.length === 0 ? (
                  <p className={styles.muted}>No active buyer requests matched this property.</p>
                ) : (
                  <ul className={styles.matches}>
                    {matches.map((match) => {
                      const verification = VERIFICATION[match.verification_status]
                        || VERIFICATION.unverified;
                      return (
                        <li key={match.id}>
                          <div className={styles.matchHead}>
                            <span className={styles.score}>
                              {Math.round(Number(match.match_score) * 100) / 100}
                            </span>
                            <span className={styles.badge} data-tone={verification.tone}>
                              {verification.label}
                            </span>
                            {match.acquisition_history_verified ? (
                              <span className={styles.badge} data-tone="good">
                                <ShieldCheck aria-hidden="true" /> History verified
                              </span>
                            ) : null}
                          </div>
                          {/* The trace is why the score is what it is. Without
                              it a number invites being trusted on its own. */}
                          {match.criteria_trace ? (
                            <ul className={styles.trace}>
                              {Object.entries(match.criteria_trace).map(([key, value]) => (
                                <li key={key}>
                                  <span>{key.replace(/_/g, ' ')}</span>
                                  <span>{typeof value === 'boolean' ? (value ? 'met' : 'not met') : String(value)}</span>
                                </li>
                              ))}
                            </ul>
                          ) : null}
                        </li>
                      );
                    })}
                  </ul>
                )}
              </div>

              {/* ── bidding message ── */}
              <div className={styles.section}>
                <div className={styles.sectionHead}>
                  <span><Gavel aria-hidden="true" /> Bidding message</span>
                </div>
                <p className={styles.muted}>
                  Drafts are approved here and sent elsewhere — approving does not
                  deliver anything.
                </p>
                <label className={styles.field}>
                  <span className={styles.srOnly}>Channel</span>
                  <select value={channel} onChange={(e) => setChannel(e.target.value)}>
                    <option value="email">Email</option>
                    <option value="sms">SMS</option>
                  </select>
                </label>
                <textarea
                  className={styles.textarea}
                  rows={4}
                  placeholder="Message to matched buyers…"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                />
                {draftError ? <p className={styles.error} role="alert">{draftError}</p> : null}
                {sendState ? (
                  <p className={styles.sendState} role="status">
                    {sendState === 'approved_not_sent'
                      ? 'Approved — not sent. Create an approval-bound email or SMS command to deliver it.'
                      : 'Drafted — not sent. Awaiting approval.'}
                  </p>
                ) : null}
                <div className={styles.actions}>
                  <button
                    type="button"
                    onClick={submitDraft}
                    disabled={!draft.trim() || drafting}
                  >
                    {drafting ? <Loader2 className={styles.spin} aria-hidden="true" /> : null}
                    {drafting ? 'Checking…' : 'Draft for approval'}
                  </button>
                  {pendingApproval ? (
                    <button
                      type="button"
                      className={styles.primary}
                      onClick={approveDraft}
                      disabled={approving}
                    >
                      {approving ? <Loader2 className={styles.spin} aria-hidden="true" /> : null}
                      {approving ? 'Approving…' : 'Approve draft'}
                    </button>
                  ) : null}
                </div>
              </div>
            </>
          )}
        </aside>
      </div>
      </>
      )}
    </section>
  );
}
