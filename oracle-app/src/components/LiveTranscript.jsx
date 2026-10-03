import { useEffect, useRef, useState, useCallback } from 'react';
import { AlertTriangle, CheckCircle2, CircleDashed, Wifi, WifiOff } from 'lucide-react';
import { useOracleState, useOracleDispatch, ACTIONS } from '../state';
import { crmGet } from '../state/useCrmApi';
import styles from './LiveTranscript.module.css';

// Speaker codes come from the call pipeline (voice_intel.py: CLIENT/AGENT/AI)
// and from this client (NOTE, MEMORY). Show people words, never the codes.
const SPEAKER_LABELS = {
  CLIENT: 'Client',
  AGENT: 'You',
  AI: 'Neoh',
  SYSTEM: 'Neoh',
  VOICE: 'Call',
  MEMORY: 'Your profile',
  WHISPER: 'Your note to Neoh',
};

function speakerLabel(code) {
  const key = String(code || '').toUpperCase();
  return SPEAKER_LABELS[key] || 'Call';
}

// Offer guidance is a safety signal, so it is spelled out and carries an icon;
// the badge colour only reinforces what the words already say.
const OFFER_GUIDANCE = {
  green: { label: 'Offer is within your limit', Icon: CheckCircle2 },
  amber: { label: 'Offer is close to your limit', Icon: AlertTriangle },
  red: { label: 'Offer is over your maximum', Icon: AlertTriangle },
};

export function LiveTranscript() {
  const {
    transcriptLog,
    negotiationTelemetry,
    aiChatConnection,
  } = useOracleState();
  const { dispatch, wsRef } = useOracleDispatch();
  const bottomRef = useRef(null);
  const inputRef = useRef(null);
  const lastTelemetryEventRef = useRef(0);
  const [whisperText, setWhisperText] = useState('');
  const [sendError, setSendError] = useState('');

  useEffect(() => {
    bottomRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' });
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

    // Only record the note once it has actually gone out. Writing it into the
    // log while the live link is down showed a note Neoh never received.
    if (wsRef.current?.readyState !== WebSocket.OPEN) {
      setSendError("Couldn't send your note — the live call connection is offline. Try again in a moment.");
      return;
    }
    wsRef.current.send(JSON.stringify({
      type: 'WHISPER_INSTRUCT',
      instruction: text,
      timestamp: Date.now(),
    }));

    dispatch({
      type: ACTIONS.APPEND_TRANSCRIPT,
      payload: {
        id: crypto.randomUUID(),
        agent: 'WHISPER',
        text,
        timestamp: Date.now(),
      },
    });

    setSendError('');
    setWhisperText('');
  }, [whisperText, dispatch, wsRef]);

  const handleKeyDown = useCallback((e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendWhisper();
    }
  }, [sendWhisper]);

  const online = aiChatConnection === 'online';
  const guidance = OFFER_GUIDANCE[negotiationTelemetry?.threshold];
  const GuidanceIcon = guidance?.Icon || CircleDashed;

  return (
    <div className={styles.panel}>
      {negotiationTelemetry && (
        <div className={styles.telemetry} role="status" aria-live="polite" aria-atomic="true">
          <span
            className={styles.maoBadge}
            data-threshold={guidance ? negotiationTelemetry.threshold : 'unavailable'}
          >
            <GuidanceIcon size={14} aria-hidden="true" />
            {guidance ? guidance.label : 'Not enough data to check this offer yet'}
          </span>
          {Number.isFinite(Number(negotiationTelemetry.mao)) && (
            <span className={styles.maoValue}>
              Max offer ${Math.round(Number(negotiationTelemetry.mao)).toLocaleString()}
            </span>
          )}
        </div>
      )}
      {negotiationTelemetry?.objection_draft && (
        <aside className={styles.objection} aria-label="Suggested response">
          <strong>Suggested response</strong>
          <p>{negotiationTelemetry.objection_draft}</p>
          <small>Draft only — nothing is said until you use it.</small>
        </aside>
      )}
      {/* aria-live off: a live call adds lines every few seconds, and announcing
          each one would talk over the call for screen-reader users. */}
      <div className={styles.body} role="log" aria-live="off" aria-label="Call transcript">
        {transcriptLog.length === 0 && (
          <p className={styles.idle}>The transcript appears here once a call starts.</p>
        )}

        {transcriptLog.map((entry) => (
          <div
            key={entry.id}
            className={`${styles.line} ${entry.agent === 'WHISPER' ? styles.whisperLine : ''}`}
          >
            <span className={styles.agent}>{speakerLabel(entry.agent)}</span>
            <span className={styles.text}>{entry.text}</span>
          </div>
        ))}

        <div ref={bottomRef} />
      </div>

      <form
        className={styles.whisperInput}
        onSubmit={(e) => { e.preventDefault(); sendWhisper(); }}
      >
        <label className={styles.srOnly} htmlFor="live-transcript-note">Private note to Neoh</label>
        <input
          id="live-transcript-note"
          ref={inputRef}
          type="text"
          className={styles.whisperField}
          value={whisperText}
          onChange={(e) => { setWhisperText(e.target.value); if (sendError) setSendError(''); }}
          onKeyDown={handleKeyDown}
          placeholder="Private note to Neoh — the caller won't hear it"
          aria-describedby={sendError ? 'live-transcript-note-error' : undefined}
        />
        <button
          type="submit"
          className={styles.whisperSend}
          disabled={!whisperText.trim()}
        >
          Send
        </button>
      </form>
      {sendError && (
        <p id="live-transcript-note-error" className={styles.sendError} role="alert">
          <AlertTriangle size={14} aria-hidden="true" /> {sendError}
        </p>
      )}

      <div className={styles.footer} data-online={online}>
        {online ? <Wifi size={14} aria-hidden="true" /> : <WifiOff size={14} aria-hidden="true" />}
        <span className={styles.linkLabel}>
          {online ? 'Live call updates connected' : 'Live updates paused — checking every few seconds'}
        </span>
      </div>
    </div>
  );
}
