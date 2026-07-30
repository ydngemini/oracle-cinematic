import { useEffect, useRef, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import styles from './AssistantShell.module.css';

const LABELS = { client: 'Client', lead: 'Lead', listing: 'Listing', contract: 'Contract' };

export function AssistantRecordPicker({ onSelect, onClose }) {
  const [query, setQuery] = useState('');
  const [records, setRecords] = useState([]);
  const [state, setState] = useState('loading');
  const inputRef = useRef(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    let active = true;
    const timer = window.setTimeout(() => {
      setState('loading');
      crmGet(`/api/ai/chat/records?q=${encodeURIComponent(query.trim())}&limit=48`).then(
        (data) => {
          if (!active) return;
          setRecords(Array.isArray(data?.records) ? data.records : []);
          setState('ready');
        },
        () => { if (active) setState('error'); },
      );
    }, 180);
    return () => { active = false; window.clearTimeout(timer); };
  }, [query]);

  return (
    <div className={styles.recordPicker} role="dialog" aria-label="Select a record">
      <div className={styles.pickerTop}>
        <div>
          <span className={styles.eyebrow}>Ground the conversation</span>
          <strong>Select a record</strong>
        </div>
        <button type="button" className={styles.iconButton} onClick={onClose} aria-label="Close record picker">×</button>
      </div>
      <label className={styles.searchField}>
        <span className={styles.srOnly}>Search clients, listings, leads, or contracts</span>
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/></svg>
        <input
          ref={inputRef}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search records"
          maxLength={160}
        />
      </label>
      <div className={styles.recordList}>
        {state === 'loading' && <p className={styles.pickerState}>Finding your records…</p>}
        {state === 'error' && <p className={styles.pickerState} role="alert">Records are unavailable right now.</p>}
        {state === 'ready' && records.length === 0 && <p className={styles.pickerState}>No matching records.</p>}
        {state === 'ready' && records.map((record) => (
          <button
            type="button"
            className={styles.recordOption}
            key={`${record.type}:${record.id}`}
            onClick={() => onSelect(record)}
          >
            <span className={styles.recordSigil} data-kind={record.type} aria-hidden="true">
              {(LABELS[record.type] || 'R').slice(0, 1)}
            </span>
            <span className={styles.recordCopy}>
              <strong>{record.label}</strong>
              <small>{LABELS[record.type] || record.type}{record.detail ? ` · ${String(record.detail).replaceAll('_', ' ')}` : ''}</small>
            </span>
            <span aria-hidden="true">›</span>
          </button>
        ))}
      </div>
    </div>
  );
}
