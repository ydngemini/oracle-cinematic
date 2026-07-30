import { useEffect, useMemo, useState } from 'react';
import { crmGet, crmPost } from '../state/useCrmApi';
import styles from './DealIntakePanel.module.css';

const ROLES = [
  ['buyer', 'Buyer'],
  ['seller', 'Seller'],
  ['assignor', 'Assignor'],
  ['assignee', 'Assignee'],
  ['joint_venture', 'Joint venture'],
];

function money(value) {
  if (value === '') return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

export default function DealIntakePanel({
  propertyId,
  propertySource,
  defaultPrice,
  address,
  onCreated,
  onCancel,
}) {
  const [clients, setClients] = useState(null);
  const [clientError, setClientError] = useState('');
  const [clientId, setClientId] = useState('');
  const [partyRole, setPartyRole] = useState('buyer');
  const [purchasePrice, setPurchasePrice] = useState(
    Number(defaultPrice) > 0 ? String(Math.round(Number(defaultPrice))) : '',
  );
  const [earnestMoney, setEarnestMoney] = useState('');
  const [closingDeadline, setClosingDeadline] = useState('');
  const [notes, setNotes] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [created, setCreated] = useState(null);

  useEffect(() => {
    let active = true;
    crmGet('/api/crm/clients?type=all&sort=recent').then(
      (payload) => {
        if (!active) return;
        setClients(Array.isArray(payload?.clients) ? payload.clients : []);
        setClientError('');
      },
      () => {
        if (!active) return;
        setClients([]);
        setClientError('Clients could not be loaded. You can still start an unlinked deal.');
      },
    );
    return () => { active = false; };
  }, []);

  const validation = useMemo(() => {
    const price = money(purchasePrice);
    const earnest = money(earnestMoney);
    if (price === null || earnest === null) return 'Enter valid non-negative amounts.';
    if (price !== undefined && earnest !== undefined && earnest > price) {
      return 'Earnest money cannot exceed the purchase price.';
    }
    return '';
  }, [purchasePrice, earnestMoney]);

  const submit = async (event) => {
    event.preventDefault();
    if (validation || busy) return;
    setBusy(true);
    setError('');

    const payload = {
      property_source: propertySource,
      property_id: propertyId,
    };
    if (clientId) {
      payload.client_id = clientId;
      payload.party_role = partyRole;
    }
    const price = money(purchasePrice);
    const earnest = money(earnestMoney);
    if (price !== undefined) payload.purchase_price = price;
    if (earnest !== undefined) payload.earnest_money = earnest;
    if (closingDeadline) payload.closing_deadline = closingDeadline;
    if (notes.trim()) payload.notes = notes.trim();

    try {
      const result = await crmPost('/api/portfolio/transactions', payload);
      const transaction = result?.transaction || null;
      setCreated(transaction);
      onCreated?.(transaction);
      if (transaction) {
        window.dispatchEvent(new CustomEvent('neoh:transactions-changed', {
          detail: { transaction },
        }));
      }
    } catch (reason) {
      setError(reason?.status === 404
        ? 'The selected property or client is no longer available.'
        : reason?.message || 'The deal could not be created.');
    } finally {
      setBusy(false);
    }
  };

  if (created) {
    return (
      <section className={styles.panel} role="status" aria-label="Deal created">
        <span className={styles.kicker}>Deal opened</span>
        <h3>{created.property_address || address || 'Property deal'}</h3>
        <p>The transaction is now tracked in Me → Portfolio. Offers and closing controls are ready there.</p>
        <button type="button" className={styles.secondary} onClick={onCancel}>Done</button>
      </section>
    );
  }

  return (
    <section className={styles.panel} aria-labelledby="deal-intake-title">
      <header className={styles.head}>
        <div>
          <span className={styles.kicker}>Source-bound transaction</span>
          <h3 id="deal-intake-title">Start a deal</h3>
        </div>
        <button type="button" className={styles.close} onClick={onCancel} aria-label="Close deal form">×</button>
      </header>
      <p className={styles.context}>
        {address || 'Selected property'} · {propertySource === 'pipeline' ? 'Pipeline' : 'Property'}
      </p>

      <form className={styles.form} onSubmit={submit}>
        <label className={styles.field}>
          <span>Client (optional)</span>
          <select value={clientId} onChange={(event) => setClientId(event.target.value)} disabled={clients === null}>
            <option value="">No client linked yet</option>
            {(clients || []).map((client) => (
              <option key={client.id} value={client.id}>{client.full_name || client.email || 'Unnamed client'}</option>
            ))}
          </select>
        </label>

        {clientId ? (
          <label className={styles.field}>
            <span>Client role</span>
            <select value={partyRole} onChange={(event) => setPartyRole(event.target.value)}>
              {ROLES.map(([value, name]) => <option key={value} value={value}>{name}</option>)}
            </select>
          </label>
        ) : null}

        <div className={styles.columns}>
          <label className={styles.field}>
            <span>Purchase price</span>
            <input value={purchasePrice} onChange={(event) => setPurchasePrice(event.target.value)} inputMode="decimal" placeholder="250000" />
          </label>
          <label className={styles.field}>
            <span>Earnest money</span>
            <input value={earnestMoney} onChange={(event) => setEarnestMoney(event.target.value)} inputMode="decimal" placeholder="2500" />
          </label>
        </div>

        <label className={styles.field}>
          <span>Target closing</span>
          <input type="date" value={closingDeadline} onChange={(event) => setClosingDeadline(event.target.value)} />
        </label>
        <label className={styles.field}>
          <span>Private deal note</span>
          <textarea value={notes} onChange={(event) => setNotes(event.target.value)} rows={3} maxLength={5000} placeholder="Terms, next decision, or context for your AI" />
        </label>

        {clientError ? <p className={styles.notice}>{clientError}</p> : null}
        {validation ? <p className={styles.error} role="alert">{validation}</p> : null}
        {error ? <p className={styles.error} role="alert">{error}</p> : null}

        <div className={styles.actions}>
          <button type="button" className={styles.secondary} onClick={onCancel}>Cancel</button>
          <button type="submit" className={styles.primary} disabled={busy || Boolean(validation)}>
            {busy ? 'Opening deal…' : 'Open deal'}
          </button>
        </div>
      </form>
    </section>
  );
}
