import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BriefcaseBusiness, RefreshCw, Search, UserRound, UsersRound } from 'lucide-react';
import { crmGet } from '../state/useCrmApi';
import { LivingStrip } from '../neoh/LivingObject';
import { composeLiving } from '../neoh/livingModel';
import { useCallPresence } from '../neoh/callPresence';
// POST /api/crm/contacts had no caller: People could list the contact book and
// never add to it, so every person had to arrive via import or an agent tool.
import ContactIntakePanel from './ContactIntakePanel';
// GET and PATCH on a contact both had no caller, so a contact could be created
// and listed and never opened — a wrong number was permanent.
import ContactDetailPanel from './ContactDetailPanel';
import { PanelDataStatus } from './PanelDataStatus';
import styles from './PeopleTab.module.css';
import { friendlyError } from '../lib/errorMessages';

const ClientCrmTab = lazy(() => import('./ClientCrmTab'));

function contactLabel(contact) {
  return contact.full_name || contact.email || contact.phone || 'Unnamed contact';
}

function contactChannel(contact) {
  const value = String(contact.preferred_channel || '').trim().toLowerCase();
  return !value || ['none', 'unknown', 'unset'].includes(value)
    ? 'Not set'
    : value.replaceAll('_', ' ');
}

function dataStateLabel(contact) {
  return contact.data_state === 'sealed' ? 'Secured' : 'Migrating';
}

const EMPTY_LIVING = Object.freeze({ key: '', living: Object.freeze({}) });

/**
 * Living state for a whole list in ONE request. The card should say what is
 * happening with this person before the row's words do; asking per row would
 * be a query per card, which is why the API takes a list.
 */
function useLivingForContacts(contacts) {
  const [answer, setAnswer] = useState(EMPTY_LIVING);
  const key = useMemo(() => Array.from(new Set(
    (contacts || []).map((c) => c.legacy_client_id).filter(Boolean),
  )).sort().join(','), [contacts]);
  useEffect(() => {
    if (!key) return undefined;
    let live = true;
    crmGet(`/api/living?client_ids=${encodeURIComponent(key)}`).then(
      (payload) => { if (live) setAnswer({ key, living: payload?.living || {} }); },
      // A person still reads fine without it; the row just says less.
      () => { if (live) setAnswer({ key, living: {} }); },
    );
    return () => { live = false; };
  }, [key]);
  // Stamped with the key it answered, so a reply that arrives after the list
  // changed is ignored rather than shown against the wrong people.
  return answer.key === key ? answer.living : EMPTY_LIVING.living;
}

/** One row's living line, with this tab's own call overlaid. */
function ContactLiving({ living, clientId, contactId }) {
  const presence = useCallPresence({ clientId, contactId });
  const composed = composeLiving(living, presence);
  if (!composed) return null;
  return <LivingStrip living={composed} compact />;
}

// The page loads contacts in pages; a search goes to the server, which looks
// through every contact (audit 33.3: the tab searched only the first 200).
export const CONTACT_PAGE = 100;
const SEARCH_DEBOUNCE_MS = 300;
const SEARCH_MAX_CHARS = 160;  // the API refuses a longer q

function matchesLocally(contact, needle) {
  return [contact.full_name, contact.email, contact.phone, contact.source]
    .some((value) => String(value || '').toLowerCase().includes(needle));
}

/** Server-side search of the whole contact book, newest answer only.
 * `generation` changes on every refresh, so an edit or a Refresh re-runs it. */
