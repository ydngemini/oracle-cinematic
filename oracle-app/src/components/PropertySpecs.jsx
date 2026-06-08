import { useRef, useEffect, useState, useCallback } from 'react';
import { useOracleState, useOracleDispatch, ACTIONS } from '../state';
import styles from './PropertySpecs.module.css';

const HERO_CARDS = [
  { key: 'squareFootage', label: 'Square Footage', format: (v) => v.toLocaleString(), unit: 'ft²' },
  { key: 'price', label: 'Price', format: (v) => `$${v.toLocaleString()}`, unit: '' },
  { key: 'novelty', label: 'Novelty', format: (v) => v.toFixed ? v.toFixed(2) : v, unit: 'score' },
];

const DETAIL_FIELDS = [
  { key: 'address', label: 'Address', format: (v) => v || '—' },
  { key: 'bedrooms', label: 'Bed', format: (v) => v },
  { key: 'bathrooms', label: 'Bath', format: (v) => v },
];

export function PropertySpecs() {
  const { propertyData, isFurnished, isAiAppraisalMode, manualComps } = useOracleState();
  const { dispatch, wsRef } = useOracleDispatch();
  const [revealed, setRevealed] = useState(false);
  const prevDataRef = useRef(propertyData);

  const handleToggleMode = useCallback(() => {
    dispatch({ type: ACTIONS.TOGGLE_APPRAISAL_MODE });
    if (isAiAppraisalMode && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'REQUEST_MANUAL_COMPS',
        address: propertyData.address,
        sqft: propertyData.squareFootage,
        bedrooms: propertyData.bedrooms,
      }));
    }
  }, [dispatch, isAiAppraisalMode, wsRef, propertyData]);

  useEffect(() => {
    const prev = prevDataRef.current;
    const changed = prev.squareFootage !== propertyData.squareFootage ||
      prev.price !== propertyData.price ||
      prev.novelty !== propertyData.novelty;

    prevDataRef.current = propertyData;

    if (changed) {
      // Animate out then in for property transitions.
      setRevealed(false);
      const frame = requestAnimationFrame(() => {
        setRevealed(true);
      });
      return () => cancelAnimationFrame(frame);
    }

    // First-data-arrives: reveal without the false→true flicker of the
    // changed-data branch. Use a 0ms timer so setState is inside a callback
    // (not synchronously in the effect body) — satisfies react-hooks/set-state-in-effect.
    if (propertyData.squareFootage || propertyData.price || propertyData.novelty) {
      const tid = setTimeout(() => setRevealed(true), 0);
      return () => clearTimeout(tid);
    }
  }, [propertyData]);

  return (
    <div className={styles.panel}>
      <header className={styles.header}>
        <span className={styles.kicker}>Property Intelligence</span>
        <h2 className={styles.headline}>
          {propertyData.address || 'Awaiting Data'}
        </h2>
      </header>

      <div className={styles.modeToggle} onClick={handleToggleMode}>
        <span
          className={styles.modeLabel}
          data-active={isAiAppraisalMode}
        >
          AI AUTONOMOUS
        </span>
        <div className={styles.toggleTrack} data-mode={isAiAppraisalMode ? 'ai' : 'manual'}>
          <div className={styles.toggleThumb} />
        </div>
        <span
          className={styles.modeLabel}
          data-active={!isAiAppraisalMode}
        >
          MANUAL COMPS
        </span>
      </div>

      <div className={styles.heroGrid}>
        {HERO_CARDS.map(({ key, label, format, unit }, i) => (
          <div
            key={key}
            className={styles.heroCard}
            data-revealed={revealed}
            style={{ transitionDelay: `${i * 0.12}s` }}
          >
            <span className={styles.cardLabel}>{label}</span>
            <span className={styles.cardValue}>{format(propertyData[key])}</span>
            {unit && <span className={styles.cardUnit}>{unit}</span>}
          </div>
        ))}
      </div>

      <div className={styles.detailStrip}>
        {DETAIL_FIELDS.map(({ key, label, format }) => (
          <div key={key} className={styles.detailItem}>
            <span className={styles.detailLabel}>{label}</span>
            <span className={styles.detailValue}>{format(propertyData[key])}</span>
          </div>
        ))}
      </div>

      <div className={styles.statusBar}>
        <span className={styles.statusLabel}>Render Mode</span>
        <span className={styles.statusBadge} data-active={isFurnished}>
          {isFurnished ? 'FURNISHED' : 'SHELL'}
        </span>
      </div>

      {!isAiAppraisalMode && (
        <div className={styles.compsSection}>
          <span className={styles.compsSectionTitle}>Recent Sale Comps</span>
          {manualComps.length === 0 && (
            <span className={styles.compsLoading}>Querying graph...</span>
          )}
          {manualComps.map((comp, i) => (
            <div key={i} className={styles.compCard}>
              <div className={styles.compAddress}>{comp.address}</div>
              <div className={styles.compRow}>
                <span className={styles.compStat}>
                  <span className={styles.compStatLabel}>Sold</span>
                  <span className={styles.compStatValue}>${comp.sale_price?.toLocaleString()}</span>
                </span>
                <span className={styles.compStat}>
                  <span className={styles.compStatLabel}>$/ft²</span>
                  <span className={styles.compStatValue}>${comp.price_per_sqft}</span>
                </span>
                <span className={styles.compStat}>
                  <span className={styles.compStatLabel}>Dist</span>
                  <span className={styles.compStatValue}>{comp.distance_miles} mi</span>
                </span>
              </div>
              <div className={styles.compRow}>
                <span className={styles.compStat}>
                  <span className={styles.compStatLabel}>Sqft</span>
                  <span className={styles.compStatValue}>{comp.sqft?.toLocaleString()}</span>
                </span>
                <span className={styles.compStat}>
                  <span className={styles.compStatLabel}>Bed</span>
                  <span className={styles.compStatValue}>{comp.bedrooms}</span>
                </span>
                <span className={styles.compStat}>
                  <span className={styles.compStatLabel}>Date</span>
                  <span className={styles.compStatValue}>{comp.sale_date}</span>
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
