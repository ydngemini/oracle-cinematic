import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import styles from './CommsTab.module.css';
import CommsComposer from './CommsComposer';
import { CommandApprovalPanel } from './CommandApprovalPanel';
import {
  GLYPHS,
  ts,
  relTime,
  fmtTime,
  initialsOf,
  firstString,
  normalizeThread,
  normalizeMessage,
  statusMeta,
} from './CommsShared';

// Channel filters offered above the thread list. 'all' is the back-compat
// default; the rest map to the normalized thread channel.
const CHANNEL_FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'email', label: 'Email' },
  { id: 'sms', label: 'SMS' },
  { id: 'note', label: 'Note' },
];

/**
 * CommsTab — the unified communications pane. Two levels in one component:
 * LEVEL 1 is the thread list (one row per client) with channel filter + search,
 * LEVEL 2 is the thread view — messages oldest→newest as bubbles, system events
 * as hairlines, channel + delivery-status badges, and a fixed enterprise
 * composer (channel selector, AI draft, templates). It renders only what the
 * API returns; it never simulates a message.
 */
export default function CommsTab() {
  // LEVEL 1 — threads.
  const [threads, setThreads] = useState(null); // null = not yet loaded
  const [threadsError, setThreadsError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [query, setQuery] = useState('');
  const [channelFilter, setChannelFilter] = useState('all');

  // LEVEL 2 — the open thread.
  const [selected, setSelected] = useState(null); // a normalized thread, or null
  const [messages, setMessages] = useState(null); // raw rows, ascending
  const [msgError, setMsgError] = useState(null);
  const [composerPad, setComposerPad] = useState(160);

  const endRef = useRef(null);
  const scrolledRef = useRef(false);

  // All setState lands in async continuations — never synchronously inside the
  // effect's call stack (react-hooks/set-state-in-effect compliant).
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

  // Sort raw rows oldest→newest regardless of which endpoint served them.
  const applyMessages = useCallback((rows) => {
    const arr = Array.isArray(rows) ? [...rows] : [];
    arr.sort((a, b) => ts(a?.created_at) - ts(b?.created_at));
    setMessages(arr);
    setMsgError(null);
  }, []);

  // Contract path: GET /comms/threads/{thread_id}. Falls back to the per-client
  // interactions feed when there's no thread id yet or the new route 404s, so
  // the tab keeps working before the backend lands.
  const fetchMessages = useCallback(
    (thread) => {
      const { threadId, clientId } = thread || {};
      const fromInteractions = () =>
        crmGet(`/api/crm/clients/${clientId}/interactions?limit=50`).then(
          (data) => applyMessages(data?.interactions),
          (err) => setMsgError(err)
        );

      if (threadId) {
        return crmGet(`/api/crm/comms/threads/${threadId}`).then(
          (data) => applyMessages(data?.messages),
          (err) => {
            if (err?.status === 404 && clientId) return fromInteractions();
            setMsgError(err);
            return undefined;
          }
        );
      }
      if (clientId) return fromInteractions();
      setMsgError(new Error('This thread has no client on file.'));
      return Promise.resolve();
    },
    [applyMessages]
  );

  const openThread = (thread) => {
    if (!thread?.clientId && !thread?.threadId) return;
    setSelected(thread);
    setMessages(null);
    setMsgError(null);
    fetchMessages(thread);
  };

  const backToList = () => {
    setSelected(null);
    setMessages(null);
    setMsgError(null);
    fetchThreads(); // silent refresh — the list reflects what was just sent
  };

  const handleSent = useCallback((msg) => {
    setMessages((prev) => [...(prev ?? []), msg]);
  }, []);

  const refetchSelected = useCallback(() => {
    setSelected((cur) => {
      if (cur) fetchMessages(cur);
      return cur;
    });
  }, [fetchMessages]);

  // Reset the "first scroll is instant" latch whenever the thread changes.
  useEffect(() => {
    scrolledRef.current = false;
  }, [selected]);

  // Auto-scroll to bottom on thread open (instant) and new message (smooth).
  useEffect(() => {
    if (!selected || messages === null) return undefined;
    const behavior = scrolledRef.current ? 'smooth' : 'auto';
    const id = requestAnimationFrame(() => {
      endRef.current?.scrollIntoView({ block: 'end', behavior });
    });
    scrolledRef.current = true;
    return () => cancelAnimationFrame(id);
  }, [selected, messages]);

  const refresh = () => {
    setRefreshing(true);
    setThreadsError(null);
    fetchThreads();
  };

  const retryThreads = () => {
    setThreadsError(null); // back to skeletons while the retry is in flight
    fetchThreads();
  };

  // Normalize → filter → sort once per change.
  const normalized = useMemo(() => {
    if (!Array.isArray(threads)) return [];
    return threads.map((raw) => {
      const thread = normalizeThread(raw);
      const client = raw?.client && typeof raw.client === 'object' ? raw.client : null;
      let phoneKnown = false;
      let phone = null;
      if (client && Object.prototype.hasOwnProperty.call(client, 'phone')) {
        phoneKnown = true;
        phone = client.phone;
      } else if (Object.prototype.hasOwnProperty.call(raw || {}, 'phone')) {
        phoneKnown = true;
        phone = raw.phone;
      } else if (Object.prototype.hasOwnProperty.call(raw || {}, 'client_phone')) {
        phoneKnown = true;
        phone = raw.client_phone;
      }
      const hasMessages = Boolean(
        thread.lastAt || thread.channelRaw || (Number.isFinite(thread.count) && thread.count > 0)
      );
      return {
        ...thread,
        channel: hasMessages ? thread.channel : null,
        hasMessages,
        phoneKnown,
        phone,
      };
    });
  }, [threads]);

  const unreadTotal = useMemo(
    () => normalized.reduce((n, t) => n + (t.unread ? 1 : 0), 0),
    [normalized]
  );

  const visibleThreads = useMemo(() => {
    const q = query.trim().toLowerCase();
    const arr = normalized.filter((t) => {
      if (channelFilter !== 'all' && t.channel !== channelFilter) return false;
      if (q) {
        const hay = `${t.clientName} ${t.snippet} ${t.subject ?? ''}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
    arr.sort((a, b) => ts(b.lastAt) - ts(a.lastAt));
    return arr;
  }, [normalized, query, channelFilter]);

  const filtersActive = channelFilter !== 'all' || query.trim() !== '';

  // ── LEVEL 2 — thread view ─────────────────────────────────────────────────
  if (selected) {
    const name = selected.clientName;
    const emailBlocked = selected.emailKnown && !String(selected.email || '').trim();
    const smsBlocked = selected.phoneKnown && !String(selected.phone || '').trim();
    const loading = messages === null && !msgError;
    const msgErrMsg =
      msgError?.status === 404
        ? 'Conversation history isn’t online yet.'
        : msgError?.message || 'Couldn’t load this conversation.';

    return (
      <section className={styles.wrap} aria-label={`Conversation with ${name}`} aria-busy={loading}>
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
          <h1 className={styles.headName}>{name}</h1>
            <span className={styles.headSub}>
              {selected.clientType && <span className={styles.headType}>{selected.clientType}</span>}
              {selected.emailKnown && selected.email && (
                <span className={styles.headEmail}>{selected.email}</span>
              )}
            </span>
          </span>
          {Number.isFinite(selected.count) && selected.count > 0 && (
            <span className={styles.headCount} title={`${selected.count} messages`}>
              {selected.count}
            </span>
          )}
        </header>

        <div className={styles.threadBody} style={{ paddingBottom: composerPad }}>
          {loading ? (
            <div className={styles.streamSkel} aria-hidden="true">
              <div className={styles.skelMsgIn} />
              <div className={styles.skelMsgOut} />
              <div className={styles.skelMsgIn} />
              <div className={styles.skelMsgOut} />
            </div>
          ) : msgError ? (
            <div className={styles.errorBox} role="alert">
              <span className={styles.errorTick} aria-hidden="true" />
              <p className={styles.errorText}>{msgErrMsg}</p>
              <button
                type="button"
                className={styles.retryBtn}
                onClick={() => {
                  setMsgError(null);
                  fetchMessages(selected);
                }}
              >
                Retry
              </button>
            </div>
          ) : messages.length === 0 ? (
            <p className={styles.streamEmpty}>No messages yet — open the line below.</p>
          ) : (
            <ol className={styles.stream} role="list">
              {messages.map((raw, i) => {
                const m = normalizeMessage(raw, i);

                if (m.system) {
                  const when = relTime(m.createdAt);
                  return (
                    <li key={m.id} className={styles.sysLine}>
                      <span className={styles.sysRule} aria-hidden="true" />
                      <span className={styles.sysText}>
                        {m.systemLabel}
                        {when && ` · ${when}`}
                      </span>
                      <span className={styles.sysRule} aria-hidden="true" />
                    </li>
                  );
                }

                const status = statusMeta(m.status);
                const when = fmtTime(m.createdAt);
                const deliveryStatus = firstString(raw?._deliveryStatus, raw?.delivery_status);
                const recordLabel =
                  m.queued && !status
                    ? 'Queued — AI sends'
                    : (raw?._loggedOnly || (m.channel === 'sms' && deliveryStatus === 'not_sent')) && !status
                      ? 'Logged only — not sent'
                      : m.channel === 'note' && !status
                        ? 'Internal note'
                        : null;

                return (
                  <li key={m.id} className={`${styles.msg} ${m.out ? styles.msgOut : styles.msgIn}`}>
                    <div className={styles.bubble}>
                      {m.voice ? (
                        <>
                          <span className={styles.voiceRow}>
                            <span className={styles.voiceGlyph} aria-hidden="true">
                              {GLYPHS.waveform}
                            </span>
                            <span className={styles.voiceLabel}>Voice note</span>
                          </span>
                          {m.text ? (
                            <p className={styles.transcript}>{m.text}</p>
                          ) : (
                            <p className={styles.transcriptGhost}>No transcript</p>
                          )}
                        </>
                      ) : (
                        <>
                          {m.subject && <span className={styles.msgSubject}>{m.subject}</span>}
                          {m.text ? (
                            <p className={styles.msgBody}>{m.text}</p>
                          ) : (
                            <p className={styles.msgBodyGhost}>No content</p>
                          )}
                        </>
                      )}
                      <span className={styles.msgMeta}>
                        <span className={styles.metaGlyph} aria-hidden="true">
                          {GLYPHS[m.channel]}
                        </span>
                        {m.role && <span>{m.role}</span>}
                        {when && <span>{when}</span>}
                        {status && (
                          <span className={styles.statusBadge} data-tone={status.tone}>
                            {status.label}
                          </span>
                        )}
                      </span>
                    </div>
                    {recordLabel && <span className={styles.queuedChip}>{recordLabel}</span>}
                  </li>
                );
              })}
            </ol>
          )}
          <div ref={endRef} aria-hidden="true" />
        </div>

        <CommsComposer
          clientId={selected.clientId}
          clientName={name}
          emailBlocked={emailBlocked}
          smsBlocked={smsBlocked}
          onSent={handleSent}
          onRefetch={refetchSelected}
          onHeightChange={setComposerPad}
        />
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
      aria-labelledby="inbox-title"
      aria-busy={isLoading || refreshing}
    >
      <header className={styles.topRow}>
        <div className={styles.destinationTitle}>
          <span className={styles.kicker}>Unified communications</span>
          <h1 id="inbox-title">Inbox</h1>
          <small>{unreadTotal > 0 ? `${unreadTotal} unread conversations` : 'All caught up'}</small>
        </div>
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

      <CommandApprovalPanel />

      {!isLoading && !(threadsError && threads === null) && normalized.length > 0 && (
        <div className={styles.toolbar}>
          <div className={styles.searchBox}>
            <span className={styles.searchGlyph} aria-hidden="true">
              {GLYPHS.search}
            </span>
            <input
              className={styles.searchInput}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search conversations"
              aria-label="Search conversations"
              type="search"
            />
            {query && (
              <button
                type="button"
                className={styles.searchClear}
                onClick={() => setQuery('')}
                aria-label="Clear search"
              >
                {GLYPHS.close}
              </button>
            )}
          </div>
          <div className={styles.filterRow} role="group" aria-label="Filter by channel">
            {CHANNEL_FILTERS.map((f) => (
              <button
                key={f.id}
                type="button"
                className={`${styles.filterKey}${channelFilter === f.id ? ` ${styles.filterKeyOn}` : ''}`}
                aria-pressed={channelFilter === f.id}
                onClick={() => setChannelFilter(f.id)}
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>
      )}

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

          {normalized.length === 0 ? (
            <div className={styles.empty}>
              <span className={styles.emptyGlyph} aria-hidden="true">{GLYPHS.sms}</span>
              <p className={styles.emptyTitle}>No conversations yet</p>
              <p className={styles.emptyHint}>
                Messages, emails, and portal activity for your clients will land here.
              </p>
            </div>
          ) : visibleThreads.length === 0 ? (
            <div className={styles.empty}>
              <span className={styles.emptyGlyph} aria-hidden="true">{GLYPHS.search}</span>
              <p className={styles.emptyTitle}>No matches</p>
              <p className={styles.emptyHint}>No conversations match your filters.</p>
              {filtersActive && (
                <button
                  type="button"
                  className={styles.retryBtn}
                  onClick={() => {
                    setQuery('');
                    setChannelFilter('all');
                  }}
                >
                  Clear filters
                </button>
              )}
            </div>
          ) : (
            <ul className={styles.threadList} role="list">
              {visibleThreads.map((t, i) => {
                const snippet = t.hasMessages
                  ? firstString(t.snippet) ?? 'No messages yet'
                  : 'Start a conversation';
                const when = relTime(t.lastAt);

                return (
                  <li key={t.threadId ?? t.clientId ?? `thread-${i}`}>
                    <button
                      type="button"
                      className={`${styles.threadRow}${t.unread ? ` ${styles.threadRowUnread}` : ''}`}
                      onClick={() => openThread(t)}
                      disabled={!t.clientId && !t.threadId}
                      style={{ animationDelay: `${Math.min(i, 8) * 60}ms` }}
                    >
                      <span className={styles.ring} aria-hidden="true">
                        {initialsOf(t.clientName)}
                      </span>
                      <span className={styles.threadMain}>
                        <span className={styles.threadTop}>
                          {t.unread && <span className={styles.unreadDot} aria-label="Unread" />}
                          <span className={styles.threadName}>{t.clientName}</span>
                          {t.clientType && <span className={styles.typeTag}>{t.clientType}</span>}
                          {when && <span className={styles.threadTime}>{when}</span>}
                        </span>
                        <span className={styles.threadBottom}>
                          <span
                            className={styles.chanGlyph}
                            data-dir={t.direction === 'out' ? 'out' : 'in'}
                            aria-hidden="true"
                          >
                            {GLYPHS[t.channel] || GLYPHS.plus}
                          </span>
                          <span className={styles.snippet}>{snippet}</span>
                          {t.unreadCount && t.unreadCount > 0 ? (
                            <span className={`${styles.countChip} ${styles.countChipUnread}`}>
                              {t.unreadCount}
                            </span>
                          ) : (
                            Number.isFinite(t.count) &&
                            t.count > 0 && <span className={styles.countChip}>{t.count}</span>
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
