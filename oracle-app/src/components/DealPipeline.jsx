import { useCallback, useMemo, useState } from 'react';
import { useOracleState, useOracleDispatch } from '../state';
import { LeadMap } from './LeadMap';
import { downloadLeadsCsv } from '../utils/csv';
import styles from './DealPipeline.module.css';

// Motivation tiers drive the badge color. Kept in sync with the backend's
// 0–100 motivation_score (see harvesters/md_sdat_ingest.py _motivation_score).
function scoreTier(score) {
  if (score >= 70) return 'hot';
  if (score >= 40) return 'warm';
  return 'cool';
}

function formatValue(v) {
  if (!v || Number.isNaN(Number(v))) return '—';
  const n = Number(v);
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `$${Math.round(n / 1_000)}K`;
  return `$${n.toLocaleString()}`;
}

const OWNER_LABEL = { corporate: 'CORP', trust: 'TRUST', individual: 'INDIV' };

export function DealPipeline() {
  const { dealPipeline, dealPipelineTotal } = useOracleState();
  const { wsRef } = useOracleDispatch();
  const [viewMode, setViewMode] = useState('grid');
  const [selected, setSelected] = useState(() => new Set());

  // Flatten groups once, tagging each lead with its state for export/selection.
  const allLeads = useMemo(
    () =>
      (dealPipeline || []).flatMap((g) =>
        g.leads.map((l) => ({ ...l, state: g.state }))
      ),
    [dealPipeline]
  );

  const selectedLeads = useMemo(
    () => allLeads.filter((l) => selected.has(l.parcel_id)),
    [allLeads, selected]
  );

  const toggleSelect = useCallback((parcelId) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(parcelId)) next.delete(parcelId);
      else next.add(parcelId);
      return next;
    });
  }, []);

  const clearSelection = useCallback(() => setSelected(new Set()), []);

  const handleRefresh = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'REQUEST_DEAL_PIPELINE' }));
    }
  }, [wsRef]);

  const handleExport = useCallback(() => {
    downloadLeadsCsv(selectedLeads);
  }, [selectedLeads]);

  const handleArchive = useCallback(() => {
    // Best-effort archive signal; backend handler is a future slice. Clears the
    // local selection so the Action Bar retracts.
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(
        JSON.stringify({
          type: 'ARCHIVE_LEADS',
          parcel_ids: selectedLeads.map((l) => l.parcel_id),
        })
      );
    }
    clearSelection();
  }, [wsRef, selectedLeads, clearSelection]);

  const isEmpty = !dealPipeline || dealPipeline.length === 0;
  const selectedCount = selected.size;

  return (
    <section className={styles.panel} aria-label="Deal pipeline">
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <span className={styles.kicker}>Deal Pipeline</span>
          <button
            type="button"
            className={styles.refresh}
            onClick={handleRefresh}
            aria-label="Refresh deal pipeline"
            title="Refresh"
          >
            ↻
          </button>
        </div>

        <h2 className={styles.headline}>
          {dealPipelineTotal}
          <span className={styles.headlineUnit}>
            {dealPipelineTotal === 1 ? 'lead' : 'leads'} · {dealPipeline.length}{' '}
            {dealPipeline.length === 1 ? 'state' : 'states'}
          </span>
        </h2>

        <div className={styles.viewToggle} role="tablist" aria-label="View mode">
          <span
            className={styles.toggleThumb}
            data-mode={viewMode}
            aria-hidden="true"
          />
          <button
            type="button"
            role="tab"
            aria-selected={viewMode === 'grid'}
            className={styles.toggleOption}
            data-active={viewMode === 'grid'}
            onClick={() => setViewMode('grid')}
          >
            Grid
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={viewMode === 'map'}
            className={styles.toggleOption}
            data-active={viewMode === 'map'}
            onClick={() => setViewMode('map')}
          >
            Map
          </button>
        </div>
      </header>

      {isEmpty ? (
        <p className={styles.empty}>Awaiting leads from the firehose…</p>
      ) : viewMode === 'map' ? (
        <LeadMap leads={allLeads} selected={selected} onToggle={toggleSelect} />
      ) : (
        <div className={styles.scroll}>
          {dealPipeline.map((group) => (
            <div key={group.state} className={styles.stateGroup}>
              <div className={styles.stateHeader}>
                <span className={styles.stateName}>{group.state}</span>
                <span className={styles.stateCount}>{group.count}</span>
              </div>

              <ul className={styles.leadList}>
                {group.leads.map((lead) => {
                  const isSel = selected.has(lead.parcel_id);
                  return (
                    <li key={lead.parcel_id}>
                      <button
                        type="button"
                        className={styles.leadCard}
                        data-selected={isSel}
                        aria-pressed={isSel}
                        onClick={() => toggleSelect(lead.parcel_id)}
                      >
                        <span className={styles.leadTop}>
                          <span className={styles.leadAddress} title={lead.address}>
                            {lead.address || lead.parcel_id}
                          </span>
                          <span
                            className={styles.scoreBadge}
                            data-tier={scoreTier(lead.motivation_score)}
                            aria-label={`Motivation ${lead.motivation_score} of 100`}
                          >
                            {lead.motivation_score}
                          </span>
                        </span>

                        <span className={styles.leadMeta}>
                          <span className={styles.ownerChip} data-type={lead.owner_type}>
                            {OWNER_LABEL[lead.owner_type] || 'INDIV'}
                          </span>
                          <span className={styles.ownerName} title={lead.owner_name}>
                            {lead.owner_name || '—'}
                          </span>
                          <span className={styles.leadValue}>
                            {formatValue(lead.estimated_value)}
                          </span>
                        </span>

                        {(lead.is_absentee_owner || lead.distress_flags?.length > 0) && (
                          <span className={styles.flags}>
                            {lead.is_absentee_owner && (
                              <span className={styles.flag} data-flag="absentee">
                                absentee
                              </span>
                            )}
                            {lead.distress_flags
                              ?.filter((f) => f !== 'absentee_owner')
                              .map((flag) => (
                                <span key={flag} className={styles.flag} data-flag={flag}>
                                  {flag.replace(/_/g, ' ')}
                                </span>
                              ))}
                          </span>
                        )}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </div>
      )}

      {/* Floating Action Bar — frosted glass, spring-slides up from the viewport
          floor whenever a selection exists. Always mounted so the retract also
          animates; visibility + pointer-events gate on the selection count. */}
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
            className={styles.actionGhost}
            onClick={handleArchive}
            tabIndex={selectedCount > 0 ? 0 : -1}
          >
            Archive
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
