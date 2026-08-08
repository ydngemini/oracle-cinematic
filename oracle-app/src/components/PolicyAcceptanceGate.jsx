import { useEffect, useState } from 'react';
import { apiGet, apiPost, ApiError } from '../lib/apiClient';
import { formatApiError } from '../lib/errorMessages';
import styles from './PolicyAcceptanceGate.module.css';

const FALLBACK_POLICY_VERSION = 'neoh-platform-use-policy-2026-07-15-v1';
const FALLBACK_ESA_VERSION = 'neoh-account-security-esa-2026-07-18-v1';
const ESA_ACK_KEY = 'oracle_account_security_esa_ack';
const FALLBACK_ESA_AGREEMENT = {
  title: 'NEOH™ Account Security Agreement (ESA)',
  operator: 'NEOH™ is operated by YDN LLC.',
  effectiveDate: 'July 18, 2026',
  introduction:
    'This account-security addendum establishes the minimum security practices that '
    + 'must govern NEOH™ account access and use. It is an account-security document for '
    + 'platform operations and does not replace legal, tax, or regulatory obligations.',
  sections: [
    {
      heading: '1. Account integrity',
      paragraphs: [
        'Use one account per person. Do not share credentials, session links, or temporary tokens.',
        'Terminate device access immediately when credentials are exposed, a device is lost, or unauthorized access is suspected.',
      ],
    },
    {
      heading: '2. Authentication and session hygiene',
      paragraphs: [
        'Keep authentication material confidential. Use a unique password and rotate it after suspected compromise.',
        'Use session controls on shared devices: lock the device, sign out on public terminals, and review active sessions.',
      ],
    },
    {
      heading: '3. Data access discipline',
      paragraphs: [
        'Access only information necessary for assigned workflows. Do not export, print, or forward confidential data without authority.',
        'Do not bypass tenant boundaries, impersonate another broker, or access records outside approved roles.',
      ],
    },
    {
      heading: '4. Compliance and enforcement',
      paragraphs: [
        'NEOH™ may temporarily restrict or suspend access when account-security obligations are violated.',
        'If this document materially changes, re-acknowledgment may be required before protected operations resume.',
      ],
    },
  ],
};

function displayPolicyVersion(value) {
  const match = /-(\d{4}-\d{2}-\d{2})-v(\d+)$/.exec(value);
  return match ? `v${match[2]}.0 · ${match[1]}` : value;
}

function normalisePolicyDocument(value) {
  if (!value || typeof value !== 'object' || !Array.isArray(value.sections)) return null;
  const sections = value.sections
    .filter((section) => (
      section
      && typeof section.heading === 'string'
      && Array.isArray(section.paragraphs)
    ))
    .map((section) => ({
      heading: section.heading,
      paragraphs: section.paragraphs.filter((paragraph) => typeof paragraph === 'string'),
    }))
    .filter((section) => section.paragraphs.length > 0);
  if (!sections.length || typeof value.title !== 'string' || typeof value.introduction !== 'string') return null;
  return {
    title: value.title,
    operator: typeof value.operator === 'string' ? value.operator : '',
    effectiveDate: typeof value.effective_date === 'string' ? value.effective_date : '',
    introduction: value.introduction,
    sections,
  };
}

function normaliseEsaDocument(value) {
  if (!value || typeof value !== 'object' || !Array.isArray(value.sections)) return null;
  const sections = value.sections
    .filter((section) => (
      section
      && typeof section.heading === 'string'
      && Array.isArray(section.paragraphs)
    ))
    .map((section) => ({
      heading: section.heading,
      paragraphs: section.paragraphs.filter((paragraph) => typeof paragraph === 'string'),
    }))
    .filter((section) => section.paragraphs.length > 0);
  if (!sections.length || typeof value.title !== 'string' || typeof value.introduction !== 'string') return null;
  return {
    title: value.title,
    operator: typeof value.operator === 'string' ? value.operator : '',
    effectiveDate: typeof value.effective_date === 'string' ? value.effective_date : '',
    introduction: value.introduction,
    sections,
  };
}

function toOfflineModeAcknowledgement() {
  return {
    version: FALLBACK_ESA_VERSION,
    document: {
      title: FALLBACK_ESA_AGREEMENT.title,
      operator: FALLBACK_ESA_AGREEMENT.operator,
      effectiveDate: FALLBACK_ESA_AGREEMENT.effectiveDate,
      introduction: FALLBACK_ESA_AGREEMENT.introduction,
      sections: FALLBACK_ESA_AGREEMENT.sections,
    },
  };
}

