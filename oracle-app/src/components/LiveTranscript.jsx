import { useEffect, useRef, useState, useCallback } from 'react';
import { useOracleState, useOracleDispatch, ACTIONS } from '../state';
import { crmGet } from '../state/useCrmApi';
import styles from './LiveTranscript.module.css';

export function LiveTranscript() {
  const {
    transcriptLog,
    jarvisListening,
    jarvisTranscript,
    negotiationTelemetry,
    aiChatConnection,
  } = useOracleState();
  const { dispatch, wsRef } = useOracleDispatch();
  const bottomRef = useRef(null);
  const inputRef = useRef(null);
  const lastTelemetryEventRef = useRef(0);
  const [whisperText, setWhisperText] = useState('');

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [transcriptLog.length]);

  useEffect(() => {
    const eventId = Number(negotiationTelemetry?.event_id);
    if (Number.isFinite(eventId)) {
      lastTelemetryEventRef.current = Math.max(lastTelemetryEventRef.current, eventId);
    }
  }, [negotiationTelemetry]);

  useEffect(() => {
    if (aiChatConnection === 'online') return undefined;
    let active = true;
    const poll = async () => {
      try {
        const result = await crmGet(
          `/api/voice/telemetry?after_event_id=${lastTelemetryEventRef.current}`,
        );
        if (!active) return;
        const events = Array.isArray(result?.events) ? result.events : [];
        events.forEach((event) => {
          if (event.transcript?.text) {
            dispatch({
              type: ACTIONS.APPEND_TRANSCRIPT,
              payload: {
                id: `voice-rest-${event.event_id}`,
                agent: event.transcript.speaker || 'VOICE',
                text: event.transcript.text,
                timestamp: Date.parse(event.created_at) || Date.now(),
              },
            });
          }
          if (event.counter_offer !== null || event.threshold || event.objection_draft) {
            dispatch({ type: ACTIONS.NEGOTIATION_TELEMETRY, payload: event });
          }
        });
        const next = Number(result?.next_event_id);
        if (Number.isFinite(next)) lastTelemetryEventRef.current = next;
      } catch {
        // The visible footer remains offline; REST polling retries without inventing data.
      }
    };
    void poll();
    const timer = window.setInterval(poll, 5_000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [aiChatConnection, dispatch]);

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
      {negotiationTelemetry && (
        <div className={styles.telemetry} aria-live="polite">
          <span
            className={styles.maoBadge}
            data-threshold={negotiationTelemetry.threshold || 'unavailable'}
          >
            {negotiationTelemetry.threshold === 'green' && 'GREEN: OFFER SAFE'}
            {negotiationTelemetry.threshold === 'amber' && 'AMBER: MARGIN TIGHT'}
            {negotiationTelemetry.threshold === 'red' && 'RED: OVER MAO'}
            {!['green', 'amber', 'red'].includes(negotiationTelemetry.threshold) && 'MAO: NEEDS DATA'}
          </span>
          {Number.isFinite(Number(negotiationTelemetry.mao)) && (
            <span className={styles.maoValue}>
              MAO ${Math.round(Number(negotiationTelemetry.mao)).toLocaleString()}
            </span>
          )}
        </div>
      )}
      {negotiationTelemetry?.objection_draft && (
        <aside className={styles.objection} aria-label="Recommended objection response">
          <strong>Recommended Objection Response</strong>
          <p>{negotiationTelemetry.objection_draft}</p>
          <small>Draft only · Agent approval required</small>
        </aside>
      )}
      <div className={styles.body}>
        {transcriptLog.length === 0 && (
          <div className={styles.idle}>Awaiting voice link...</div>
        )}

        {transcriptLog.map((entry, i) => (
          <div
            key={entry.id}
            className={`${styles.line} ${entry.agent === 'WHISPER' ? styles.whisperLine : ''} ${entry.agent === 'JARVIS' ? styles.jarvisLine : ''}`}
            style={{ animationDelay: `${i * 0.04}s` }}
            data-seq={String(i + 1).padStart(2, '0')}
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
          type="button"
          className={styles.whisperSend}
          onClick={sendWhisper}
          disabled={!whisperText.trim()}
        >
          SEND
        </button>
      </div>

      <div className={styles.footer}>
        <span className={styles.pulse} data-online={aiChatConnection === 'online'} />
        <span className={styles.linkLabel}>
          {aiChatConnection === 'online' ? 'ORCL_VOICE_LINK_ACTIVE' : 'VOICE_LINK_OFFLINE · REST FALLBACK READY'}
        </span>
      </div>
    </div>
  );
}
