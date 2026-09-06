import { useCallback, useEffect, useState } from 'react';

import { ACTIONS, useOracleDispatch, useOracleState } from '../state';
import { crmGet, crmPost } from '../state/useCrmApi';

/**
 * useNeohChannel — the private operating channel, without the panel.
 *
 * Extracted from AssistantShell so the morphing surface speaks EXACTLY the
 * protocol the backend already accepts: the same optimistic pair of local
 * messages, the same AI_CHAT_SEND frame, the same hydrate call and the same
 * undo. Anything that reads or writes the wire lives here and nowhere else;
 * the surface only decides what shape to be.
 */

const HISTORY_LIMIT = 80;

export function useNeohChannel({ open = false } = {}) {
  const { aiChatMessages, aiChatRevision, aiChatConnection } = useOracleState();
  const { dispatch, wsRef } = useOracleDispatch();
  const [available, setAvailable] = useState(null);
  const [notice, setNotice] = useState('');
  const [undoing, setUndoing] = useState('');

  useEffect(() => {
    let active = true;
    crmGet('/api/ai/chat/status').then(
      (data) => { if (active) setAvailable(data?.enabled === true); },
      () => { if (active) setAvailable(false); },
    );
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    const timer = window.setTimeout(() => {
      if (available !== true) return;
      crmGet(`/api/ai/chat/messages?limit=${HISTORY_LIMIT}`).then(
        (data) => {
          if (active) dispatch({ type: ACTIONS.AI_CHAT_HYDRATE, payload: data?.messages || [] });
        },
        () => { if (active && open) setNotice('Conversation history is temporarily unavailable.'); },
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
      setNotice('Reconnecting to the private channel. Try again in a moment.');
      return false;
    }
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
      context: record ? { type: record.type, id: record.id } : null,
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
    messages: aiChatMessages,
    connection: aiChatConnection,
    notice,
    clearNotice,
    undoing,
    send,
    undo,
  };
}
