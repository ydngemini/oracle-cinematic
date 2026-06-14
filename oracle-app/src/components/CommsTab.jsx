import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { crmGet, crmPost } from '../state/useCrmApi';
import styles from './CommsTab.module.css';

// Inline stroke glyphs — currentColor, zero icon deps (house rule).
const GLYPHS = {
  email: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3.5" y="5.5" width="17" height="13" rx="2" />
      <path d="m4.5 7.5 7.5 5.5 7.5-5.5" />
    </svg>
  ),
  sms: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 5.5h16v11H9l-5 4v-15Z" />
      <path d="M8 9.5h8M8 12.5h5" />
    </svg>
  ),
  voice: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 4.5h3.2l1.5 4-2 1.6a12.5 12.5 0 0 0 6.2 6.2l1.6-2 4 1.5v3.2c0 .9-.7 1.6-1.6 1.5C10.7 19.9 4.1 13.3 3.5 6.1c-.1-.9.6-1.6 1.5-1.6Z" />
    </svg>
  ),
  portal: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3.5" y="4.5" width="17" height="15" rx="2" />
      <path d="M3.5 9h17" />
      <path d="M6.5 6.8h.01M9.2 6.8h.01" />
    </svg>
  ),
  doc: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M6.5 3.5h7L18.5 8v12.5h-12V3.5Z" />
      <path d="M13.5 3.5V8h5" />
      <path d="M9.5 12h5M9.5 15h5" />
    </svg>
  ),
  waveform: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 10v4M8 7v10M12 4.5v15M16 7v10M20 10v4" />
    </svg>
  ),
  chevron: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m14.5 5.5-6.5 6.5 6.5 6.5" />
    </svg>
  ),
  send: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3.5 11.2 20.5 4l-6.8 16.5-2.7-7.1-7.5-2.2Z" />
      <path d="M20.5 4 11 13.4" />
    </svg>
  ),
  refresh: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M20.5 12a8.5 8.5 0 1 1-2.49-6.01" />
      <path d="M20.5 3.5V8H16" />
    </svg>
  ),
};

const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
const SYSTEM_TYPES = new Set(['portal_view', 'document_signed', 'status_change']);
const MAX_BODY_HEIGHT = 132;

// ── Defensive readers — never trust shape, never crash ─────────────────────

const firstString = (...vals) =>
  vals.find((v) => typeof v === 'string' && v.trim() !== '') ?? null;

function parsePayload(p) {
  if (p == null) return {};
  if (typeof p === 'string') {
    try {
      const o = JSON.parse(p);
      return o && typeof o === 'object' ? o : { body: p };
    } catch {
      return { body: p };
    }
  }
  return typeof p === 'object' ? p : {};
}

function initialsOf(name) {
  return (
    String(name || '')
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((w) => w[0].toUpperCase())
      .join('') || '·'
  );
}

function ts(iso) {
  const t = new Date(iso ?? 0).getTime();
  return Number.isNaN(t) ? 0 : t;
}

