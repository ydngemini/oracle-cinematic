import { motion } from 'framer-motion';
import { History, Maximize2, X } from 'lucide-react';
import { useEffect, useMemo, useRef } from 'react';

import { AssistantMessages } from '../components/AssistantMessages';
import { Blocks } from './Blocks';
import { NeohAvatar } from './NeohAvatar';
import {
  Captions, ComposerField, ComposerNotices, ContextChip, MicButton, SendButton,
} from './NeohComposer';
import { useCommandReceipts } from './useCommandReceipts';
import { useGlobalShortcuts } from './useGlobalShortcuts';
import { useNeohComposer } from './useNeohComposer';
import styles from './NeohSurface.module.css';

/**
 * The open half of the floating Neoh bar: composer, conversation, receipts.
 *
 * Split from NeohSurface so the pill at rest — which is on every Home, Work
 * and record view — costs only itself. The field, the microphone, the
 * message list and the answer blocks are fetched the first time someone
 * opens the bar (or hovers the pill), not on every page load.
 *
 * The draft is owned by NeohSurface, so closing the bar keeps what was typed.
 */
export function NeohSurfacePanel({
  channel, record, clearRecord, draftState, state, setShowResult,
  avatar, voiceActive, policy, onCollapse, onExpand, onNavigate, onOpenEntity, onActivity,
}) {
  const inputRef = useRef(null);
  const listRef = useRef(null);
  const composer = useNeohComposer({
    channel,
    record,
    draftState,
    onSettled: (outcome) => { if (outcome !== 'refused') setShowResult(true); },
  });
  const { speech, rendered, setDraft } = composer;
  const receipts = useCommandReceipts({
    enabled: state === 'result' && channel.available === true,
    revision: channel.revision,
    messages: channel.messages,
  });

  // The bar only ever opens because someone asked for it (a tap, ⌘K, "/",
  // a staged request), so the cursor goes straight into the field.
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => inputRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, []);

  // The face and the Escape key live in the shell; tell it what is true here.
  const listening = speech.state === 'listening' || speech.state === 'requesting';
  useEffect(() => {
    onActivity?.({ asking: composer.asking, listening: speech.state === 'listening' });
  }, [composer.asking, onActivity, speech.state]);

  // First Escape stops the microphone; the shell's Escape then closes the bar.
  useGlobalShortcuts({
    onEscape: () => { if (listening) speech.stop(); },
    onFocus: () => inputRef.current?.focus(),
  });

  useEffect(() => {
    if (state === 'result' && listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [channel.messages, state]);

  const blockContext = useMemo(() => ({
    onOpen: (href) => { onCollapse(); onOpenEntity?.(href); },
    onAct: (item) => setDraft(item.action || ''),
  }), [onCollapse, onOpenEntity, setDraft]);

  const send = () => {
    void composer.submit();
    inputRef.current?.focus();
  };

  const transition = policy.transition;

  return (
    <>
      {state === 'result' && (
        <div className={styles.messages} ref={listRef}>
          {rendered ? (
            <div className={styles.answer}>
              <p className={styles.question}>{rendered.question}</p>
              {rendered.spoken && <p className={styles.spoken}>{rendered.spoken}</p>}
              <Blocks blocks={rendered.blocks} ctx={blockContext} />
            </div>
          ) : (
            <AssistantMessages
              messages={channel.messages}
              onUndo={channel.undo}
              undoing={channel.undoing}
              receipts={receipts}
              onReview={onNavigate ? () => { onCollapse(); onNavigate('automations'); } : undefined}
            />
          )}
        </div>
      )}

      {record && (
        <div className={styles.contextRow}>
          <ContextChip record={record} onClear={() => { clearRecord(); inputRef.current?.focus(); }} />
        </div>
      )}

      <div className={styles.bar}>
        {/* Same layoutId as the pill's: one object changing shape. */}
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
        <ComposerField inputRef={inputRef} composer={composer} record={record} />
        <MicButton composer={composer} />
        {channel.messages.length > 0 && state !== 'result' && (
          <button type="button" className={`${styles.iconBtn} ${styles.historyBtn}`} onClick={() => setShowResult(true)} aria-label="Show the conversation">
            <History aria-hidden="true" size={16} />
          </button>
        )}
        <SendButton composer={composer} onSend={send} />
        {onExpand && (
          <button type="button" className={styles.iconBtn} onClick={onExpand} aria-label="Open the full conversation">
            <Maximize2 aria-hidden="true" size={15} />
          </button>
        )}
        <button type="button" className={styles.iconBtn} onClick={onCollapse} aria-label="Close Neoh">
          <X aria-hidden="true" size={16} />
        </button>
      </div>

      <Captions speech={speech} />
      <ComposerNotices speech={speech} notice={channel.notice} connection={channel.connection} />
    </>
  );
}

export default NeohSurfacePanel;
