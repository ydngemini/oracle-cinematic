import { useMemo } from 'react';
import styles from './LeadMap.module.css';

// Minimalist spatial-view scaffold. No mapping library (Oracle convention).
// Until real geocoding is wired, markers are positioned deterministically from
// the parcel_id hash so the layout is stable across renders — a clean canvas
// ready to be swapped for true lat/lng plotting later.
function placement(parcelId) {
  let h = 0;
  for (let i = 0; i < parcelId.length; i += 1) {
    h = (h * 31 + parcelId.charCodeAt(i)) >>> 0;
  }
  const x = 8 + ((h % 1000) / 1000) * 84; // 8%–92%
  const y = 10 + (((h >> 10) % 1000) / 1000) * 78; // 10%–88%
  return { left: `${x}%`, top: `${y}%` };
}

function tier(score) {
  if (score >= 70) return 'hot';
  if (score >= 40) return 'warm';
  return 'cool';
}

export function LeadMap({ leads, selected, onToggle }) {
  const markers = useMemo(
    () => leads.map((lead) => ({ ...lead, pos: placement(lead.parcel_id) })),
    [leads]
  );

  return (
    <div className={styles.map} role="group" aria-label="Spatial lead map">
      <div className={styles.grid} aria-hidden="true" />
      {markers.length === 0 && (
        <p className={styles.empty}>No leads to plot</p>
      )}
      {markers.map((m) => (
        <button
          key={m.parcel_id}
          type="button"
          className={styles.marker}
          data-tier={tier(m.motivation_score)}
          data-selected={selected.has(m.parcel_id)}
          style={m.pos}
          onClick={() => onToggle(m.parcel_id)}
          aria-pressed={selected.has(m.parcel_id)}
          aria-label={`${m.address || m.parcel_id}, motivation ${m.motivation_score}`}
          title={`${m.address || m.parcel_id} · ${m.motivation_score}`}
        />
      ))}
      <span className={styles.scaffoldNote}>SPATIAL · SCAFFOLD</span>
    </div>
  );
}
