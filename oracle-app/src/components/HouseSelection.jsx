import {
  startTransition,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from 'react';
import {
  ChevronLeft,
  ChevronRight,
  House,
  MapPin,
  RefreshCw,
  Search,
} from 'lucide-react';
import { crmGet } from '../state/useCrmApi';
import { formatApiError } from '../lib/errorMessages';
import { US_STATES } from '../lib/usStates';
import HouseWorkspace from './HouseWorkspace';
import { AdaptiveViewTransition } from './motion/AdaptiveViewTransition';
import styles from './HouseSelection.module.css';

const COUNT_FORMAT = new Intl.NumberFormat('en-US');

function publicRecord(record) {
  return {
    ...record,
    id: String(record.id),
    isManual: false,
    recordSource: 'public_record',
  };
}

function buildPath({ q, state, page }) {
  const params = new URLSearchParams({ page: String(page) });
  if (q) params.set('q', q);
  if (state) params.set('state', state);
  return `/api/mls/public-records?${params.toString()}`;
}

function RecordSkeletons() {
  return (
    <div className={styles.skeletons} aria-busy="true" aria-label="Loading public property records">
      {Array.from({ length: 6 }, (_, index) => (
        <div className={styles.skeleton} key={index} />
      ))}
    </div>
  );
}

function RecordCard({ record, isSelected, onSelect }) {
  const locality = [record.city, record.state, record.zip].filter(Boolean).join(', ');
  return (
    <button
      type="button"
      className={styles.recordCard}
      data-selected={isSelected ? '' : undefined}
      aria-pressed={isSelected}
      onClick={() => onSelect(record)}
    >
      <span className={styles.recordGlyph} aria-hidden="true">
        <House />
      </span>
      <span className={styles.recordCopy}>
        <strong>{record.address || 'Address unavailable'}</strong>
        <span>{locality || record.county || 'Public property record'}</span>
      </span>
      <span className={styles.recordMeta}>
        <span>{record.parcel_id ? `Parcel ${record.parcel_id}` : 'Public record'}</span>
        <small>{record.source || 'Source-backed'}</small>
      </span>
    </button>
  );
}

export default function HouseSelection() {
  const [qInput, setQInput] = useState('');
  const [stateInput, setStateInput] = useState('');
  const [query, setQuery] = useState({ q: '', state: '', page: 1 });
  const [refreshKey, setRefreshKey] = useState(0);
  const [records, setRecords] = useState(null);
  const [meta, setMeta] = useState({
    total: 0,
    totalIsEstimate: false,
    page: 1,
    pageSize: 24,
    hasMore: false,
    coverage: null,
    notice: '',
  });
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState('');
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [manualAddress, setManualAddress] = useState('');
  const [manualError, setManualError] = useState('');

  const requestPath = useMemo(() => buildPath(query), [query]);

  useEffect(() => {
    const controller = new AbortController();
    // This state mirrors an external request lifecycle, not derived render data.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setIsRefreshing(true);
    crmGet(requestPath, { signal: controller.signal, retries: 1 }).then(
      (data) => {
        if (controller.signal.aborted) return;
        const rows = (Array.isArray(data?.listings) ? data.listings : []).map(publicRecord);
        setRecords(rows);
        setMeta({
          total: Number(data?.total) || 0,
          totalIsEstimate: data?.total_is_estimate === true,
          page: Number(data?.page) || query.page,
          pageSize: Number(data?.page_size) || 24,
          hasMore: data?.has_more === true,
          coverage: data?.coverage || null,
          notice: typeof data?.notice === 'string' ? data.notice : '',
        });
        setSelected((current) => {
          if (current?.isManual) return current;
          return rows.find((record) => record.id === current?.id) || rows[0] || null;
        });
        setError('');
        setIsRefreshing(false);
      },
      (reason) => {
        if (controller.signal.aborted) return;
        setError(formatApiError(reason));
        setRecords((current) => current ?? []);
        setIsRefreshing(false);
      },
    );
    return () => controller.abort();
  }, [requestPath, refreshKey, query.page]);

  const submitSearch = useCallback((event) => {
    event.preventDefault();
    setQuery({ q: qInput.trim(), state: stateInput, page: 1 });
  }, [qInput, stateInput]);

  const resetSearch = useCallback(() => {
    setQInput('');
    setStateInput('');
    setQuery({ q: '', state: '', page: 1 });
  }, []);

  const selectRecord = useCallback((record) => {
    startTransition(() => setSelected(record));
  }, []);

  // Linking a house to a client creates the tenant's lead for that parcel, and
  // the POST returns its id. `selected` is owned here, so the workspace cannot
  // record it itself — without this the agent links a house and the interior
  // affordances stay hidden until a full refetch happens to pick the lead up.
  // The record id is unchanged, so the keyed transition above does not remount.
  const applyHouseUpdate = useCallback((patch) => {
    if (!patch) return;
    setSelected((current) => (current ? { ...current, ...patch } : current));
  }, []);

  const selectManualHouse = useCallback((event) => {
    event.preventDefault();
    const address = ' '.concat(manualAddress).trim().replace(/\s+/g, ' ');
    if (address.length < 3) {
      setManualError('Enter the property street address.');
      return;
    }
    setManualError('');
    startTransition(() => {
      setSelected({
        id: `manual:${address.toLowerCase()}`,
        address,
        isManual: true,
        recordSource: 'crm_manual',
        status: 'draft',
      });
    });
  }, [manualAddress]);

  const pageStart = meta.total === 0 ? 0 : (meta.page - 1) * meta.pageSize + 1;
  const pageEnd = Math.min(meta.page * meta.pageSize, meta.total);

  return (
    <section className={styles.page} aria-labelledby="houses-title">
      <header className={styles.hero}>
        <div>
          <span className={styles.eyebrow}>Property workspace</span>
          <h2 id="houses-title">House selection</h2>
        </div>
        <p>
          Browse source-backed public property records, or create a manual CRM
          house and connect it to the right client.
        </p>
      </header>

      <div className={styles.layout}>
        <section className={styles.directory} aria-labelledby="public-records-title">
          <div className={styles.sectionHead}>
            <div>
              <span>Source-backed</span>
              <h2 id="public-records-title">Public property records</h2>
            </div>
            <button
              type="button"
              className={styles.iconButton}
              onClick={() => setRefreshKey((value) => value + 1)}
              disabled={isRefreshing}
              aria-label="Refresh public property records"
            >
              <RefreshCw aria-hidden="true" />
            </button>
          </div>

          <form className={styles.searchForm} onSubmit={submitSearch}>
            <label className={styles.searchField}>
              <span>Search records</span>
              <div>
                <Search aria-hidden="true" />
                <input
                  type="search"
                  value={qInput}
                  onChange={(event) => setQInput(event.target.value)}
                  placeholder="Address, owner, or parcel"
                  autoComplete="off"
                />
              </div>
            </label>
            <label className={styles.stateField}>
              <span>State</span>
              <select value={stateInput} onChange={(event) => setStateInput(event.target.value)}>
                <option value="">All states + DC</option>
                {US_STATES.map(({ code, name }) => (
                  <option key={code} value={code}>{name}</option>
                ))}
              </select>
            </label>
            <button type="submit" className={styles.searchButton}>Search</button>
            {(query.q || query.state) && (
              <button type="button" className={styles.textButton} onClick={resetSearch}>
                Clear
              </button>
            )}
          </form>

          <div className={styles.resultBar} aria-live="polite">
            <span title={meta.totalIsEstimate ? 'Estimated from catalog statistics, not an exact count.' : undefined}>
              {meta.totalIsEstimate ? '≈' : ''}
              {COUNT_FORMAT.format(meta.total)} public {meta.total === 1 ? 'record' : 'records'}
            </span>
            {meta.total > 0 && <span>{pageStart}–{pageEnd}</span>}
          </div>

          {meta.coverage && (
            <div className={styles.coverageNote}>
              <strong>
                {meta.coverage.jurisdictions_live} jurisdictions indexed
              </strong>
              <span>{meta.notice}</span>
            </div>
          )}

          {error && (
            <div className={styles.errorState} role="alert">
              <strong>Public records could not be loaded.</strong>
              <span>{error} You can still add a manual CRM house.</span>
            </div>
          )}

          {records === null ? (
            <RecordSkeletons />
          ) : records.length > 0 ? (
            <div className={styles.recordList} aria-label="Public property records">
              {records.map((record) => (
                <RecordCard
                  key={record.id}
                  record={record}
                  isSelected={!selected?.isManual && selected?.id === record.id}
                  onSelect={selectRecord}
                />
              ))}
            </div>
          ) : !error ? (
            <div className={styles.emptyState}>
              <MapPin aria-hidden="true" />
              <strong>No public records match this search.</strong>
              <span>Try a broader search, or enter the address manually below.</span>
            </div>
          ) : null}

          <div className={styles.pagination} aria-label="Public record pages">
            <button
              type="button"
              onClick={() => setQuery((current) => ({ ...current, page: current.page - 1 }))}
              disabled={query.page <= 1 || isRefreshing}
            >
              <ChevronLeft aria-hidden="true" />
              Previous
            </button>
            <span>Page {meta.page}</span>
            <button
              type="button"
              onClick={() => setQuery((current) => ({ ...current, page: current.page + 1 }))}
              disabled={!meta.hasMore || isRefreshing}
            >
              Next
              <ChevronRight aria-hidden="true" />
            </button>
          </div>

          <form className={styles.manualForm} onSubmit={selectManualHouse}>
            <div>
              <span>Manual CRM house</span>
              <h2>Add an address</h2>
              <p>Manual entries remain labeled unverified until matched to a source record.</p>
            </div>
            <label>
              <span>Street address</span>
              <input
                value={manualAddress}
                onChange={(event) => setManualAddress(event.target.value)}
                aria-invalid={manualError ? 'true' : undefined}
                aria-describedby={manualError ? 'manual-address-error' : undefined}
                placeholder="15 Main Street, Dover, DE 19901"
              />
            </label>
            {manualError && <span id="manual-address-error" className={styles.fieldError}>{manualError}</span>}
            <button type="submit" className={styles.secondaryButton}>Use this address</button>
          </form>
        </section>

        <AdaptiveViewTransition
          key={selected?.id || 'empty-house-workspace'}
          enter="fade-in"
          exit="fade-out"
          default="none"
        >
          <HouseWorkspace house={selected} onHouseUpdate={applyHouseUpdate} />
        </AdaptiveViewTransition>
      </div>
    </section>
  );
}
