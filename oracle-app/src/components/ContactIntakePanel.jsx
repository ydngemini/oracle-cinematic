import { useState } from 'react';
import { crmPost } from '../state/useCrmApi';
import styles from './PeopleTab.module.css';

/**
 * Create one canonical contact.
 *
 * `POST /api/crm/contacts` shipped with no caller, so People could list the
 * contact book and never add to it — every person had to arrive through an
 * import or an agent tool. The record it creates is the identity the dialer,
 * the compliance gate and the nurture scheduler all resolve against, so this is
 * the front door to most of the CRM.
 *
 * Consent is deliberately explicit and defaults to nothing. `ContactConsent`
 * defaults every channel to ungranted on the server, and TCPA turns on whether
 * consent was actually given — so a checkbox that silently defaults to "yes"
 * would manufacture a legal claim about a conversation that never happened.
 */

const CHANNELS = [
  ['email', 'Email'],
  ['sms', 'SMS'],
  ['voice', 'Voice'],
];

export default function ContactIntakePanel({ onCreated, onCancel }) {
  const [form, setForm] = useState({
    full_name: '',
    email: '',
    phone: '',
    state_code: '',
    preferred_channel: 'none',
    source: '',
  });
  const [consent, setConsent] = useState({ email: false, sms: false, voice: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const set = (key) => (event) => {
    setForm((prev) => ({ ...prev, [key]: event.target.value }));
    setError('');
  };

  const submit = async (event) => {
    event.preventDefault();
    const fullName = form.full_name.trim();
    if (!fullName || busy) return;
    setBusy(true);
    setError('');

    const payload = { full_name: fullName };
    if (form.email.trim()) payload.email = form.email.trim();
    if (form.phone.trim()) payload.phone = form.phone.trim();
    if (form.state_code.trim()) payload.state_code = form.state_code.trim().toUpperCase();
    if (form.source.trim()) payload.source = form.source.trim();
    if (form.preferred_channel !== 'none') payload.preferred_channel = form.preferred_channel;

    // Only send a grant where one was actually ticked. Sending `false`
    // explicitly would be indistinguishable from a recorded refusal.
    const granted = CHANNELS.filter(([key]) => consent[key]);
    if (granted.length > 0) {
      payload.consent = Object.fromEntries(
        granted.map(([key]) => [key, { granted: true }]),
      );
    }

    try {
      const created = await crmPost('/api/crm/contacts', payload);
      onCreated?.(created?.contact || created || null);
    } catch (reason) {
      setError(
        reason?.status === 409
          ? 'A contact with that email or phone already exists.'
          : reason?.message || 'The contact could not be created.',
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className={styles.contactBook} onSubmit={submit} aria-label="New contact">
      <div className={styles.contactHead}>
        <strong>New contact</strong>
        {onCancel ? (
          <button type="button" onClick={onCancel} disabled={busy}>Cancel</button>
        ) : null}
      </div>

      {error ? <p className={styles.errorState} role="alert">{error}</p> : null}

      <label>
        <span>Full name</span>
        <input value={form.full_name} onChange={set('full_name')} required maxLength={160} />
      </label>
      <label>
        <span>Email</span>
        <input value={form.email} onChange={set('email')} type="email" maxLength={254} />
      </label>
      <label>
        <span>Phone</span>
        <input value={form.phone} onChange={set('phone')} inputMode="tel" maxLength={40} />
      </label>
      <label>
        <span>State</span>
        <input
          value={form.state_code}
          onChange={set('state_code')}
          maxLength={2}
          placeholder="DE"
        />
      </label>
      <label>
        <span>Preferred channel</span>
        <select value={form.preferred_channel} onChange={set('preferred_channel')}>
          <option value="none">Not stated</option>
          {CHANNELS.map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
      </label>
      <label>
        <span>Source</span>
        <input value={form.source} onChange={set('source')} maxLength={80} placeholder="Referral" />
      </label>

      <fieldset>
        <legend>Consent given for</legend>
        {CHANNELS.map(([key, label]) => (
          <label key={key}>
            <input
              type="checkbox"
              checked={consent[key]}
              onChange={(event) => setConsent((prev) => ({ ...prev, [key]: event.target.checked }))}
            />
            <span>{label}</span>
          </label>
        ))}
        <p className={styles.sourceStatus}>
          Tick only what this person actually agreed to. Outreach is gated on these, and an
          unticked channel is recorded as no consent rather than as a refusal.
        </p>
      </fieldset>

      <button type="submit" disabled={busy || !form.full_name.trim()}>
        {busy ? 'Creating…' : 'Create contact'}
      </button>
    </form>
  );
}
