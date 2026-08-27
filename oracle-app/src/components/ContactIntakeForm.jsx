import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPost } from '../state/useCrmApi';
import styles from './PeopleTab.module.css';

/**
 * The three-question buyer or seller intake, on one contact.
 *
 * `GET /api/crm/intake/questions/{persona}` and
 * `POST /api/crm/contacts/{id}/intakes` both shipped with no caller, so the
 * structured intake — the thing that turns "someone called" into a qualified
 * buyer or seller with a budget and a timeline — could not be recorded anywhere
 * in the product.
 *
 * The questions come FROM the server rather than being hardcoded here. They are
 * versioned (INTAKE_QUESTION_SET_VERSION) and the answers are parsed downstream
 * for budget and intent, so a copy of the wording in the frontend would drift
 * out of step with what the parser expects.
 *
 * All three answers are required — the server rejects a partial set rather than
 * storing a half-qualified contact.
 */

const PERSONAS = [['buyer', 'Buyer'], ['seller', 'Seller']];

export default function ContactIntakeForm({ contactId, onSubmitted }) {
  const [persona, setPersona] = useState('buyer');
  const [questions, setQuestions] = useState([]);
  const [version, setVersion] = useState('');
  const [answers, setAnswers] = useState(['', '', '']);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = useCallback(() => {
    setError('');
    return crmGet(`/api/crm/intake/questions/${persona}`).then(
      (payload) => {
        const rows = Array.isArray(payload?.questions) ? payload.questions : [];
        setQuestions(rows);
        setVersion(payload?.version || '');
        setAnswers(['', '', '']);
      },
      (reason) => setError(reason?.message || 'The intake questions could not be loaded.'),
    );
  }, [persona]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => { void load(); });
    return () => window.cancelAnimationFrame(frame);
  }, [load]);

  const submit = async (event) => {
    event.preventDefault();
    if (busy || answers.some((a) => !a.trim())) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await crmPost(`/api/crm/contacts/${contactId}/intakes`, {
        persona,
        answers: answers.map((a) => a.trim()),
      });
      setAnswers(['', '', '']);
      setNotice('Intake recorded.');
      await onSubmitted?.();
    } catch (reason) {
      setError(reason?.message || 'The intake was refused.');
    } finally {
      setBusy(false);
    }
  };

  const complete = answers.every((a) => a.trim());

  return (
    <form className={styles.contactBook} onSubmit={submit} aria-label="Contact intake">
      <div className={styles.contactHead}>
        <strong>Intake</strong>
        <select
          value={persona}
          onChange={(event) => setPersona(event.target.value)}
          aria-label="Intake persona"
        >
          {PERSONAS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </div>

      {error ? <p className={styles.errorState} role="alert">{error}</p> : null}
      {notice ? <p role="status">{notice}</p> : null}

      {questions.length === 0 && !error ? <div className={styles.skeleton} aria-hidden="true" /> : null}

      {questions.slice(0, 3).map((question, index) => (
        <label key={question}>
          <span>{question}</span>
          <input
            value={answers[index] || ''}
            onChange={(event) => setAnswers((prev) => {
              const next = [...prev];
              next[index] = event.target.value;
              return next;
            })}
            maxLength={2000}
          />
        </label>
      ))}

      <button type="submit" disabled={!complete || busy || questions.length < 3}>
        {busy ? 'Recording…' : 'Record intake'}
      </button>
      <p className={styles.sourceStatus}>
        All three answers are required — a partial intake is refused rather than stored as a
        half-qualified contact. {version ? `Question set ${version}.` : ''}
      </p>
    </form>
  );
}
