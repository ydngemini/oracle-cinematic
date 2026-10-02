// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
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
});