async function loadAccountSecurityAgreement() {
  try {
    const payloadEsa = await apiGet('/auth/account-security-esa', { retries: 0 });
    const agreement = normaliseEsaDocument(payloadEsa?.agreement);
    if (agreement) {
      return {
        version: typeof payloadEsa.agreement_version === 'string'
          ? payloadEsa.agreement_version
          : FALLBACK_ESA_VERSION,
        document: agreement,
      };
    }
  } catch {
    // If policy API can be reached but this endpoint can't, keep moving with
    // a local fallback so the user still sees the account-security surface.
  }

  return toOfflineModeAcknowledgement();
}

function isNetworkFailure(error) {
  if (error instanceof ApiError && error.isNetworkError) return true;
  if (typeof error?.message === 'string') {
    const lower = error.message.toLowerCase();
    return (
      lower.includes('failed to fetch')
      || lower.includes('network')
      || lower.includes('typeerror')
      || lower.includes('load failed')
    );
  }
  return false;
}

function getStoredAccountSecurityAcknowledgement() {
  const raw = sessionStorage.getItem(ESA_ACK_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed.version !== 'string') return null;
    return {
      version: parsed.version,
      acknowledgedAt: parsed.acknowledgedAt,
    };
  } catch {
    return null;
  }
}

/**
 * New self-serve accounts receive a short-lived pending token. This gate is the
 * only client surface that can exchange it for a normal application token.
 */
