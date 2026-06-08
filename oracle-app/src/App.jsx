import { useEffect, useRef, useCallback } from 'react';
import { useOracleWebSocket, useOracleDispatch, ACTIONS } from './state';
import { DashboardLayout } from './components';

function useJarvisVoice() {
  const { dispatch } = useOracleDispatch();
  const recognitionRef = useRef(null);
  const holdingRef = useRef(false);

  useEffect(() => {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) return;

    const recognition = new SpeechRecognition();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = 'en-US';

    recognition.onresult = (event) => {
      let interim = '';
      let final = '';

      for (let i = event.resultIndex; i < event.results.length; i++) {
        const transcript = event.results[i][0].transcript;
        if (event.results[i].isFinal) {
          final += transcript;
        } else {
          interim += transcript;
        }
      }

      if (interim) {
        dispatch({ type: ACTIONS.SET_JARVIS_TRANSCRIPT, payload: interim });
      }

      if (final) {
        dispatch({ type: ACTIONS.SET_JARVIS_TRANSCRIPT, payload: final });
        dispatch({ type: ACTIONS.JARVIS_COMMAND, payload: final });

        dispatch({
          type: ACTIONS.APPEND_TRANSCRIPT,
          payload: {
            id: crypto.randomUUID(),
            agent: 'JARVIS',
            text: final,
            timestamp: Date.now(),
          },
        });
      }
    };

    recognition.onerror = (event) => {
      if (event.error !== 'aborted') {
        console.warn('[Jarvis] Speech error:', event.error);
      }
    };

    recognition.onend = () => {
      if (holdingRef.current) {
        try { recognition.start(); } catch { /* already started — ignore */ }
      }
    };

    recognitionRef.current = recognition;
  }, [dispatch]);

  const startListening = useCallback(() => {
    if (holdingRef.current) return;
    holdingRef.current = true;
    dispatch({ type: ACTIONS.SET_JARVIS_LISTENING, payload: true });

    try {
      recognitionRef.current?.start();
    } catch { /* not ready / already started — ignore */ }
  }, [dispatch]);

  const stopListening = useCallback(() => {
    holdingRef.current = false;
    dispatch({ type: ACTIONS.SET_JARVIS_LISTENING, payload: false });
    dispatch({ type: ACTIONS.SET_JARVIS_TRANSCRIPT, payload: '' });

    try {
      recognitionRef.current?.stop();
    } catch { /* not running — ignore */ }
  }, [dispatch]);

  useEffect(() => {
    function onKeyDown(e) {
      if (e.code === 'Space' && !e.repeat && e.target === document.body) {
        e.preventDefault();
        startListening();
      }
    }

    function onKeyUp(e) {
      if (e.code === 'Space' && e.target === document.body) {
        e.preventDefault();
        stopListening();
      }
    }

    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);

    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('keyup', onKeyUp);
    };
  }, [startListening, stopListening]);
}

function App() {
  useOracleWebSocket();
  useJarvisVoice();

  return <DashboardLayout />;
}

export default App;
