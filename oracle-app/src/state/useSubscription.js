import { useState, useEffect, useCallback } from 'react';

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

function getTenantId() {
  return (
    import.meta.env.VITE_TENANT_ID ||
    (typeof localStorage !== 'undefined' && localStorage.getItem('oracle_tenant_id')) ||
    'default'
  );
}

export function useSubscription() {
  const [sub, setSub] = useState({ active: false, status: 'loading', plan: 'none', currentPeriodEnd: null });
  const [loading, setLoading] = useState(true);

  const tenantId = getTenantId();

  const refresh = useCallback(async () => {
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
    } catch {}
  }, [tenantId]);

  return { ...sub, loading, refresh, openPortal, tenantId };
}
