import { useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import styles from './ServiceStatusBanner.module.css';

// What is degraded right now, in product language — "Listing data may be out
// of date", never "Bridge HTTP 503". The server (component_health) decides the
// wording; this only shows it, quietly, and hides itself when all is well.
const POLL_MS = 60_000;
// UX-001: one failed status check stays quiet (a blip), but a status that
// cannot be read for this many checks in a row is itself worth saying —
// otherwise "no banner" read as "all is well" while nothing was known.
export const UNKNOWN_AFTER_FAILURES = 3;
export const STATUS_UNKNOWN = 'Service status can’t be checked right now.';

export function ServiceStatusBanner() {
  const [messages, setMessages] = useState([]);
  const [failures, setFailures] = useState(0);

  useEffect(() => {
    let alive = true;
    const load = () => crmGet('/api/status')
      .then((data) => {
        if (!alive) return;
        setFailures(0);
        setMessages(Array.isArray(data?.messages) ? data.messages : []);
      })
      .catch(() => { if (alive) setFailures((n) => n + 1); });
    load();
    const id = setInterval(load, POLL_MS + Math.floor(Math.random() * 10_000));
    return () => { alive = false; clearInterval(id); };
  }, []);

  const shown = failures >= UNKNOWN_AFTER_FAILURES ? [STATUS_UNKNOWN] : messages;
  if (!shown.length) return null;
  return (
    <div className={styles.banner} role="status" aria-live="polite" data-service-banner="">
      {shown.map((m) => <span key={m}>{m}</span>)}
    </div>
  );
}

export default ServiceStatusBanner;
