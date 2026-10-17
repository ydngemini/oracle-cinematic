import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useSubscription } from '../state/useSubscription';
import { apiGet, apiPost, ApiError } from '../lib/apiClient';
import { formatApiError } from '../lib/errorMessages';
import { formatPlanPrice } from '../lib/planPrice';
import styles from './BillingOverlay.module.css';

/**
 * The price shown is read from the backend (GET /billing/plan), which reads the
 * Stripe Price that checkout actually bills. This file holds no figure: it used
 * to hard-code a solo-plan price while checkout charged whatever
 * STRIPE_PRICE_ID said. When the price can't be read, the page says so instead
 * of guessing (prompt 18 T18.03; pricing is undecided, D-2026-10-12-02).
 */


const FEATURES = [
  { id: 'crm',       label: 'Complete CRM',        detail: 'People, property dossiers, inbox, and deals' },
  { id: 'ai',        label: 'Policy Autopilot',    detail: 'Core AI agents with approval guardrails' },
  { id: 'site',      label: 'Hyperlocal Website',  detail: 'One source-backed local site and private preview' },
  { id: 'voice',     label: 'Inbound AI Voice',    detail: 'One routed voice line with transcript and handoff' },
  { id: 'contracts', label: 'Contract Vault',      detail: 'Your brokerage’s templates, approvals, and documents' },
  { id: 'property',  label: 'Property View',       detail: 'Address lookup, exterior and interior capture, client upload links' },
  { id: 'export',    label: 'Your data, exportable', detail: 'Full export at any time' },
];

const dialogCopy = (planName) => ({
  checking: {
    kicker: `${planName} — Access check`,
    title: 'Verifying your workspace',
    description: 'Please wait while Neoh confirms this workspace\u2019s billing status.',
  },
  error: {
    kicker: `${planName} — Billing unavailable`,
    title: 'Workspace verification unavailable',
    description: 'The workspace remains locked until billing status can be confirmed.',
  },
  purchase: {
    kicker: 'For real-estate brokerages',
    title: planName,
    description: 'Run relationships, transactions, local marketing, voice, and protected AI from one quiet workspace.',
  },
});

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
// A plan that may still bill but is not paid up. Checkout refuses a second
// plan while one exists (409), so the way forward is the billing portal.
const UNSETTLED_STATUSES = new Set(['incomplete', 'unpaid', 'paused']);

/**
 * @param {{ onLockedChange?: (locked: boolean) => void }} props
 *   Told whether the workspace is behind this gate, so the shell can hold
 *   anything else that would open over it (the first-visit walkthrough opened
 *   on top of the locked dialog and took the keyboard).
 */
export function BillingOverlay({ onLockedChange } = {}) {
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
  // Checkout answered 409: this brokerage already holds a plan, which the
  // status read may not have shown (it changed since, or Stripe reported a
  // status this page does not know). The portal is where that plan is managed.
  const [checkoutRefused, setCheckoutRefused] = useState(false);
  const dialogRef = useRef(null);
  const titleRef = useRef(null);
  const titleId = useId();
  const descriptionId = useId();

  const verificationError = statusError || (
    status === 'error' || status === 'no_db' ? BILLING_STATUS_ERROR : null
  );
  const mode = subLoading ? 'checking' : (verificationError ? 'error' : 'purchase');
  const isVisible = !active;
  const [plan, setPlan] = useState(null);
  useEffect(() => {
    if (mode !== 'purchase' || plan) return undefined;
    let live = true;
    apiGet('/billing/plan').then(
      (data) => { if (live) setPlan(data || { source: 'unavailable' }); },
      () => { if (live) setPlan({ source: 'unavailable' }); },
    );
    return () => { live = false; };
  }, [mode, plan]);
  const planName = plan?.name || 'Neoh';
  const price = formatPlanPrice(plan);
  // No key or price on this deployment: checkout cannot work, and saying
  // "temporarily unavailable" would send people to retry something that is not
  // set up. A Stripe outage (reason 'lookup_failed') keeps the button.
  const notConfigured = plan?.reason === 'not_configured';
  const copy = dialogCopy(planName)[mode];
  const unsettled = UNSETTLED_STATUSES.has(status);
  const offerPortal = status === 'canceled' || unsettled || checkoutRefused;

  useEffect(() => { onLockedChange?.(isVisible); }, [isVisible, onLockedChange]);

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
      if (err instanceof ApiError && err.status === 409) setCheckoutRefused(true);
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
            <div className={styles.priceBlock} data-price-source={plan?.source || 'loading'}>
              {price ? (
                <>
                  <span className={styles.price}>{price.amount}</span>
                  {price.period ? <span className={styles.period}>{price.period}</span> : null}
                </>
              ) : (
                <span className={styles.period}>
                  {notConfigured ? 'Billing isn\'t set up on this deployment yet' : plan ? 'Price shown at secure checkout' : 'Loading price…'}
                </span>
              )}
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
              {notConfigured ? (
                <p className={styles.errorMsg} role="status">
                  Checkout can&apos;t start until billing is connected on this deployment. Ask your Neoh administrator.
                </p>
              ) : null}
              {unsettled ? (
                <p className={styles.fine}>
                  This brokerage&apos;s plan payment isn&apos;t settled. Use Manage billing to update the card or review the invoice.
                </p>
              ) : null}

              <button
                className={styles.cta}
                type="button"
                onClick={handleSubscribe}
                disabled={checkoutLoading || portalLoading || notConfigured}
                data-loading={checkoutLoading}
              >
                {checkoutLoading ? (
                  <>
                    <span className={styles.ctaSpinner} aria-hidden="true" />
                    <span>Opening secure checkout…</span>
                  </>
                ) : (
                  `Start ${planName}`
                )}
              </button>

              {offerPortal ? (
                <button
                  className={styles.portalBtn}
                  type="button"
                  onClick={handleOpenPortal}
                  disabled={checkoutLoading || portalLoading}
                >
                  {portalLoading ? 'Opening portal…' : status === 'canceled' ? 'Reactivate via Portal' : 'Manage billing'}
                </button>
              ) : null}

              <p className={styles.fine}>
                Billed by Stripe at the price above{price?.period ? `, ${price.period}` : ''}. Telecom, model, e-sign and ad spend are separate usage charges.
              </p>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}
