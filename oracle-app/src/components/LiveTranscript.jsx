import { useEffect, useRef, useState, useCallback } from 'react';
import { useOracleState, useOracleDispatch, ACTIONS } from '../state';
import styles from './LiveTranscript.module.css';

export function LiveTranscript() {
  const { transcriptLog, jarvisListening, jarvisTranscript } = useOracleState();
  const { dispatch, wsRef } = useOracleDispatch();
  const bottomRef = useRef(null);
  const inputRef = useRef(null);
  const [whisperText, setWhisperText] = useState('');

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [transcriptLog.length]);

  const sendWhisper = useCallback(() => {
    const text = whisperText.trim();
    if (!text) return;

    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'WHISPER_INSTRUCT',
        instruction: text,
        timestamp: Date.now(),
      }));
    }

    dispatch({
      type: ACTIONS.APPEND_TRANSCRIPT,
      payload: {
        id: crypto.randomUUID(),
        agent: 'WHISPER',
        text,
        timestamp: Date.now(),
      },
    });

    setWhisperText('');
  }, [whisperText, dispatch, wsRef]);

  const handleKeyDown = useCallback((e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendWhisper();
    }
  }, [sendWhisper]);

  return (
    <div className={styles.panel}>
      <div className={styles.body}>
        {transcriptLog.length === 0 && (
          <div className={styles.idle}>Awaiting voice link...</div>
        )}

        {transcriptLog.map((entry, i) => (
          <div
            key={entry.id}
            className={`${styles.line} ${entry.agent === 'WHISPER' ? styles.whisperLine : ''} ${entry.agent === 'JARVIS' ? styles.jarvisLine : ''}`}
            style={{ animationDelay: `${i * 0.04}s` }}
          >
            <span className={styles.agent}>{entry.agent}</span>
            <span className={styles.text}>{entry.text}</span>
          </div>
        ))}

        <div ref={bottomRef} />
      </div>

      {/* Jarvis listening indicator */}
      {jarvisListening && (
        <div className={styles.jarvisBar}>
          <span className={styles.jarvisPulse} />
          <span className={styles.jarvisLabel}>
            {jarvisTranscript || 'LISTENING...'}
          </span>
        </div>
      )}

      {/* Whisper command input */}
      <div className={styles.whisperInput}>
        <input
          ref={inputRef}
          type="text"
          className={styles.whisperField}
          value={whisperText}
          onChange={(e) => setWhisperText(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Whisper instruction to AI Closer..."
          spellCheck={false}
        />
        <button
          className={styles.whisperSend}
          onClick={sendWhisper}
          disabled={!whisperText.trim()}
        >
          SEND
        </button>
      </div>

      <div className={styles.footer}>
        <span className={styles.pulse} />
        <span className={styles.linkLabel}>ORCL_VOICE_LINK_ACTIVE</span>
      </div>
    </div>
  );
}
