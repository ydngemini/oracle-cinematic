import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPatch, crmPost } from '../state/useCrmApi';
// The three-question buyer/seller intake. Both its routes had no caller, so the
// structured qualification that turns "someone called" into a budget and a
// timeline could not be recorded anywhere.
import ContactIntakeForm from './ContactIntakeForm';
import styles from './PeopleTab.module.css';

/**
 * One contact: read it, correct it, and reserve the next nurture touch.
 *
 * GET and PATCH on /api/crm/contacts/{id} both shipped with no caller, so a
 * contact could be created and listed and then never opened — a wrong phone
 * number was permanent, and consent could not be corrected after the fact.
 *
 * Suppression is separate from consent on purpose and is shown that way. A
 * contact with no consent has simply never agreed; a suppressed contact has
 * asked not to be contacted, and STOP handling writes that flag. Collapsing
 * them would let a re-grant of consent quietly override an opt-out.
 */

const CHANNELS = [['email', 'Email'], ['sms', 'SMS'], ['voice', 'Voice']];

export default function ContactDetailPanel({ contactId, onClose, onChanged }) {
  const [contact, setContact] = useState(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState('');
  const [edits, setEdits] = useState({});

  const load = useCallback(() => {
    setError('');
    return crmGet(`/api/crm/contacts/${contactId}`).then(
      (payload) => {
        const record = payload?.contact || payload || null;
        setContact(record);
        setEdits({});
      },
      (reason) => setError(reason?.message || 'This contact could not be read.'),
    );
  }, [contactId]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => { void load(); });
    return () => window.cancelAnimationFrame(frame);
  }, [load]);

  const field = (key) => (edits[key] !== undefined ? edits[key] : (contact?.[key] ?? ''));
  const set = (key) => (event) => {
    setEdits((prev) => ({ ...prev, [key]: event.target.value }));
    setNotice('');
  };

  const save = async () => {
    if (busy || Object.keys(edits).length === 0) return;
    setBusy('save');
    setError('');
    // Send only what changed. ContactPatch forbids extras and treats a field as
    // "leave alone" when absent, so posting the whole record back would
    // resubmit values nobody touched.
    const payload = {};
    Object.entries(edits).forEach(([key, value]) => {
      const trimmed = typeof value === 'string' ? value.trim() : value;
      if (key === 'state_code') payload[key] = String(trimmed).toUpperCase() || null;
      else payload[key] = trimmed === '' ? null : trimmed;
    });
    try {
      await crmPatch(`/api/crm/contacts/${contactId}`, payload);
      setNotice('Saved.');
      await load();
      await onChanged?.();
    } catch (reason) {
      setError(reason?.message || 'The change was refused.');
    } finally {
      setBusy('');
    }
  };

  const reserveNurture = async () => {
    if (busy) return;
    setBusy('nurture');
    setError('');
    try {
      const result = await crmPost(`/api/crm/contacts/${contactId}/nurture-jobs`, {});
      setNotice(result?.job
        ? 'Next nurture touch reserved.'
        : 'No touch is due for this contact right now.');
    } catch (reason) {
      setError(
        reason?.status === 409
          ? 'A nurture touch is already reserved for this contact.'
          : reason?.message || 'No nurture touch could be reserved.',
      );
    } finally {
      setBusy('');
    }
  };

  const consent = contact?.consent || {};
  const suppression = contact?.suppression || {};

  return (
    <div className={styles.contactBook}>
      <div className={styles.contactHead}>
        <strong>{contact?.full_name || 'Contact'}</strong>
        <button type="button" onClick={onClose}>Close</button>
      </div>

      {error ? <p className={styles.errorState} role="alert">{error}</p> : null}
      {notice ? <p role="status">{notice}</p> : null}
      {!contact && !error ? <div className={styles.skeleton} aria-hidden="true" /> : null}

      {contact ? (
        <>
          <label><span>Full name</span><input value={field('full_name')} onChange={set('full_name')} maxLength={160} /></label>
          <label><span>Email</span><input value={field('email')} onChange={set('email')} maxLength={254} /></label>
          <label><span>Phone</span><input value={field('phone')} onChange={set('phone')} maxLength={40} /></label>
          <label><span>State</span><input value={field('state_code')} onChange={set('state_code')} maxLength={2} /></label>
          <label>
            <span>Preferred channel</span>
            <select value={field('preferred_channel') || 'none'} onChange={set('preferred_channel')}>
              <option value="none">Not stated</option>
              {CHANNELS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>

          <button type="button" onClick={save} disabled={busy !== '' || Object.keys(edits).length === 0}>
            {busy === 'save' ? 'Saving…' : 'Save changes'}
          </button>

          <p className={styles.sourceStatus}>
            Consent — {CHANNELS.map(([key, label]) => (
              `${label}: ${consent?.[key]?.granted ? 'given' : 'none'}`
            )).join(' · ')}
          </p>
          <p className={styles.sourceStatus}>
            Suppression — {suppression.global ? 'globally suppressed' : 'none global'}
            {suppression.dnc ? ' · on DNC' : ''}
            {CHANNELS.filter(([key]) => suppression[key]).map(([, label]) => ` · ${label} blocked`).join('')}
          </p>
          <p className={styles.sourceStatus}>
            Suppression is not the absence of consent. A suppressed contact asked not to be
            contacted, and granting consent here does not lift that.
          </p>

          <ContactIntakeForm contactId={contactId} onSubmitted={load} />

          <button type="button" onClick={reserveNurture} disabled={busy !== '' || !contact.nurture_enabled}>
            {busy === 'nurture' ? 'Reserving…' : 'Reserve next nurture touch'}
          </button>
          {!contact.nurture_enabled ? (
            <p className={styles.sourceStatus}>Nurture is disabled for this contact.</p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
