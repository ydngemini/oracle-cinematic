import { AnimatePresence, motion } from 'framer-motion';
import { ArrowUp, History, Mic, Square, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useAssistant } from '../components/AssistantContext';
import { crmPost } from '../state/useCrmApi';
import { AssistantMessages } from '../components/AssistantMessages';
import { useMotionPolicy } from './motion';
import { NeohAvatar } from './NeohAvatar';
import { useNeohAvatarState } from './useNeohAvatarState';
import { inputPlaceholder, isBusy, restLabel, surfaceState } from './surfaceModel';
import { useGlobalShortcuts } from './useGlobalShortcuts';
import { useNeohChannel } from './useNeohChannel';
import { useSpeechInput } from './useSpeechInput';
import { Blocks } from './Blocks';
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
 * It owns no protocol. useNeohChannel speaks the wire; surfaceModel decides
 * the shape; this file only renders.
 */

const MAX_DRAFT = 8_000;

export function NeohSurface({ entityOpen = false, onOpenEntity }) {
  const {
    open, setOpen, record, clearRecord, commandRequest, clearCommandRequest, commandStatus,
  } = useAssistant();
  const channel = useNeohChannel({ open });
  const policy = useMotionPolicy();
  const [draft, setDraft] = useState('');
  const [showResult, setShowResult] = useState(false);
  // The last rendered answer, when the question was one Neoh could draw.
  const [rendered, setRendered] = useState(null);
  const [asking, setAsking] = useState(false);
  const inputRef = useRef(null);
  const submitRef = useRef(null);
  const pillRef = useRef(null);
  const listRef = useRef(null);

  const busy = asking || isBusy(channel.messages);
  const state = surfaceState({ open, entityOpen, messages: channel.messages, showResult });
  const expanded = state === 'input' || state === 'thinking' || state === 'result';

  // The face, derived from the same facts the shape is. Nothing here sets an
  // avatar state by hand; there is one source and it is what is true.
  const avatar = useNeohAvatarState({
    connection: channel.connection,
    messages: channel.messages,
    asking,
    commandStatus,
    failed: Boolean(channel.notice) && channel.connection !== 'online',
  });

  // Voice gives Neoh room to be expressive: when a real-time conversation is
  // running the face in the bar grows into a bust, and shrinks back when it
  // ends. The shared layoutId carries it, so it is one character changing
  // size rather than two components swapping.
  //
  // This is wired to real state and nothing else. `listening` and `speaking`
  // come from micActive/speaking in useNeohAvatarState, which NeohSurface
  // does not yet have a source for — the realtime voice channel is a backend
  // capability that has not reached this component. So the mechanism is live
  // and correct and will simply never fire until voice is connected here.
  // Faking a trigger to demo it would make the avatar lie about the session.
  const voiceActive = avatar.state === 'listening' || avatar.state === 'speaking';

  // Speaking to Neoh. A finished utterance is submitted immediately rather
  // than dropped into the field for the person to press send again — holding
  // a button and then having to click is two interactions for one intent.
  const speech = useSpeechInput({
    onFinal: (text) => { void submitRef.current?.(text); },
    disabled: busy,
  });

  const focusInput = useCallback(() => {
    setOpen(true);
    window.requestAnimationFrame(() => inputRef.current?.focus());
  }, [setOpen]);

  const collapse = useCallback(() => {
    setOpen(false);
    setShowResult(false);
    window.requestAnimationFrame(() => pillRef.current?.focus());
  }, [setOpen]);

  useGlobalShortcuts({
    onFocus: focusInput,
    onEscape: () => { if (open) collapse(); },
  });

  // Today, record surfaces and the old AI tab hand a draft straight to Neoh.
  // It opens with the text staged; it never sends on its own.
  useEffect(() => {
    if (!commandRequest) return undefined;
    const frame = window.requestAnimationFrame(() => {
      setDraft((commandRequest.rawText || '').slice(0, MAX_DRAFT));
      clearCommandRequest();
      focusInput();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [clearCommandRequest, commandRequest, focusInput]);

  // A reply arriving while the bar is open is the moment to show the panel.
  useEffect(() => {
    if (!open || state !== 'thinking') return undefined;
    const frame = window.requestAnimationFrame(() => setShowResult(true));
    return () => window.cancelAnimationFrame(frame);
  }, [open, state]);

  useEffect(() => {
    if (state === 'result' && listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [channel.messages, state]);

  // Ask the deterministic path first; fall through to the model on a miss.
  // The order matters: a question with a real interface behind it should never
  // come back as a paragraph, and a question without one must still be
  // answered rather than refused.
  // Built once, not during render: `collapse` reads a ref, and the linter is
  // right that a ref must not be reached for while rendering.
  const blockContext = useMemo(() => ({
    onOpen: (href) => { collapse(); onOpenEntity?.(href); },
    onAct: (item) => setDraft(item.action || ''),
  }), [collapse, onOpenEntity]);

  // `spoken` goes through here unchanged. A turn that arrived from the
  // microphone is the same kind of turn as one that arrived from the
  // keyboard — same ask path, same channel, same transcript. Nothing
  // downstream can tell the difference, which is the whole point.
  const submit = async (override) => {
    const spoken = typeof override === 'string';
    const text = (spoken ? override : draft).trim();
    if (!text) return;
    // A spoken turn must not eat a half-typed message. The composer invites
    // the person to "keep typing" while the microphone is open, so anything
    // already in the field survives the utterance and stays theirs to send.
    if (!spoken) setDraft('');
    setAsking(true);
    // The ask path is an optimisation, never a gate: if it fails, the question
    // still reaches the model, which is what would have happened without it.
    const answer = await crmPost('/api/neoh/ask', { text }).catch(() => null);
    setAsking(false);
    if (answer && !answer.fallthrough && (answer.blocks || []).length > 0) {
      setRendered({ ...answer, question: text });
      setShowResult(true);
      return;
    }
    setRendered(null);
    if (!channel.send(text, record)) {
      // The channel refused (reconnecting); put the text back rather than
      // silently eating it — but never overwrite a draft the person is still
      // typing with a spoken utterance they have already finished.
      setDraft((current) => (current.trim() ? current : text));
      return;
    }
    setShowResult(true);
  };

  // Kept current so useSpeechInput can reach the latest submit without
  // depending on it and tearing down a live recognition session.
  useEffect(() => { submitRef.current = submit; });

  const onKeyDown = (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

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
      aria-live={state === 'thinking' ? 'polite' : undefined}
    >
      <AnimatePresence mode="popLayout" initial={false}>
        {state === 'rest' || state === 'yielded' ? (
          <motion.button
            key="pill"
            ref={pillRef}
            type="button"
            className={styles.pill}
            onClick={focusInput}
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
            <motion.span layoutId="neoh-label" layout={policy.layout} className={styles.label} transition={transition}>
              {label}
            </motion.span>
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
            {state === 'result' && (
              <div className={styles.messages} ref={listRef}>
                {rendered ? (
                  <div className={styles.answer}>
                    <p className={styles.question}>{rendered.question}</p>
                    {rendered.spoken && <p className={styles.spoken}>{rendered.spoken}</p>}
                    <Blocks blocks={rendered.blocks} ctx={blockContext} />
                  </div>
                ) : (
                  <AssistantMessages messages={channel.messages} onUndo={channel.undo} undoing={channel.undoing} />
                )}
              </div>
            )}

            <div className={styles.bar}>
              {/* Same layoutId as the pill's: one object changing shape, not
                  an avatar destroyed and rebuilt between states. */}
              <motion.span
                layoutId="neoh-avatar"
                layout={policy.layout}
                transition={transition}
                className={styles.markSlot}
                data-voice={voiceActive ? 'true' : 'false'}
              >
                <NeohAvatar
                  state={avatar.state}
                  audioLevel={avatar.audioLevel}
                  actionType={avatar.actionType}
                  attentionLevel={avatar.attentionLevel}
                  variant={voiceActive ? 'bust' : 'head'}
                  size={voiceActive ? '56px' : '20px'}
                />
              </motion.span>
              {record && (
                <span className={styles.record}>
                  <motion.span layoutId="neoh-label" layout={policy.layout} transition={transition}>
                    {record.label}
                  </motion.span>
                  <button type="button" className={styles.recordClear} onClick={() => clearRecord()} aria-label={`Stop looking at ${record.label}`}>
                    <X aria-hidden="true" size={12} />
                  </button>
                </span>
              )}
              {/* The field is never removed, never disabled and never moves —
                  not while Neoh is working, not while the microphone is open.
                  Typing and speaking are peers, so either is always available. */}
              <textarea
                ref={inputRef}
                className={styles.input}
                value={draft}
                rows={1}
                maxLength={MAX_DRAFT}
                placeholder={
                  speech.state === 'listening'
                    ? 'Listening… or keep typing'
                    : (busy ? 'Neoh is working…' : inputPlaceholder(record))
                }
                aria-label="Message Neoh"
                onChange={(event) => setDraft(event.target.value.slice(0, MAX_DRAFT))}
                onKeyDown={onKeyDown}
              />
              {speech.supported && (
                <button
                  type="button"
                  className={styles.mic}
                  data-speech={speech.state}
                  onClick={() => (speech.state === 'listening' ? speech.stop() : speech.start())}
                  disabled={busy && speech.state !== 'listening'}
                  aria-label={speech.state === 'listening' ? 'Stop listening' : 'Talk to Neoh'}
                  aria-pressed={speech.state === 'listening'}
                >
                  {speech.state === 'listening'
                    ? <Square aria-hidden="true" size={14} />
                    : <Mic aria-hidden="true" size={16} />}
                </button>
              )}
              {channel.messages.length > 0 && state !== 'result' && (
                <button type="button" className={styles.iconBtn} onClick={() => setShowResult(true)} aria-label="Show the conversation">
                  <History aria-hidden="true" size={16} />
                </button>
              )}
              <button
                type="button"
                className={styles.send}
                onClick={submit}
                disabled={!draft.trim()}
                aria-label="Send"
              >
                <ArrowUp aria-hidden="true" size={16} />
              </button>
              <button type="button" className={styles.iconBtn} onClick={collapse} aria-label="Close Neoh">
                <X aria-hidden="true" size={16} />
              </button>
            </div>

            {/* What Neoh is hearing, as it forms. It settles into the
                conversation as an ordinary turn the moment it is final —
                there is no separate voice transcript to reconcile. */}
            {speech.interim && (
              <p className={styles.hearing} aria-live="polite">{speech.interim}</p>
            )}

            {speech.error && (
              <p className={styles.notice} role="status">
                {speech.error}{' '}
                <button type="button" className={styles.noticeAction} onClick={speech.clearError}>
                  Dismiss
                </button>
              </p>
            )}

            {(channel.notice || channel.connection !== 'online') && (
              <p className={styles.notice} role="status">
                {channel.notice || `Channel ${channel.connection}.`}
              </p>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

export default NeohSurface;
