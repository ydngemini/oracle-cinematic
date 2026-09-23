import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Speaking to Neoh, as a peer of typing.
 *
 * Browser speech recognition, owned by the composer. The final transcript is
 * handed back as plain text and submitted down the SAME path a typed message
 * takes — one conversation, one transcript, one Neoh. Nothing downstream can
 * tell whether a turn arrived from a keyboard or a microphone, which is the
 * point.
 *
 * This deliberately does NOT touch the legacy `useJarvisVoice` recogniser in
 * App.jsx. That one routes to `applyJarvisCommand`, a keyword matcher for the
 * 3D property viewer ("show property lines", "furnish"), and it has never been
 * reachable — its ref is never returned and its hold flag is never set. It is
 * a different feature that happens to use the same browser API.
 *
 * What this hook does not do: make Neoh speak. There is no TTS and no audio
 * channel in the browser today, so there is nothing to interrupt and no
 * barge-in to implement. Adding a "stop speaking" affordance for audio that
 * does not exist would be a lie the UI tells every time it renders.
 */

/** @typedef {'unsupported'|'idle'|'requesting'|'listening'|'error'} SpeechState */

function recogniser() {
  if (typeof window === 'undefined') return null;
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

export function isSpeechInputSupported() {
  return Boolean(recogniser());
}

/** Turn a Web Speech error code into something a person can act on. */
export function speechErrorMessage(code) {
  switch (code) {
    case 'not-allowed':
    case 'service-not-allowed':
      return 'Neoh needs microphone permission. Allow it in your browser, then try again.';
    case 'no-speech':
      return "Neoh didn't hear anything.";
    case 'audio-capture':
      return 'No microphone found.';
    case 'network':
      return 'Speech recognition needs a connection.';
    default:
      return 'Speech input stopped unexpectedly.';
  }
}

/**
 * @param {object} opts
 * @param {(text: string) => void} opts.onFinal  a completed utterance
 * @param {boolean} [opts.disabled]
 */
export function useSpeechInput({ onFinal, disabled = false } = {}) {
  const supported = isSpeechInputSupported();
  const [state, setState] = useState(() => (supported ? 'idle' : 'unsupported'));
  const [interim, setInterim] = useState('');
  const [error, setError] = useState('');

  const recognitionRef = useRef(null);
  // The callback lives in a ref so re-rendering the composer (every keystroke)
  // cannot tear down an in-flight recognition session.
  const onFinalRef = useRef(onFinal);
  useEffect(() => { onFinalRef.current = onFinal; });

  const stop = useCallback(() => {
    const rec = recognitionRef.current;
    if (!rec) return;
    try { rec.stop(); } catch { /* already stopped */ }
  }, []);

  const start = useCallback(() => {
    const Ctor = recogniser();
    if (!Ctor || disabled) return;

    // Already running: treat a second press as "stop", which is what the
    // button's own label promises while listening.
    if (recognitionRef.current) {
      stop();
      return;
    }

    const rec = new Ctor();
    rec.continuous = false;       // one utterance per press, like a turn
    rec.interimResults = true;    // so the composer can show it forming
    rec.lang = 'en-US';

    rec.onstart = () => {
      setState('listening');
      setError('');
    };

    rec.onresult = (event) => {
      let live = '';
      let done = '';
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const chunk = event.results[i][0].transcript;
        if (event.results[i].isFinal) done += chunk;
        else live += chunk;
      }
      if (live) setInterim(live);
      if (done.trim()) {
        setInterim('');
        onFinalRef.current?.(done.trim());
      }
    };

    rec.onerror = (event) => {
      // `aborted` is what a deliberate stop() produces; it is not a failure.
      if (event.error === 'aborted') return;
      setError(speechErrorMessage(event.error));
      setState('error');
    };

    rec.onend = () => {
      recognitionRef.current = null;
      setInterim('');
      // Keep an error visible; otherwise settle back to idle so the button is
      // immediately usable again.
      setState((prev) => (prev === 'error' ? 'error' : 'idle'));
    };

    recognitionRef.current = rec;
    setState('requesting');
    try {
      rec.start();
    } catch {
      recognitionRef.current = null;
      setState('error');
      setError('Could not start the microphone.');
    }
  }, [disabled, stop]);

  const clearError = useCallback(() => {
    setError('');
    setState((prev) => (prev === 'error' ? 'idle' : prev));
  }, []);

  // A recognition session must not outlive the surface that owns it.
  useEffect(() => () => {
    const rec = recognitionRef.current;
    recognitionRef.current = null;
    if (rec) {
      try { rec.abort(); } catch { /* already gone */ }
    }
  }, []);

  return { supported, state, interim, error, start, stop, clearError };
}

export default useSpeechInput;
