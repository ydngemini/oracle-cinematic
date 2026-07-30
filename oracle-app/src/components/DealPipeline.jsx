import {
  lazy,
  memo,
  startTransition,
  Suspense,
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  Columns3,
  LayoutGrid,
  Map as MapIcon,
  RefreshCw,
  Search,
} from 'lucide-react';
import { useOracleState, useOracleDispatch } from '../state';
import { downloadLeadsCsv } from '../utils/csv';
import { US_STATES } from '../lib/usStates';
import { contractCountdown } from './pipelineUtils';
import { DossierPanel } from './DossierPanel';
import styles from './DealPipeline.module.css';

const PAGE_SIZE = 60;
const OWNER_LABEL = { corporate: 'CORP', trust: 'TRUST', individual: 'INDIV' };
const loadLeadMap = () => import('./LeadMap');
const loadPipelineBoard = () => import('./PipelineBoard');
const LeadMap = lazy(() =>
  loadLeadMap().then((module) => ({ default: module.LeadMap }))
);
const PipelineBoard = lazy(() =>
  loadPipelineBoard().then((module) => ({ default: module.PipelineBoard }))
);

function scoreTier(score) {
  if (score >= 70) return 'hot';
  if (score >= 40) return 'warm';
  return 'cool';
}

function formatValue(value) {
  if (!value || Number.isNaN(Number(value))) return '—';
  const number = Number(value);
  if (number >= 1_000_000) return `$${(number / 1_000_000).toFixed(1)}M`;
  if (number >= 1_000) return `$${Math.round(number / 1_000)}K`;
  return `$${number.toLocaleString()}`;
}

function formatCount(value) {
  const count = Number(value);
  return Number.isFinite(count) ? count.toLocaleString() : '—';
}

function matchesPriority(lead, priority) {
  if (priority === 'hot') return Number(lead.motivation_score) >= 70;
  if (priority === 'contract') return Boolean(contractCountdown(lead));
  if (priority === 'distress') {
    return Boolean(lead.is_absentee_owner || lead.distress_flags?.length);
  }
  return true;
}

function label(value) {
  return String(value || 'unknown').replaceAll('_', ' ');
}

function matchesQuery(lead, query) {
  if (!query) return true;
  const haystack = [
    lead.address,
    lead.parcel_id,
    lead.owner_name,
    lead.state,
    ...(lead.distress_flags || []),
  ].join(' ').toLowerCase();
  return haystack.includes(query);
}

