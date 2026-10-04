import { Suspense, lazy, useEffect, useState, useCallback } from 'react';
import { useOracleWebSocket } from './state';
// Direct imports, not the old components/index.js barrel: a barrel's static
// re-exports dragged every legacy panel it listed into the entry chunk.
import { CrmShell } from './components/CrmShell';
import { LoginVault } from './components/LoginVault';
import { PolicyAcceptanceGate } from './components/PolicyAcceptanceGate';
import { NetworkProvider } from './context/NetworkContext';
import { apiPost } from './lib/apiClient';
import { ReelExperience } from './components/ReelExperience';
import { SitePreview } from './components/SitePreview';
import { clearPrivateCaches } from './lib/clearPrivateCaches.js';
import { useSessionRestore } from './lib/useSessionRestore';
// Unauthenticated client capture page — the token in the URL is the whole
// capability, so this route deliberately renders outside the auth shell.
const PropertyUploadPage = lazy(() => import('./components/PropertyUploadPage'));
const SecureDossierPage = lazy(() => import('./components/SecureDossierPage'));
// An invited agent has no account yet, so this renders outside the auth
// shell too — the token in the link is the whole capability.
const AcceptInvitePage = lazy(() => import('./components/AcceptInvitePage'));

function ReadyCrm() {
  useOracleWebSocket();

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

// What a restored identity leaves behind for the rest of the app.
function rememberIdentity(identity) {
  if (identity?.authenticated && identity?.role) {
    sessionStorage.setItem('oracle_role', identity.role);
  }
  // A restored session (cookie still valid, storage cleared) used to fall
  // back to the demo identity in state/identity.js, so Settings showed
  // "demo-operator" and the demo brokerage id to a real customer.
  if (identity?.authenticated) {
    try {
      if (identity.agent_id) localStorage.setItem('oracle_user_id', identity.agent_id);
      if (identity.tenant_id) localStorage.setItem('oracle_tenant_id', identity.tenant_id);
    } catch { /* storage blocked — identity falls back as before */ }
  }
}

function NeohApp() {
  const { authed, unreachable, setAuthed, retry } = useSessionRestore({
    bypass: import.meta.env.VITE_AUTH_BYPASS === '1',
    onIdentity: rememberIdentity,
  });

  useEffect(() => {
    const expireSession = () => {
      sessionStorage.removeItem('oracle_role');
      clearPrivateCaches();
      setAuthed(false);
    };
    window.addEventListener('auth:expired', expireSession);
    return () => window.removeEventListener('auth:expired', expireSession);
  }, [setAuthed]);

  const signOut = useCallback(async () => {
    try { await apiPost('/auth/logout', {}, { retries: 0 }); } catch { /* expire locally regardless */ }
    sessionStorage.removeItem('oracle_role');
    await clearPrivateCaches();
    setAuthed(false);
  }, [setAuthed]);

  return (
    <div className="neoh-app-shell">
      <div className="neoh-app-atmosphere" aria-hidden="true" />
      <div className="neoh-app-foreground">
        <NetworkProvider>
          {unreachable ? (
            <div role="alert" className="neoh-session-unreachable">
              <p>Neoh can’t be reached right now. You are still signed in.</p>
              <button type="button" onClick={retry}>Try again</button>
            </div>
          ) : authed === null ? (
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
  const isSitePreviewRoute = window.location.pathname.startsWith('/site-preview/');
  const isPropertyUploadRoute = window.location.pathname.startsWith('/property-upload/');
  // The homeowner's dossier. The backend has served this since 0008 and the
  // agent UI has been minting links to it, but no route consumed them — every
  // dossier link issued before this landed on the agent application instead.
  const isSecureDossierRoute = window.location.pathname.startsWith('/vault/secure-access/');
  const isAcceptInviteRoute = window.location.pathname === '/accept-invite';
  if (isReelRoute) return <ReelExperience />;
  if (isSitePreviewRoute) return <SitePreview />;
  if (isPropertyUploadRoute) {
    return (
      <Suspense fallback={null}>
        <PropertyUploadPage />
      </Suspense>
    );
  }
  if (isSecureDossierRoute) {
    return (
      <Suspense fallback={null}>
        <SecureDossierPage />
      </Suspense>
    );
  }
  if (isAcceptInviteRoute) {
    return (
      <Suspense fallback={null}>
        <AcceptInvitePage />
      </Suspense>
    );
  }
  return <NeohApp />;
}

export default App;
