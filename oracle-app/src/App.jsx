import { useEffect, useRef, useState, useCallback } from 'react';
import { useOracleWebSocket, useOracleDispatch, ACTIONS } from './state';
import { CrmShell, LoginVault } from './components';
import { PolicyAcceptanceGate } from './components/PolicyAcceptanceGate';
import { NetworkProvider } from './context/NetworkContext';
import { apiPost } from './lib/apiClient';
import { ReelBackdrop, ReelExperience } from './components/ReelExperience';

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

  // Voice capture is controlled by explicit press-to-talk controls. A global
  // Space shortcut used to steal focus after leaving a text field and make the
  // page look selected; keyboard input now remains native and predictable.
}

function ReadyCrm() {
  useOracleWebSocket();
  useJarvisVoice();

  return <CrmShell />;
}

function AuthedApp({ onSignOut }) {
  const [policyReady, setPolicyReady] = useState(false);
  const markPolicyReady = useCallback(() => setPolicyReady(true), []);

  // The agent CRM opens directly to the source-backed Houses workspace.
  return (
    <>
      {policyReady ? <ReadyCrm /> : null}
      <PolicyAcceptanceGate onReady={markPolicyReady} onSignOut={onSignOut} />
    </>
  );
}

function NeohApp() {
  const [authed, setAuthed] = useState(() => (
    import.meta.env.VITE_AUTH_BYPASS === '1' ? true : null
  ));

  useEffect(() => {
    if (authed !== null) return;
    apiPost('/auth/verify', {}, { retries: 0 })
      .then((identity) => {
        if (identity?.role) sessionStorage.setItem('oracle_role', identity.role);
        setAuthed(true);
      })
      .catch(() => setAuthed(false));
  }, [authed]);

  useEffect(() => {
    const expireSession = () => {
      sessionStorage.removeItem('oracle_role');
      setAuthed(false);
    };
    window.addEventListener('auth:expired', expireSession);
    return () => window.removeEventListener('auth:expired', expireSession);
  }, []);

  const signOut = useCallback(async () => {
    try { await apiPost('/auth/logout', {}, { retries: 0 }); } catch { /* expire locally regardless */ }
    sessionStorage.removeItem('oracle_role');
    setAuthed(false);
  }, []);

  return (
    <div className="neoh-app-shell">
      <ReelBackdrop />
      <div className="neoh-app-foreground">
        <NetworkProvider>
          {authed === null ? (
            <div role="status" aria-live="polite">Restoring secure session…</div>
          ) : !authed ? (
            <LoginVault onAuthenticated={() => setAuthed(true)} />
          ) : (
            <AuthedApp onSignOut={signOut} />
          )}
        </NetworkProvider>
      </div>
    </div>
  );
}

function App() {
  const isReelRoute = window.location.pathname === '/reel' || window.location.pathname.startsWith('/reel/');
  return isReelRoute ? <ReelExperience /> : <NeohApp />;
}

export default App;