export function PolicyAcceptanceGate({ onReady, onSignOut }) {
  const [phase, setPhase] = useState('loading');
  const [policyVersion, setPolicyVersion] = useState(FALLBACK_POLICY_VERSION);
  const [policy, setPolicy] = useState(null);
  const [accountSecurityAgreement, setAccountSecurityAgreement] = useState(null);
  const [acceptedSecurityAgreement, setAcceptedSecurityAgreement] = useState(false);
  const [securitySubmitting, setSecuritySubmitting] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [acceptedOffline, setAcceptedOffline] = useState(false);
  const [allowOfflineContinue, setAllowOfflineContinue] = useState(false);
  const [error, setError] = useState('');
  const [retryKey, setRetryKey] = useState(0);

  useEffect(() => {
    let active = true;

    async function loadStatus() {
      setAccountSecurityAgreement(null);
      setAllowOfflineContinue(false);
      setAcceptedOffline(false);
      setAcceptedSecurityAgreement(false);
      const accountSecurityAgreementPromise = loadAccountSecurityAgreement();

      try {
        const payload = await apiGet('/auth/policy-acceptance');
        if (!active) return;
        const agreementToShow = await accountSecurityAgreementPromise;
        setAccountSecurityAgreement(agreementToShow);
        if (payload.required === true) {
          const nextPolicy = normalisePolicyDocument(payload.policy);
          if (!nextPolicy) throw new ApiError('Unable to load the current platform policy.', 0, false);
          setPolicyVersion(typeof payload.policy_version === 'string' ? payload.policy_version : FALLBACK_POLICY_VERSION);
          setPolicy(nextPolicy);
          setPhase('required');
          return;
        }
        if (payload.account_security_required === true) {
          setPhase('esa');
          return;
        }
        setPhase('resolved');
        onReady?.();
      } catch (requestError) {
        if (!active) return;
        const errorMessage = requestError instanceof ApiError
          ? formatApiError(requestError)
          : 'Unable to verify the policy acknowledgement.';
        setError(errorMessage);
        const canContinueOffline = isNetworkFailure(requestError);
        setAllowOfflineContinue(canContinueOffline);
        const storedAck = getStoredAccountSecurityAcknowledgement();
        const agreementToShow = await accountSecurityAgreementPromise;
        setAccountSecurityAgreement(agreementToShow);
        if (canContinueOffline && storedAck && agreementToShow && storedAck.version === agreementToShow.version) {
          setPhase('resolved');
          onReady?.();
          return;
        }
        setPhase('error');
      }
    }

    void loadStatus();
    return () => { active = false; };
  }, [onReady, retryKey]);

  async function acceptPolicy(event) {
    event.preventDefault();
    if (phase === 'submitting') return;
    if (!acceptedSecurityAgreement) {
      setError('Please acknowledge the account security agreement before continuing.');
      return;
    }
    if (!accepted) return;
    setPhase('submitting');
    setError('');
    try {
      const payload = await apiPost('/auth/policy-acceptance', {
        policy_version: policyVersion,
        account_security_version: accountSecurityAgreement?.version || FALLBACK_ESA_VERSION,
      });
      if (payload.accepted !== true || payload.account_security_required === true) {
        throw new ApiError('Unable to record your acknowledgement.', 0, false);
      }
      if (payload.role) sessionStorage.setItem('oracle_role', payload.role);
      setPhase('resolved');
      onReady?.();
    } catch (requestError) {
      setError(requestError instanceof ApiError ? formatApiError(requestError) : 'Unable to record your acknowledgement.');
      setPhase('required');
    }
  }

  function acceptAccountSecurityAgreement(event) {
    event.preventDefault();
    sessionStorage.setItem(
      ESA_ACK_KEY,
      JSON.stringify({
        version: accountSecurityAgreement?.version || FALLBACK_ESA_VERSION,
        acknowledgedAt: new Date().toISOString(),
      }),
    );
    setPhase('resolved');
    onReady?.();
  }

  async function acceptOnlyAccountSecurityAgreement(event) {
    event.preventDefault();
    if (!acceptedSecurityAgreement || securitySubmitting) return;
    setSecuritySubmitting(true);
    setError('');
    try {
      const payload = await apiPost('/auth/account-security-esa', {
        agreement_version: accountSecurityAgreement?.version || FALLBACK_ESA_VERSION,
      });
      if (payload.accepted !== true || payload.required === true) {
        throw new ApiError('Unable to record your account-security acknowledgement.', 0, false);
      }
      setPhase('resolved');
      onReady?.();
    } catch (requestError) {
      setError(requestError instanceof ApiError
        ? formatApiError(requestError)
        : 'Unable to record your account-security acknowledgement.');
    } finally {
      setSecuritySubmitting(false);
    }
  }

  if (phase === 'resolved') return null;

  if (phase === 'loading') {
    return <div className={styles.loading} role="status" aria-live="polite">Verifying account policy…</div>;
  }

  if (phase === 'esa') {
    return (
      <section className={styles.overlay} role="dialog" aria-modal="true" aria-labelledby="esa-only-title">
        <form className={styles.panel} onSubmit={acceptOnlyAccountSecurityAgreement}>
          <p className={styles.eyebrow}>NEOH™ account security</p>
          <h1 id="esa-only-title">Acknowledge the NEOH™ Account Security Agreement</h1>
          <p className={styles.copy}>Your policy is already accepted for this account. Acknowledge ESA to continue.</p>

          {accountSecurityAgreement && (
            <article className={styles.policyDocument} tabIndex={0} aria-labelledby="account-security-document-title">
              <header className={styles.policyHeader}>
                <p>{accountSecurityAgreement.document.effectiveDate ? `Effective ${accountSecurityAgreement.document.effectiveDate}` : 'Current account-security addendum'}</p>
                <h2 id="account-security-document-title">{accountSecurityAgreement.document.title}</h2>
                {accountSecurityAgreement.document.operator && <p>{accountSecurityAgreement.document.operator}</p>}
              </header>
              <p id="account-security-introduction" className={styles.policyIntroduction}>{accountSecurityAgreement.document.introduction}</p>
              {accountSecurityAgreement.document.sections.map((section) => (
                <section className={styles.policySection} key={section.heading}>
                  <h3>{section.heading}</h3>
                  {section.paragraphs.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
                </section>
              ))}
              <p className={styles.version}>ESA version: {accountSecurityAgreement.version}</p>
            </article>
          )}

          <label className={styles.checkRow}>
            <input
              id="account-security-ack-only"
              type="checkbox"
              checked={acceptedSecurityAgreement}
              onChange={(event) => setAcceptedSecurityAgreement(event.target.checked)}
            />
            <span>I have read and accept the Account Security Agreement (ESA).</span>
          </label>

          <div className={styles.actions}>
            <button
              type="submit"
              className={styles.primary}
              disabled={!acceptedSecurityAgreement || securitySubmitting}
            >
              {securitySubmitting ? 'Recording…' : 'Acknowledge ESA and continue'}
            </button>
            <button type="button" className={styles.tertiary} onClick={onSignOut} disabled={securitySubmitting}>Decline and sign out</button>
          </div>
          {error && <p className={styles.error} role="alert">{error}</p>}
        </form>
      </section>
    );
  }

  if (phase === 'error') {
    return (
      <section className={styles.overlay} role="dialog" aria-modal="true" aria-labelledby="policy-error-title">
        <div className={styles.panel}>
          <p className={styles.eyebrow}>NEOH™ account security</p>
          <h1 id="policy-error-title">NEOH™ Account Security ESA status unavailable</h1>
          <p className={styles.copy}>{error}</p>
          {allowOfflineContinue && (
            <p className={styles.version}>You can continue after acknowledging the account-security addendum for this session.</p>
          )}
          {accountSecurityAgreement && (
            <article className={styles.policyDocument} tabIndex={0} aria-labelledby="esa-document-title">
              <header className={styles.policyHeader}>
                <p>{accountSecurityAgreement.document.effectiveDate ? `Effective ${accountSecurityAgreement.document.effectiveDate}` : 'Current account-security addendum'}</p>
                <h2 id="esa-document-title">{accountSecurityAgreement.document.title}</h2>
                {accountSecurityAgreement.document.operator && <p>{accountSecurityAgreement.document.operator}</p>}
              </header>
              <p id="esa-introduction" className={styles.policyIntroduction}>{accountSecurityAgreement.document.introduction}</p>
              {accountSecurityAgreement.document.sections.map((section) => (
                <section className={styles.policySection} key={section.heading}>
                  <h3>{section.heading}</h3>
                  {section.paragraphs.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
                </section>
              ))}
              <p className={styles.version}>ESA version: {accountSecurityAgreement.version}</p>
            </article>
          )}
          <div className={styles.actions}>
            {allowOfflineContinue && accountSecurityAgreement ? (
              <>
                <label className={styles.checkRow}>
                  <input
                    id="account-security-acceptance"
                    type="checkbox"
                    checked={acceptedOffline}
                    onChange={(event) => setAcceptedOffline(event.target.checked)}
                  />
                  <span>I have read and acknowledged the account-security form for this session.</span>
                </label>
                <button
                  type="button"
                  className={styles.primary}
                  onClick={acceptAccountSecurityAgreement}
                  disabled={!acceptedOffline}
                >
                  Continue with temporary offline acknowledgement
                </button>
              </>
            ) : null}
            <button type="button" className={styles.secondary} onClick={() => setRetryKey((value) => value + 1)}>Try again</button>
            <button type="button" className={styles.tertiary} onClick={onSignOut}>Sign out</button>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className={styles.overlay} role="dialog" aria-modal="true" aria-labelledby="policy-title">
      <form className={styles.panel} onSubmit={acceptPolicy} aria-describedby="policy-introduction policy-version">
        <p className={styles.eyebrow}>One-time account acknowledgement</p>
        <h1 id="policy-title">Before you enter NEOH™</h1>
        <p className={styles.copy}>Review the policy below. Your acknowledgement is stored with your account and this policy version.</p>

        {policy && (
          <article className={styles.policyDocument} tabIndex={0} aria-labelledby="policy-document-title">
            <header className={styles.policyHeader}>
              <p>{policy.effectiveDate ? `Effective ${policy.effectiveDate}` : 'Current platform policy'}</p>
              <h2 id="policy-document-title">{policy.title}</h2>
              {policy.operator && <p>{policy.operator}</p>}
            </header>
            <p id="policy-introduction" className={styles.policyIntroduction}>{policy.introduction}</p>
            {policy.sections.map((section) => (
              <section className={styles.policySection} key={section.heading}>
                <h3>{section.heading}</h3>
                {section.paragraphs.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
              </section>
            ))}
          </article>
        )}

        {accountSecurityAgreement && (
          <article className={styles.policyDocument} tabIndex={0} aria-labelledby="account-security-document-title">
            <header className={styles.policyHeader}>
              <p>{accountSecurityAgreement.document.effectiveDate ? `Effective ${accountSecurityAgreement.document.effectiveDate}` : 'Current account-security addendum'}</p>
              <h2 id="account-security-document-title">{accountSecurityAgreement.document.title}</h2>
              {accountSecurityAgreement.document.operator && <p>{accountSecurityAgreement.document.operator}</p>}
            </header>
            <p id="account-security-introduction" className={styles.policyIntroduction}>{accountSecurityAgreement.document.introduction}</p>
            {accountSecurityAgreement.document.sections.map((section) => (
              <section className={styles.policySection} key={section.heading}>
                <h3>{section.heading}</h3>
                {section.paragraphs.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
              </section>
            ))}
            <p className={styles.version}>ESA version: {accountSecurityAgreement.version}</p>
          </article>
        )}

        <label className={styles.checkRow}>
          <input
            id="account-security-acceptance"
            type="checkbox"
            checked={acceptedSecurityAgreement}
            onChange={(event) => setAcceptedSecurityAgreement(event.target.checked)}
          />
          <span>I have read and accept the Account Security Agreement (ESA).</span>
        </label>

        <label className={styles.checkRow}>
          <input id="policy-acceptance" type="checkbox" checked={accepted} onChange={(event) => setAccepted(event.target.checked)} autoFocus />
          <span>I have read and accept the NEOH™ Platform Use Policy.</span>
        </label>
        <p className={styles.version}>Policy version: {displayPolicyVersion(policyVersion)}</p>
        {accountSecurityAgreement && <p className={styles.version}>ESA version: {accountSecurityAgreement.version}</p>}
        {error && <p className={styles.error} role="alert">{error}</p>}
        <div className={styles.actions}>
          <button
            type="submit"
            className={styles.primary}
            disabled={!accepted || !acceptedSecurityAgreement || phase === 'submitting'}
          >
            {phase === 'submitting' ? 'Recording…' : 'Accept and continue'}
          </button>
          <button type="button" className={styles.tertiary} onClick={onSignOut} disabled={phase === 'submitting'}>Decline and sign out</button>
        </div>
      </form>
    </section>
  );
}
