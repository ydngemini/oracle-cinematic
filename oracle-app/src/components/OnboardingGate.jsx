import { useState } from 'react';
import { useOracleState, useOracleDispatch, ACTIONS } from '../state';
import styles from './OnboardingGate.module.css';

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

// Three lethal data points — nothing else. Agents close 5-page wizards.
const EXPERIENCE_TIERS = [
  { value: 'scout', label: 'Scout', detail: '0–1 years in the field' },
  { value: 'closer', label: 'Closer', detail: '2–5 years, closing solo' },
  { value: 'fund', label: 'Enterprise Fund', detail: '5+ years, institutional volume' },
];

const VOLUME_TARGETS = [
  { value: '1-2', label: '1–2 deals / mo', detail: 'Precision strikes' },
  { value: '3-5', label: '3–5 deals / mo', detail: 'Steady pipeline' },
  { value: '6+', label: '6+ deals / mo', detail: 'Scale operations' },
];

const ZIP_RE = /^\d{5}$/;
const DISMISS_KEY = 'oracle_onboarding_dismissed';

function parseZips(raw) {
  return [...new Set(raw.split(/[,\s]+/).filter(Boolean))];
}

export function OnboardingGate() {
  const { memorySync, targetMarkets } = useOracleState();
  const { dispatch } = useOracleDispatch();

  const [step, setStep] = useState(1);
  const [experience, setExperience] = useState('');
  const [zipInput, setZipInput] = useState('');
  const [volume, setVolume] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [dismissed, setDismissed] = useState(
    () => sessionStorage.getItem(DISMISS_KEY) === '1'
  );

  // Gate only when memory restored AND the operator has no strike zones yet —
  // a DB-less dev run (memorySync false) is never nagged.
  const visible = memorySync && targetMarkets.length === 0 && !dismissed;
  if (!visible) return null;

  const zips = parseZips(zipInput);
  const badZips = zips.filter((z) => !ZIP_RE.test(z));
  const zipsValid = zips.length > 0 && badZips.length === 0;

  const dismiss = () => {
    sessionStorage.setItem(DISMISS_KEY, '1');
    setDismissed(true);
  };

  const submit = async () => {
    setSubmitting(true);
    setError('');
    try {
      const token = sessionStorage.getItem('oracle_token');
      const res = await fetch(`${API_BASE}/api/agents/profile`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          experience_level: experience,
          target_zips: zips,
          monthly_deal_target: volume,
        }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail || `profile save failed (${res.status})`);
      }
      const saved = await res.json();
      dispatch({
        type: ACTIONS.PROFILE_SAVED,
        payload: { markets: saved.target_markets },
      });
      sessionStorage.setItem(DISMISS_KEY, '1');
    } catch (err) {
      setError(String(err.message || err));
      setSubmitting(false);
    }
  };

  return (
    <div className={styles.backdrop} role="dialog" aria-modal="true" aria-label="Agent onboarding">
      <div className={styles.panel}>
        <header className={styles.chrome}>
          <span className={styles.kicker}>Agent Profiling</span>
          <span className={styles.progress}>0{step} / 03</span>
        </header>

        {step === 1 && (
          <div className={styles.step} key="exp">
            <h2 className={styles.title}>What is your operational experience?</h2>
            <p className={styles.subtitle}>
              Calibrates the AI&rsquo;s legal and underwriting vocabulary.
            </p>
            <div className={styles.options}>
              {EXPERIENCE_TIERS.map((tier) => (
                <button
                  key={tier.value}
                  type="button"
                  className={styles.option}
                  data-selected={experience === tier.value}
                  onClick={() => {
                    setExperience(tier.value);
                    setStep(2);
                  }}
                >
                  <span className={styles.optionLabel}>{tier.label}</span>
                  <span className={styles.optionDetail}>{tier.detail}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        {step === 2 && (
          <div className={styles.step} key="geo">
            <h2 className={styles.title}>Assign your strike zones.</h2>
            <p className={styles.subtitle}>
              Primary target ZIP codes — the harvesters prioritize these
              municipal data grids.
            </p>
            <input
              type="text"
              inputMode="numeric"
              className={styles.zipField}
              placeholder="19901, 19904, 19711"
              value={zipInput}
              onChange={(e) => setZipInput(e.target.value)}
              aria-label="Target ZIP codes"
              aria-invalid={zipInput.length > 0 && !zipsValid}
            />
            {zipInput.length > 0 && badZips.length > 0 && (
              <p className={styles.fieldError}>
                Not 5-digit ZIPs: {badZips.join(', ')}
              </p>
            )}
            <button
              type="button"
              className={styles.advance}
              disabled={!zipsValid}
              onClick={() => setStep(3)}
            >
              Lock Grid Target
            </button>
          </div>
        )}

        {step === 3 && (
          <div className={styles.step} key="vol">
            <h2 className={styles.title}>Set your acquisition targets.</h2>
            <p className={styles.subtitle}>
              Monthly volume — dictates your pipeline countdown clocks.
            </p>
            <div className={styles.options}>
              {VOLUME_TARGETS.map((v) => (
                <button
                  key={v.value}
                  type="button"
                  className={styles.option}
                  data-selected={volume === v.value}
                  onClick={() => setVolume(v.value)}
                >
                  <span className={styles.optionLabel}>{v.label}</span>
                  <span className={styles.optionDetail}>{v.detail}</span>
                </button>
              ))}
            </div>
            {error && <p className={styles.fieldError}>{error}</p>}
            <button
              type="button"
              className={styles.advance}
              disabled={!volume || submitting}
              onClick={submit}
            >
              {submitting ? 'Initializing…' : 'Initialize Dashboard'}
            </button>
          </div>
        )}

        <footer className={styles.footer}>
          {step > 1 && (
            <button
              type="button"
              className={styles.ghost}
              onClick={() => setStep(step - 1)}
            >
              Back
            </button>
          )}
          <button type="button" className={styles.ghost} onClick={dismiss}>
            Later
          </button>
        </footer>
      </div>
    </div>
  );
}
