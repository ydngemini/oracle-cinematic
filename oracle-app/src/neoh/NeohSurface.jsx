import { AnimatePresence, motion } from 'framer-motion';
import { ArrowUp, History, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';

import { useAssistant } from '../components/AssistantContext';
import { AssistantMessages } from '../components/AssistantMessages';
import { useMotionPolicy } from './motion';
import { inputPlaceholder, isBusy, restLabel, surfaceState } from './surfaceModel';
import { useGlobalShortcuts } from './useGlobalShortcuts';
import { useNeohChannel } from './useNeohChannel';
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

export function NeohSurface({ entityOpen = false }) {
  const { open, setOpen, record, clearRecord, commandRequest, clearCommandRequest } = useAssistant();
  const channel = useNeohChannel({ open });
  const policy = useMotionPolicy();
  const [draft, setDraft] = useState('');
  const [showResult, setShowResult] = useState(false);
  const inputRef = useRef(null);
  const pillRef = useRef(null);
  const listRef = useRef(null);

  const busy = isBusy(channel.messages);
  const state = surfaceState({ open, entityOpen, messages: channel.messages, showResult });
  const expanded = state === 'input' || state === 'thinking' || state === 'result';

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

  const submit = () => {
    if (channel.send(draft, record)) {
      setDraft('');
      setShowResult(true);
    }
  };

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
      className={`${styles.surface} hud-glass-panel`}
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
            <span className={styles.mark} aria-hidden="true" data-busy={busy} />
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
                <AssistantMessages messages={channel.messages} onUndo={channel.undo} undoing={channel.undoing} />
              </div>
            )}

            <div className={styles.bar}>
              <span className={styles.mark} aria-hidden="true" data-busy={busy} />
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
              <textarea
                ref={inputRef}
                className={styles.input}
                value={draft}
                rows={1}
                maxLength={MAX_DRAFT}
                placeholder={busy ? 'Neoh is working…' : inputPlaceholder(record)}
                aria-label="Message Neoh"
                onChange={(event) => setDraft(event.target.value.slice(0, MAX_DRAFT))}
                onKeyDown={onKeyDown}
              />
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
