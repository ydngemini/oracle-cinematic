import { useEffect, useMemo, useRef, useState } from 'react';
import { Link2, Search, UserRound, X } from 'lucide-react';
import { crmGet, crmPost } from '../state/useCrmApi';
import { formatApiError } from '../lib/errorMessages';
import styles from './HouseSelection.module.css';

function trapFocus(event, root) {
  if (event.key !== 'Tab') return;
  const focusable = [...(root?.querySelectorAll(
    'button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
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

export default function HouseLinkDialog({ house, onClose, onLinked }) {
  const dialogRef = useRef(null);
  const searchRef = useRef(null);
  const [clients, setClients] = useState(null);
  const [query, setQuery] = useState('');
  const [selectedId, setSelectedId] = useState('');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const previouslyFocused = document.activeElement;
    const controller = new AbortController();
    window.requestAnimationFrame(() => searchRef.current?.focus());
    crmGet('/api/crm/clients?type=all&sort=recent', {
      signal: controller.signal,
      retries: 1,
    }).then(
      (data) => {
        if (controller.signal.aborted) return;
        const rows = Array.isArray(data?.clients) ? data.clients : [];
        setClients(rows);
        setSelectedId(rows[0]?.id || '');
        setError('');
      },
      (reason) => {
        if (controller.signal.aborted) return;
        setClients([]);
        setError(formatApiError(reason));
      },
    );
    return () => {
      controller.abort();
      previouslyFocused?.focus?.({ preventScroll: true });
    };
  }, []);

  const filteredClients = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return clients || [];
    return (clients || []).filter((client) => (
      client.full_name?.toLowerCase().includes(needle)
      || client.email?.toLowerCase().includes(needle)
      || client.phone?.toLowerCase().includes(needle)
    ));
  }, [clients, query]);

  const linkHouse = async (event) => {
    event.preventDefault();
    const client = (clients || []).find((row) => row.id === selectedId);
    if (!client) {
      setError('Choose a client before linking this house.');
      return;
    }
    setSaving(true);
    setError('');
    try {
      const body = house.isManual
        ? { address: house.address }
        : { public_record_id: house.public_record_id || house.id };
      const result = await crmPost(`/api/crm/clients/${client.id}/houses`, body);
      onLinked(client, result);
    } catch (reason) {
      setError(formatApiError(reason));
      setSaving(false);
    }
  };

  return (
    <div
      className={styles.dialogLayer}
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        ref={dialogRef}
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby="link-house-title"
        aria-describedby="link-house-description"
        tabIndex={-1}
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            event.preventDefault();
            onClose();
          } else {
            trapFocus(event, dialogRef.current);
          }
        }}
      >
        <header className={styles.dialogHead}>
          <div>
            <span>CRM relationship</span>
            <h2 id="link-house-title">Link house to client</h2>
          </div>
          <button type="button" onClick={onClose} aria-label="Close client selector">
            <X aria-hidden="true" />
          </button>
        </header>

        <p id="link-house-description" className={styles.dialogAddress}>{house.address}</p>

        <form onSubmit={linkHouse}>
          <label className={styles.dialogSearch}>
            <span>Find client</span>
            <div>
              <Search aria-hidden="true" />
              <input
                ref={searchRef}
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Name, email, or phone"
                autoComplete="off"
              />
            </div>
          </label>

          <fieldset className={styles.clientList}>
            <legend>Choose one client</legend>
            {clients === null ? (
              <div className={styles.clientLoading} role="status">Loading clients…</div>
            ) : filteredClients.length > 0 ? (
              filteredClients.map((client) => (
                <label key={client.id} className={styles.clientOption}>
                  <input
                    type="radio"
                    name="client"
                    value={client.id}
                    checked={selectedId === client.id}
                    onChange={() => setSelectedId(client.id)}
                  />
                  <span className={styles.clientAvatar} aria-hidden="true"><UserRound /></span>
                  <span>
                    <strong>{client.full_name}</strong>
                    <small>{client.email || client.phone || 'No contact details'}</small>
                  </span>
                  <em>{client.client_type || 'client'}</em>
                </label>
              ))
            ) : (
              <div className={styles.noClients} role="status">
                {query ? 'No clients match this search.' : 'No CRM clients are available yet.'}
              </div>
            )}
          </fieldset>

          {error && <div className={styles.dialogError} role="alert">{error}</div>}

          <div className={styles.dialogActions}>
            <button type="button" className={styles.textButton} onClick={onClose}>Cancel</button>
            <button
              type="submit"
              className={styles.primaryButton}
              disabled={!selectedId || saving || clients === null}
            >
              <Link2 aria-hidden="true" />
              {saving ? 'Linking…' : 'Link house'}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
