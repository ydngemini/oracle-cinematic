import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useOracleState, useOracleDispatch } from '../state';
import { contractCountdown } from './DealPipeline';
import { DossierPanel } from './DossierPanel';
import styles from './PipelineBoard.module.css';

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

// Columns mirror the 0007 dossier_status state machine exactly — the CHECK
// constraint is the source of truth, these are just display labels.
const STAGES = [
  { id: 'draft', title: 'Intake' },
  { id: 'under_contract', title: 'Under Contract' },
  { id: 'marketing', title: 'Disposition' },
  { id: 'assigned', title: 'Assigned' },
  { id: 'closed', title: 'Closed' },
  { id: 'expired', title: 'Expired' },
  { id: 'dead', title: 'Dead' },
];

function formatValue(v) {
  const n = Number(v);
  if (!n || Number.isNaN(n)) return '—';
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `$${Math.round(n / 1_000)}K`;
  return `$${n.toLocaleString()}`;
}

export function PipelineBoard({ onClose }) {
  const { dealPipeline } = useOracleState();
  const { wsRef } = useOracleDispatch();

  // Optimistic per-lead status overrides — applied until the next
  // DEAL_PIPELINE refresh arrives, then the server truth wins.
  const [overrides, setOverrides] = useState({});
  const [dragId, setDragId] = useState(null);
  const [hoverCol, setHoverCol] = useState(null);
  const [error, setError] = useState('');
  const [dossierId, setDossierId] = useState(null);
  // A drag that just ended must not register as a card click.
  const dragJustEnded = useRef(false);

  useEffect(() => setOverrides({}), [dealPipeline]);

  // Esc closes the dossier first, then the board.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'Escape') return;
      if (dossierId) setDossierId(null);
      else onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, dossierId]);

  // Move keyboard focus into the dialog on open.
  const closeRef = useRef(null);
  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  // Keyboard alternative to drag-and-drop (WCAG 2.1.1): focus a card,
  // ArrowLeft/ArrowRight moves it one lane over.
  const moveByKey = (e, lead, stageIndex) => {
    if (e.key === 'ArrowRight' && stageIndex < STAGES.length - 1) {
      e.preventDefault();
      moveLead(lead.id, STAGES[stageIndex + 1].id);
    } else if (e.key === 'ArrowLeft' && stageIndex > 0) {
      e.preventDefault();
      moveLead(lead.id, STAGES[stageIndex - 1].id);
    }
  };

  const leads = useMemo(
    () =>
      (dealPipeline || []).flatMap((g) =>
        g.leads.map((l) => ({
          ...l,
          geo_state: g.state,
          dossier_status: overrides[l.id] || l.dossier_status || 'draft',
        }))
      ),
    [dealPipeline, overrides]
  );

  const moveLead = async (leadId, status) => {
    setError('');
    const prev = leads.find((l) => l.id === leadId)?.dossier_status;
    if (!leadId || prev === status) return;

    setOverrides((o) => ({ ...o, [leadId]: status }));
    try {
      const token = sessionStorage.getItem('oracle_token');
      const res = await fetch(`${API_BASE}/api/leads/${leadId}/dossier-status`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ status }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail || `move failed (${res.status})`);
      }
      // Pull fresh truth — the DEAL_PIPELINE frame clears the override.
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: 'REQUEST_DEAL_PIPELINE' }));
      }
    } catch (err) {
      setOverrides((o) => {
        const next = { ...o };
        delete next[leadId];
        return next;
      });
      setError(String(err.message || err));
    }
  };

  const board = (
    <div className={styles.backdrop} role="dialog" aria-modal="true" aria-label="Pipeline board">
      <header className={styles.chrome}>
        <div>
          <span className={styles.kicker}>Acquisition Pipeline</span>
          <span className={styles.hint}>drag to mutate dossier state</span>
        </div>
        <div className={styles.chromeRight}>
          {error && <span className={styles.error}>{error}</span>}
          <button
            type="button"
            ref={closeRef}
            className={styles.close}
            onClick={onClose}
            aria-label="Close board"
          >
            ✕
          </button>
        </div>
      </header>

      <div className={styles.lanes}>
        {STAGES.map((stage, stageIndex) => {
          const cards = leads.filter((l) => l.dossier_status === stage.id);
          return (
            <section
              key={stage.id}
              className={styles.lane}
              data-stage={stage.id}
              data-hover={hoverCol === stage.id && dragId != null}
              aria-label={`${stage.title} — ${cards.length} assets`}
              onDragOver={(e) => {
                e.preventDefault();
                setHoverCol(stage.id);
              }}
              onDragLeave={() => setHoverCol(null)}
              onDrop={(e) => {
                e.preventDefault();
                setHoverCol(null);
                const id = e.dataTransfer.getData('text/lead-id') || dragId;
                if (id) moveLead(id, stage.id);
                setDragId(null);
              }}
            >
              <header className={styles.laneHeader}>
                <span className={styles.laneTitle}>{stage.title}</span>
                <span className={styles.laneCount}>{cards.length}</span>
              </header>

              <div className={styles.laneBody}>
                {cards.map((lead) => {
                  const countdown = contractCountdown(lead);
                  return (
                    <article
                      key={lead.id || lead.parcel_id}
                      className={styles.card}
                      draggable={!!lead.id}
                      data-dragging={dragId === lead.id}
                      tabIndex={lead.id ? 0 : -1}
                      role="button"
                      aria-roledescription="deal card"
                      aria-label={`${lead.address || lead.parcel_id} — ${stage.title}. Enter opens the dossier; arrow keys move between stages.`}
                      onClick={() => {
                        if (dragJustEnded.current || !lead.id) return;
                        setDossierId(lead.id);
                      }}
                      onKeyDown={(e) => {
                        if (!lead.id) return;
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          setDossierId(lead.id);
                        } else {
                          moveByKey(e, lead, stageIndex);
                        }
                      }}
                      onDragStart={(e) => {
                        e.dataTransfer.setData('text/lead-id', lead.id);
                        e.dataTransfer.effectAllowed = 'move';
                        setDragId(lead.id);
                      }}
                      onDragEnd={() => {
                        setDragId(null);
                        dragJustEnded.current = true;
                        setTimeout(() => {
                          dragJustEnded.current = false;
                        }, 0);
                      }}
                    >
                      <p className={styles.cardAddress} title={lead.address}>
                        {lead.address || lead.parcel_id}
                      </p>
                      <div className={styles.cardMeta}>
                        <span className={styles.cardValue}>
                          {formatValue(lead.estimated_value)}
                        </span>
                        <span className={styles.cardGeo}>{lead.geo_state}</span>
                        {countdown && (
                          <span
                            className={styles.cardClock}
                            data-zone={countdown.zone}
                            title={`Assignment window — ${countdown.days} days remaining`}
                          >
                            {countdown.days}d
                          </span>
                        )}
                      </div>
                    </article>
                  );
                })}
                {cards.length === 0 && <p className={styles.laneEmpty}>—</p>}
              </div>
            </section>
          );
        })}
      </div>

      {dossierId && (
        <DossierPanel leadId={dossierId} onClose={() => setDossierId(null)} />
      )}
    </div>
  );

  // Portal to <body>: the rightPanel's entrance transform would otherwise
  // turn position:fixed into position:absolute-within-panel.
  return createPortal(board, document.body);
}
