// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { clearPrivateCaches } from './clearPrivateCaches.js';

describe('clearPrivateCaches', () => {
  it('forgets who signed in, but keeps device preferences', async () => {
    localStorage.setItem('oracle_user_id', 'agent@b.test');
    localStorage.setItem('oracle_tenant_id', 't1');
    localStorage.setItem('oracle_comms_templates_v1', '[{"body":"Hi Jane"}]');
    localStorage.setItem('neoh.theme', 'dark');
    await clearPrivateCaches();
    expect(localStorage.getItem('oracle_user_id')).toBeNull();
    expect(localStorage.getItem('oracle_tenant_id')).toBeNull();
    expect(localStorage.getItem('oracle_comms_templates_v1')).toBeNull();
    expect(localStorage.getItem('neoh.theme')).toBe('dark');
  });

  afterEach(() => { vi.unstubAllGlobals(); });

  it('deletes the service-worker cache and staged capture originals', async () => {
    const deleted = [];
    vi.stubGlobal('indexedDB', {
      deleteDatabase(name) {
        deleted.push(name);
        const req = {};
        queueMicrotask(() => req.onsuccess?.());
        return req;
      },
    });
    await clearPrivateCaches();
    expect(deleted).toEqual(['oracle-predictive-cache', 'neoh-capture']);
  });
});
