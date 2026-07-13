import { useCallback, useEffect, useRef, useState } from 'react';
import { crmGet, crmPost, crmPut } from '../state/useCrmApi';
import { getTenantId, getUserId } from '../state/identity';
import { LicenseStatusWidget } from './LicenseStatusWidget';
import styles from './MyProfileTab.module.css';

// Inline stroke glyphs — same idiom as TabBar GLYPHS, zero icon deps.
const GLYPHS = {
  check: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 12.5 10 17.5 19 7" />
    </svg>
  ),
  power: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 3.5v8" />
      <path d="M6.4 6.6a8 8 0 1 0 11.2 0" />
    </svg>
  ),
};

const EDITABLE = [
  'display_name',
  'public_email',
  'phone',
  'headshot_url',
  'license_number',
  'brokerage',
  'bio',
  'email_signature',
];

const EMPTY_FORM = Object.fromEntries(EDITABLE.map((k) => [k, '']));

function initialsOf(name) {
  return (
    name
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((w) => w[0].toUpperCase())
      .join('') || '·'
  );
}

// Server fields may arrive null — coerce everything editable to strings once.
function toFormState(profile) {
  const next = { ...EMPTY_FORM };
  for (const k of EDITABLE) {
    const v = profile?.[k];
    next[k] = v == null ? '' : String(v);
  }
  return next;
}

// target_markets may land as an array or a comma-separated string — never trust shape.
function toMarkets(v) {
  if (Array.isArray(v)) return v.map((m) => String(m).trim()).filter(Boolean);
  if (typeof v === 'string') return v.split(',').map((m) => m.trim()).filter(Boolean);
  return [];
}

function toText(v) {
  return v == null || v === '' ? '' : String(v).trim();
}

/**
 * MyProfileTab — the agent's own card in the deck.
 * Renders only what GET /api/crm/profile returns; never simulates data.
 * The identity card previews the live form, so edits (name, headshot)
 * reflect before they're saved. PUT sends a partial body — changed keys
 * only. The SESSION block and SIGN OUT are local, so they render even
 * while the profile service is dark.
 */
