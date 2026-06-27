import React, { useState, useEffect } from 'react';
import styles from './HarvesterDashboard.module.css';

export default function HarvesterDashboard() {
  const [harvesters, setHarvesters] = useState([
    { id: 'wy_parcels', name: 'Wyoming Parcels', status: 'active', lastRun: '2 mins ago', records: 373666 },
    { id: 'ct_sales', name: 'CT Sales', status: 'idle', lastRun: '1 hour ago', records: 15420 },
    { id: 'il_cook', name: 'IL Cook County', status: 'error', lastRun: '5 hours ago', records: 0 },
  ]);

  return (
    <div className={styles.dashboardContainer}>
      <header className={styles.header}>
        <h2>Data Harvester Pipeline</h2>
        <span className={styles.badge}>Live Sync</span>
      </header>
      
      <div className={styles.grid}>
        {harvesters.map(h => (
          <div key={h.id} className={`${styles.card} ${styles[h.status]}`}>
            <div className={styles.cardHeader}>
              <h3>{h.name}</h3>
              <div className={`${styles.statusIndicator} ${styles[h.status + 'Indicator']}`}></div>
            </div>
            <div className={styles.cardBody}>
              <p><strong>Status:</strong> {h.status.toUpperCase()}</p>
              <p><strong>Last Run:</strong> {h.lastRun}</p>
              <p><strong>Records:</strong> {h.records.toLocaleString()}</p>
            </div>
            <div className={styles.cardFooter}>
              <button className={styles.actionBtn}>
                {h.status === 'active' ? 'Stop' : 'Run'}
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
