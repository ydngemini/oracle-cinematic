import { useCallback, useEffect, useRef, useState } from 'react';
import styles from './SecureDossierPage.module.css';

/**
 * SecureDossierPage — the page a HOMEOWNER lands on from a portal link.
 *
 * The backend for this has existed since 0008 and nothing has ever served it.
 * `POST /portal/links` mints a URL, the agent copies it out of the client
 * drawer and sends it to their seller, and until now that URL resolved to the
 * agent application — so every dossier link ever issued was a dead link.
 *
 * It is also the perception producer. Everything the intent model wants to know
 * about a seller's engagement happens on this page, and none of it was being
 * recorded because the page did not exist.
 *
 * FOUR DECISIONS WORTH DEFENDING:
 *
 * 1. **The session token is never persisted.** It stays in a ref for the life
 *    of the tab. A capability URL gets forwarded, opened on shared machines and
 *    left in browser history; writing the minted bearer to localStorage would
 *    outlive the tab and survive the link being revoked. The cost is one extra
 *    resolve on refresh, which is the correct trade.
 *
 * 2. **Telemetry can never break the page.** Every emit is fire-and-forget with
 *    a swallowed failure. A homeowner came here to read their own property
 *    record; an analytics outage must not be something they can perceive.
 *
 * 3. **Only scoped sections render.** The dossier returns exactly what the
 *    agent granted. The page does not ask for more and does not hint at what is
 *    missing — an absent section should look like an absent section, not like
 *    something withheld.
 *
 * 4. **The watermark is part of the document, not decoration.** It is issued
 *    per link and states that access is revocable. It stays on screen while
 *    content is on screen.
 */

const API_BASE = import.meta.env.VITE_API_BASE
  || (import.meta.env.DEV ? 'http://localhost:8000' : '');

function tokenFromPath() {
  // /vault/secure-access/:token
  const parts = window.location.pathname.split('/').filter(Boolean);
  return parts[2] ? decodeURIComponent(parts[2]) : '';
}

