import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { crmGet, crmPost } from '../state/useCrmApi';
import styles from './CommsTab.module.css';
import { GLYPHS, MAX_BODY_HEIGHT, firstNameOf } from './CommsShared';

/* CommsComposer — the fixed comms chrome pinned above the Deck. Channel
   selector (email/SMS/internal note), an email subject line, the growing body,
   plus two
   enterprise affordances: a property-specific AI-script action and client-side quick
   templates (localStorage, with {{name}} interpolation). It owns its own send
   state and reports its measured height up so the stream clears it. It never
   fabricates a sent message: the parent only renders what the API echoes back. */

const TEMPLATES_KEY = 'oracle_comms_templates_v1';

const INTENTS = [
  { id: 'follow_up_showing', label: 'Follow up after showing' },
  { id: 'offer_recap', label: 'Offer recap' },
  { id: 'check_in', label: 'Check-in' },
  { id: 'request_docs', label: 'Request documents' },
  { id: 'reengage', label: 'Re-engage a cold lead' },
];

function loadTemplates() {
  try {
    const raw = localStorage.getItem(TEMPLATES_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return parsed.filter((t) => t && typeof t.body === 'string');
    }
  } catch {
    /* corrupt or unavailable storage — start with no user templates */
  }
  return [];
}

function persistTemplates(list) {
  try {
    localStorage.setItem(TEMPLATES_KEY, JSON.stringify(list));
  } catch {
    /* private mode / quota — templates simply won't persist this session */
  }
}

const fillTokens = (text, name) =>
  String(text || '')
    .replace(/\{\{\s*first_?name\s*\}\}/gi, firstNameOf(name))
    .replace(/\{\{\s*name\s*\}\}/gi, String(name || '').trim());