// Compact mono relative time for thread rows / system lines: NOW · 5M · 2H ·
// 3D · MAY 28. Instrument-cluster terse, tabular by construction.
function relTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const diff = Date.now() - d.getTime();
  if (diff < 60e3) return 'NOW';
  const m = Math.floor(diff / 60e3);
  if (m < 60) return `${m}M`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}H`;
  const days = Math.floor(h / 24);
  if (days < 7) return `${days}D`;
  return `${MONTHS[d.getMonth()]} ${d.getDate()}`;
}

// Bubble timestamp: clock for today, MON D · clock otherwise.
function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  let h = d.getHours();
  const mins = String(d.getMinutes()).padStart(2, '0');
  const ap = h >= 12 ? 'P' : 'A';
  h = h % 12 || 12;
  const clock = `${h}:${mins}${ap}`;
  return d.toDateString() === new Date().toDateString()
    ? clock
    : `${MONTHS[d.getMonth()]} ${d.getDate()} · ${clock}`;
}

function channelOf(type) {
  const t = String(type || '').toLowerCase();
  if (t.includes('email')) return 'email';
  if (t.includes('voice') || t.includes('call')) return 'voice';
  if (t.includes('portal')) return 'portal';
  if (t.includes('doc') || t.includes('sign') || t.includes('contract')) return 'doc';
  return 'sms'; // sms / message / note / unknown — speech bubble
}

function isSystem(it) {
  return SYSTEM_TYPES.has(String(it?.interaction_type || '').toLowerCase());
}

function isVoiceNote(it) {
  return String(it?.interaction_type || '').toLowerCase().includes('voice');
}

function isOutbound(it) {
  const d = String(it?.direction || '').toLowerCase();
  if (d) return d.startsWith('out') || d === 'sent';
  const role = String(it?.actor_role || '').toLowerCase();
  return role === 'agent' || role === 'ai';
}

function bodyOf(it) {
  const p = parsePayload(it?.payload);
  return firstString(p.body, p.text, p.message, p.content, p.snippet) ?? '';
}

function transcriptOf(it) {
  const p = parsePayload(it?.payload);
  return firstString(p.transcript, p.transcript_snippet, p.summary, p.text, p.body);
}

function systemLabel(it) {
  const t = String(it?.interaction_type || '').toLowerCase();
  const p = parsePayload(it?.payload);
  if (t === 'portal_view') {
    const where = firstString(p.page, p.section, p.url);
    return where ? `Portal view — ${where}` : 'Portal viewed';
  }
  if (t === 'document_signed') {
    const doc = firstString(p.document_name, p.document, p.title, p.name);
    return doc ? `Signed — ${doc}` : 'Document signed';
  }
  if (t === 'status_change') {
    const to = firstString(p.to, p.new_status, p.status);
    const from = firstString(p.from, p.old_status);
    if (to) return from ? `Status ${from} → ${to}` : `Status → ${to}`;
    return 'Status changed';
  }
  return t.replace(/_/g, ' ') || 'Event';
}

function roleLabel(it) {
  const role = String(it?.actor_role || '').toLowerCase();
  if (role === 'ai') return 'AI';
  if (role === 'agent') return 'AGENT';
  return null;
}

/**
 * CommsTab — the unified communications pane. Two levels in one component:
 * LEVEL 1 is the thread list (one row per client), LEVEL 2 is the thread
 * view — interactions oldest→newest as bubbles, system events as hairlines,
 * a fixed composer pinned above the Deck. Renders only what the API returns;
 * never simulates a message.
 */
export default function CommsTab() {
  // LEVEL 1 — threads.
  const [threads, setThreads] = useState(null); // null = not yet loaded
  const [threadsError, setThreadsError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);

  // LEVEL 2 — the open thread.
  const [selected, setSelected] = useState(null); // a thread object, or null
  const [interactions, setInteractions] = useState(null);
  const [intError, setIntError] = useState(null);

  // Composer.
  const [channel, setChannel] = useState('message'); // 'email' | 'message'
  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState(null);
  const [composerPad, setComposerPad] = useState(148);

  const bodyRef = useRef(null);
  const composerRef = useRef(null);
  const endRef = useRef(null);
  const scrolledRef = useRef(false);

  // All setState lands in async continuations — never synchronously inside
  // the effect's call stack (react-hooks/set-state-in-effect compliant).
  const fetchThreads = useCallback(() => {
    return crmGet('/api/crm/comms/threads').then(
      (data) => {
        setThreads(Array.isArray(data?.threads) ? data.threads : []);
        setThreadsError(null);
        setRefreshing(false);
      },
      (err) => {
        setThreadsError(err); // 404 until the route deploys — quiet, never crash
        setRefreshing(false);
      }
    );
  }, []);

  useEffect(() => {
    fetchThreads();
  }, [fetchThreads]);

  const fetchInteractions = useCallback((clientId) => {
    return crmGet(`/api/crm/clients/${clientId}/interactions?limit=50`).then(
      (data) => {
        const arr = Array.isArray(data?.interactions) ? data.interactions : [];
        setInteractions([...arr].reverse()); // API is newest-first; render oldest→newest
        setIntError(null);
      },
      (err) => {
        setIntError(err);
      }
    );
  }, []);

  const openThread = (t) => {
    if (!t?.client?.id) return;
    setSelected(t);
    setInteractions(null);
    setIntError(null);
    setSendError(null);
    setChannel('message');
    setSubject('');
    setBody('');
    fetchInteractions(t.client.id);
  };

  const backToList = () => {
    setSelected(null);
    setInteractions(null);
    setIntError(null);
    setSendError(null);
    fetchThreads(); // silent refresh — the list reflects what was just sent
  };

  // Reset the "first scroll is instant" latch whenever the thread changes.
  useEffect(() => {
    scrolledRef.current = false;
  }, [selected]);

  // Auto-scroll to bottom on thread open (instant) and new message (smooth).
  useEffect(() => {
    if (!selected || interactions === null) return undefined;
    const behavior = scrolledRef.current ? 'smooth' : 'auto';
    const id = requestAnimationFrame(() => {
      endRef.current?.scrollIntoView({ block: 'end', behavior });
    });
    scrolledRef.current = true;
    return () => cancelAnimationFrame(id);
  }, [selected, interactions]);

  // The composer is fixed chrome of variable height (subject field, growing
  // textarea) — measure it so the stream's bottom padding always clears it.
  useEffect(() => {
    if (!selected) return undefined;
    const el = composerRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver((entries) => {
      const h = entries[0]?.contentRect?.height;
      if (h) setComposerPad(Math.ceil(h) + 24);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [selected]);

  const autoGrow = () => {
    const el = bodyRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, MAX_BODY_HEIGHT)}px`;
  };

  const send = () => {
    const text = body.trim();
    const clientId = selected?.client?.id;
    if (!text || sending || !clientId) return;
    setSending(true);
    setSendError(null);
    const payload = { channel, body: text };
    if (channel === 'email' && subject.trim()) payload.subject = subject.trim();
    crmPost(`/api/crm/clients/${clientId}/messages`, payload).then(
      (data) => {
        setSending(false);
        setBody('');
        if (channel === 'email') setSubject('');
        if (bodyRef.current) bodyRef.current.style.height = 'auto';
        if (data?.interaction) {
          const sent = { ...data.interaction, _queued: Boolean(data?.queued_email_id) };
          setInteractions((prev) => [...(prev ?? []), sent]);
        } else {
          // Server didn't echo the interaction — pull the real thread instead.
          fetchInteractions(clientId);
        }
      },
      (err) => {
        setSending(false);
        setSendError(err); // body preserved — Send doubles as retry
      }
    );
  };

  const refresh = () => {
    setRefreshing(true);
    setThreadsError(null);
    fetchThreads();
  };

  const retryThreads = () => {
    setThreadsError(null); // back to skeletons while the retry is in flight
    fetchThreads();
  };

  const sortedThreads = useMemo(() => {
    const arr = Array.isArray(threads) ? [...threads] : [];
    return arr.sort((a, b) => ts(b?.last?.created_at) - ts(a?.last?.created_at));
  }, [threads]);

  // ── LEVEL 2 — thread view ─────────────────────────────────────────────────
  if (selected) {
    const client = selected.client ?? {};
    const name = firstString(client.full_name) ?? 'Unknown client';
    // Email availability is only known when the threads payload carries the
    // field; if absent, allow EMAIL and let the backend reject gracefully.
    const emailKnown = Object.prototype.hasOwnProperty.call(client, 'email');
    const emailBlocked = emailKnown && !String(client.email || '').trim();
    const intLoading = interactions === null && !intError;
    const intErrMsg =
      intError?.status === 404
        ? 'Conversation history isn’t online yet.'
        : intError?.message || 'Couldn’t load this conversation.';
    const sendErrMsg =
      sendError?.status === 404
        ? 'Comms service isn’t online yet — nothing was sent.'
        : sendError?.message || 'The message didn’t go through.';

    return (
      <section
        className={styles.wrap}
        aria-label={`Conversation with ${name}`}
        aria-busy={intLoading}
      >
        <header className={styles.threadHeader}>
          <button
            type="button"
            className={styles.backBtn}
            onClick={backToList}
            aria-label="Back to all conversations"
          >
            {GLYPHS.chevron}
          </button>
          <span className={styles.headRing} aria-hidden="true">
            {initialsOf(name)}
          </span>
          <span className={styles.headText}>
            <span className={styles.headName}>{name}</span>
            {client.client_type && (
              <span className={styles.headType}>{client.client_type}</span>
            )}
          </span>
        </header>

        <div className={styles.threadBody} style={{ paddingBottom: composerPad }}>
          {intLoading ? (
            <div className={styles.streamSkel} aria-hidden="true">
              <div className={styles.skelMsgIn} />
              <div className={styles.skelMsgOut} />
              <div className={styles.skelMsgIn} />
              <div className={styles.skelMsgOut} />
            </div>
          ) : intError ? (
            <div className={styles.errorBox} role="alert">
              <span className={styles.errorTick} aria-hidden="true" />
              <p className={styles.errorText}>{intErrMsg}</p>
              <button
                type="button"
                className={styles.retryBtn}
                onClick={() => {
                  setIntError(null);
                  fetchInteractions(client.id);
                }}
              >
                Retry
              </button>
            </div>
          ) : interactions.length === 0 ? (
            <p className={styles.streamEmpty}>No messages yet — open the line below.</p>
          ) : (
            <ol className={styles.stream} role="list">
              {interactions.map((it, i) => {
                const key = it.id ?? `${it.created_at ?? 'x'}-${i}`;

                if (isSystem(it)) {
                  const when = relTime(it.created_at);
                  return (
                    <li key={key} className={styles.sysLine}>
                      <span className={styles.sysRule} aria-hidden="true" />
                      <span className={styles.sysText}>
                        {systemLabel(it)}
                        {when && ` · ${when}`}
                      </span>
                      <span className={styles.sysRule} aria-hidden="true" />
                    </li>
                  );
                }

                const out = isOutbound(it);
                const chan = channelOf(it.interaction_type);
                const voice = isVoiceNote(it);
                const text = voice ? transcriptOf(it) : bodyOf(it);
                const subjectLine = firstString(it.subject);
                const role = out ? roleLabel(it) : null;
                const when = fmtTime(it.created_at);

                return (
                  <li
                    key={key}
                    className={`${styles.msg} ${out ? styles.msgOut : styles.msgIn}`}
                  >
                    <div className={styles.bubble}>
                      {voice ? (
                        <>
                          <span className={styles.voiceRow}>
                            <span className={styles.voiceGlyph} aria-hidden="true">
                              {GLYPHS.waveform}
                            </span>
                            <span className={styles.voiceLabel}>Voice note</span>
                          </span>
                          {text ? (
                            <p className={styles.transcript}>{text}</p>
                          ) : (
                            <p className={styles.transcriptGhost}>No transcript</p>
                          )}
                        </>
                      ) : (
                        <>
                          {subjectLine && (
                            <span className={styles.msgSubject}>{subjectLine}</span>
                          )}
                          {text ? (
                            <p className={styles.msgBody}>{text}</p>
                          ) : (
                            <p className={styles.msgBodyGhost}>No content</p>
                          )}
                        </>
                      )}
                      <span className={styles.msgMeta}>
                        <span className={styles.metaGlyph} aria-hidden="true">
                          {GLYPHS[chan]}
                        </span>
                        {role && <span>{role}</span>}
                        {when && <span>{when}</span>}
                      </span>
                    </div>
                    {it._queued && (
                      <span className={styles.queuedChip}>Queued — AI sends</span>
                    )}
                  </li>
                );
              })}
            </ol>
          )}
          <div ref={endRef} aria-hidden="true" />
        </div>

        {/* Composer — fixed chrome, pinned above the Deck. */}
        <div className={styles.composer} ref={composerRef}>
          <div className={styles.chRow}>
            <div className={styles.chToggle} role="group" aria-label="Channel">
              <button
                type="button"
                className={`${styles.chKey}${channel === 'email' ? ` ${styles.chKeyOn}` : ''}`}
                aria-pressed={channel === 'email'}
                disabled={emailBlocked}
                onClick={() => setChannel('email')}
              >
                Email
              </button>
              <button
                type="button"
                className={`${styles.chKey}${channel === 'message' ? ` ${styles.chKeyOn}` : ''}`}
                aria-pressed={channel === 'message'}
                onClick={() => setChannel('message')}
              >
                Note
              </button>
            </div>
            {emailBlocked && (
              <span className={styles.chHint}>No email on file — notes only</span>
            )}
          </div>

          {channel === 'email' && (
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
              placeholder={channel === 'email' ? 'Compose email…' : 'Log a note…'}
              aria-label={channel === 'email' ? 'Email body' : 'Note body'}
            />
            <button
              type="button"
              className={`${styles.sendBtn}${sending ? ` ${styles.sending}` : ''}`}
              onClick={send}
              disabled={!body.trim() || sending}
              aria-label={sending ? 'Sending' : 'Send'}
            >
              {GLYPHS.send}
            </button>
          </div>
        </div>
      </section>
    );
  }

  // ── LEVEL 1 — thread list ─────────────────────────────────────────────────
  const isLoading = threads === null && !threadsError;
  const errMsg =
    threadsError?.status === 404
      ? 'Comms service isn’t online yet.'
      : threadsError?.message || 'Couldn’t reach the comms service.';

  return (
    <section
      className={styles.wrap}
      aria-label="Comms — all conversations"
      aria-busy={isLoading || refreshing}
    >
      <header className={styles.topRow}>
        <span className={styles.kicker}>Comms</span>
        <button
          type="button"
          className={`${styles.refreshBtn} ${refreshing ? styles.refreshing : ''}`}
          onClick={refresh}
          disabled={refreshing || isLoading}
          aria-label="Refresh conversations"
        >
          {GLYPHS.refresh}
        </button>
      </header>

      {isLoading ? (
        <div className={styles.skeleton} aria-hidden="true">
          <div className={styles.skelRow} />
          <div className={styles.skelRow} />
          <div className={styles.skelRow} />
          <div className={styles.skelRow} />
        </div>
      ) : threadsError && threads === null ? (
        <div className={styles.errorBox} role="alert">
          <span className={styles.errorTick} aria-hidden="true" />
          <p className={styles.errorText}>{errMsg}</p>
          <button type="button" className={styles.retryBtn} onClick={retryThreads}>
            Retry
          </button>
        </div>
      ) : (
        <>
          {threadsError && (
            <div className={styles.errorStrip} role="alert">
              <span className={styles.errorTick} aria-hidden="true" />
              <p className={styles.errorText}>{errMsg}</p>
              <button type="button" className={styles.retrySm} onClick={refresh}>
                Retry
              </button>
            </div>
          )}

          {sortedThreads.length === 0 ? (
            <div className={styles.empty}>
              <span className={styles.emptyGlyph} aria-hidden="true">{GLYPHS.sms}</span>
              <p className={styles.emptyTitle}>No conversations yet</p>
              <p className={styles.emptyHint}>
                Messages, emails, and portal activity for your clients will land here.
              </p>
            </div>
          ) : (
            <ul className={styles.threadList} role="list">
              {sortedThreads.map((t, i) => {
                const client = t?.client ?? {};
                const name = firstString(client.full_name) ?? 'Unknown client';
                const last = t?.last ?? {};
                const snippet =
                  firstString(last.snippet, last.subject) ?? 'No messages yet';
                const chan = channelOf(last.interaction_type);
                const out = isOutbound(last);
                const count = Number(t?.count);
                const when = relTime(last.created_at);

                return (
                  <li key={client.id ?? `thread-${i}`}>
                    <button
                      type="button"
                      className={styles.threadRow}
                      onClick={() => openThread(t)}
                      disabled={!client.id}
                      style={{ animationDelay: `${Math.min(i, 8) * 60}ms` }}
                    >
                      <span className={styles.ring} aria-hidden="true">
                        {initialsOf(name)}
                      </span>
                      <span className={styles.threadMain}>
                        <span className={styles.threadTop}>
                          <span className={styles.threadName}>{name}</span>
                          {client.client_type && (
                            <span className={styles.typeTag}>{client.client_type}</span>
                          )}
                          {when && <span className={styles.threadTime}>{when}</span>}
                        </span>
                        <span className={styles.threadBottom}>
                          <span
                            className={styles.chanGlyph}
                            data-dir={out ? 'out' : 'in'}
                            aria-hidden="true"
                          >
                            {GLYPHS[chan]}
                          </span>
                          <span className={styles.snippet}>{snippet}</span>
                          {Number.isFinite(count) && count > 0 && (
                            <span className={styles.countChip}>{count}</span>
                          )}
                        </span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
