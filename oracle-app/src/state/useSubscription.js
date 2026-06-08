import { useState, useEffect, useCallback } from 'react';
import { getTenantId } from './identity';

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

// Dev-only escape hatch: a local run without a live Stripe subscription would
// otherwise be permanently stuck behind the $299 BillingOverlay. Gated on
// import.meta.env.DEV, which is statically false in a production build — so this
// can NEVER unlock a deployed app, regardless of the env var.
const BILLING_BYPASS =
  import.meta.env.DEV && import.meta.env.VITE_BILLING_BYPASS === 'true';

export function useSubscription() {
  const [sub, setSub] = useState(
    BILLING_BYPASS
      ? { active: true, status: 'dev_bypass', plan: 'dev', currentPeriodEnd: null }
      : { active: false, status: 'loading', plan: 'none', currentPeriodEnd: null }
  );
  const [loading, setLoading] = useState(!BILLING_BYPASS);

  const tenantId = getTenantId();

  const refresh = useCallback(async () => {
    if (BILLING_BYPASS) return;
    try {
      const res = await fetch(`${API_BASE}/billing/status/${tenantId}`);
      if (res.ok) {
        const data = await res.json();
        setSub({
          active: data.active,
          status: data.status,
          plan: data.plan,
          currentPeriodEnd: data.current_period_end,
        });
      }
    } catch {
      setSub(s => ({ ...s, status: 'error' }));
    } finally {
      setLoading(false);
    }
  }, [tenantId]);

  // Fetch subscription status once on mount (and whenever the resolver changes).
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { refresh(); }, [refresh]);

  const openPortal = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/billing/create-portal-session`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tenant_id: tenantId }),
      });
      if (res.ok) {
        const { url } = await res.json();
        window.location.href = url;
      }
    } catch {
      /* portal navigation is best-effort; surface nothing on transient failure */
    }
  }, [tenantId]);

  return { ...sub, loading, refresh, openPortal, tenantId };
}
