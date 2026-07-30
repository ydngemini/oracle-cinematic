import { useState, useEffect, useCallback } from 'react';
import { getTenantId } from './identity';
import { apiGet, apiPost, ApiError } from '../lib/apiClient';
import { formatApiError } from '../lib/errorMessages';

const BILLING_BYPASS =
  import.meta.env.DEV && import.meta.env.VITE_BILLING_BYPASS === 'true';

const BILLING_STATUS_ERROR =
  'We couldn\'t verify your license because the billing service is unavailable. Please try again.';
const BILLING_PORTAL_ERROR =
  'We couldn\'t open the billing portal. Please try again.';
const SERVICE_ERROR_STATUSES = new Set(['error', 'no_db']);

export function useSubscription() {
  const [sub, setSub] = useState(
    BILLING_BYPASS
      ? { active: true, status: 'dev_bypass', plan: 'dev', currentPeriodEnd: null }
      : { active: false, status: 'loading', plan: 'none', currentPeriodEnd: null }
  );
  const [loading, setLoading] = useState(!BILLING_BYPASS);
  const [error, setError] = useState(null);

  const tenantId = getTenantId();

  const refresh = useCallback(async () => {
    if (BILLING_BYPASS) return { ok: true };

    setLoading(true);
    setError(null);

    try {
      const data = await apiGet(`/billing/status/${encodeURIComponent(tenantId)}`);
      if (
        typeof data?.active !== 'boolean' ||
        typeof data?.status !== 'string' ||
        SERVICE_ERROR_STATUSES.has(data.status)
      ) {
        throw new ApiError('Billing status response was unavailable', 0, false);
      }

      setSub({
        active: data.active,
        status: data.status,
        plan: typeof data.plan === 'string' ? data.plan : 'none',
        currentPeriodEnd: data.current_period_end ?? null,
      });
      return { ok: true };
    } catch (err) {
      setSub((current) => (
        current.active ? current : { ...current, active: false, status: 'error' }
      ));
      const msg = err instanceof ApiError ? formatApiError(err) : BILLING_STATUS_ERROR;
      setError(msg);
      return { ok: false, error: msg };
    } finally {
      setLoading(false);
    }
  }, [tenantId]);

  // Fetch subscription status once on mount (and whenever the resolver changes).
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { refresh(); }, [refresh]);

  const openPortal = useCallback(async () => {
    try {
      const data = await apiPost('/billing/create-portal-session', { tenant_id: tenantId });
      const url = data?.url;
      if (typeof url !== 'string' || !url) {
        return { ok: false, error: BILLING_PORTAL_ERROR };
      }

      let parsedUrl;
      try {
        parsedUrl = new URL(url);
      } catch {
        return { ok: false, error: BILLING_PORTAL_ERROR };
      }
      if (parsedUrl.protocol !== 'https:' && parsedUrl.protocol !== 'http:') {
        return { ok: false, error: BILLING_PORTAL_ERROR };
      }

      window.location.href = url;
      return { ok: true };
    } catch (err) {
      const msg = err instanceof ApiError ? formatApiError(err) : BILLING_PORTAL_ERROR;
      return { ok: false, error: msg };
    }
  }, [tenantId]);

  return { ...sub, loading, error, refresh, openPortal, tenantId };
}
