import { useCallback, useEffect, useRef, useState } from 'react';
import { crmGet, crmPost } from '../../state/useCrmApi';
import { alertDeliverySummary } from './operatorModel';
import styles from './OperatorOverview.module.css';

// Audit 30.3: a set ORACLE_ALERT_EMAIL proves nothing about a mistyped address
// or a broken relay. A test alert carries a one-time code; typing it back is
// the proof the launch check credits.
const ENDPOINT = '/api/admin/alerts/test';

function sendResultWords(result) {
  if (result?.outcome === 'sent') return { ok: true, text: 'Sent. Check the alert inbox for the code.' };
  if (result?.outcome === 'recipient_missing') return { ok: false, text: 'Nothing was sent: no alert recipient is set.' };
  return { ok: false, text: `The mail relay did not accept the test${result?.error ? ` (${result.error})` : ''}.` };
}

export default function AlertDeliveryCard() {
  const [slot, setSlot] = useState(null);
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState(null);
  const [code, setCode] = useState('');
  const alive = useRef(true);

  const load = useCallback(() => crmGet(ENDPOINT).then(
    (data) => { if (alive.current) setSlot({ data, error: null }); },
    (error) => { if (alive.current) setSlot((prev) => ({ data: prev?.data ?? null, error })); },
  ), []);

  useEffect(() => {
    alive.current = true;
    load();
    return () => { alive.current = false; };
  }, [load]);

  const send = async () => {
    setBusy('send');
    setNote(null);
    try {
      setNote(sendResultWords(await crmPost(ENDPOINT, {})));
    } catch (err) {
      setNote({ ok: false, text: err?.status === 429
        ? 'A test alert was sent less than a minute ago. Try again shortly.'
        : 'The test alert could not be requested.' });
    } finally {
      if (alive.current) setBusy('');
      load();
    }
  };

  const confirm = async (event) => {
    event.preventDefault();
    if (!code.trim()) return;
    setBusy('confirm');
    setNote(null);
    try {
      await crmPost(`${ENDPOINT}/confirm`, { code: code.trim() });
      setCode('');
      setNote({ ok: true, text: 'Confirmed: alerts reach the current recipient.' });
    } catch (err) {
      const left = err?.detail?.attempts_left;
      setNote({ ok: false, text: `${err?.message || 'The code could not be checked.'}`
        + (typeof left === 'number' ? ` ${left} ${left === 1 ? 'try' : 'tries'} left.` : '') });
    } finally {
      if (alive.current) setBusy('');
      load();
    }
  };

  if (!slot) return <p className={styles.quiet} aria-live="polite">Loading alert delivery…</p>;
  const data = slot.data;
  if (!data) {
    return (
      <div className={styles.error} role="alert">
        <p>Couldn’t load alert delivery{slot.error?.status === 403 ? ' — platform admin only' : ''}.</p>
        <button type="button" className={styles.linkBtn} onClick={load}>Retry</button>
      </div>
    );
  }
  return (
    <>
      {slot.error ? <p className={styles.staleNote}>Showing the last answer — the latest refresh failed.</p> : null}
      <p className={styles.lead}>{data.proven ? 'Proven' : 'Not proven'}</p>
      <p className={styles.sub}>{alertDeliverySummary(data)}</p>
      <div className={styles.inlineForm}>
        <button type="button" className={styles.linkBtn} onClick={send}
          disabled={busy !== '' || !data.recipient_configured}>
          {busy === 'send' ? 'Sending…' : 'Send test alert'}
        </button>
      </div>
      {data.awaiting_code ? (
        <form className={styles.inlineForm} onSubmit={confirm}>
          <label className={styles.sub} htmlFor="alert-test-code">Code from the email</label>
          <input id="alert-test-code" className={styles.input} value={code} maxLength={32}
            autoComplete="one-time-code" autoCapitalize="characters" spellCheck={false}
            onChange={(e) => setCode(e.target.value)} />
          <button type="submit" className={styles.linkBtn} disabled={busy !== '' || !code.trim()}>
            {busy === 'confirm' ? 'Checking…' : 'Confirm'}
          </button>
        </form>
      ) : null}
      {note ? (
        <p className={note.ok ? styles.sub : styles.warnNote} role={note.ok ? 'status' : 'alert'}>{note.text}</p>
      ) : null}
    </>
  );
}