export default function MyProfileTab() {
  const [profile, setProfile] = useState(null); // null = not yet loaded
  const [loadError, setLoadError] = useState(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [saveStatus, setSaveStatus] = useState('idle'); // idle|busy|saved|error
  const [saveError, setSaveError] = useState('');
  const [brokenUrl, setBrokenUrl] = useState(null); // last headshot URL that 404'd
  // Self-service password change (logged-in). DB-user accounts only — the env
  // operator account is managed via ORACLE_ADMIN_PASSPHRASE.
  const [pw, setPw] = useState({ current: '', next: '' });
  const [pwStatus, setPwStatus] = useState('idle'); // idle|busy|saved|error
  const [pwMsg, setPwMsg] = useState('');
  const savedTimer = useRef(null);

  // All setState lands in async continuations — never synchronously inside
  // the effect's call stack (react-hooks/set-state-in-effect compliant).
  const fetchProfile = useCallback(() => {
    return crmGet('/api/crm/profile').then(
      (data) => {
        const p = data?.profile ?? {};
        setProfile(p);
        setForm(toFormState(p));
        setLoadError(null);
      },
      (err) => {
        setLoadError(err); // 404 until the route deploys — handled below, never crash
      }
    );
  }, []);

  useEffect(() => {
    fetchProfile();
  }, [fetchProfile]);

  useEffect(() => () => clearTimeout(savedTimer.current), []);

  const retry = () => {
    setLoadError(null); // back to skeletons while the retry is in flight
    fetchProfile();
  };

  const setField = (key, value) => {
    setForm((f) => ({ ...f, [key]: value }));
    if (saveStatus === 'error') {
      setSaveStatus('idle');
      setSaveError('');
    }
  };

  const baseline = (k) => (profile?.[k] == null ? '' : String(profile[k]));
  const dirty = profile !== null && EDITABLE.some((k) => form[k] !== baseline(k));

  const save = async () => {
    if (!dirty || saveStatus === 'busy') return;
    setSaveStatus('busy');
    setSaveError('');
    // Partial body — only the keys the agent actually changed.
    const body = {};
    for (const k of EDITABLE) {
      if (form[k] !== baseline(k)) body[k] = form[k];
    }
    try {
      const data = await crmPut('/api/crm/profile', body);
      const p = data?.profile ?? { ...profile, ...body };
      setProfile(p);
      setForm(toFormState(p));
      setSaveStatus('saved');
      clearTimeout(savedTimer.current);
      savedTimer.current = setTimeout(() => {
        setSaveStatus((s) => (s === 'saved' ? 'idle' : s));
      }, 2400);
    } catch (err) {
      setSaveError(
        err.status === 404
          ? 'The profile service is not deployed yet — nothing was saved.'
          : err.message || 'The profile could not be saved.'
      );
      setSaveStatus('error');
    }
  };

  const signOut = () => {
    sessionStorage.removeItem('oracle_token');
    window.location.reload();
  };

  const changePassword = async (e) => {
    e.preventDefault();
    if (pwStatus === 'busy' || !pw.current || pw.next.length < 10) return;
    setPwStatus('busy');
    setPwMsg('');
    try {
      await crmPost('/auth/change-password', { current_password: pw.current, new_password: pw.next });
      setPw({ current: '', next: '' });
      setPwStatus('saved');
      setPwMsg('Password updated.');
    } catch (err) {
      setPwStatus('error');
      setPwMsg(err?.message || 'Could not change password.');
    }
  };
  const setPwField = (key) => (e) => {
    const v = e.target.value;
    setPw((p) => ({ ...p, [key]: v }));
    if (pwStatus !== 'idle' && pwStatus !== 'busy') { setPwStatus('idle'); setPwMsg(''); }
  };

  const isLoading = profile === null && !loadError;
  const loadErrMsg =
    loadError?.status === 404
      ? 'Profile service isn’t online yet.'
      : loadError?.message || 'Couldn’t reach the profile service.';

  // Identity card binds to the live form — edits preview before they save.
  const displayName = form.display_name.trim();
  const brokerage = form.brokerage.trim();
  const license = form.license_number.trim();
  const headshotUrl = form.headshot_url.trim();
  const showImg = headshotUrl !== '' && brokenUrl !== headshotUrl;

  // Investor persona — read-only, server-owned, shown only when present.
  const markets = toMarkets(profile?.target_markets);
  const experience = toText(profile?.experience_level);
  const dealTarget = toText(profile?.monthly_deal_target);
  const hasPersona = markets.length > 0 || experience !== '' || dealTarget !== '';

  // SESSION — identity helpers resolve env → localStorage → demo default.
  const userId = getUserId();
  const tenantId = getTenantId();

  const busy = saveStatus === 'busy';
  const saveLabel = busy
    ? 'Saving…'
    : saveStatus === 'error'
      ? 'Retry'
      : 'Save Profile';

  return (
    <section className={styles.wrap} aria-label="My profile" aria-busy={isLoading || busy}>
      <header className={styles.topRow}>
        <span className={styles.kicker}>My Profile</span>
      </header>

      {isLoading ? (
        <div className={styles.skeleton} aria-hidden="true">
          <div className={styles.skelIdentity} />
          <div className={styles.skelForm} />
          <div className={styles.skelRow} />
        </div>
      ) : loadError && profile === null ? (
        <div className={styles.errorBox} role="alert">
          <span className={styles.errorText}>{loadErrMsg}</span>
          <button type="button" className={styles.retryBtn} onClick={retry}>
            Retry
          </button>
        </div>
      ) : (
        <>
          {/* ── Identity card ─────────────────────────────────────────── */}
          <div className={styles.identity}>
            <span className={styles.headshot}>
              {showImg ? (
                <img
                  className={styles.headshotImg}
                  src={headshotUrl}
                  alt=""
                  onError={() => setBrokenUrl(headshotUrl)}
                />
              ) : (
                <span className={styles.initials} aria-hidden="true">
                  {initialsOf(displayName)}
                </span>
              )}
            </span>
            <div className={styles.identityText}>
              <span className={styles.name}>{displayName || 'Unnamed Agent'}</span>
              {brokerage && <span className={styles.brokerage}>{brokerage}</span>}
              {license && <span className={styles.license}>LIC · {license}</span>}
            </div>
          </div>

          {/* ── Edit form — PUT /api/crm/profile, partial body ────────── */}
          <form
            className={styles.form}
            onSubmit={(e) => {
              e.preventDefault();
              save();
            }}
          >
            <label className={styles.field}>
              <span className={styles.microLabel}>Display Name</span>
              <input
                className={styles.input}
                value={form.display_name}
                onChange={(e) => setField('display_name', e.target.value)}
                autoComplete="name"
                placeholder="Your name as clients see it"
              />
            </label>

            <label className={styles.field}>
              <span className={styles.microLabel}>Public Email</span>
              <input
                className={styles.input}
                value={form.public_email}
                onChange={(e) => setField('public_email', e.target.value)}
                type="email"
                inputMode="email"
                autoComplete="email"
                placeholder="agent@brokerage.com"
              />
            </label>

            <label className={styles.field}>
              <span className={styles.microLabel}>Phone</span>
              <input
                className={`${styles.input} ${styles.inputMono}`}
                value={form.phone}
                onChange={(e) => setField('phone', e.target.value)}
                type="tel"
                inputMode="tel"
                autoComplete="tel"
                placeholder="555 000 0000"
              />
            </label>

            <label className={styles.field}>
              <span className={styles.microLabel}>Headshot URL</span>
              <input
                className={`${styles.input} ${styles.inputMono}`}
                value={form.headshot_url}
                onChange={(e) => setField('headshot_url', e.target.value)}
                type="url"
                inputMode="url"
                placeholder="https://…"
              />
            </label>

            <div className={styles.duoGrid}>
              <label className={styles.field}>
                <span className={styles.microLabel}>License №</span>
                <input
                  className={`${styles.input} ${styles.inputMono}`}
                  value={form.license_number}
                  onChange={(e) => setField('license_number', e.target.value)}
                  placeholder="—"
                />
              </label>
              <label className={styles.field}>
                <span className={styles.microLabel}>Brokerage</span>
                <input
                  className={styles.input}
                  value={form.brokerage}
                  onChange={(e) => setField('brokerage', e.target.value)}
                  autoComplete="organization"
                  placeholder="Brokerage of record"
                />
              </label>
            </div>

            <label className={styles.field}>
              <span className={styles.microLabel}>Bio</span>
              <textarea
                className={styles.textarea}
                value={form.bio}
                onChange={(e) => setField('bio', e.target.value)}
                rows={4}
                placeholder="A few lines clients and counterparties will read."
              />
            </label>

            <label className={styles.field}>
              <span className={styles.microLabel}>Email Signature</span>
              <textarea
                className={`${styles.textarea} ${styles.textareaMono}`}
                value={form.email_signature}
                onChange={(e) => setField('email_signature', e.target.value)}
                rows={3}
                placeholder={'—\nName · Brokerage · Phone'}
              />
              <span className={styles.hint}>The AI signs outbound email with this.</span>
            </label>

            {saveStatus === 'error' && (
              <div className={styles.errorBox} role="alert">
                <span className={styles.errorText}>{saveError}</span>
              </div>
            )}

            <div className={styles.actions}>
              <button
                type="submit"
                className={`${styles.saveBtn}${saveStatus === 'saved' ? ` ${styles.saveBtnSaved}` : ''}`}
                disabled={busy || !dirty}
              >
                {saveStatus === 'saved' ? (
                  <>
                    <span className={styles.saveTick} aria-hidden="true">
                      {GLYPHS.check}
                    </span>
                    Saved
                  </>
                ) : (
                  saveLabel
                )}
              </button>
            </div>
          </form>

          {/* ── Investor persona — read-only, set during onboarding ───── */}
          {hasPersona && (
            <section className={styles.panel} aria-label="Investor persona">
              <span className={styles.sectionLabel}>Investor Persona</span>

              {markets.length > 0 && (
                <div className={styles.personaRow}>
                  <span className={styles.microLabel}>Target Markets</span>
                  <div className={styles.chips}>
                    {markets.map((m) => (
                      <span key={m} className={styles.chip}>
                        {m}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {experience !== '' && (
                <div className={styles.personaRow}>
                  <span className={styles.microLabel}>Experience</span>
                  <span className={styles.personaValue}>{experience}</span>
                </div>
              )}

              {dealTarget !== '' && (
                <div className={styles.personaRow}>
                  <span className={styles.microLabel}>Monthly Deal Target</span>
                  <span className={`${styles.personaValue} ${styles.personaMono}`}>
                    {dealTarget}
                  </span>
                </div>
              )}
            </section>
          )}
        </>
      )}

      {/* ── State licenses — per-state compliance tracking ──────────── */}
      <LicenseStatusWidget />

      {/* ── Security — self-service password change (DB accounts) ──────── */}
      <section className={styles.panel} aria-label="Security">
        <span className={styles.sectionLabel}>Security</span>
        <form onSubmit={changePassword}>
          <label className={styles.field}>
            <span className={styles.microLabel}>Current Password</span>
            <input
              type="password"
              className={styles.input}
              autoComplete="current-password"
              value={pw.current}
              onChange={setPwField('current')}
              placeholder="••••••••"
            />
          </label>
          <label className={styles.field}>
            <span className={styles.microLabel}>New Password</span>
            <input
              type="password"
              className={styles.input}
              autoComplete="new-password"
              value={pw.next}
              onChange={setPwField('next')}
              placeholder="At least 10 characters"
            />
          </label>
          {pwMsg && (
            <div className={styles.errorBox} role={pwStatus === 'error' ? 'alert' : 'status'}>
              <span
                className={styles.errorText}
                style={pwStatus === 'saved' ? { color: 'var(--oracle-amber, #d8a657)' } : undefined}
              >
                {pwMsg}
              </span>
            </div>
          )}
          <div className={styles.actions}>
            <button
              type="submit"
              className={styles.saveBtn}
              disabled={pwStatus === 'busy' || !pw.current || pw.next.length < 10}
            >
              {pwStatus === 'busy' ? 'Updating…' : 'Change Password'}
            </button>
          </div>
        </form>
      </section>

      {/* ── Session — local identity, renders even when the API is dark ── */}
      <section className={styles.panel} aria-label="Session">
        <span className={styles.sectionLabel}>Session</span>
        <dl className={styles.sessionGrid}>
          <div className={styles.sessionRow}>
            <dt className={styles.microLabel}>Operator</dt>
            <dd className={styles.sessionValue}>{userId}</dd>
          </div>
          <div className={styles.sessionRow}>
            <dt className={styles.microLabel}>Tenant</dt>
            <dd className={styles.sessionValue}>{tenantId}</dd>
          </div>
        </dl>
        <button type="button" className={styles.signOutBtn} onClick={signOut}>
          <span className={styles.btnGlyph} aria-hidden="true">
            {GLYPHS.power}
          </span>
          Sign Out
        </button>
      </section>

      <footer className={styles.foot}>
        <span className={styles.wordmark}>NEOH · AGENT CRM</span>
      </footer>
    </section>
  );
}
