import { AnimatePresence, motion } from 'framer-motion';
import { Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react';

import { useAssistant } from '../components/AssistantContext';
import { useMotionPolicy } from './motion';
import { NeohAvatar } from './NeohAvatar';
import { useNeohAvatarState } from './useNeohAvatarState';
import { isBusy, restLabel, surfaceState } from './surfaceModel';
import { useGlobalShortcuts } from './useGlobalShortcuts';
import { useNeohChannel } from './useNeohChannel';
import { useCompletedAnnouncement } from './useNeohComposer';
import styles from './NeohSurface.module.css';

/**
 * NeohSurface — one object that changes shape.
 *
 * At rest it is a pill above the deck that says what Neoh is looking at.
 * ⌘K, "/" or a tap and it is a bar with the cursor in it. Send, and it holds
 * as thinking. Open the conversation and it is a panel. A record sheet takes
 * the screen and it yields. It is the same element throughout — framer's
 * `layout` on one persistent node, one spring — so the eye tracks a thing
 * moving, never a thing replaced. Reduced motion cuts; a low motion budget
 * turns layout animation off and keeps every state.
 *
 * It is the quick way in. The Neoh tab is the same conversation at full
 * height (the thread lives in the shared store), so "Open the full
 * conversation" is a change of size, not a change of chat. The shell does
 * not mount this on the Neoh tab — one composer on screen, never two.
 *
 * At rest this file is all that loads: the pill, the face, the channel's
 * availability and the reply announcer. Everything the open bar needs is
 * NeohSurfacePanel, fetched on first open (or on hover/focus of the pill).
 */

const loadPanel = () => import('./NeohSurfacePanel');
const NeohSurfacePanel = lazy(() => loadPanel().then((m) => ({ default: m.NeohSurfacePanel })));

const MAX_DRAFT = 8_000;

export function NeohSurface({ entityOpen = false, onOpenEntity, onExpand, onNavigate }) {
  const {
    open, setOpen, record, clearRecord, commandRequest, clearCommandRequest, commandStatus,
    requestCommand,
  } = useAssistant();
  const channel = useNeohChannel({ open });
  const policy = useMotionPolicy();
  const [showResult, setShowResult] = useState(false);
  const draftState = useState('');
  const [draft, setDraft] = draftState;
  const [activity, setActivity] = useState({ asking: false, listening: false });
  const pillRef = useRef(null);

  const state = surfaceState({ open, entityOpen, messages: channel.messages, showResult });
  const expanded = state === 'input' || state === 'thinking' || state === 'result';
  const busy = activity.asking || isBusy(channel.messages);
  const announcement = useCompletedAnnouncement(channel.messages);

  // The face, derived from the same facts the shape is.
  const avatar = useNeohAvatarState({
    connection: channel.connection,
    messages: channel.messages,
    asking: activity.asking,
    // The browser recogniser actually capturing is a real source for
    // "listening". Nothing yet is a source for "speaking" — there is no TTS.
    micActive: activity.listening,
    commandStatus,
    failed: Boolean(channel.notice) && channel.connection !== 'online',
  });

  // Voice gives Neoh room to be expressive: while the person is talking to
  // Neoh the face in the bar grows into a bust, and shrinks back after.
  const voiceActive = avatar.state === 'listening' || avatar.state === 'speaking';

  const openBar = useCallback(() => setOpen(true), [setOpen]);

  const collapse = useCallback(() => {
    setOpen(false);
    setShowResult(false);
    setActivity({ asking: false, listening: false });
    window.requestAnimationFrame(() => pillRef.current?.focus());
  }, [setOpen]);

  useGlobalShortcuts({
    onFocus: openBar,
    onEscape: () => {
      // While the microphone is open the panel's own Escape stops it first.
      if (activity.listening) return;
      if (open) collapse();
    },
  });

  // Record surfaces and Work hand a draft straight to Neoh. It opens with the
  // text staged; it never sends on its own. A request addressed to the full
  // conversation is the Neoh tab's to take, not this bar's.
  useEffect(() => {
    if (!commandRequest || commandRequest.surface === 'conversation') return undefined;
    const frame = window.requestAnimationFrame(() => {
      setDraft((commandRequest.rawText || '').slice(0, MAX_DRAFT));
      clearCommandRequest();
      setOpen(true);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [clearCommandRequest, commandRequest, setDraft, setOpen]);

  // A reply arriving while the bar is open is the moment to show the panel.
  useEffect(() => {
    if (!open || state !== 'thinking') return undefined;
    const frame = window.requestAnimationFrame(() => setShowResult(true));
    return () => window.cancelAnimationFrame(frame);
  }, [open, state]);

  const expand = useCallback(() => {
    // The draft travels with the person to the full view, staged there.
    if (draft.trim()) requestCommand({ rawText: draft, surface: 'conversation' });
    setDraft('');
    setOpen(false);
    setShowResult(false);
    onExpand?.();
  }, [draft, onExpand, requestCommand, setDraft, setOpen]);

  if (channel.available !== true) return null;

  const label = restLabel({ record, messages: channel.messages, busy });
  const transition = policy.transition;

  return (
    <motion.div
      layout={policy.layout}
      layoutId="neoh-surface"
      transition={transition}
      className={styles.surface}
      data-state={state}
      role={expanded ? 'dialog' : undefined}
      aria-label={expanded ? 'Neoh' : undefined}
    >
      <AnimatePresence mode="popLayout" initial={false}>
        {state === 'rest' || state === 'yielded' ? (
          <motion.button
            key="pill"
            ref={pillRef}
            type="button"
            className={styles.pill}
            onClick={openBar}
            onPointerEnter={() => { void loadPanel(); }}
            onFocus={() => { void loadPanel(); }}
            aria-label={`${label}. Press slash or command K.`}
            aria-expanded={false}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={transition}
          >
            <motion.span layoutId="neoh-avatar" layout={policy.layout} transition={transition} className={styles.markSlot}>
              <NeohAvatar
                state={avatar.state}
                audioLevel={avatar.audioLevel}
                actionType={avatar.actionType}
                attentionLevel={avatar.attentionLevel}
              />
            </motion.span>
            <span className={styles.label}>{label}</span>
            <kbd className={styles.kbd} aria-hidden="true">/</kbd>
          </motion.button>
        ) : (
          <motion.div
            key="open"
            className={styles.open}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={transition}
          >
            <Suspense fallback={<div className={styles.bar} aria-hidden="true" />}>
              <NeohSurfacePanel
                channel={channel}
                record={record}
                clearRecord={clearRecord}
                draftState={draftState}
                state={state}
                setShowResult={setShowResult}
                avatar={avatar}
                voiceActive={voiceActive}
                policy={policy}
                onCollapse={collapse}
                onExpand={onExpand ? expand : undefined}
                onNavigate={onNavigate}
                onOpenEntity={onOpenEntity}
                onActivity={setActivity}
              />
            </Suspense>
          </motion.div>
        )}
      </AnimatePresence>
      <p className={styles.srOnly} aria-live="polite" aria-atomic="true">{announcement}</p>
    </motion.div>
  );
}

export default NeohSurface;