function useContactSearch(query, generation) {
  const [found, setFound] = useState({ key: '', contacts: [], more: false, error: null });
  const latest = useRef(0);
  const trimmed = query.trim().slice(0, SEARCH_MAX_CHARS);
  const key = `${generation}:${trimmed}`;
  useEffect(() => {
    if (!trimmed) return undefined;
    const request = latest.current + 1;
    latest.current = request;
    const timer = window.setTimeout(() => {
      crmGet(`/api/crm/contacts?q=${encodeURIComponent(trimmed)}&limit=${CONTACT_PAGE}`).then(
        (payload) => {
          if (latest.current !== request) return;  // a newer search is under way
          setFound({ key, contacts: Array.isArray(payload?.contacts) ? payload.contacts : [],
            more: Boolean(payload?.next_cursor), error: null });
        },
        (reason) => {
          if (latest.current !== request) return;
          setFound({ key, contacts: [], more: false, error: reason });
        },
      );
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [key, trimmed]);
  const current = found.key === key && trimmed !== '';
  return { results: current ? found.contacts : [], more: current && found.more, searched: current,
    error: current ? found.error : null };
}

function searchStatus(search, count, searching) {
  if (searching) return 'Searching all your contacts…';
  if (search.error) return `Full search unavailable; ${count} ${count === 1 ? 'match' : 'matches'} among loaded contacts.`;
  if (search.more) return `First ${count} matches. Keep typing to narrow the search.`;
  return `${count} ${count === 1 ? 'match' : 'matches'} across all your contacts.`;
}

function ContactList({ contacts, error, loading, refreshing, updatedAt, onRetry, onOpenOpportunities, openId, onOpen, onChanged,
  hasMore, loadingMore, loadMoreError, onLoadMore, generation }) {
  const [query, setQuery] = useState('');
  const search = useContactSearch(query, generation);
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return contacts || [];
    // Every contact the server found, plus partial email / phone matches
    // among the loaded ones (the server matches whole emails and phones).
    // A loaded copy is the freshest after an edit, so it replaces the found one.
    const loaded = new Map((contacts || []).map((c) => [c.id, c]));
    const seen = new Set();
    const merged = [];
    for (const contact of [...search.results, ...(contacts || []).filter((c) => matchesLocally(c, needle))]) {
      if (!seen.has(contact.id)) { seen.add(contact.id); merged.push(loaded.get(contact.id) || contact); }
    }
    return merged;
  }, [contacts, query, search.results]);
  // Living lines for the loaded contacts and the server's matches: these
  // change on a load or a search answer, not on every keystroke.
  const livingSet = useMemo(() => {
    const seen = new Set();
    return [...(contacts || []), ...search.results].filter((c) => !seen.has(c.id) && seen.add(c.id));
  }, [contacts, search.results]);
  const living = useLivingForContacts(livingSet);
  const searching = query.trim() !== '' && !search.searched;

  return (
    <section className={styles.contactBook} aria-labelledby="canonical-contacts-title" aria-busy={loading || refreshing}>
      <header className={styles.contactHead}>
        <div>
          <span className={styles.kicker}>People</span>
          <h2 id="canonical-contacts-title">Contacts</h2>
        </div>
        <ul className={styles.sourceStatus} aria-label="Contact source status">
          <PanelDataStatus
            label="Contact source"
            loading={loading}
            refreshing={refreshing}
            error={error}
            updatedAt={updatedAt}
            onRetry={onRetry}
          />
        </ul>
      </header>

      {!loading && !error && (contacts || []).length > 0 ? (
        <label className={styles.search}>
          <span className={styles.srOnly}>Search contacts</span>
          <Search aria-hidden="true" />
          <input type="search" value={query} maxLength={SEARCH_MAX_CHARS} aria-describedby="contact-search-hint"
            onChange={(event) => setQuery(event.target.value)} placeholder="Search name, email, phone…" />
        </label>
      ) : null}
      {!loading && !error && (contacts || []).length > 0 ? (
        <>
          <p id="contact-search-hint" className={styles.listNote}>
            Search finds any contact by name, full email or full phone; part of an email or phone matches loaded contacts.
          </p>
          <p className={styles.listNote} role="status">
            {query.trim()
              ? searchStatus(search, visible.length, searching)
              : `${(contacts || []).length} contacts loaded${hasMore ? ' — more below' : ''}`}
          </p>
        </>
      ) : null}

      {loading ? (
        <div className={styles.skeleton} aria-hidden="true"><span /><span /><span /></div>
      ) : error ? (
        <div className={styles.errorState} role="alert">
          <strong>Your contacts are temporarily unavailable</strong>
          <p>{friendlyError(error, { fallback: 'The contact source could not be loaded.' })}</p>
          <div><button type="button" onClick={onRetry}>Retry contacts</button><button type="button" onClick={onOpenOpportunities}>Open opportunities</button></div>
        </div>
      ) : (contacts || []).length === 0 ? (
        <div className={styles.empty} role="status">
          <UsersRound aria-hidden="true" />
          <div><strong>No contacts yet</strong><p>Add your first client with New contact, or ask Neoh to add someone for you.</p></div>
          <button type="button" onClick={onOpenOpportunities}>Open opportunities</button>
        </div>
      ) : visible.length === 0 && searching ? (
        <div className={styles.skeleton} aria-hidden="true"><span /></div>
      ) : visible.length === 0 ? (
        <div className={styles.empty} role="status">
          <Search aria-hidden="true" />
          <div><strong>No matches</strong><p>No contact matches this search.</p></div>
          <button type="button" onClick={() => setQuery('')}>Clear search</button>
        </div>
      ) : (
        <ul className={styles.contacts}>
          {visible.map((contact) => (
            <li key={contact.id}>
              <span className={styles.avatar} aria-hidden="true"><UserRound /></span>
              <div className={styles.identity}>
                <strong>{contactLabel(contact)}</strong>
                <ContactLiving
                  living={living[contact.legacy_client_id]}
                  clientId={contact.legacy_client_id}
                  contactId={contact.id}
                />
                <small>{contact.email || 'Email not provided'}{contact.phone ? ` · ${contact.phone}` : ''}</small>
              </div>
              <div className={styles.contactMeta}>
                <span>{contactChannel(contact)}</span>
                <small>{contact.timezone || 'Timezone not set'}</small>
              </div>
              <span className={styles.security} data-state={contact.data_state}>{dataStateLabel(contact)}</span>
              <button
                type="button"
                onClick={() => onOpen(openId === contact.id ? '' : contact.id)}
                aria-expanded={openId === contact.id}
              >
                {openId === contact.id ? 'Close' : 'Open'}
              </button>
              {contact.legacy_client_id ? <button type="button" onClick={onOpenOpportunities}>Opportunity</button> : null}
              {openId === contact.id ? (
                <ContactDetailPanel
                  contactId={contact.id}
                  onClose={() => onOpen('')}
                  onChanged={onChanged}
                />
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {!loading && !error && !query.trim() && loadMoreError ? (
        <p className={styles.listNote} role="alert">The next contacts could not be loaded. Try again.</p>
      ) : null}
      {!loading && !error && !query.trim() && hasMore ? (
        <button type="button" className={styles.loadMore} onClick={onLoadMore} disabled={loadingMore}>
          {loadingMore ? 'Loading…' : loadMoreError ? 'Try loading more again' : 'Load more contacts'}
        </button>
      ) : null}
    </section>
  );
}

export default function PeopleTab() {
  const [view, setView] = useState('contacts');
  const [contacts, setContacts] = useState(null);
  const [creating, setCreating] = useState(false);
  const [openContactId, setOpenContactId] = useState('');
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [nextCursor, setNextCursor] = useState(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState(null);
  // Bumped by every refresh: a page still in flight from before it is
  // dropped, and an open search re-runs (33.3 review).
  const [generation, setGeneration] = useState(0);
  const generationRef = useRef(0);
  const selectedByUser = useRef(false);

  const load = useCallback(() => {
    setRefreshing(true);
    generationRef.current += 1;
    const mine = generationRef.current;
    setGeneration(mine);
    setLoadingMore(false);
    setLoadMoreError(null);
    return crmGet(`/api/crm/contacts?limit=${CONTACT_PAGE}`).then(
      (payload) => {
        if (generationRef.current !== mine) return;
        setContacts(Array.isArray(payload?.contacts) ? payload.contacts : []);
        setNextCursor(payload?.next_cursor || null);
        setError(null);
        setRefreshing(false);
        setUpdatedAt(new Date());
      },
      (reason) => {
        if (generationRef.current !== mine) return;  // a newer refresh decides
        setError(reason);
        setRefreshing(false);
        if (!selectedByUser.current) setView('opportunities');
      },
    );
  }, []);

  useEffect(() => {
    const initial = Promise.resolve().then(load);
    return () => { void initial; };
  }, [load]);

  const loadMore = useCallback(() => {
    if (!nextCursor) return undefined;
    const mine = generationRef.current;
    setLoadingMore(true);
    setLoadMoreError(null);
    return crmGet(`/api/crm/contacts?limit=${CONTACT_PAGE}&cursor=${encodeURIComponent(nextCursor)}`).then(
      (payload) => {
        if (generationRef.current !== mine) return;  // a refresh replaced the list meanwhile
        const page = Array.isArray(payload?.contacts) ? payload.contacts : [];
        setContacts((current) => {
          const seen = new Set((current || []).map((c) => c.id));
          return [...(current || []), ...page.filter((c) => !seen.has(c.id))];
        });
        setNextCursor(payload?.next_cursor || null);
        setLoadingMore(false);
      },
      (reason) => {
        if (generationRef.current !== mine) return;
        setLoadMoreError(reason);  // the loaded contacts stay on screen
        setLoadingMore(false);
      },
    );
  }, [nextCursor]);

  const selectView = (next) => {
    selectedByUser.current = true;
    setView(next);
  };

  return (
    <section className={styles.wrap} aria-labelledby="people-title">
      <header className={styles.hero}>
        <div>
          <span className={styles.kicker}>Relationships and opportunity</span>
          <h1 id="people-title">People</h1>
          <p>One identity record for every person, with opportunity and property work kept in context.</p>
        </div>
        <div className={styles.contactHead}>
          <button type="button" onClick={() => setCreating((open) => !open)} aria-expanded={creating}>
            {creating ? 'Close' : 'New contact'}
          </button>
          <button type="button" className={styles.refresh} onClick={load} disabled={refreshing} aria-label="Refresh contacts"><RefreshCw aria-hidden="true" /></button>
        </div>
      </header>

      {creating ? (
        <ContactIntakePanel
          onCreated={() => { setCreating(false); void load(); }}
          onCancel={() => setCreating(false)}
        />
      ) : null}

      <nav className={styles.switcher} aria-label="People workspace">
        <button type="button" aria-pressed={view === 'contacts'} data-source={error ? 'error' : contacts === null ? 'loading' : 'ready'} onClick={() => selectView('contacts')}>
          <UsersRound aria-hidden="true" /><span>Contacts</span><small>{contacts ? `${contacts.length}${nextCursor ? '+' : ''}` : '—'}</small>
        </button>
        <button type="button" aria-pressed={view === 'opportunities'} onClick={() => selectView('opportunities')}>
          <BriefcaseBusiness aria-hidden="true" /><span>Opportunities</span>
        </button>
      </nav>

      {view === 'contacts' ? (
        <ContactList
          contacts={contacts}
          error={error}
          loading={contacts === null && !error}
          refreshing={refreshing}
          updatedAt={updatedAt}
          onRetry={load}
          onOpenOpportunities={() => selectView('opportunities')}
          openId={openContactId}
          onOpen={setOpenContactId}
          onChanged={load}
          hasMore={Boolean(nextCursor)}
          loadingMore={loadingMore}
          loadMoreError={loadMoreError}
          onLoadMore={loadMore}
          generation={generation}
        />
      ) : (
        <Suspense fallback={<div className={styles.skeleton} aria-hidden="true"><span /><span /><span /></div>}>
          <ClientCrmTab embedded />
        </Suspense>
      )}
    </section>
  );
}
