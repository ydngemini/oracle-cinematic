import { useEffect, useMemo, useRef, useState } from 'react';

import { avatarState, SUCCESS_HOLD_MS } from './avatarModel';

/**
 * useNeohAvatarState — one derived truth for Neoh's face.
 *
 * Everything the avatar shows comes from facts the app already has. There is
 * no timer anywhere in here that pretends Neoh is busy: if the face says
 * thinking, a message really is pending; if it says acting, an action really
 * is executing; if it says success, the backend really confirmed something.
 *
 * The one timer that does exist holds a SUCCESS acknowledgement for a beat
 * so it can be seen before it settles — it can only ever shorten a state
 * that already happened, never invent one.
 *
 * Callers pass real state in rather than this hook reaching into five
 * contexts itself, so it stays pure enough to test and impossible to wire
 * to the wrong source by accident.
 *
 * @param {object} facts
 * @param {string}  [facts.connection]  channel connection ('online' | …)
 * @param {Array}   [facts.messages]    conversation
 * @param {boolean} [facts.asking]      deterministic ask path in flight
 * @param {boolean} [facts.micActive]   person is speaking to Neoh
 * @param {boolean} [facts.speaking]    assistant audio is playing
 * @param {number}  [facts.audioLevel]  0..1 smoothed amplitude
 * @param {object}  [facts.commandStatus] AssistantContext command status
 * @param {string}  [facts.actionName]  executing tool/action
 * @param {boolean} [facts.attention]   something needs the person
 * @param {boolean} [facts.failed]      a genuine failure
 */
export function useNeohAvatarState(facts = {}) {
  const {
    connection = 'online',
    messages = [],
    asking = false,
    micActive = false,
    speaking = false,
    audioLevel = 0,
    commandStatus = null,
    actionName = '',
    attention = false,
    failed = false,
  } = facts;

  // A command that the backend has confirmed done is the only thing that may
  // light SUCCESS. A clicked button is not a success; a receipt is.
  const commandState = String(commandStatus?.state || '').toLowerCase();
  const confirmedSuccess = commandState === 'completed' || commandState === 'succeeded';
  const commandFailed = commandState === 'failed' || commandState === 'error';
  const commandNeedsPerson = commandState === 'awaiting_approval' || commandState === 'needs_approval';
  const commandRunning = commandState === 'running' || commandState === 'executing';

  const [successHeld, setSuccessHeld] = useState(false);
  const lastConfirmed = useRef(false);

  // Latch a confirmed success briefly so it is visible, then let it go.
  useEffect(() => {
    if (confirmedSuccess && !lastConfirmed.current) {
      setSuccessHeld(true);
      const timer = window.setTimeout(() => setSuccessHeld(false), SUCCESS_HOLD_MS);
      lastConfirmed.current = true;
      return () => window.clearTimeout(timer);
    }
    if (!confirmedSuccess) lastConfirmed.current = false;
    return undefined;
  }, [confirmedSuccess]);

  return useMemo(() => {
    const resolvedAction = actionName || (commandRunning ? commandStatus?.detail || 'generic' : '');
    const { state, actionType } = avatarState({
      connection,
      messages,
      asking,
      micActive,
      speaking,
      attention: attention || commandNeedsPerson,
      failed: failed || commandFailed,
      actionName: resolvedAction,
      succeeded: successHeld,
    });
    return {
      state,
      actionType,
      audioLevel: state === 'listening' || state === 'speaking' ? audioLevel : 0,
      attentionLevel: state === 'needs_attention' ? 1 : 0,
    };
  }, [
    actionName,
    asking,
    attention,
    audioLevel,
    commandFailed,
    commandNeedsPerson,
    commandRunning,
    commandStatus,
    connection,
    failed,
    messages,
    micActive,
    speaking,
    successHeld,
  ]);
}

export default useNeohAvatarState;
