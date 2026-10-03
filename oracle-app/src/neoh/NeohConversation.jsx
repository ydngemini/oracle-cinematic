import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useReducedMotion } from 'framer-motion';

import { useAssistant } from '../components/AssistantContext';
import { AssistantMessages } from '../components/AssistantMessages';
import { crmGet } from '../state/useCrmApi';
import { Blocks } from './Blocks';
import { NeohAvatar } from './NeohAvatar';
import {
  Captions, ComposerField, ComposerNotices, ContextChip, MicButton, SendButton,
} from './NeohComposer';
import { presenceLine } from './surfaceModel';
import { useCommandReceipts } from './useCommandReceipts';
import { useGlobalShortcuts } from './useGlobalShortcuts';
import { useNeohAvatarState } from './useNeohAvatarState';
import { useNeohChannel, wireContext } from './useNeohChannel';
import { useCompletedAnnouncement, useNeohComposer } from './useNeohComposer';
import styles from './NeohConversation.module.css';

/**
 * The Neoh tab — the conversation, at full height.
 *
 * Neoh, the thread, and a composer that never goes away:
 *
 *     [ Ask Neoh anything… ] [ mic ] [ send ]
 *
 * It is the same thread as the floating bar — both read the shared store —
 * so a question started anywhere continues here. It is the same composer,
 * too (useNeohComposer), so typing and talking behave identically in both.
 *
 * What it refuses to do:
 * - Split chat and voice. The microphone sits beside send; what it hears
 *   shows as a caption under the field and lands in the thread as an
 *   ordinary turn.
 * - Hide context. If Neoh is reading a record alongside the question, a chip
 *   says so in words — "Talking about 123 Main St" — and one tap removes it.
 * - Claim an outcome it does not have. Applied changes carry their ledger
 *   receipt and Undo; staged outreach carries the command's real state.
 * - Surround the character with furniture. One face, one line of status,
 *   then the conversation.
 */

// What a Recent hit becomes as Neoh context. Deals and conversations have no
// context type the chat backend accepts, so they are not offered.
const RECENT_CONTEXT = Object.freeze({ people: 'client', properties: 'lead' });

const STARTERS = Object.freeze([
  'What needs me today?',
  'Who should I call first?',
  'Which deals are at risk this week?',
]);

/** Real people and properties to ask about, for an empty conversation. */
function useStarterRecords(enabled) {
  const [records, setRecords] = useState([]);
  useEffect(() => {
    if (!enabled) return undefined;
    let live = true;
    crmGet('/api/search/recent?limit=6', { retries: 0 }).then(
      (data) => {
        if (!live) return;
        const rows = (data?.results || [])
          .map((hit) => ({ type: RECENT_CONTEXT[hit.kind], id: hit.id, label: hit.label }))
          .filter((row) => row.type && row.label && wireContext(row))
          .slice(0, 3);
        setRecords(rows);
      },
      () => {},
    );
    return () => { live = false; };
  }, [enabled]);
  return records;
}

function canAutofocus() {
  // A phone would raise its keyboard over the conversation the moment the
  // tab opened. A mouse-and-keyboard machine wants the cursor ready.
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(hover: hover) and (pointer: fine)').matches;
}

