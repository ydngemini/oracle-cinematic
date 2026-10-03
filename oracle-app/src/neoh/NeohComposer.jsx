import { ArrowUp, Mic, MicOff, Square, X } from 'lucide-react';

import { MAX_DRAFT } from './useNeohComposer';
import { connectionMessage, contextLabel, inputPlaceholder, micControl } from './surfaceModel';
import styles from './NeohComposer.module.css';

/**
 * The composer's parts — [ Ask Neoh anything… ] [ mic ] [ send ] — shared by
 * the floating bar and the Neoh tab so the two can never disagree about how
 * asking works. Each part is dumb; useNeohComposer holds the behaviour.
 *
 * Nothing here is ever removed or disabled because Neoh is busy or listening.
 * The field stays a field while the microphone is open; send stays send
 * while a reply streams. Typing and talking are peers.
 */

/** "Talking about Sarah Johnson ×" — small, plain, removable. */
export function ContextChip({ record, onClear }) {
  if (!record?.label) return null;
  return (
    <span className={styles.context}>
      <span className={styles.contextLabel}>{contextLabel(record)}</span>
      <button
        type="button"
        className={styles.contextClear}
        onClick={onClear}
        aria-label={`Stop talking about ${record.label}`}
      >
        <X aria-hidden="true" size={12} />
      </button>
    </span>
  );
}

export function ComposerField({ inputRef, composer, record, label = 'Message Neoh' }) {
  const listening = composer.speech.state === 'listening';
  return (
    <textarea
      ref={inputRef}
      className={styles.input}
      value={composer.draft}
      rows={1}
      maxLength={MAX_DRAFT}
      placeholder={listening ? 'Listening… or keep typing' : inputPlaceholder(record)}
      aria-label={label}
      enterKeyHint="send"
      onChange={(event) => composer.setDraft(event.target.value)}
      onKeyDown={composer.onKeyDown}
    />
  );
}

export function MicButton({ composer }) {
  const { speech } = composer;
  const control = micControl({ supported: speech.supported, state: speech.state });
  return (
    <button
      type="button"
      className={styles.mic}
      data-speech={speech.state}
      onClick={composer.toggleMic}
      disabled={control.disabled}
      aria-label={control.label}
      aria-pressed={speech.supported ? control.pressed : undefined}
      title={control.label}
    >
      {!speech.supported ? <MicOff aria-hidden="true" size={16} />
        : speech.state === 'listening' ? <Square aria-hidden="true" size={14} />
          : <Mic aria-hidden="true" size={16} />}
    </button>
  );
}

export function SendButton({ composer, onSend }) {
  return (
    <button
      type="button"
      className={styles.send}
      onClick={onSend}
      disabled={!composer.draft.trim()}
      aria-label="Send"
    >
      <ArrowUp aria-hidden="true" size={16} />
    </button>
  );
}

/**
 * What Neoh is hearing, as it forms. Visible for sighted people, and
 * deliberately NOT a live region: recognition rewrites this several times a
 * second, and a screen reader would re-read every partial. The finished
 * utterance becomes an ordinary turn in the conversation instead.
 */
export function Captions({ speech }) {
  if (!speech.interim) return null;
  return (
    <p className={styles.captions} aria-hidden="true" data-captions="interim">
      {speech.interim}
    </p>
  );
}

/** Mic errors and connection state, in product language. */
export function ComposerNotices({ speech, notice, connection }) {
  const offline = connectionMessage(connection);
  return (
    <>
      {speech.error && (
        <p className={styles.notice} role="status">
          {speech.error}{' '}
          <button type="button" className={styles.noticeAction} onClick={speech.clearError}>
            Dismiss
          </button>
        </p>
      )}
      {(notice || offline) && (
        <p className={styles.notice} role="status">{notice || offline}</p>
      )}
    </>
  );
}
