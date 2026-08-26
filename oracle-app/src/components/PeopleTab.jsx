import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BriefcaseBusiness, RefreshCw, Search, UserRound, UsersRound } from 'lucide-react';
import { crmGet } from '../state/useCrmApi';
// POST /api/crm/contacts had no caller: People could list the contact book and
// never add to it, so every person had to arrive via import or an agent tool.
import ContactIntakePanel from './ContactIntakePanel';
// GET and PATCH on a contact both had no caller, so a contact could be created
// and listed and never opened — a wrong number was permanent.
import ContactDetailPanel from './ContactDetailPanel';
import { PanelDataStatus } from './PanelDataStatus';
import styles from './PeopleTab.module.css';

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

function ContactList({ contacts, error, loading, refreshing, updatedAt, onRetry, onOpenOpportunities, openId, onOpen, onChanged }) {
  const [query, setQuery] = useState('');
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return contacts || [];
    return (contacts || []).filter((contact) => [
      contact.full_name,
      contact.email,
      contact.phone,
      contact.source,
    ].some((value) => String(value || '').toLowerCase().includes(needle)));
  }, [contacts, query]);

  return (
    <section className={styles.contactBook} aria-labelledby="canonical-contacts-title" aria-busy={loading || refreshing}>
      <header className={styles.contactHead}>
        <div>
          <span className={styles.kicker}>Canonical identity</span>
          <h2 id="canonical-contacts-title">Contacts</h2>
        </div>
        <ul className={styles.sourceStatus}>
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
        </ul>
      </header>

      {!loading && !error && (contacts || []).length > 0 ? (
        <label className={styles.search}>
          <span className={styles.srOnly}>Search contacts</span>
          <Search aria-hidden="true" />
          <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search name, email, phone…" />
        </label>
      ) : null}

      {loading ? (
        <div className={styles.skeleton} aria-hidden="true"><span /><span /><span /></div>
      ) : error ? (
        <div className={styles.errorState} role="alert">
          <strong>Canonical contacts are temporarily unavailable</strong>
          <p>{error.message || 'The contact source could not be loaded.'}</p>
          <div><button type="button" onClick={onRetry}>Retry contacts</button><button type="button" onClick={onOpenOpportunities}>Open opportunities</button></div>
        </div>
      ) : (contacts || []).length === 0 ? (
        <div className={styles.empty} role="status">
          <UsersRound aria-hidden="true" />
          <div><strong>No contacts yet</strong><p>Canonical identities will appear here when they are created or migrated from an opportunity.</p></div>
          <button type="button" onClick={onOpenOpportunities}>Open opportunities</button>
        </div>
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
  const selectedByUser = useRef(false);

  const load = useCallback(() => {
    setRefreshing(true);
    return crmGet('/api/crm/contacts?limit=200').then(
      (payload) => {
        setContacts(Array.isArray(payload?.contacts) ? payload.contacts : []);
        setError(null);
        setRefreshing(false);
        setUpdatedAt(new Date());
      },
      (reason) => {
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
          <UsersRound aria-hidden="true" /><span>Contacts</span><small>{contacts?.length ?? '—'}</small>
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
        />
      ) : (
        <Suspense fallback={<div className={styles.skeleton} aria-hidden="true"><span /><span /><span /></div>}>
          <ClientCrmTab embedded />
        </Suspense>
      )}
    </section>
  );
}