function money(value) {
  if (value == null) return null;
  return `$${Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function humanize(text) {
  return String(text || '').replace(/_/g, ' ');
}

function Section({ title, children }) {
  return (
    <section className={styles.section}>
      <h2 className={styles.sectionTitle}>{title}</h2>
      {children}
    </section>
  );
}

export default function SecureDossierPage() {
  const [token] = useState(tokenFromPath);
  // A missing token is knowable at first render, so it is initial state rather
  // than something an effect sets — the lint rule this satisfies exists because
  // the effect version flashes a valid-looking page first.
  const [state, setState] = useState(() => (tokenFromPath() ? 'checking' : 'invalid'));
  const [error, setError] = useState(
    () => (tokenFromPath() ? null : 'This link is missing its access code.'),
  );
  const [dossier, setDossier] = useState(null);
  const sessionRef = useRef(null);

  /** Report one homeowner action. Never throws, never blocks, never retries. */
  const emit = useCallback((event, payload) => {
    const session = sessionRef.current;
    if (!session) return;
    // keepalive so a click that also navigates away still records.
    fetch(`${API_BASE}/portal/activity`, {
      method: 'POST',
      keepalive: true,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session}`,
      },
      body: JSON.stringify({ event, payload: payload || {} }),
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (!token) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const sessionRes = await fetch(
          `${API_BASE}/portal/session/${encodeURIComponent(token)}`,
        );
        if (!sessionRes.ok) throw new Error('This link is no longer valid.');
        const session = await sessionRes.json();
        if (cancelled) return;
        sessionRef.current = session.session_token;

        const res = await fetch(`${API_BASE}/portal/dossier`, {
          headers: { Authorization: `Bearer ${session.session_token}` },
        });
        if (!res.ok) throw new Error('This dossier is no longer available.');
        const payload = await res.json();
        if (cancelled) return;
        setDossier(payload);
        setState('ready');
        // The open itself. The server collapses repeats within its own
        // cooldown, so a refresh loop cannot manufacture engagement here.
        emit('listing_view', { section: 'dossier' });
      } catch (err) {
        if (cancelled) return;
        setState('invalid');
        // Deliberately the same message for expired, revoked and never-existed.
        // The backend returns a uniform 404 for exactly this reason and the UI
        // must not reintroduce the oracle it was careful to avoid.
        setError(err?.message || 'This link is no longer valid.');
      }
    })();
    return () => { cancelled = true; };
  }, [token, emit]);

  if (state === 'checking') {
    return (
      <main className={styles.shell} aria-busy="true" aria-label="Opening your dossier">
        <div className={styles.skeleton} />
        <div className={styles.skeleton} />
      </main>
    );
  }

  if (state === 'invalid') {
    return (
      <main className={styles.shell}>
        <div className={styles.notice} role="alert">
          <h1 className={styles.noticeTitle}>This link is not available</h1>
          <p className={styles.noticeBody}>{error}</p>
          <p className={styles.noticeBody}>
            Dossier links expire and can be withdrawn at any time. Please ask
            your agent for a new one.
          </p>
        </div>
      </main>
    );
  }

  const assets = dossier?.assets || {};
  const summary = assets.summary;

  return (
    <main className={styles.shell}>
      <header className={styles.header}>
        <p className={styles.watermark}>{dossier?.watermark_text}</p>
        <h1 className={styles.address}>
          {summary?.address || 'Your property record'}
        </h1>
        {summary?.asking_price != null && (
          <p className={styles.price}>{money(summary.asking_price)}</p>
        )}
        <p className={styles.readonly}>
          Read-only. Prepared by your agent and updated as things change.
        </p>
      </header>

      {summary && (
        <Section title="Summary">
          <dl className={styles.facts}>
            {summary.address && (
              <div>
                <dt>Address</dt>
                <dd>
                  {summary.address}
                  <button
                    type="button"
                    className={styles.inlineLink}
                    onClick={() => emit('map_view', { section: 'summary' })}
                  >
                    View location
                  </button>
                </dd>
              </div>
            )}
            {summary.state && <div><dt>State</dt><dd>{summary.state}</dd></div>}
            {summary.dossier_status && (
              <div><dt>Status</dt><dd>{humanize(summary.dossier_status)}</dd></div>
            )}
            {summary.contract_expires_at && (
              <div>
                <dt>Contract expires</dt>
                <dd>{new Date(summary.contract_expires_at).toLocaleDateString()}</dd>
              </div>
            )}
          </dl>
        </Section>
      )}

      {Array.isArray(assets.media) && assets.media.length > 0 && (
        <Section title="Photos">
          <ul className={styles.media}>
            {assets.media.map((item) => (
              <li key={item.id}>
                <a
                  href={item.url}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => emit('link_click', { asset: item.kind, asset_id: item.id })}
                >
                  <img src={item.url} alt={item.caption || 'Property photo'} loading="lazy" />
                  {item.caption && <span className={styles.caption}>{item.caption}</span>}
                </a>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {Array.isArray(assets.milestones) && assets.milestones.length > 0 && (
        <Section title="What happens next">
          <ol className={styles.milestones}>
            {assets.milestones.map((m) => (
              <li key={m.id} className={m.completed_at ? styles.milestoneDone : ''}>
                <span className={styles.milestoneTitle}>{m.title || humanize(m.milestone_type)}</span>
                <span className={styles.milestoneMeta}>
                  {m.completed_at
                    ? `Done ${new Date(m.completed_at).toLocaleDateString()}`
                    : m.due_at
                      ? `Due ${new Date(m.due_at).toLocaleDateString()}`
                      : humanize(m.status)}
                </span>
              </li>
            ))}
          </ol>
        </Section>
      )}

      {Array.isArray(assets.documents) && assets.documents.length > 0 && (
        <Section title="Documents">
          <ul className={styles.documents}>
            {assets.documents.map((doc) => (
              <li key={doc.id}>
                <button
                  type="button"
                  className={styles.docRow}
                  onClick={() => emit('link_click', {
                    asset: doc.document_type, asset_id: doc.id,
                  })}
                >
                  <span>{humanize(doc.document_type)}</span>
                  <span className={styles.docStatus}>{humanize(doc.status)}</span>
                </button>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {Array.isArray(assets.title_summary) && assets.title_summary.length > 0 && (
        <Section title="Public records">
          <ul className={styles.findings}>
            {assets.title_summary.map((finding, i) => (
              <li key={`${finding.finding_type}-${i}`}>
                <span>{humanize(finding.finding_type)}</span>
                {finding.amount != null && <span>{money(finding.amount)}</span>}
              </li>
            ))}
          </ul>
          {/* The backend attaches this warning to every row; it belongs on
              screen, not in a payload the homeowner never sees. */}
          <p className={styles.caveat}>
            Preliminary public-record findings. Not an insured title search.
          </p>
        </Section>
      )}

      <footer className={styles.footer}>
        <p>{dossier?.watermark_text}</p>
      </footer>
    </main>
  );
}
