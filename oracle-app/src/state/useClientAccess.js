import { useCallback, useEffect, useState } from 'react';
import { crmGet } from './useCrmApi';

/**
 * Who can see and change one record (D-2026-10-13-01): a client (0131) or a
 * contact (0134). The caller's access ('edit' | 'view'), whether it sits in
 * the brokerage pool, its active shares (only for the assignee or the owner),
 * and the colleagues it can be assigned or shared to.
 *
 * `kind` is the API collection: 'clients' or 'contacts'.
 */
export function useRecordAccess(kind, recordId, reloadKey = 0) {
  const [state, setState] = useState({ access: null, colleagues: [], error: null, loading: true });
  const [key, setKey] = useState(0);
  useEffect(() => {
    if (!recordId) return undefined;
    let live = true;
    Promise.all([
      crmGet(`/api/crm/${kind}/${recordId}/access`),
      crmGet('/api/crm/colleagues'),
    ]).then(
      ([access, people]) => {
        if (live) setState({ access, colleagues: people?.colleagues || [], error: null, loading: false });
      },
      (error) => { if (live) setState((s) => ({ ...s, error, loading: false })); },
    );
    return () => { live = false; };
  }, [kind, recordId, reloadKey, key]);
  const reload = useCallback(() => setKey((k) => k + 1), []);
  const me = state.colleagues.find((c) => c.is_me) || null;
  return { ...state, me, isOwner: me?.role === 'broker_owner', reload };
}

export function useClientAccess(clientId, reloadKey = 0) {
  return useRecordAccess('clients', clientId, reloadKey);
}
