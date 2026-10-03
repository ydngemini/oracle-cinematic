import { useEffect, useMemo, useState } from 'react';

import { crmGet } from '../state/useCrmApi';
import { TERMINAL_COMMAND_STATES, receiptsByMessage } from './actionLabels';

/**
 * useCommandReceipts — the durable outcome of outreach Neoh staged.
 *
 * A text, email, call or calendar write the chat agent prepares is not an
 * applied action: it is a command_executions row waiting for approval, and
 * the AI_CHAT_COMPLETE frame (rightly) does not list it as one. So the only
 * evidence of it in the conversation used to be the model's own sentence.
 * This reads the rows themselves, joins each to the turn that staged it, and
 * keeps polling — gently, and only while something is still moving — so
 * "waiting for approval" becomes "Text sent" (or "needs review") in place.
 *
 * One request when the conversation opens or a turn completes; nothing at
 * all when there is no assistant turn to attach a receipt to, or when this
 * workspace's plan has no automation (the route answers 403 — that is a
 * capability, not an error, and it is remembered for the session).
 */

const POLL_MS = 15_000;
let unavailable = false;

export function resetCommandReceiptCache() {
  unavailable = false;
}

export function useCommandReceipts({ enabled = true, revision = 0, messages = [] }) {
  const [commands, setCommands] = useState([]);
  const [tick, setTick] = useState(0);
  const hasTurns = useMemo(
    () => messages.some((m) => m?.role === 'assistant' && m?.status === 'completed' && !m?.local),
    [messages],
  );
  const active = enabled && hasTurns;

  useEffect(() => {
    if (!active || unavailable) return undefined;
    let live = true;
    const timer = window.setTimeout(() => {
      crmGet('/api/commands?limit=50', { retries: 0 }).then(
        (data) => { if (live) setCommands(Array.isArray(data?.commands) ? data.commands : []); },
        (error) => {
          if (error?.status === 403 || error?.status === 404) unavailable = true;
        },
      );
    }, 150);
    return () => { live = false; window.clearTimeout(timer); };
  }, [active, revision, tick]);

  const byMessage = useMemo(() => receiptsByMessage(commands), [commands]);

  // Poll only while a linked command can still change, and only while the
  // page is visible: a backgrounded tab must not keep asking.
  const moving = useMemo(() => {
    for (const list of byMessage.values()) {
      if (list.some((c) => !TERMINAL_COMMAND_STATES.has(c.state))) return true;
    }
    return false;
  }, [byMessage]);

  useEffect(() => {
    if (!active || !moving) return undefined;
    const interval = window.setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return;
      setTick((n) => n + 1);
    }, POLL_MS);
    return () => window.clearInterval(interval);
  }, [active, moving]);

  return byMessage;
}
