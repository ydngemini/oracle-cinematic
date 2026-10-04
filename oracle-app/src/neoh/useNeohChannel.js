import { useCallback, useEffect, useState } from 'react';

import { ACTIONS, useOracleDispatch, useOracleState } from '../state';
import { crmGet, crmPost } from '../state/useCrmApi';

/**
 * useNeohChannel — the private operating channel, without the panel.
 *
 * Extracted from AssistantShell so every Neoh surface speaks EXACTLY the
 * protocol the backend already accepts: the same optimistic pair of local
 * messages, the same AI_CHAT_SEND frame, the same hydrate call and the same
 * undo. Anything that reads or writes the wire lives here and nowhere else.
 *
 * The conversation itself is NOT held here. It is the app-wide reducer's
 * `aiChatMessages`, so the floating composer and the Neoh tab are two views
 * of one thread: start a question in the bar, open the tab, and it is there.
 *
 * Two hooks mounted at once must not mean two of every request. The status
 * check is answered once per session, and history is fetched once per
 * conversation revision no matter how many surfaces ask (see `hydrateFor`).
 */

const HISTORY_LIMIT = 80;

// A failed status check — a 429 after moving quickly between views, a dropped
// connection — is not "switched off". It is asked again on this schedule and
// only then reported, as unreachable rather than disabled.
export const STATUS_RETRY_MS = Object.freeze([1500, 4000, 9000]);

/** The record types the backend's ChatContext accepts (ai_chat_models.py).
 *  Anything else is rejected as INVALID_MESSAGE, so it must never be sent. */
export const WIRE_CONTEXT_TYPES = Object.freeze(['client', 'lead', 'listing', 'contract']);

// The surfaces' names for things, mapped to the wire's. A property sheet is a
// `leads` row; it registered as 'property', which the backend rejected — so
// asking Neoh about the house on screen failed every time.
const CONTEXT_ALIASES = Object.freeze({ property: 'lead', person: 'client' });

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * The context frame for a selected record, or null when the backend could
 * not use it. Pure, so the chip and the wire can agree on what Neoh sees.
 */
export function wireContext(record) {
  if (!record?.type || !record?.id) return null;
  const type = CONTEXT_ALIASES[record.type] || record.type;
  if (!WIRE_CONTEXT_TYPES.includes(type)) return null;
  const id = String(record.id);
  if (!UUID.test(id)) return null;
  return { type, id };
}

// ── Shared, session-wide request state ──────────────────────────────────
let statusPromise = null;
// Keyed by the store's dispatch, so a fresh store (a new sign-in) hydrates
// again even if its revision counter starts where the old one did.
let hydrated = new WeakMap();

/** /api/ai/chat/status, asked once per session. A failure is not cached —
 *  the next surface to mount asks again rather than staying dark forever. */
export function fetchChatStatus() {
  if (!statusPromise) {
    statusPromise = crmGet('/api/ai/chat/status').catch((error) => {
      statusPromise = null;
      throw error;
    });
  }
  return statusPromise;
}

/** True the first time a store sees a revision; every later caller skips. */
function hydrateFor(dispatch, revision) {
  if (hydrated.get(dispatch) === revision) return false;
  hydrated.set(dispatch, revision);
  return true;
}

/** Test seam: forget the session-wide caches. */
export function resetNeohChannelCache() {
  statusPromise = null;
  hydrated = new WeakMap();
}

export function useNeohChannel({ open = false } = {}) {
  const { aiChatMessages, aiChatRevision, aiChatConnection } = useOracleState();
  const { dispatch, wsRef } = useOracleDispatch();
  const [available, setAvailable] = useState(null);
  const [statusFailed, setStatusFailed] = useState(false);
  const [statusAttempt, setStatusAttempt] = useState(0);
  const [notice, setNotice] = useState('');
  const [undoing, setUndoing] = useState('');

  useEffect(() => {
    let active = true;
    let timer = 0;
    fetchChatStatus().then(
      (data) => {
        if (!active) return;
        setStatusFailed(false);
        setAvailable(data?.enabled === true);
      },
      () => {
        if (!active) return;
        if (statusAttempt < STATUS_RETRY_MS.length) {
          timer = window.setTimeout(() => setStatusAttempt((n) => n + 1), STATUS_RETRY_MS[statusAttempt]);
        } else {
          setStatusFailed(true);
          setAvailable(false);
        }
      },
    );
    return () => { active = false; window.clearTimeout(timer); };
  }, [statusAttempt]);

  const retryStatus = useCallback(() => {
    setStatusFailed(false);
    setAvailable(null);
    setStatusAttempt(0);
  }, []);

  useEffect(() => {
    let active = true;
    const timer = window.setTimeout(() => {
      if (available !== true) return;
      if (!hydrateFor(dispatch, aiChatRevision)) return;
      crmGet(`/api/ai/chat/messages?limit=${HISTORY_LIMIT}`).then(
        // Dispatched even if this surface has unmounted meanwhile: the store
        // is shared, and the surface that replaced it skipped this revision
        // on the strength of this request.
        (data) => dispatch({ type: ACTIONS.AI_CHAT_HYDRATE, payload: data?.messages || [] }),
        () => {
          // Let the next surface (or the next revision) try again.
          hydrated.delete(dispatch);
          if (active && open) setNotice('Earlier messages are unavailable right now. New ones still work.');
        },
      );
    }, aiChatRevision ? 120 : 0);
    return () => { active = false; window.clearTimeout(timer); };
  }, [aiChatRevision, available, dispatch, open]);

  /** Send one message. `record` is the selected record or null; attachments
   *  are ids already saved on that record. Returns false when nothing went. */
  const send = useCallback((text, record = null, attachmentIds = []) => {
    const content = (text || '').trim();
    if (!content && attachmentIds.length === 0) return false;
    if (wsRef.current?.readyState !== WebSocket.OPEN) {
      setNotice('Neoh is reconnecting. Your message is still here — send it again in a moment.');
      return false;
    }
    const context = wireContext(record);
    const requestId = crypto.randomUUID();
    const now = new Date().toISOString();
    dispatch({
      type: ACTIONS.AI_CHAT_SEND_LOCAL,
      payload: {
        user: {
          id: `local-user-${requestId}`, request_id: requestId, role: 'user', content,
          status: 'completed', context: record, attachments: [], created_at: now, local: true,
        },
        assistant: {
          id: `local-assistant-${requestId}`, request_id: requestId, role: 'assistant',
          content: '', status: 'pending', context: record, actions: [], created_at: now, local: true,
        },
      },
    });
    wsRef.current.send(JSON.stringify({
      type: 'AI_CHAT_SEND', version: 1, request_id: requestId, content,
      context,
      attachment_ids: attachmentIds,
    }));
    setNotice('');
    return true;
  }, [dispatch, wsRef]);

  const undo = useCallback(async (action) => {
    setUndoing(action.action_id);
    try {
      const result = await crmPost(`/api/ai/chat/actions/${encodeURIComponent(action.action_id)}/undo`, {});
      dispatch({ type: ACTIONS.AI_CHAT_ACTION_UNDONE, payload: result });
    } catch (error) {
      setNotice(error.message || 'This change could not be undone.');
    } finally {
      setUndoing('');
    }
  }, [dispatch]);

  const clearNotice = useCallback(() => setNotice(''), []);

  return {
    available,
    statusFailed,
    retryStatus,
    messages: aiChatMessages,
    connection: aiChatConnection,
    revision: aiChatRevision,
    notice,
    clearNotice,
    undoing,
    send,
    undo,
  };
}
