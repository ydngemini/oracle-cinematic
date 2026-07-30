import { useCallback, useEffect, useMemo, useState } from 'react';
import { crmGet, crmPatch, crmPost } from '../state/useCrmApi';
import styles from './DealBook.module.css';

const money = new Intl.NumberFormat('en-US', {
  maximumFractionDigits: 0,
  style: 'currency',
  currency: 'USD',
});

const dateLabel = new Intl.DateTimeFormat('en-US', {
  month: 'short',
  day: 'numeric',
  year: 'numeric',
});

const refreshGlyph = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M20.5 12a8.5 8.5 0 1 1-2.5-6" />
    <path d="M20.5 3.5V8H16" />
  </svg>
);

function safeMoney(value, precision = 2) {
  if (value === null || value === undefined || value === '') return undefined;
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) return null;
  return Number(parsed.toFixed(precision));
}

function parseMoney(value, fallback) {
  const parsed = safeMoney(value);
  return parsed === undefined ? fallback : parsed;
}

function parseMoneyRequired(value, label) {
  const next = parseMoney(value);
  if (next === undefined) {
    return { error: `${label} is required.` };
  }
  if (next === null) {
    return { error: `${label} must be a valid number greater than or equal to 0.` };
  }
  return { value: next };
}

function parseMoneyOptional(value, label) {
  const next = parseMoney(value);
  if (next === null) {
    return { error: `${label} must be a valid number greater than or equal to 0.` };
  }
  return { value: next };
}

function dateOrNull(value) {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return { error: `${value} is not a valid date.` };
  return { value: parsed };
}

function toMoneyLabel(value) {
  const parsed = safeMoney(value, 2);
  return parsed === null || parsed === undefined ? '—' : money.format(parsed);
}

function toDateLabel(value) {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return '—';
  return dateLabel.format(parsed);
}

function countsByStatus(list) {
  const counts = { all: 0, active: 0, under_contract: 0, closed: 0, cancelled: 0 };
  for (const transaction of list) {
    counts.all += 1;
    if (counts[transaction.status] !== undefined) counts[transaction.status] += 1;
  }
  return counts;
}

function normalizeInputDate(value) {
  if (!value) return '';
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return '';
  return parsed.toISOString().slice(0, 10);
}

export default function DealBook() {
  const [transactions, setTransactions] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadingQuietly, setLoadingQuietly] = useState(false);
  const [error, setError] = useState(null);
  const [expandedId, setExpandedId] = useState(null);

  const load = useCallback(() => {
    const setLoadingState = transactions.length === 0 ? setLoading : setLoadingQuietly;
    setLoadingState(true);
    return crmGet('/api/portfolio/transactions?limit=100').then(
      (payload) => {
        setTransactions(payload.transactions || []);
        setTotal(payload.total || 0);
        setError(null);
        setLoading(false);
        setLoadingQuietly(false);
      },
      (reason) => {
        setError(reason);
        setLoading(false);
        setLoadingQuietly(false);
      },
    );
  }, [transactions.length]);

  const refresh = useCallback(() => {
    load();
  }, [load]);

  useEffect(() => {
    const initialFrame = window.requestAnimationFrame(() => { void load(); });
    const timer = window.setInterval(load, 75_000);
    const onExternal = () => {
      load();
    };
    window.addEventListener('neoh:transactions-changed', onExternal);
    return () => {
      window.cancelAnimationFrame(initialFrame);
      window.clearInterval(timer);
      window.removeEventListener('neoh:transactions-changed', onExternal);
    };
  }, [load]);

  const applyUpdate = useCallback((transaction) => {
    setTransactions((prev) => {
      const next = prev.map((item) => (item.id === transaction.id ? { ...item, ...transaction } : item));
      return next;
    });
  }, []);

  const summary = useMemo(() => countsByStatus(transactions), [transactions]);

  const sorted = useMemo(
    () => [...transactions].sort((a, b) => new Date(b.updated_at).valueOf() - new Date(a.updated_at).valueOf()),
    [transactions],
  );

  return (
    <section className={styles.panel} aria-label="Deal room">
      <header className={styles.panelHeader}>
        <div className={styles.panelTop}>
          <div>
            <span className={styles.panelKicker}>Execution suite</span>
            <h2>Deal room</h2>
            <p>Track every active transaction, update terms, capture offers, and close deals.</p>
          </div>
          <button type="button" className={styles.refresh} onClick={refresh} disabled={loading || loadingQuietly} aria-label="Refresh deal room">
            {refreshGlyph}
          </button>
        </div>

        <div className={styles.meta} aria-label="Deal room overview">
          <span className={styles.chip}>{total} shown</span>
          <span className={styles.chip}>{summary.active} active</span>
          <span className={styles.chip}>{summary.under_contract} under contract</span>
          <span className={styles.chip}>{summary.closed} closed</span>
          <span className={styles.chip}>{summary.cancelled} cancelled</span>
        </div>
      </header>

      <dl className={styles.metrics}>
        <Metric label="Active" value={summary.active} />
        <Metric label="Under contract" value={summary.under_contract} />
        <Metric label="Closed" value={summary.closed} />
        <Metric label="Total" value={summary.all} />
      </dl>

      {error ? (
        <p className={styles.error} role="alert">{error.message || 'Unable to load deal activity.'}</p>
      ) : null}

      {(loading && transactions.length === 0) ? (
        <p className={styles.skeleton}>Loading your live deals…</p>
      ) : sorted.length === 0 ? (
        <p className={styles.empty}>No deals yet. Start a deal from an eligible pipeline property.</p>
      ) : (
        <ul className={styles.list}>
          {sorted.map((transaction) => (
            <li key={transaction.id}>
              <TransactionCard
                key={`${transaction.id}:${transaction.version}`}
                transaction={transaction}
                expanded={expandedId === transaction.id}
                onToggle={() => setExpandedId((current) => (current === transaction.id ? null : transaction.id))}
                onTransactionUpdate={applyUpdate}
              />
            </li>
          ))}
        </ul>
      )}
      {loadingQuietly ? <span className={styles.empty} role="status" aria-live="polite">refreshing…</span> : null}
    </section>
  );
}

