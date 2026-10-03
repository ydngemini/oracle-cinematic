import { Suspense, lazy, useEffect, useState, useCallback } from 'react';
import { useOracleWebSocket } from './state';
// Direct imports, not the old components/index.js barrel: a barrel's static
// re-exports dragged every legacy panel it listed into the entry chunk.
import { CrmShell } from './components/CrmShell';
import { LoginVault } from './components/LoginVault';
import { PolicyAcceptanceGate } from './components/PolicyAcceptanceGate';
import { NetworkProvider } from './context/NetworkContext';
import { apiGet, apiPost } from './lib/apiClient';
import { ReelExperience } from './components/ReelExperience';
import { SitePreview } from './components/SitePreview';
import { clearPrivateCaches } from './lib/clearPrivateCaches.js';
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

function NeohApp() {
  const [authed, setAuthed] = useState(() => (
    import.meta.env.VITE_AUTH_BYPASS === '1' ? true : null
  ));

  useEffect(() => {
    if (authed !== null) return;
    apiGet('/auth/session', { retries: 0 })
      .then((identity) => {
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
        setAuthed(Boolean(identity?.authenticated));
      })
      .catch(() => setAuthed(false));
  }, [authed]);

  useEffect(() => {
    const expireSession = () => {
      sessionStorage.removeItem('oracle_role');
      clearPrivateCaches();
      setAuthed(false);
    };
    window.addEventListener('auth:expired', expireSession);
    return () => window.removeEventListener('auth:expired', expireSession);
  }, []);

  const signOut = useCallback(async () => {
    try { await apiPost('/auth/logout', {}, { retries: 0 }); } catch { /* expire locally regardless */ }
    sessionStorage.removeItem('oracle_role');
    await clearPrivateCaches();
    setAuthed(false);
  }, []);

  return (
    <div className="neoh-app-shell">
      <div className="neoh-app-atmosphere" aria-hidden="true" />
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