const LeadCard = memo(function LeadCard({ lead, selected, onToggle, onOpen }) {
  const countdown = contractCountdown(lead);
  const visibleFlags = (lead.distress_flags || []).filter(
    (flag) => flag !== 'absentee_owner'
  );

  return (
    <li className={styles.leadItem}>
      <label className={styles.selectLead}>
        <input
          type="checkbox"
          checked={selected}
          onChange={() => onToggle(lead.parcel_id)}
          aria-label={`Select ${lead.address || lead.parcel_id}`}
        />
      </label>
      <button
        type="button"
        className={styles.leadCard}
        data-selected={selected}
        aria-pressed={selected}
        onClick={() => onOpen(lead.id || lead.parcel_id)}
      >
        <span className={styles.leadTop}>
          <span className={styles.leadAddress} title={lead.address}>
            {lead.address || lead.parcel_id}
          </span>
          <span
            className={styles.scoreBadge}
            data-tier={scoreTier(lead.motivation_score)}
            aria-label={`Public-record priority ${lead.motivation_score} of 100`}
          >
            {lead.motivation_score}
          </span>
        </span>

        <span className={styles.leadMeta}>
          {countdown ? (
            <span
              className={styles.countdownChip}
              data-zone={countdown.zone}
              aria-label={`Contract window: ${countdown.days} days remaining`}
              title={`Assignment window — ${countdown.days} days remaining`}
            >
              {countdown.days}d
            </span>
          ) : null}
          <span className={styles.ownerChip} data-type={lead.owner_type}>
            {OWNER_LABEL[lead.owner_type] || 'INDIV'}
          </span>
          <span className={styles.ownerName} title={lead.owner_name}>
            {lead.owner_name || 'Owner unavailable'}
          </span>
          <span className={styles.leadValue}>{formatValue(lead.estimated_value)}</span>
        </span>

        {lead.is_absentee_owner || visibleFlags.length > 0 ? (
          <span className={styles.flags}>
            {lead.is_absentee_owner ? (
              <span className={styles.flag} data-flag="absentee">
                absentee
              </span>
            ) : null}
            {visibleFlags.slice(0, 3).map((flag) => (
              <span key={flag} className={styles.flag} data-flag={flag}>
                {flag.replaceAll('_', ' ')}
              </span>
            ))}
            {visibleFlags.length > 3 ? (
              <span className={styles.moreFlags}>+{visibleFlags.length - 3}</span>
            ) : null}
          </span>
        ) : null}
        <span className={styles.triage} aria-label="Lead verification context">
          <span data-kind="scope">{label(lead.scope_class)}</span>
          <span data-kind={lead.source_health === 'fresh' ? 'fresh' : 'verify'}>
            {lead.source_health === 'fresh' ? 'source current' : 'verify source'}
          </span>
          <span data-kind={lead.location_confidence}>{label(lead.location_confidence)}</span>
        </span>
        <span className={styles.sourceLine} title={lead.source_name || ''}>
          {lead.source_name || 'Public property record'} · {label(lead.detail_level)} detail
        </span>
        <span className={styles.verifyLine}>
          {(lead.priority_factors || []).join(' · ') || 'Public-record priority'}
          {lead.verification_required ? ' · verify before outreach' : ''}
        </span>
      </button>
    </li>
  );
});

