import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useSubscription } from '../state/useSubscription';
import { apiPost, ApiError } from '../lib/apiClient';
import { formatApiError } from '../lib/errorMessages';
import styles from './BillingOverlay.module.css';

/**
 * Displayed monthly price.
 *
 * WARNING: this is a LABEL, not the amount charged. Checkout bills whatever the
 * Stripe Price object in STRIPE_PRICE_ID says (backend/billing.py:147 puts it
 * straight into line_items), and Stripe Prices are immutable — changing an
 * amount means creating a NEW Price and repointing STRIPE_PRICE_ID at it.
 *
 * So editing this number alone makes the page advertise one figure while the
 * card is charged another. If you change it here, change the Stripe Price too.
 */
const PLAN_PRICE_USD = 199;


const FEATURES = [
  { id: 'crm',       label: 'Complete CRM',        detail: 'People, property dossiers, inbox, and deals' },
  { id: 'ai',        label: 'Policy Autopilot',    detail: 'Core AI agents with approval guardrails' },
  { id: 'site',      label: 'Hyperlocal Website',  detail: 'One source-backed local site and private preview' },
  { id: 'voice',     label: 'Inbound AI Voice',    detail: 'One routed voice line with transcript and handoff' },
  { id: 'contracts', label: 'Contract Vault',      detail: 'Tenant-scoped templates, approvals, and documents' },
  { id: 'property',  label: 'Property View',       detail: 'Address lookup, exterior and interior capture, client upload links' },
  { id: 'user',      label: 'One Named User',      detail: 'Full export, monthly billing, no setup fee' },
];

const DIALOG_COPY = {
  checking: {
    kicker: 'Neoh Solo Premium — Access Check',
    title: 'Verifying your workspace',
    description: 'Please wait while Neoh confirms this workspace\u2019s billing status.',
  },
  error: {
    kicker: 'Neoh Solo Premium — Billing Unavailable',
    title: 'Workspace verification unavailable',
    description: 'The workspace remains locked until billing status can be confirmed.',
  },
  purchase: {
    kicker: 'Premium CRM for Independent Agents',
    title: 'Neoh Solo Premium',
    description: 'Run relationships, transactions, local marketing, voice, and protected AI from one quiet workspace.',
  },
};

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

const BILLING_STATUS_ERROR =
  'We couldn\'t verify your license because the billing service is unavailable. Please try again.';
const CHECKOUT_ERROR = 'We couldn\'t open secure checkout. Please try again.';
const PORTAL_ERROR = 'We couldn\'t open the billing portal. Please try again.';

