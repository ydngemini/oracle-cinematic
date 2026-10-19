import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPut } from '../state/useCrmApi';
import styles from './AiBudgetPanel.module.css';

/**
 * AI budget — what Neoh's chat has used this month, and the brokerage's cap
 * on it (T18.07). The broker owner's screen; a platform admin sees it too.
 *
 * The server enforces every part of this: who may read or change it (an
 * agent gets 403 and sees nothing here), and the stop itself, which refuses a
 * model call before it is sent. This only shows and edits the three values.
 *
 * Honesty rules the screen keeps:
 *   · no cap is said as "No cap set — not limited", and only when the server
 *     said the cap is null — a budget that could not be read is never shown
 *     as unlimited, and usage that could not be read is never shown as 0;
 *   · the panel cannot know what Neoh's admission decides, so a failed load
 *     says only that the budget isn't shown, never that Neoh has stopped;
 *   · only a save the server refused (4xx) says nothing changed; a dropped
 *     connection or a 5xx may follow a committed write, so it says the
 *     outcome is unknown and re-reads what is in force;
 *   · the numbers are tokens as the provider reported them, not money, and the
 *     panel names what is not counted;
 *   · the warning percent is the owner's threshold, not a measurement.
 */

const PATH = '/api/billing/budgets/ai-tokens';

const STATE_LABELS = {
  unlimited: 'Not limited',
  ok: 'Within budget',
  warning: 'Near the cap',
  exhausted: 'Cap reached — Neoh is stopped',
  insufficient: 'Too little left for a request',
  unknown: 'Usage can’t be read',
  paused: 'Paused',
  platform_halted: 'Paused by the platform',
};

const count = (n) => Number(n || 0).toLocaleString('en-US');

function monthOf(iso) {
  const parsed = new Date(iso || '');
  if (Number.isNaN(parsed.valueOf())) return 'this month';
  return parsed.toLocaleDateString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' });
}

function draftOf(data) {
  return {
    cap: data?.cap == null ? '' : String(data.cap),
    warn: String(data?.warn_pct ?? 80),
    paused: Boolean(data?.paused),
  };
}