export default function CommsComposer({
  clientId,
  clientName,
  emailBlocked,
  smsBlocked,
  onSent,
  onRefetch,
  onHeightChange,
}) {
  const [channel, setChannel] = useState('note'); // 'email' | 'sms' | 'note'
  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState(null);

  // AI draft + templates trays (one open at a time).
  const [tray, setTray] = useState(null); // 'draft' | 'templates' | null
  const [drafting, setDrafting] = useState(false);
  const [draftError, setDraftError] = useState(null);
  const [templates, setTemplates] = useState(loadTemplates);
  const [newTitle, setNewTitle] = useState('');
  const [properties, setProperties] = useState([]);
  const [propertyId, setPropertyId] = useState('');

  const bodyRef = useRef(null);
  const rootRef = useRef(null);

  useEffect(() => {
    let active = true;
    if (!clientId) {
      Promise.resolve().then(() => {
        if (active) {
          setProperties([]);
          setPropertyId('');
        }
      });
      return () => { active = false; };
    }
    crmGet(`/api/crm/clients/${encodeURIComponent(clientId)}`).then(
      (result) => {
        if (!active) return;
        const detail = result?.client ?? result;
        const houses = Array.isArray(detail?.houses) ? detail.houses : [];
        const normalized = houses
          .map((house) => ({
            id: house.id,
            propertyId: house.lead_id || house.id,
            address: house.address || 'Property record',
          }))
          .filter((house) => house.propertyId);
        setProperties(normalized);
        setPropertyId((current) => (
          normalized.some((house) => house.propertyId === current)
            ? current
            : normalized[0]?.propertyId || ''
        ));
      },
      () => {
        if (active) {
          setProperties([]);
          setPropertyId('');
        }
      },
    );
    return () => { active = false; };
  }, [clientId]);

  // A remembered channel that is unavailable for this client resolves to an
  // internal note without a setState effect. The disabled channel key prevents
  // selecting an address/number the client does not have.
  const channelIsBlocked =
    (channel === 'email' && emailBlocked) || (channel === 'sms' && smsBlocked);
  const activeChannel = channelIsBlocked ? 'note' : channel;

  // Report measured height up so the stream's bottom padding always clears the
  // composer (subject field, growing body, and open trays all change it).
  useEffect(() => {
    const el = rootRef.current;
    if (!el || typeof ResizeObserver === 'undefined') {
      onHeightChange?.(160);
      return undefined;
    }
    const ro = new ResizeObserver((entries) => {
      const h = entries[0]?.contentRect?.height;
      if (h) onHeightChange?.(Math.ceil(h) + 24);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [onHeightChange]);

  const autoGrow = useCallback(() => {
    const el = bodyRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, MAX_BODY_HEIGHT)}px`;
  }, []);

  // Re-fit the textarea whenever body is set programmatically (draft/template).
  useEffect(() => {
    autoGrow();
  }, [body, autoGrow]);

  const send = useCallback(() => {
    const text = body.trim();
    if (!text || sending || !clientId) return;
    setSending(true);
    setSendError(null);
    const payload = { channel: activeChannel, body: text };
    if (activeChannel !== 'note') payload.direction = 'outbound';
    if (activeChannel === 'email' && subject.trim()) payload.subject = subject.trim();
    crmPost(`/api/crm/clients/${clientId}/messages`, payload).then(
      (data) => {
        setSending(false);
        setBody('');
        if (activeChannel === 'email') setSubject('');
        if (bodyRef.current) bodyRef.current.style.height = 'auto';
        const echoed = data?.message ?? data?.interaction ?? null;
        if (echoed) {
          const deliveryStatus = data?.delivery_status ?? echoed?.delivery_status ?? null;
          onSent?.({
            ...echoed,
            _queued: Boolean(data?.queued_email_id),
            _deliveryStatus: deliveryStatus,
            _loggedOnly: activeChannel === 'sms' && deliveryStatus === 'not_sent',
          });
        } else {
          onRefetch?.(); // server didn't echo — pull the real thread
        }
      },
      (err) => {
        setSending(false);
        setSendError(err); // body preserved — Send doubles as retry
      }
    );
  }, [body, sending, clientId, activeChannel, subject, onSent, onRefetch]);

  // Signature-aware fallback draft. /api/crm/comms/draft is not channel-gated
  // and always returns {subject, body} — it degrades to a clean templated draft
  // when no LLM is reachable. It had no caller at all, which meant the two cases
  // generate-script legitimately refuses (a note, or a client with no linked
  // property) were dead ends rather than a plainer draft.
  const runFallbackDraft = useCallback(
    (intent) => {
      if (!clientId || drafting) return;
      setDrafting(true);
      setDraftError(null);
      crmPost('/api/crm/comms/draft', { client_id: clientId, intent: intent || '' }).then(
        (data) => {
          setDrafting(false);
          const draftSubject = typeof data?.subject === 'string' ? data.subject : '';
          const draftBody = typeof data?.body === 'string' ? data.body : '';
          if (!draftBody && !draftSubject) {
            setDraftError(new Error('The draft came back empty.'));
            return;
          }
          if (draftSubject && activeChannel === 'email') setSubject(draftSubject);
          if (draftBody) setBody(draftBody);
          setTray(null);
        },
        (err) => {
          setDrafting(false);
          setDraftError(err);
        }
      );
    },
    [clientId, drafting, activeChannel]
  );

  const runDraft = useCallback(
    (intent) => {
      if (!clientId || drafting) return;
      // A compliant outreach script needs a property and a real channel. Where
      // it cannot run, fall through to the signature-aware draft rather than
      // refusing outright — the agent still gets something to edit.
      if (!propertyId || activeChannel === 'note') {
        runFallbackDraft(intent);
        return;
      }
      setDrafting(true);
      setDraftError(null);
      crmPost('/api/comms/generate-script', {
        client_id: clientId,
        property_id: propertyId,
        channel: activeChannel.toUpperCase(),
        objective: intent,
      }).then(
        (data) => {
          setDrafting(false);
          const draftSubject = typeof data?.subject === 'string' ? data.subject : '';
          const draftBody = typeof data?.script === 'string' ? data.script : '';
          if (!draftBody && !draftSubject) {
            setDraftError(new Error('The draft came back empty.'));
            return;
          }
          if (draftSubject && activeChannel === 'email') setSubject(draftSubject);
          if (draftBody) setBody(draftBody);
          setTray(null);
        },
        (err) => {
          setDrafting(false);
          // A refused or failed outreach script is still a draft the agent
          // wanted. Offer the plainer one instead of leaving them with an error
          // and an empty body.
          if (err?.status === 422 || err?.status === 409 || err?.status === 503) {
            runFallbackDraft(intent);
            return;
          }
          setDraftError(err);
        }
      );
    },
    [clientId, drafting, activeChannel, propertyId, runFallbackDraft]
  );

  const insertTemplate = useCallback(
    (tpl) => {
      const filled = fillTokens(tpl.body, clientName);
      setBody((prev) => (prev.trim() ? `${prev.trimEnd()}\n\n${filled}` : filled));
      setTray(null);
    },
    [clientName]
  );

  const saveTemplate = useCallback(() => {
    const text = body.trim();
    if (!text) return;
    const title = newTitle.trim() || `${text.slice(0, 24)}${text.length > 24 ? '…' : ''}`;
    const tpl = { id: `tpl-${Date.now()}`, title, body: text };
    setTemplates((prev) => {
      const next = [tpl, ...prev];
      persistTemplates(next);
      return next;
    });
    setNewTitle('');
  }, [body, newTitle]);

  const deleteTemplate = useCallback((id) => {
    setTemplates((prev) => {
      const next = prev.filter((t) => t.id !== id);
      persistTemplates(next);
      return next;
    });
  }, []);

  const draftErrMsg = useMemo(() => {
    if (!draftError) return '';
    return draftError.status === 404
      ? 'AI drafting isn’t online yet — try a template.'
      : draftError.message || 'Couldn’t draft that — try a template.';
  }, [draftError]);

  const sendErrMsg = useMemo(() => {
    if (!sendError) return '';
    return sendError.status === 404
      ? 'Comms service isn’t online yet — nothing was saved or sent.'
      : sendError.message || 'The communication wasn’t saved.';
  }, [sendError]);

  const actionLabel =
    activeChannel === 'email'
      ? 'Queue email'
      : activeChannel === 'sms'
        ? 'Log SMS without sending'
        : 'Save internal note';
  const channelHint = channelIsBlocked
    ? channel === 'email'
      ? 'No email on file — switched to an internal note.'
      : 'No phone on file — switched to an internal note.'
    : activeChannel === 'sms'
      ? 'Logged only — no SMS provider is connected.'
      : activeChannel === 'note'
        ? 'Internal only — never sent to the client.'
        : 'Email is queued for delivery.';

  const toggleTray = (which) => setTray((cur) => (cur === which ? null : which));

  return (
    <div className={styles.composer} ref={rootRef}>
      {/* Trays open ABOVE the fields so the body stays anchored to the Deck. */}
      {tray === 'draft' && (
        <div className={styles.tray} role="group" aria-label="AI draft intents">
          <div className={styles.trayHead}>
            <span className={styles.trayTitle}>AI draft</span>
            <button
              type="button"
              className={styles.trayClose}
              onClick={() => setTray(null)}
              aria-label="Close AI draft"
            >
              {GLYPHS.close}
            </button>
          </div>
          <p className={styles.trayHint}>Pick an intent — you can edit before sending.</p>
          <label className={styles.scriptProperty}>
            <span>Property context</span>
            <select
              value={propertyId}
              onChange={(event) => setPropertyId(event.target.value)}
              disabled={properties.length === 0}
            >
              {properties.length === 0
                ? <option value="">No linked property</option>
                : properties.map((property) => (
                  <option key={`${property.id}:${property.propertyId}`} value={property.propertyId}>
                    {property.address}
                  </option>
                ))}
            </select>
          </label>
          <div className={styles.chipWrap}>
            {INTENTS.map((it) => (
              <button
                key={it.id}
                type="button"
                className={styles.intentChip}
                onClick={() => runDraft(it.label)}
                disabled={drafting}
              >
                {it.label}
              </button>
            ))}
          </div>
          {drafting && <p className={styles.trayBusy}>Drafting…</p>}
          {draftError && <p className={styles.trayErr} role="alert">{draftErrMsg}</p>}
        </div>
      )}

      {tray === 'templates' && (
        <div className={styles.tray} role="group" aria-label="Quick templates">
          <div className={styles.trayHead}>
            <span className={styles.trayTitle}>Templates</span>
            <button
              type="button"
              className={styles.trayClose}
              onClick={() => setTray(null)}
              aria-label="Close templates"
            >
              {GLYPHS.close}
            </button>
          </div>
          <ul className={styles.tplList} role="list">
            {templates.length === 0 ? (
              <li className={styles.tplEmpty}>No saved templates yet.</li>
            ) : (
              templates.map((t) => (
                <li key={t.id} className={styles.tplRow}>
                  <button
                    type="button"
                    className={styles.tplInsert}
                    onClick={() => insertTemplate(t)}
                    title="Insert template"
                  >
                    <span className={styles.tplTitle}>{t.title}</span>
                    <span className={styles.tplPreview}>{fillTokens(t.body, clientName)}</span>
                  </button>
                  <button
                    type="button"
                    className={styles.tplDel}
                    onClick={() => deleteTemplate(t.id)}
                    aria-label={`Delete template ${t.title}`}
                  >
                    {GLYPHS.close}
                  </button>
                </li>
              ))
            )}
          </ul>
          <div className={styles.tplSaveRow}>
            <input
              className={styles.tplSaveInput}
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              placeholder="Name this template"
              aria-label="New template name"
            />
            <button
              type="button"
              className={styles.tplSaveBtn}
              onClick={saveTemplate}
              disabled={!body.trim()}
              aria-label="Save current draft as a template"
            >
              {GLYPHS.plus}
            </button>
          </div>
        </div>
      )}

      {/* Channel selector + tool keys. */}
      <div className={styles.chRow}>
        <div className={styles.chToggle} role="group" aria-label="Channel">
          <button
            type="button"
            className={`${styles.chKey}${activeChannel === 'email' ? ` ${styles.chKeyOn}` : ''}`}
            aria-pressed={activeChannel === 'email'}
            disabled={emailBlocked}
            title={emailBlocked ? 'No email address on file' : 'Compose an email'}
            onClick={() => setChannel('email')}
          >
            Email
          </button>
          <button
            type="button"
            className={`${styles.chKey}${activeChannel === 'sms' ? ` ${styles.chKeyOn}` : ''}`}
            aria-pressed={activeChannel === 'sms'}
            disabled={smsBlocked}
            title={smsBlocked ? 'No phone number on file' : 'Log an SMS without sending'}
            onClick={() => setChannel('sms')}
          >
            SMS
          </button>
          <button
            type="button"
            className={`${styles.chKey}${activeChannel === 'note' ? ` ${styles.chKeyOn}` : ''}`}
            aria-pressed={activeChannel === 'note'}
            onClick={() => setChannel('note')}
          >
            Note
          </button>
        </div>
        <div className={styles.toolKeys}>
          <button
            type="button"
            className={`${styles.toolKey}${tray === 'draft' ? ` ${styles.toolKeyOn}` : ''}`}
            onClick={() => toggleTray('draft')}
            aria-pressed={tray === 'draft'}
            aria-label="Generate Script with AI Assistant"
            title="Generate Script with AI Assistant"
          >
            {GLYPHS.sparkle}
          </button>
          <button
            type="button"
            className={`${styles.toolKey}${tray === 'templates' ? ` ${styles.toolKeyOn}` : ''}`}
            onClick={() => toggleTray('templates')}
            aria-pressed={tray === 'templates'}
            aria-label="Quick templates"
          >
            {GLYPHS.template}
          </button>
        </div>
      </div>

      <span className={styles.chHint}>{channelHint}</span>

      {activeChannel === 'email' && (
        <input
          className={styles.subjectInput}
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          placeholder="Subject"
          aria-label="Email subject"
        />
      )}

      {sendError && (
        <div className={styles.sendError} role="alert">
          <span className={styles.errorTick} aria-hidden="true" />
          <p className={styles.errorText}>{sendErrMsg}</p>
          <button type="button" className={styles.retrySm} onClick={send}>
            Retry
          </button>
        </div>
      )}

      <div className={styles.composeRow}>
        <textarea
          ref={bodyRef}
          className={styles.bodyInput}
          value={body}
          rows={1}
          onChange={(e) => {
            setBody(e.target.value);
            autoGrow();
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send();
          }}
          placeholder={
            activeChannel === 'email'
              ? 'Compose email…'
              : activeChannel === 'sms'
                ? 'Text message…'
                : 'Log a note…'
          }
          aria-label={
            activeChannel === 'email' ? 'Email body' : activeChannel === 'sms' ? 'SMS body' : 'Note body'
          }
        />
        <button
          type="button"
          className={`${styles.sendBtn}${sending ? ` ${styles.sending}` : ''}`}
          onClick={send}
          disabled={!body.trim() || sending}
          aria-label={sending ? 'Saving communication' : actionLabel}
          title={actionLabel}
        >
          {GLYPHS.send}
        </button>
      </div>
    </div>
  );
}