export function DealPipeline() {
  const { dealPipeline, dealPipelineTotal, dealPipelinePage, marketCoverage } = useOracleState();
  const { wsRef } = useOracleDispatch();
  const [viewMode, setViewMode] = useState('grid');
  const [boardOpen, setBoardOpen] = useState(false);
  const [selected, setSelected] = useState(() => new Set());
  const [query, setQuery] = useState('');
  const [stateFilter, setStateFilter] = useState('all');
  const [priority, setPriority] = useState('all');
  const [scopeFilter, setScopeFilter] = useState('all');
  const [detailFilter, setDetailFilter] = useState('all');
  const [freshnessFilter, setFreshnessFilter] = useState('all');
  const [mapFilter, setMapFilter] = useState('all');
  const [dossierId, setDossierId] = useState(null);
  const [scrollTop, setScrollTop] = useState(0);
  const resultsRef = useRef(null);
  const deferredQuery = useDeferredValue(query.trim().toLowerCase());

  const requestPipeline = useCallback((cursor = null) => {
    if (wsRef.current?.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(JSON.stringify({
      type: 'REQUEST_DEAL_PIPELINE',
      cursor,
      limit: PAGE_SIZE,
      state: stateFilter === 'all' ? '' : stateFilter,
      priority,
      scope: scopeFilter,
      detail: detailFilter,
      freshness: freshnessFilter,
      map_confidence: mapFilter,
      query: deferredQuery,
    }));
  }, [deferredQuery, detailFilter, freshnessFilter, mapFilter, priority, scopeFilter, stateFilter, wsRef]);

  useEffect(() => {
    const timer = window.setTimeout(() => requestPipeline(), deferredQuery ? 220 : 0);
    return () => window.clearTimeout(timer);
  }, [deferredQuery, requestPipeline]);

  const allLeads = useMemo(
    () =>
      (dealPipeline || []).flatMap((group) =>
        group.leads.map((lead) => ({ ...lead, state: group.state }))
      ),
    [dealPipeline]
  );

  const metrics = useMemo(() => {
    let hot = 0;
    let contracts = 0;
    let urgent = 0;
    for (const lead of allLeads) {
      if (Number(lead.motivation_score) >= 70) hot += 1;
      const countdown = contractCountdown(lead);
      if (countdown) {
        contracts += 1;
        if (countdown.zone === 'danger') urgent += 1;
      }
    }
    return { hot, contracts, urgent };
  }, [allLeads]);

  const filteredLeads = useMemo(
    () =>
      allLeads.filter(
        (lead) =>
          (stateFilter === 'all' || lead.state === stateFilter) &&
          matchesPriority(lead, priority) &&
          matchesQuery(lead, deferredQuery)
      ),
    [allLeads, deferredQuery, priority, stateFilter]
  );

  const selectedLeads = useMemo(
    () => allLeads.filter((lead) => selected.has(lead.parcel_id)),
    [allLeads, selected]
  );

  const toggleSelect = useCallback((parcelId) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(parcelId)) next.delete(parcelId);
      else next.add(parcelId);
      return next;
    });
  }, []);

  const clearSelection = useCallback(() => setSelected(new Set()), []);

  const handleRefresh = useCallback(() => {
    requestPipeline();
  }, [requestPipeline]);

  const handleExport = useCallback(() => {
    downloadLeadsCsv(selectedLeads);
  }, [selectedLeads]);

  const changeView = useCallback((mode) => {
    startTransition(() => setViewMode(mode));
  }, []);

  const openDossier = useCallback((leadId) => {
    if (leadId) setDossierId(leadId);
  }, []);

  const clearFilters = () => {
    setQuery('');
    setStateFilter('all');
    setPriority('all');
    setScopeFilter('all');
    setDetailFilter('all');
    setFreshnessFilter('all');
    setMapFilter('all');
  };

  const selectedCount = selected.size;
  const rowHeight = 184;
  const overscan = 8;
  const virtualWindow = useMemo(() => {
    const start = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
    const end = Math.min(filteredLeads.length, start + 32);
    return {
      start,
      end,
      top: start * rowHeight,
      bottom: Math.max(0, (filteredLeads.length - end) * rowHeight),
      leads: filteredLeads.slice(start, end),
    };
  }, [filteredLeads, rowHeight, scrollTop]);
  const filtersActive = Boolean(
    query || stateFilter !== 'all' || priority !== 'all' || scopeFilter !== 'all'
    || detailFilter !== 'all' || freshnessFilter !== 'all' || mapFilter !== 'all'
  );
  const listingLoaded = dealPipelinePage?.loaded === true;
  const listingError = dealPipelinePage?.error || '';
  const isWaiting = !listingLoaded;
  const coverageSummary = useMemo(() => {
    const entries = Object.values(marketCoverage || {});
    return {
      statewide: entries.filter((market) => market.scope_class === 'statewide').length,
      local: entries.filter((market) => ['county', 'city'].includes(market.scope_class)).length,
      verify: entries.filter((market) => market.health !== 'fresh').length,
    };
  }, [marketCoverage]);

  return (
    <section className={styles.panel} aria-labelledby="pipeline-title">
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <div className={styles.titleBlock}>
            <span className={styles.kicker}>Acquisition workspace</span>
            <h2 id="pipeline-title">Deal Pipeline</h2>
            <span className={styles.liveStatus}>
              <span aria-hidden="true" />
              {isWaiting ? 'Waiting for live feed' : `${allLeads.length} records live`}
            </span>
          </div>
          <div className={styles.headerActions}>
            <button
              type="button"
              className={styles.utilityButton}
              onClick={() => setBoardOpen(true)}
              onPointerEnter={() => { void loadPipelineBoard(); }}
              onFocus={() => { void loadPipelineBoard(); }}
            >
              <Columns3 aria-hidden="true" />
              <span>Board</span>
            </button>
            <button
              type="button"
              className={styles.utilityButton}
              onClick={handleRefresh}
            >
              <RefreshCw aria-hidden="true" />
              <span>Refresh</span>
            </button>
          </div>
        </div>

        <dl className={styles.metrics} aria-label="Pipeline overview">
          <div>
            <dt>Total leads</dt>
            <dd>{formatCount(dealPipelineTotal)}</dd>
          </div>
          <div>
            <dt>Public priority</dt>
            <dd>{metrics.hot}</dd>
          </div>
          <div>
            <dt>Active contracts</dt>
            <dd>{metrics.contracts}</dd>
          </div>
          <div data-alert={metrics.urgent > 0}>
            <dt>≤15 day clock</dt>
            <dd>{metrics.urgent}</dd>
          </div>
        </dl>

        <div className={styles.toolbar}>
          <label className={styles.searchField}>
            <Search aria-hidden="true" />
            <span className={styles.srOnly}>Search pipeline</span>
            <input
              type="search"
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
              }}
              placeholder="Address, owner, parcel…"
              autoComplete="off"
            />
          </label>
          <label className={styles.selectField}>
            <span>State</span>
            <select
              value={stateFilter}
              onChange={(event) => {
                setStateFilter(event.target.value);
              }}
              >
              <option value="all">All states + DC</option>
              {US_STATES.map(({ code, name }) => (
                <option key={code} value={code}>{name} ({code})</option>
              ))}
            </select>
          </label>
          <label className={styles.selectField}>
            <span>Focus</span>
            <select
              value={priority}
              onChange={(event) => {
                setPriority(event.target.value);
              }}
            >
              <option value="all">All leads</option>
              <option value="hot">Public-record priority</option>
              <option value="contract">Contract clock</option>
              <option value="distress">Distress signals</option>
            </select>
          </label>
          <div className={styles.viewToggle} role="tablist" aria-label="Pipeline view">
            <button
              type="button"
              role="tab"
              aria-selected={viewMode === 'grid'}
              data-active={viewMode === 'grid'}
              onClick={() => changeView('grid')}
            >
              <LayoutGrid aria-hidden="true" />
              <span>Grid</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={viewMode === 'map'}
              data-active={viewMode === 'map'}
              onClick={() => changeView('map')}
              onPointerEnter={() => { void loadLeadMap(); }}
              onFocus={() => { void loadLeadMap(); }}
            >
              <MapIcon aria-hidden="true" />
              <span>Map</span>
            </button>
          </div>
        </div>

        <details className={styles.agentFilters}>
          <summary>Agent verification filters</summary>
          <div>
            <label className={styles.selectField}>
              <span>Coverage</span>
              <select value={scopeFilter} onChange={(event) => setScopeFilter(event.target.value)}>
                <option value="all">Any scope</option>
                <option value="statewide">Statewide</option>
                <option value="county">County source</option>
                <option value="city">City source</option>
                <option value="geometry_only">Geometry only</option>
              </select>
            </label>
            <label className={styles.selectField}>
              <span>Record quality</span>
              <select value={detailFilter} onChange={(event) => setDetailFilter(event.target.value)}>
                <option value="all">Any detail</option>
                <option value="comprehensive">Comprehensive</option>
                <option value="standard">Standard</option>
                <option value="limited">Limited</option>
              </select>
            </label>
            <label className={styles.selectField}>
              <span>Freshness</span>
              <select value={freshnessFilter} onChange={(event) => setFreshnessFilter(event.target.value)}>
                <option value="all">Any age</option>
                <option value="fresh">Fresh source record</option>
                <option value="verify">Verify before use</option>
              </select>
            </label>
            <label className={styles.selectField}>
              <span>Map confidence</span>
              <select value={mapFilter} onChange={(event) => setMapFilter(event.target.value)}>
                <option value="all">Any location</option>
                <option value="source_coordinate">Source coordinate</option>
                <option value="address_approximation">Address approximation</option>
                <option value="unmapped">No usable map point</option>
              </select>
            </label>
          </div>
        </details>

        <div className={styles.resultBar} aria-live="polite">
          <span>
            Loaded <strong>{formatCount(filteredLeads.length)}</strong> of{' '}
            <strong>{formatCount(dealPipelineTotal)}</strong> matching records
          </span>
          {filtersActive ? (
            <button type="button" onClick={clearFilters}>Clear filters</button>
          ) : (
            <span>{coverageSummary.statewide} statewide · {coverageSummary.local} local · {coverageSummary.verify} verify source</span>
          )}
        </div>
      </header>

      {isWaiting ? (
        <div className={styles.empty} role="status">
          <strong>Connecting to the live pipeline</strong>
          <span>Records will appear as soon as the secure feed responds.</span>
          <button type="button" onClick={handleRefresh}>Request refresh</button>
        </div>
      ) : listingError ? (
        <div className={styles.empty} role="alert">
          <strong>The lead listing could not be refreshed</strong>
          <span>Existing records remain private. Request the current listing again.</span>
          <button type="button" onClick={handleRefresh}>Try again</button>
        </div>
      ) : filteredLeads.length === 0 ? (
        <div className={styles.empty} role="status">
          <strong>No matching leads</strong>
          <span>Try a broader search or reset the current filters.</span>
          <button type="button" onClick={clearFilters}>Clear filters</button>
        </div>
      ) : viewMode === 'map' ? (
        <Suspense fallback={<div className={styles.modeLoading}>Preparing map…</div>}>
          <LeadMap
            leads={filteredLeads}
            selected={selected}
            onToggle={toggleSelect}
            onOpen={openDossier}
          />
        </Suspense>
      ) : (
        <div
          ref={resultsRef}
          className={`${styles.results} ${styles.virtualResults}`}
          onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
          aria-label="Virtualized lead results"
        >
          <div style={{ height: virtualWindow.top }} aria-hidden="true" />
          <ul className={styles.leadList}>
            {virtualWindow.leads.map((lead) => (
              <LeadCard
                key={lead.id || lead.parcel_id}
                lead={lead}
                selected={selected.has(lead.parcel_id)}
                onToggle={toggleSelect}
                onOpen={openDossier}
              />
            ))}
          </ul>
          <div style={{ height: virtualWindow.bottom }} aria-hidden="true" />
          {dealPipelinePage.hasMore ? (
            <button
              type="button"
              className={styles.loadMore}
              onClick={() => requestPipeline(dealPipelinePage.nextCursor)}
            >
              Load next {PAGE_SIZE}
              <span>{Math.max(0, dealPipelineTotal - filteredLeads.length)} remaining</span>
            </button>
          ) : null}
        </div>
      )}

      {dossierId ? (
        <DossierPanel leadId={dossierId} onClose={() => setDossierId(null)} />
      ) : null}

      {boardOpen ? (
        <Suspense fallback={<div className={styles.modeLoading}>Preparing board…</div>}>
          <PipelineBoard onClose={() => setBoardOpen(false)} onOpen={openDossier} />
        </Suspense>
      ) : null}

      <div
        className={styles.actionBar}
        data-visible={selectedCount > 0}
        role="toolbar"
        aria-label="Bulk lead actions"
        aria-hidden={selectedCount === 0}
      >
        <span className={styles.actionCount}>
          <strong>{selectedCount}</strong> selected
        </span>
        <div className={styles.actionButtons}>
          <button
            type="button"
            className={styles.actionGhost}
            onClick={clearSelection}
            tabIndex={selectedCount > 0 ? 0 : -1}
          >
            Clear
          </button>
          <button
            type="button"
            className={styles.actionPrimary}
            onClick={handleExport}
            tabIndex={selectedCount > 0 ? 0 : -1}
          >
            Export CSV
          </button>
        </div>
      </div>
    </section>
  );
}