function Metric({ label, value }) {
  return (
    <div className={styles.metric}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function TransactionCard({ transaction, expanded, onToggle, onTransactionUpdate }) {
  const [termsBusy, setTermsBusy] = useState(false);
  const [termError, setTermError] = useState('');
  const [closingError, setClosingError] = useState('');
  const [terms, setTerms] = useState({
    version: transaction.version,
    purchasePrice: transaction.purchase_price != null ? String(transaction.purchase_price) : '',
    earnestMoney: transaction.earnest_money != null ? String(transaction.earnest_money) : '',
    financingAmount: transaction.financing_amount != null ? String(transaction.financing_amount) : '',
    offerDeadline: normalizeInputDate(transaction.offer_deadline),
    inspectionDeadline: normalizeInputDate(transaction.inspection_deadline),
    financingDeadline: normalizeInputDate(transaction.financing_deadline),
    closingDeadline: normalizeInputDate(transaction.closing_deadline),
  });

  const property = transaction.property_address || 'Property address pending';

  const saveTerms = useCallback(async () => {
    const payload = { version: terms.version };
    const purchase = parseMoneyOptional(terms.purchasePrice, 'Purchase price');
    if (purchase.error) {
      setTermError(purchase.error);
      return;
    }
    const earnest = parseMoneyOptional(terms.earnestMoney, 'Earnest money');
    if (earnest.error) {
      setTermError(earnest.error);
      return;
    }
    const financingAmount = parseMoneyOptional(terms.financingAmount, 'Financing amount');
    if (financingAmount.error) {
      setTermError(financingAmount.error);
      return;
    }

    const offerDeadline = dateOrNull(terms.offerDeadline);
    if (offerDeadline?.error) {
      setTermError(`Offer deadline: ${offerDeadline.error}`);
      return;
    }
    const inspectionDeadline = dateOrNull(terms.inspectionDeadline);
    if (inspectionDeadline?.error) {
      setTermError(`Inspection deadline: ${inspectionDeadline.error}`);
      return;
    }
    const financingDeadline = dateOrNull(terms.financingDeadline);
    if (financingDeadline?.error) {
      setTermError(`Financing deadline: ${financingDeadline.error}`);
      return;
    }
    const closingDeadline = dateOrNull(terms.closingDeadline);
    if (closingDeadline?.error) {
      setTermError(`Target closing: ${closingDeadline.error}`);
      return;
    }

    if (offerDeadline?.value && closingDeadline?.value && offerDeadline.value > closingDeadline.value) {
      setTermError('Offer deadline cannot be after target closing.');
      return;
    }
    if (inspectionDeadline?.value && closingDeadline?.value && inspectionDeadline.value > closingDeadline.value) {
      setTermError('Inspection deadline cannot be after target closing.');
      return;
    }
    if (financingDeadline?.value && closingDeadline?.value && financingDeadline.value > closingDeadline.value) {
      setTermError('Financing deadline cannot be after target closing.');
      return;
    }

    const purchasePrice = purchase.value ?? null;
    const earnestMoney = earnest.value ?? null;
    const financingAmountValue = financingAmount.value ?? null;

    if (earnestMoney !== null && purchasePrice !== null && earnestMoney > purchasePrice) {
      setTermError('Earnest money cannot exceed purchase price.');
      return;
    }

    payload.purchase_price = purchasePrice;
    payload.earnest_money = earnestMoney;
    payload.financing_amount = financingAmountValue;
    payload.offer_deadline = offerDeadline?.value ? terms.offerDeadline : null;
    payload.inspection_deadline = inspectionDeadline?.value ? terms.inspectionDeadline : null;
    payload.financing_deadline = financingDeadline?.value ? terms.financingDeadline : null;
    payload.closing_deadline = closingDeadline?.value ? terms.closingDeadline : null;

    setTermError('');
    setTermsBusy(true);
    try {
      const result = await crmPatch(`/api/portfolio/transactions/${transaction.id}`, payload);
      onTransactionUpdate(result.transaction);
      setTerms((previous) => ({ ...previous, version: result.transaction.version }));
    } catch (err) {
      setTermError(err?.message || 'Could not save terms.');
    } finally {
      setTermsBusy(false);
    }
  }, [terms, onTransactionUpdate, transaction.id]);

  const closeTransaction = useCallback(async () => {
    if (!window.confirm('Close this transaction and mark all related records closed?')) {
      return;
    }
    setClosingError('');
    try {
      const result = await crmPost(`/api/portfolio/transactions/${transaction.id}/close`, {
        version: transaction.version,
      });
      onTransactionUpdate(result.transaction);
    } catch (err) {
      setClosingError(err?.message || 'Could not close transaction.');
    }
  }, [transaction.id, transaction.version, onTransactionUpdate]);

  return (
    <article className={styles.card}>
      <button type="button" className={styles.cardHead} onClick={onToggle} aria-expanded={expanded}>
        <div className={styles.titleRow}>
          <h3 className={styles.dealTitle}>{property}</h3>
          <span className={styles.state} data-status={transaction.status}>{transaction.status || 'draft'}</span>
        </div>
        <p className={styles.metaLine}>
          {transaction.property_source === 'pipeline'
            ? 'PIPELINE'
            : 'PROPERTY'} · v{transaction.version} · {toDateLabel(transaction.updated_at)}
          {transaction.client_name ? ` · ${transaction.client_name}` : ''}
        </p>

        <p className={styles.metaLine}>
          {toMoneyLabel(transaction.purchase_price)} purchase · {toMoneyLabel(transaction.earnest_money)} earnest
          {transaction.accepted_offer_amount ? ` · accepted offer ${toMoneyLabel(transaction.accepted_offer_amount)}` : ''}
        </p>
      </button>

      {expanded ? (
        <div className={styles.details}>
          <section aria-label="Transaction terms">
            <h4>Terms</h4>
            <div className={styles.termGrid}>
              <label className={styles.field}>
                <span>Purchase price</span>
                <input
                  className={styles.input}
                  value={terms.purchasePrice}
                  onChange={(event) => setTerms((previous) => ({ ...previous, purchasePrice: event.target.value }))}
                  inputMode="decimal"
                  placeholder="e.g. 250000"
                />
              </label>
              <label className={styles.field}>
                <span>Earnest money</span>
                <input
                  className={styles.input}
                  value={terms.earnestMoney}
                  onChange={(event) => setTerms((previous) => ({ ...previous, earnestMoney: event.target.value }))}
                  inputMode="decimal"
                  placeholder="e.g. 5000"
                />
              </label>
              <label className={styles.field}>
                <span>Financing amount</span>
                <input
                  className={styles.input}
                  value={terms.financingAmount}
                  onChange={(event) => setTerms((previous) => ({ ...previous, financingAmount: event.target.value }))}
                  inputMode="decimal"
                  placeholder="e.g. 0"
                />
              </label>
              <label className={styles.field}>
                <span>Offer deadline</span>
                <input
                  className={styles.input}
                  type="date"
                  value={terms.offerDeadline}
                  onChange={(event) => setTerms((previous) => ({ ...previous, offerDeadline: event.target.value }))}
                />
              </label>
              <label className={styles.field}>
                <span>Inspection deadline</span>
                <input
                  className={styles.input}
                  type="date"
                  value={terms.inspectionDeadline}
                  onChange={(event) => setTerms((previous) => ({ ...previous, inspectionDeadline: event.target.value }))}
                />
              </label>
              <label className={styles.field}>
                <span>Financing deadline</span>
                <input
                  className={styles.input}
                  type="date"
                  value={terms.financingDeadline}
                  onChange={(event) => setTerms((previous) => ({ ...previous, financingDeadline: event.target.value }))}
                />
              </label>
              <label className={styles.field}>
                <span>Target closing</span>
                <input
                  className={styles.input}
                  type="date"
                  value={terms.closingDeadline}
                  onChange={(event) => setTerms((previous) => ({ ...previous, closingDeadline: event.target.value }))}
                />
              </label>
            </div>
            {termError ? <p className={styles.error}>{termError}</p> : null}
            <div className={styles.actions}>
              <button
                type="button"
                className={styles.action}
                onClick={saveTerms}
                disabled={termsBusy}
              >
                {termsBusy ? 'Saving…' : 'Save terms'}
              </button>
            </div>
          </section>

          {transaction.status === 'active' ? (
            <OfferPanel transaction={transaction} onTransactionUpdate={onTransactionUpdate} />
          ) : null}

          {transaction.status === 'under_contract' ? (
            <div className={styles.offerBar}>
              <div className={styles.offerRow}>
                <strong>Under contract</strong>
                <button type="button" className={styles.action} onClick={closeTransaction}>Close</button>
              </div>
              {closingError ? <p className={styles.error}>{closingError}</p> : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

function OfferPanel({ transaction, onTransactionUpdate }) {
  const [loading, setLoading] = useState(false);
  const [offers, setOffers] = useState([]);
  const [loadError, setLoadError] = useState('');
  const [offerError, setOfferError] = useState('');
  const [saveBusy, setSaveBusy] = useState(false);
  const [accepting, setAccepting] = useState('');
  const [form, setForm] = useState({ amount: '', earnest: '', financingType: 'cash', closeDate: '', expiresAt: '' });

  const loadOffers = useCallback(() => {
    setLoading(true);
    setLoadError('');
    crmGet(`/api/portfolio/transactions/${transaction.id}/offers`).then(
      (payload) => {
        setOffers(Array.isArray(payload?.offers) ? payload.offers : []);
        setLoading(false);
      },
      () => {
        setOffers([]);
        setLoadError('Unable to load offer history.');
        setLoading(false);
      },
    );
  }, [transaction.id]);

  useEffect(() => {
    const initialFrame = window.requestAnimationFrame(() => { void loadOffers(); });
    return () => window.cancelAnimationFrame(initialFrame);
  }, [loadOffers]);

  const createOffer = useCallback(async () => {
    const amountParsed = parseMoneyRequired(form.amount, 'Offer amount');
    if (amountParsed.error) {
      setOfferError(amountParsed.error);
      return;
    }
    const amount = amountParsed.value;
    if (amount <= 0) {
      setOfferError('Offer amount must be greater than zero.');
      return;
    }
    const earnestParsed = parseMoneyOptional(form.earnest, 'Earnest money');
    if (earnestParsed.error) {
      setOfferError(earnestParsed.error);
      return;
    }
    const earnest = earnestParsed.value ?? 0;

    let closingDateValue;
    if (form.closeDate) {
      const closeDate = dateOrNull(form.closeDate);
      if (closeDate?.error) {
        setOfferError(`Target closing date: ${closeDate.error}`);
        return;
      }
      closingDateValue = closeDate?.value;
    }

    let expiryDateValue;
    if (form.expiresAt) {
      const expires = dateOrNull(form.expiresAt);
      if (expires?.error) {
        setOfferError(`Offer expiry: ${expires.error}`);
        return;
      }
      expiryDateValue = expires?.value;
    }

    if (expiryDateValue && expiryDateValue <= new Date()) {
      setOfferError('Offer expiry must be in the future.');
      return;
    }

    if (closingDateValue && expiryDateValue && closingDateValue <= expiryDateValue) {
      setOfferError('Target closing date must be after offer expiry.');
      return;
    }

    if (earnest > amount) {
      setOfferError('Offer earnest money cannot exceed offer amount.');
      return;
    }

    setOfferError('');
    setSaveBusy(true);
    try {
      const offer = await crmPost(`/api/portfolio/transactions/${transaction.id}/offers`, {
        amount,
        earnest_money: earnest,
        financing_type: form.financingType,
        proposed_closing_date: form.closeDate || undefined,
        expires_at: expiryDateValue?.toISOString(),
      });
      setOffers((prev) => [offer.offer, ...prev]);
      setForm((prev) => ({ ...prev, amount: '', earnest: '', closeDate: '', expiresAt: '' }));
      if (form.expiresAt || form.closeDate) {
        loadOffers();
      }
    } catch (err) {
      setOfferError(err?.message || 'Could not submit offer.');
    } finally {
      setSaveBusy(false);
    }
  }, [transaction.id, form, loadOffers]);

  const acceptOffer = useCallback(async (offer) => {
    setAccepting(offer.id);
    setOfferError('');
    try {
      const payload = await crmPost(`/api/portfolio/transactions/${transaction.id}/offers/${offer.id}/accept`, {
        transaction_version: transaction.version,
        offer_version: offer.version,
      });
      onTransactionUpdate(payload.transaction);
      setOffers((prev) => prev.map((item) => ({ ...item, status: item.id === offer.id ? 'accepted' : 'rejected' })));
      setAccepting('');
    } catch (err) {
      setOfferError(err?.message || 'Could not accept this offer.');
      setAccepting('');
    }
  }, [transaction.id, transaction.version, onTransactionUpdate]);

  return (
    <section className={styles.offerBar} aria-label="Offer workspace">
      <h4>Offers</h4>

      <div className={styles.offerRow}>
        <label className={styles.field}>
          <span>Offer amount</span>
          <input
            className={styles.input}
            value={form.amount}
            onChange={(event) => setForm((prev) => ({ ...prev, amount: event.target.value }))}
            inputMode="decimal"
            placeholder="Offer amount"
          />
        </label>

        <label className={styles.field}>
          <span>Earnest money</span>
          <input
            className={styles.input}
            value={form.earnest}
            onChange={(event) => setForm((prev) => ({ ...prev, earnest: event.target.value }))}
            inputMode="decimal"
            placeholder="Earnest"
          />
        </label>

        <label className={styles.field}>
          <span>Close date</span>
          <input
            className={styles.input}
            type="date"
            value={form.closeDate}
            onChange={(event) => setForm((prev) => ({ ...prev, closeDate: event.target.value }))}
          />
        </label>

        <label className={styles.field}>
          <span>Offer expires</span>
          <input
            className={styles.input}
            inputMode="numeric"
            type="datetime-local"
            value={form.expiresAt}
            onChange={(event) => setForm((prev) => ({ ...prev, expiresAt: event.target.value }))}
          />
        </label>

        <label className={styles.field}>
          <span>Financing</span>
          <select
            className={styles.input}
            value={form.financingType}
            onChange={(event) => setForm((prev) => ({ ...prev, financingType: event.target.value }))}
          >
            <option value="cash">Cash</option>
            <option value="conventional">Conventional</option>
            <option value="fha">FHA</option>
            <option value="va">VA</option>
            <option value="usda">USDA</option>
            <option value="other">Other</option>
          </select>
        </label>
        <div className={styles.offerActions}>
          <button type="button" className={styles.action} onClick={createOffer} disabled={saveBusy || loading}>
            {saveBusy ? 'Submitting…' : 'Submit offer'}
          </button>
        </div>
      </div>

      {offerError ? <p className={styles.error} role="alert">{offerError}</p> : null}

      {loading ? <p className={styles.small}>Loading offers…</p> : null}
      {loadError ? <p className={styles.error}>{loadError}</p> : null}
      {!loading && offers.length === 0 ? (
        <p className={styles.small}>No offers yet.</p>
      ) : (
        <ul className={styles.offerList}>
          {offers.map((offer) => (
            <li key={offer.id} className={styles.offerItem} data-status={offer.status}>
              <div className={styles.offerItemText}>
                <strong>{money.format(safeMoney(offer.amount, 2) || 0)}</strong>
                <span className={styles.small}>
                  Earnest {money.format(safeMoney(offer.earnest_money, 2) || 0)} · {toDateLabel(offer.proposed_closing_date)}
                </span>
              </div>
              <span className={styles.status} data-status={offer.status}>{offer.status}</span>
              {offer.status === 'submitted' ? (
                <button
                  type="button"
                  className={styles.action}
                  onClick={() => acceptOffer(offer)}
                  disabled={accepting === offer.id}
                >
                  {accepting === offer.id ? 'Accepting…' : 'Accept'}
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
