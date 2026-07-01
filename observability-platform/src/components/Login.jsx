import { useState } from 'react';
import { login } from '../lib/auth';

// Sign-in gate for the observability dashboard. On success it hands the minted
// JWT up to <App>, which mounts the dashboard (and its authed WebSocket).
export default function Login({ onAuth }) {
  const [agentId, setAgentId] = useState('');
  const [passphrase, setPassphrase] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const body = await login(agentId.trim(), passphrase);
      onAuth(body.token);
    } catch (err) {
      setError(err.message || 'Login failed.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={submit}>
        <h1>AWS <span>Observability</span></h1>
        <p className="login-sub">platform_admin / broker_owner sign-in</p>
        <input
          autoFocus
          autoComplete="username"
          placeholder="agent id / email"
          value={agentId}
          onChange={(e) => setAgentId(e.target.value)}
        />
        <input
          type="password"
          autoComplete="current-password"
          placeholder="passphrase"
          value={passphrase}
          onChange={(e) => setPassphrase(e.target.value)}
        />
        {error && <div className="login-error">{error}</div>}
        <button type="submit" disabled={busy || !agentId || !passphrase}>
          {busy ? 'Authenticating…' : 'Connect'}
        </button>
      </form>
    </div>
  );
}