export function NeohConversation({ onNavigate, onOpenEntity }) {
  const {
    record, clearRecord, registerRecord, commandRequest, clearCommandRequest, commandStatus,
  } = useAssistant();
  const channel = useNeohChannel({ open: true });
  const composer = useNeohComposer({ channel, record });
  const { speech, busy, rendered, setDraft } = composer;
  const reducedMotion = useReducedMotion();
  const announcement = useCompletedAnnouncement(channel.messages);
  const receipts = useCommandReceipts({
    enabled: channel.available === true,
    revision: channel.revision,
    messages: channel.messages,
  });
  const empty = channel.messages.length === 0 && !rendered;
  const starters = useStarterRecords(channel.available === true && channel.messages.length === 0);
  const listening = speech.state === 'listening';
  const avatar = useNeohAvatarState({
    connection: channel.connection,
    messages: channel.messages,
    asking: composer.asking,
    micActive: listening,
    commandStatus,
    failed: Boolean(channel.notice) && channel.connection !== 'online',
  });

  const inputRef = useRef(null);
  const endRef = useRef(null);
  const dockRef = useRef(null);
  const [dockHeight, setDockHeight] = useState(0);

  const focusInput = useCallback(() => {
    window.requestAnimationFrame(() => inputRef.current?.focus());
  }, []);

  // The cursor is ready on arrival where that helps (see canAutofocus).
  useEffect(() => {
    if (channel.available === true && canAutofocus()) focusInput();
  }, [channel.available, focusInput]);

  useGlobalShortcuts({
    onFocus: focusInput,
    onEscape: () => {
      if (speech.state === 'listening' || speech.state === 'requesting') speech.stop();
    },
  });

  // Anything handed to Neoh — "Ask Neoh about Sarah" on Home, the bar's
  // "Open the full conversation" — arrives staged, never sent.
  useEffect(() => {
    if (!commandRequest || channel.available !== true) return undefined;
    const frame = window.requestAnimationFrame(() => {
      if (commandRequest.rawText) setDraft(commandRequest.rawText);
      clearCommandRequest();
      inputRef.current?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [channel.available, clearCommandRequest, commandRequest, setDraft]);

  // Keep the newest turn in view, above the composer.
  const last = channel.messages[channel.messages.length - 1];
  const lastKey = `${channel.messages.length}:${last?.status || ''}:${(last?.content || '').length > 0}:${Boolean(rendered)}`;
  const firstScroll = useRef(true);
  useEffect(() => {
    const behavior = firstScroll.current || reducedMotion ? 'auto' : 'smooth';
    firstScroll.current = false;
    endRef.current?.scrollIntoView?.({ block: 'end', behavior });
  }, [lastKey, reducedMotion]);

  // The thread must end above the composer, whatever the composer's height
  // (a chip, a caption and a notice can each add a line).
  useEffect(() => {
    const node = dockRef.current;
    if (!node || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(([entry]) => {
      const next = Math.round(entry.contentRect.height);
      setDockHeight((was) => (was === next ? was : next));
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [channel.available]);

  const blockContext = useMemo(() => ({
    onOpen: (href) => onOpenEntity?.(href),
    onAct: (item) => setDraft(item.action || ''),
  }), [onOpenEntity, setDraft]);

  const send = () => {
    void composer.submit();
    inputRef.current?.focus();
  };

  const stage = (text, nextRecord = null) => {
    if (nextRecord) registerRecord(nextRecord, 'neoh-starter');
    setDraft(text);
    inputRef.current?.focus();
  };

  if (channel.available === null) {
    return (
      <section className={styles.page} aria-busy="true" aria-label="Neoh">
        <div className={styles.presence}>
          <span className={styles.faceSkeleton} />
          <span className={styles.lineSkeleton} />
        </div>
      </section>
    );
  }

  if (channel.available === false) {
    return (
      <section className={styles.page} aria-labelledby="neoh-title">
        <header className={styles.presence}>
          <span className={styles.face}><NeohAvatar state="idle" variant="bust" size="64px" /></span>
          <div>
            <h1 id="neoh-title" className={styles.name}>Neoh</h1>
            <p className={styles.presenceLine}>Not switched on for this workspace yet</p>
          </div>
        </header>
        <p className={styles.unavailable}>
          The conversation with Neoh isn&rsquo;t available here right now. Everything
          in Home and Work still works, and nothing you have done is lost.
        </p>
        <button type="button" className={styles.secondary} onClick={() => onNavigate?.('work')}>
          Go to Work
        </button>
      </section>
    );
  }

  return (
    <section
      className={styles.page}
      aria-labelledby="neoh-title"
      style={{ '--neoh-dock-h': `${dockHeight}px` }}
    >
      <header className={styles.presence}>
        <span className={styles.face}>
          <NeohAvatar
            state={avatar.state}
            audioLevel={avatar.audioLevel}
            actionType={avatar.actionType}
            attentionLevel={avatar.attentionLevel}
            variant="bust"
            size="64px"
          />
        </span>
        <div>
          <h1 id="neoh-title" className={styles.name}>Neoh</h1>
          <p className={styles.presenceLine}>
            {presenceLine({ connection: channel.connection, busy, listening })}
          </p>
        </div>
      </header>

      <div className={styles.thread}>
        <AssistantMessages
          variant="page"
          messages={channel.messages}
          onUndo={channel.undo}
          undoing={channel.undoing}
          receipts={receipts}
          onReview={() => onNavigate?.('automations')}
        />

        {rendered && (
          <div className={styles.answer}>
            <p className={styles.question}>{rendered.question}</p>
            {rendered.spoken && <p className={styles.spoken}>{rendered.spoken}</p>}
            <Blocks blocks={rendered.blocks} ctx={blockContext} />
          </div>
        )}

        {empty && (
          <div className={styles.starters}>
            {starters.length > 0 && (
              <>
                <h2 className={styles.startersHead}>Start with someone real</h2>
                <ul className={styles.starterList}>
                  {starters.map((row) => (
                    <li key={`${row.type}-${row.id}`}>
                      <button
                        type="button"
                        className={styles.starter}
                        onClick={() => stage(`What should I know about ${row.label} today?`, row)}
                      >
                        Ask about {row.label}
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}
            <ul className={styles.starterList} aria-label="Things to ask">
              {STARTERS.map((text) => (
                <li key={text}>
                  <button type="button" className={styles.starterQuiet} onClick={() => stage(text)}>
                    {text}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
        <div ref={endRef} className={styles.end} aria-hidden="true" />
      </div>

      <div ref={dockRef} className={styles.dock}>
        {record && (
          <div className={styles.contextRow}>
            <ContextChip record={record} onClear={() => { clearRecord(); inputRef.current?.focus(); }} />
          </div>
        )}
        <div className={styles.bar}>
          <ComposerField inputRef={inputRef} composer={composer} record={record} />
          <MicButton composer={composer} />
          <SendButton composer={composer} onSend={send} />
        </div>
        <Captions speech={speech} />
        <ComposerNotices speech={speech} notice={channel.notice} connection={channel.connection} />
      </div>

      <p className={styles.srOnly} aria-live="polite" aria-atomic="true">{announcement}</p>
    </section>
  );
}

export default NeohConversation;
