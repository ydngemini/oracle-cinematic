import { useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import styles from './ServiceStatusBanner.module.css';

// What is degraded right now, in product language — "Listing data may be out
// of date", never "Bridge HTTP 503". The server (component_health) decides the
// wording; this only shows it, quietly, and hides itself when all is well.
const POLL_MS = 60_000;

export function ServiceStatusBanner() {
  const [messages, setMessages] = useState([]);

  useEffect(() => {
    let alive = true;
    const load = () => crmGet('/api/status')
      .then((data) => { if (alive) setMessages(Array.isArray(data?.messages) ? data.messages : []); })
      .catch(() => { /* the status endpoint failing is itself shown by request errors */ });
    load();
    const id = setInterval(load, POLL_MS + Math.floor(Math.random() * 10_000));
    return () => { alive = false; clearInterval(id); };
  }, []);

  if (!messages.length) return null;
  return (
    <div className={styles.banner} role="status" aria-live="polite" data-service-banner="">
      {messages.map((m) => <span key={m}>{m}</span>)}
    </div>
  );
}

export default ServiceStatusBanner;