export function BillingOverlay() {
  const {
    active,
    status,
    loading: subLoading,
    error: statusError,
    refresh,
    openPortal,
    tenantId,
  } = useSubscription();
  const [checkoutLoading, setCheckoutLoading] = useState(false);
  const [portalLoading, setPortalLoading] = useState(false);
  const [actionError, setActionError] = useState(null);
  const dialogRef = useRef(null);
  const titleRef = useRef(null);
  const titleId = useId();
  const descriptionId = useId();

  const verificationError = statusError || (
    status === 'error' || status === 'no_db' ? BILLING_STATUS_ERROR : null
  );
  const mode = subLoading ? 'checking' : (verificationError ? 'error' : 'purchase');
  const isVisible = !active;
  const copy = DIALOG_COPY[mode];

  const handleSubscribe = useCallback(async () => {
    setCheckoutLoading(true);
    setActionError(null);

    try {
      const data = await apiPost('/billing/create-checkout-session', { tenant_id: tenantId });
      const url = data?.url;
      if (typeof url !== 'string' || !url) {
        throw new ApiError('Checkout response was invalid', 0, false);
      }

      window.location.href = url;
    } catch (err) {
      const msg = err instanceof ApiError ? formatApiError(err) : CHECKOUT_ERROR;
      setActionError(msg);
      setCheckoutLoading(false);
    }
  }, [tenantId]);

  const handleOpenPortal = useCallback(async () => {
    setPortalLoading(true);
    setActionError(null);

    try {
      const result = await openPortal();
      if (!result?.ok) {
        setActionError(result?.error || PORTAL_ERROR);
        setPortalLoading(false);
      }
    } catch {
      setActionError(PORTAL_ERROR);
      setPortalLoading(false);
    }
  }, [openPortal]);

  // Capture focus only when the blocking dialog appears, then restore it when
  // verification succeeds or the component unmounts. There is deliberately no
  // Escape/close path: dismissing this enforcement gate would be a billing bypass.
  useEffect(() => {
    if (!isVisible) return undefined;

    const previouslyFocused = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    return () => {
      document.body.style.overflow = previousOverflow;
      if (previouslyFocused instanceof HTMLElement && previouslyFocused.isConnected) {
        previouslyFocused.focus({ preventScroll: true });
      }
    };
  }, [isVisible]);

  // Focus the dialog title on first paint and whenever its verification mode
  // changes so keyboard and screen-reader users receive the new state in context.
  useEffect(() => {
    if (isVisible) {
      titleRef.current?.focus({ preventScroll: true });
    }
  }, [isVisible, mode]);

  useEffect(() => {
    if (!isVisible) return undefined;

    const trapFocus = (event) => {
      if (event.key !== 'Tab') return;

      const dialog = dialogRef.current;
      if (!dialog) return;

      const focusable = Array.from(dialog.querySelectorAll(FOCUSABLE_SELECTOR));
      if (focusable.length === 0) {
        event.preventDefault();
        titleRef.current?.focus({ preventScroll: true });
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const focused = document.activeElement;

      if (!dialog.contains(focused)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && (
        focused === first || focused === titleRef.current || focused === dialog
      )) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && focused === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', trapFocus);
    return () => document.removeEventListener('keydown', trapFocus);
  }, [isVisible]);

  if (!isVisible) return null;

  return (
    <div className={styles.backdrop} data-visible="true">
      <div
        ref={dialogRef}
        className={styles.panel}
        data-visible="true"
        data-mode={mode}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        aria-busy={mode === 'checking' || checkoutLoading || portalLoading}
        tabIndex={-1}
      >
        <div className={styles.header}>
          <span className={styles.kicker}>{copy.kicker}</span>
          <h2 ref={titleRef} id={titleId} className={styles.heading} tabIndex={-1}>
            {copy.title}
          </h2>
          <p id={descriptionId} className={styles.subheading}>{copy.description}</p>
        </div>

        {mode === 'checking' ? (
          <div className={styles.statusState} role="status" aria-live="polite">
            <span className={styles.statusSpinner} aria-hidden="true" />
            <p className={styles.statusCopy}>Checking billing status…</p>
          </div>
        ) : null}

        {mode === 'error' ? (
          <div className={styles.statusState}>
            <p className={styles.errorMsg} role="alert">{verificationError}</p>
            <button className={styles.retryBtn} type="button" onClick={refresh}>
              Retry license check
            </button>
            <p className={styles.fine}>Checkout stays unavailable until verification succeeds.</p>
          </div>
        ) : null}

        {mode === 'purchase' ? (
          <>
            <div className={styles.priceBlock}>
              <span className={styles.srOnly}>${PLAN_PRICE_USD} per month</span>
              <span className={styles.currency} aria-hidden="true">$</span>
              <span className={styles.price} aria-hidden="true">{PLAN_PRICE_USD}</span>
              <span className={styles.period} aria-hidden="true">/mo</span>
            </div>

            <ul className={styles.featureList}>
              {FEATURES.map(({ id, label, detail }) => (
                <li key={id} className={styles.featureItem}>
                  <span className={styles.featureDot} aria-hidden="true" />
                  <span className={styles.featureLabel}>{label}</span>
                  <span className={styles.featureDetail}>{detail}</span>
                </li>
              ))}
            </ul>

            <div className={styles.ctaWrapper}>
              {actionError ? (
                <p className={styles.errorMsg} role="alert">{actionError}</p>
              ) : null}

              <button
                className={styles.cta}
                type="button"
                onClick={handleSubscribe}
                disabled={checkoutLoading || portalLoading}
                data-loading={checkoutLoading}
              >
                {checkoutLoading ? (
                  <>
                    <span className={styles.ctaSpinner} aria-hidden="true" />
                    <span>Opening secure checkout…</span>
                  </>
                ) : (
                  'Start Neoh Solo Premium'
                )}
              </button>

              {status === 'canceled' ? (
                <button
                  className={styles.portalBtn}
                  type="button"
                  onClick={handleOpenPortal}
                  disabled={checkoutLoading || portalLoading}
                >
                  {portalLoading ? 'Opening portal…' : 'Reactivate via Portal'}
                </button>
              ) : null}

              <p className={styles.fine}>
                Billed monthly. Cancel anytime. No setup fee. Extra named users are $39/month; telecom, model, e-sign, and ad spend remain transparent usage charges.
              </p>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}
