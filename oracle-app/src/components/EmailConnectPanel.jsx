import { useState } from 'react';
import { Mail, PlugZap, Send, Trash2 } from 'lucide-react';
import { EMAIL_PRESETS, explainEmailError, guessPreset, presetById, smtpPayload } from './emailPresets';
import styles from './SalesWorkspace.module.css';

const EMPTY = { email: '', password: '', from_name: '', host: '', port: '', username: '' };

/**
 * "Connect your email": each agent's approved emails go out from their own
 * mailbox. Pick the provider, follow its two or three steps, paste an app
 * password; Connect saves it (encrypted) and proves the login in one go, and
 * "Send me a test" proves the mail arrives — always to the agent's own address.
 */
export default function EmailConnectPanel({ provider, platformSender, working, onConnect, onSendTest, onDisconnect }) {
  const connected = Boolean(provider?.configured);
  const own = Boolean(provider?.own);
  const [editing, setEditing] = useState(false);
  const [presetId, setPresetId] = useState('gmail');
  const [form, setForm] = useState(EMPTY);
  const preset = presetById(presetId);
  const busy = Boolean(working);
  const set = (key) => (event) => setForm((current) => ({ ...current, [key]: event.target.value }));

  const onEmailBlur = () => {
    const guess = guessPreset(form.email);
    if (guess && guess !== presetId) setPresetId(guess);
  };

  const submit = async (event) => {
    event.preventDefault();
    if (!preset || preset.unsupported) return;
    const ok = await onConnect(smtpPayload(preset, form));
    if (ok) { setForm(EMPTY); setEditing(false); }
  };

  const showForm = !(connected && own) || editing;
  const failure = provider?.validation_status === 'invalid' && own ? provider.validation_error : '';

  return (
    <article className={styles.panel} aria-labelledby="email-connect-title">
      <header className={styles.panelHeader}>
        <div><h4 id="email-connect-title">Your email</h4><p>Emails you approve go out from your own address, and replies come back to your inbox</p></div>
      </header>
      <div className={styles.panelBody}>
        {connected && own ? (
          <div className={styles.providerIcon} data-email-state="own">
            <Mail aria-hidden="true" />
            <span><strong>Sending as {provider.sending_as || 'your address'}</strong><small>Connected and checked. Send yourself a test to see one arrive.</small></span>
          </div>
        ) : (
          <div className={styles.providerIcon} data-email-state={connected ? 'shared' : 'platform'}>
            <Mail aria-hidden="true" />
            <span>
              <strong>{connected
                ? `Your emails go out from ${provider.sending_as || 'your brokerage’s address'}, your brokerage’s email`
                : `Your emails go out from ${platformSender || 'Neoh’s address'}, with replies sent to you`}</strong>
              <small>Connect your own email so they come from you.</small>
            </span>
          </div>
        )}

        {connected && own && !editing ? (
          <div className={styles.buttonRow}>
            <button type="button" className={styles.primaryButton} onClick={onSendTest} disabled={busy}><Send aria-hidden="true" /> Send me a test email</button>
            <button type="button" className={styles.secondaryButton} onClick={() => setEditing(true)} disabled={busy}>Change</button>
            <button type="button" className={styles.dangerButton} onClick={onDisconnect} disabled={busy}><Trash2 aria-hidden="true" /> Disconnect</button>
          </div>
        ) : null}

        {showForm ? (
          <form className={styles.stack} onSubmit={submit} autoComplete="off" aria-label="Connect your email">
            <div className={styles.field}>
              <label htmlFor="email-provider">Your email provider</label>
              <select id="email-provider" value={presetId} onChange={(event) => setPresetId(event.target.value)}>
                {EMAIL_PRESETS.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
              </select>
            </div>

            {preset?.unsupported ? (
              <p className={styles.inlineError} role="note">{preset.unsupported}</p>
            ) : (
              <>
                <ol className={styles.setupSteps}>
                  {preset.steps.map((step) => <li key={step}>{step}</li>)}
                </ol>
                {preset.note ? <p className={styles.setupNote}>{preset.note}</p> : null}
                <div className={styles.fieldGrid}>
                  <div className={styles.field}><label htmlFor="email-address">Email address</label><input id="email-address" type="email" value={form.email} onChange={set('email')} onBlur={onEmailBlur} placeholder="you@example.com" required /></div>
                  <div className={styles.field}><label htmlFor="email-password">{preset.passwordLabel}</label><input id="email-password" type="password" value={form.password} onChange={set('password')} autoComplete="new-password" required /></div>
                  <div className={styles.field}><label htmlFor="email-from-name">Your name (optional)</label><input id="email-from-name" value={form.from_name} onChange={set('from_name')} placeholder="Jane Doe" /></div>
                  {preset.custom ? (
                    <>
                      <div className={styles.field}><label htmlFor="email-host">SMTP server</label><input id="email-host" value={form.host} onChange={set('host')} placeholder="mail.yourbrokerage.com" required /></div>
                      <div className={styles.field}><label htmlFor="email-port">Port</label><input id="email-port" type="number" min={1} max={65535} value={form.port} onChange={set('port')} placeholder="587" /></div>
                      <div className={styles.field}><label htmlFor="email-username">Login (if not your email)</label><input id="email-username" value={form.username} onChange={set('username')} autoComplete="off" /></div>
                    </>
                  ) : null}
                </div>
                <div className={styles.buttonRow}>
                  <button type="submit" className={styles.primaryButton} disabled={busy}><PlugZap aria-hidden="true" /> {working === 'connect:smtp' ? 'Connecting…' : 'Connect'}</button>
                  {editing ? <button type="button" className={styles.secondaryButton} onClick={() => setEditing(false)} disabled={busy}>Cancel</button> : null}
                </div>
                <small className={styles.setupNote}>Your password is encrypted when saved and never shown again. Connecting only checks the login. It never sends anything.</small>
              </>
            )}
          </form>
        ) : null}

        {failure ? <p className={styles.inlineError} role="alert">{explainEmailError(failure, preset)}</p> : null}
      </div>
    </article>
  );
}
