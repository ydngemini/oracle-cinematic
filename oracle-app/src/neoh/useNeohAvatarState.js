import { useLayoutEffect, useState, useSyncExternalStore } from 'react';

import { createAvatarStore } from './avatarFacts';

/**
 * useNeohAvatarState — one derived truth for Neoh's face.
 *
 * Everything the avatar shows comes from facts the app already has, turned
 * into validated NeohAnimationEvents (avatarFacts.js) and resolved by the
 * statechart (animationEvent.js):
 *
 *   · thinking only while a request is in flight and recently active;
 *   · acting only while a command request is executing;
 *   · success only for a backend receipt of an applied action, held for
 *     SUCCESS_HOLD_MS from when that receipt arrived — never on mount for an
 *     old one, never for "analysis complete";
 *   · listening only while the browser recogniser is capturing;
 *   · speaking only while audio is actually playing (nothing in the browser
 *     plays Neoh's voice today, so callers never pass it);
 *   · error for a real failure, with a reason (timeout, unavailable,
 *     interrupted, rejected, failed, send_refused); cancellation is idle with
 *     reason "cancelled", not an error.
 *
 * Timers exist only to END states (an expiry, the end of a hysteresis wait).
 * No timer can start one, and nothing runs per frame.
 *
 * Callers pass real state in rather than this hook reaching into five
 * contexts itself, so it stays pure enough to test and impossible to wire
 * to the wrong source by accident.
 *
 * @param {object} facts
 * @param {string}  [facts.connection]  channel connection ('online' | …)
 * @param {Array}   [facts.messages]    conversation (assistant turns carry status + timestamps)
 * @param {boolean} [facts.asking]      deterministic ask path in flight
 * @param {boolean} [facts.micActive]   the browser recogniser is capturing
 * @param {boolean} [facts.speaking]    an audio element is PLAYING Neoh's voice
 * @param {number}  [facts.audioLevel]  0..1 smoothed amplitude
 * @param {object}  [facts.commandStatus] AssistantContext command status (stamped `at`)
 * @param {string}  [facts.actionName]  executing tool/action
 * @param {boolean} [facts.attention]   something is waiting for the person's approval
 * @param {boolean} [facts.failed]      a message could not be sent
 * @returns {{state: string, actionType: string, reason: string|null, audioLevel: number, attentionLevel: number}}
 */
export function useNeohAvatarState(facts = {}) {
  const [store] = useState(createAvatarStore);
  const view = useSyncExternalStore(store.subscribe, store.getView, store.getView);

  const {
    connection = 'online',
    messages = EMPTY,
    asking = false,
    micActive = false,
    speaking = false,
    audioLevel = 0,
    commandStatus = null,
    actionName = '',
    attention = false,
    failed = false,
  } = facts;

  // Layout effect: the resolved state is in place before the browser paints,
  // so a state that just ended is never shown for a frame.
  useLayoutEffect(() => {
    store.observe({
      connection, messages, asking, micActive, speaking, audioLevel, commandStatus, actionName, attention, failed,
    });
  }, [store, connection, messages, asking, micActive, speaking, audioLevel, commandStatus, actionName, attention, failed]);

  useLayoutEffect(() => () => store.dispose(), [store]);

  return view;
}

const EMPTY = Object.freeze([]);

export default useNeohAvatarState;