export function AiBudgetPanel() {
  // loading | ready | hidden (not the owner) | unreadable (503) | failed
  const [status, setStatus] = useState('loading');
  const [data, setData] = useState(null);
  const [draft, setDraft] = useState(draftOf(null));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const show = useCallback((payload) => {
    setData(payload);
    setDraft(draftOf(payload));
    setStatus('ready');
  }, []);

  useEffect(() => {
    let live = true;
    crmGet(PATH).then(
      (payload) => { if (live) show(payload); },
      (reason) => {
        if (!live) return;
        if (reason?.status === 403) setStatus('hidden');
        else setStatus(reason?.status === 503 ? 'unreadable' : 'failed');
      },
    );
    return () => { live = false; };
  }, [show]);

  if (status === 'hidden') return null;

  if (status === 'loading') {
    return (
      <section className={styles.panel} aria-label="AI budget" aria-busy="true">
        <span className={styles.label}>AI budget</span>
      </section>
    );
  }

  if (status === 'unreadable' || status === 'failed') {
    return (
      <section className={styles.panel} aria-label="AI budget">
        <span className={styles.label}>AI budget</span>
        <p className={styles.text} role="status">
          {status === 'unreadable'
            ? 'The budget can’t be read right now, so it isn’t shown. Try again in a moment.'
            : 'The AI budget didn’t load. Reload the page to try again.'}
        </p>
      </section>
    );
  }

  const save = async (event) => {
    event.preventDefault();
    setError('');
    setNotice('');
    const capText = draft.cap.trim();
    const warn = Number(draft.warn);
    if (capText !== '' && !/^[1-9]\d*$/.test(capText)) {
      setError('Enter a whole number of tokens, or leave the cap empty for no cap.');
      return;
    }
    if (!Number.isInteger(warn) || warn < 1 || warn > 100) {
      setError('The warning has to be a whole percent from 1 to 100.');
      return;
    }
    setBusy(true);
    try {
      const saved = await crmPut(PATH, {
        monthly_cap_tokens: capText === '' ? null : Number(capText),
        warn_pct: warn,
        paused: draft.paused,
      });
      show(saved);
      // Committed even when the month's usage could not be read back.
      setNotice(saved?.used == null ? 'Saved. This month’s usage can’t be read right now.' : 'Saved.');
    } catch (err) {
      const status = Number(err?.status) || 0;
      if (status === 403) {
        setError('Only a broker owner can change the AI budget.');
      } else if (status >= 400 && status < 500) {
        // Refused before the write: the request never reached a commit.
        setError('The budget was not saved. Nothing was changed; try again in a moment.');
      } else {
        // A dropped connection or a 5xx (an edge 502/504 can stand in front of
        // a committed write): the outcome is unknown, so read what is in force.
        try {
          setData(await crmGet(PATH));
          setError('We couldn’t confirm whether the budget was saved. The budget shown here is '
            + 'what is in force now; save again if it isn’t what you set.');
        } catch {
          setError('We couldn’t confirm whether the budget was saved, and it can’t be read right '
            + 'now. Reload the page to see what is in force.');
        }
      }
    } finally {
      setBusy(false);
    }
  };

  const used = data.used;
  const notCounted = Array.isArray(data.not_counted) ? data.not_counted : [];

  return (
    <section className={styles.panel} aria-label="AI budget" aria-busy={busy}>
      <span className={styles.label}>AI budget</span>

      <div className={styles.summary}>
        <span className={styles.chip} data-state={data.state}>{STATE_LABELS[data.state] || data.state}</span>
        {used == null ? (
          <p className={styles.text}>This month’s usage can’t be read right now.</p>
        ) : (
          <>
            <p className={styles.text}>
              {`${monthOf(data.period?.start)} (UTC) so far: ${count(used.total)} tokens`}
            </p>
            <p className={styles.dim}>
              {`${count(used.prompt)} prompt · ${count(used.completion)} completion`}
              {data.reserved > 0 ? ` · ${count(data.reserved)} held for answers in progress` : ''}
            </p>
          </>
        )}
        <p className={styles.text}>
          {data.cap == null ? 'No cap set — not limited' : `Cap: ${count(data.cap)} tokens a month`}
        </p>
        {data.state === 'insufficient' && data.smallest_request_estimate > 0 && (
          <p className={styles.text}>
            {`Less is left than the smallest Neoh request holds (about ${count(data.smallest_request_estimate)} `
              + 'tokens, an estimate), so Neoh stops before asking a model.'}
          </p>
        )}
        {data.platform_halted && (
          <p className={styles.text}>Neoh’s AI is paused across the platform by the Neoh team.</p>
        )}
      </div>

      <form className={styles.form} onSubmit={save}>
        <label className={styles.field}>
          <span className={styles.micro}>Monthly cap, in tokens (empty = no cap)</span>
          <input
            className={styles.input} inputMode="numeric" value={draft.cap}
            onChange={(e) => setDraft((d) => ({ ...d, cap: e.target.value }))}
          />
        </label>
        <label className={styles.field}>
          <span className={styles.micro}>Warn at, % of the cap</span>
          <input
            className={styles.input} inputMode="numeric" value={draft.warn}
            onChange={(e) => setDraft((d) => ({ ...d, warn: e.target.value }))}
          />
        </label>
        <label className={styles.check}>
          <input
            type="checkbox" checked={draft.paused}
            onChange={(e) => setDraft((d) => ({ ...d, paused: e.target.checked }))}
          />
          <span>Pause Neoh’s AI for the brokerage</span>
        </label>
        {error && <p className={styles.error} role="alert">{error}</p>}
        {notice && <p className={styles.dim} role="status">{notice}</p>}
        <div className={styles.actions}>
          <button type="submit" className={styles.button} disabled={busy}>Save budget</button>
        </div>
      </form>

      <p className={styles.dim}>
        {data.counts}
        {notCounted.length > 0 ? ` Not counted: ${notCounted.join(', ')}.` : ''}
        {' '}Tokens, not money: no price is applied. Near the cap, Neoh says so in the chat; at the
        cap it stops before asking a model, and an answer already on its way is not cut off.
      </p>
    </section>
  );
}

export default AiBudgetPanel;
