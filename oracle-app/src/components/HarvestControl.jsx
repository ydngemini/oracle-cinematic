import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPost, crmPut } from '../state/useCrmApi';
import styles from './HarvestControl.module.css';

const integer = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });

function duration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return 'never';
  if (value < 60) return `${Math.round(value)}s`;
  if (value < 3600) return `${Math.round(value / 60)}m`;
  if (value < 86400) return `${Math.round(value / 3600)}h`;
  return `${Math.round(value / 86400)}d`;
}

function title(value) {
  return String(value || 'unknown').replaceAll('_', ' ');
}

export function HarvestControl() {
  const [feed, setFeed] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState('');
  const [rerun, setRerun] = useState(null);
  const [reason, setReason] = useState('Manual source verification and controlled refresh.');

  const load = useCallback(() => Promise.all([
    crmGet('/api/harvests'),
    crmGet('/api/harvests/jobs?limit=20'),
  ]).then(
    ([status, jobData]) => {
      setFeed(status);
      setJobs(Array.isArray(jobData?.jobs) ? jobData.jobs : []);
      setError(null);
    },
    setError,
  ), []);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 30_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const toggle = (source) => {
    setBusy(`schedule:${source.source_key}`);
    crmPut(`/api/harvests/${source.source_key}/schedule`, {
      enabled: !source.enabled,
      schedule_seconds: Number(source.schedule_seconds || source.default_schedule_seconds || 86400),
    }).then(load).catch(setError).finally(() => setBusy(''));
  };

  const submitRerun = (event) => {
    event.preventDefault();
    if (!rerun || reason.trim().length < 8) return;
    setBusy(`rerun:${rerun}`);
    crmPost(`/api/harvests/${rerun}/rerun`, {
      reason: reason.trim(), max_records: null, state_codes: [],
    }).then(() => {
      setRerun(null);
      return load();
    }).catch(setError).finally(() => setBusy(''));
  };

  return (
    <section className={styles.wrap} aria-labelledby="harvest-control-title" aria-busy={!feed || Boolean(busy)}>
      <header className={styles.header}>
        <div>
          <span className={styles.kicker}>Municipal data plane</span>
          <h2 id="harvest-control-title">Harvest control</h2>
        </div>
        <button type="button" onClick={load} className={styles.refresh}>Refresh</button>
      </header>

      {error && <p className={styles.error} role="alert">{error.message || 'Harvest status is unavailable.'}</p>}

      {!feed ? <div className={styles.skeleton} aria-hidden="true" /> : (
        <>
          <div className={styles.scheduler}>
            <span className={styles.statusDot} data-state={feed.scheduler?.running ? 'running' : 'stopped'} aria-hidden="true" />
            <div><strong>Durable scheduler</strong><small>{feed.scheduler?.running ? 'PostgreSQL leases active' : 'worker not reporting'}</small></div>
          </div>

          <ul className={styles.sources}>
            {(feed.sources || []).map((source) => (
              <li key={source.source_key} className={styles.source}>
                <header>
                  <div><strong>{source.display_name || title(source.source_key)}</strong><small>{source.jurisdiction || 'public records'}</small></div>
                  <span className={styles.circuit} data-state={source.circuit_state || 'closed'}>{title(source.circuit_state || 'closed')}</span>
                </header>
                <dl className={styles.stats}>
                  <div><dt>Freshness</dt><dd>{duration(source.source_freshness_seconds)}</dd></div>
                  <div><dt>Cursor age</dt><dd>{duration(source.cursor_age_seconds)}</dd></div>
                  <div><dt>Cache saved</dt><dd>{source.cache_savings_rate == null ? '—' : `${Math.round(Number(source.cache_savings_rate) * 100)}%`}</dd></div>
                  <div><dt>Failures</dt><dd>{integer.format(Number(source.failure_count) || 0)}</dd></div>
                  <div><dt>Fetched</dt><dd>{integer.format(Number(source.latest_fetched) || 0)}</dd></div>
                  <div><dt>Inserted</dt><dd>{integer.format(Number(source.latest_inserted) || 0)}</dd></div>
                </dl>
                {source.latest_error_summary || source.last_error ? <p className={styles.sourceError}>{source.latest_error_summary || source.last_error}</p> : null}
                <footer>
                  <button type="button" onClick={() => toggle(source)} disabled={Boolean(busy)}>
                    {source.enabled ? 'Pause schedule' : 'Enable schedule'}
                  </button>
                  <button type="button" onClick={() => setRerun(source.source_key)} disabled={Boolean(busy)}>
                    Controlled rerun
                  </button>
                </footer>
              </li>
            ))}
          </ul>
        </>
      )}

      {rerun && (
        <form className={styles.rerun} onSubmit={submitRerun}>
          <h3>Rerun {title(rerun)}</h3>
          <p>This creates a durable, idempotent job and records your reason.</p>
          <label>
            <span>Reason</span>
            <textarea value={reason} onChange={(event) => setReason(event.target.value)} rows={3} minLength={8} maxLength={500} required />
          </label>
          <div>
            <button type="button" onClick={() => setRerun(null)}>Cancel</button>
            <button type="submit" disabled={busy === `rerun:${rerun}` || reason.trim().length < 8}>Queue rerun</button>
          </div>
        </form>
      )}

      <section className={styles.jobs} aria-labelledby="harvest-jobs-title">
        <header><h3 id="harvest-jobs-title">Recent jobs</h3><span>{jobs.length}</span></header>
        {jobs.length === 0 ? <p>No harvest jobs recorded.</p> : (
          <ul>
            {jobs.slice(0, 8).map((job) => (
              <li key={job.id}>
                <div><strong>{title(job.job_type)}</strong><small>{job.message || `attempt ${job.attempt || 0}`}</small></div>
                <span data-state={job.state}>{title(job.state)} · {Number(job.progress || 0)}%</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </section>
  );
}
