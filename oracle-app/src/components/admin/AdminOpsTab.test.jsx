// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn() }));
vi.mock('../../state/useCrmApi', () => api);
vi.mock('../../state', () => ({ useOracleState: () => ({ liveFeed: [] }) }));
vi.mock('../HarvestControl', () => ({ HarvestControl: () => null }));
vi.mock('../BillingUsagePanel', () => ({ default: () => null }));
vi.mock('../DataSourceHealthPanel', () => ({ default: () => null }));
vi.mock('../RoleChangePanel', () => ({ default: () => null }));

const { default: AdminOpsTab } = await import('../AdminOpsTab');

beforeEach(() => {
  api.crmGet.mockReset();
  api.crmGet.mockImplementation((path) => {
    if (path === '/api/admin/billing-summary') {
      return Promise.resolve({ stripe_ok: false, mrr: null, mtd_revenue: null,
                               subscriptions_by_status: { active: 2 }, tenants: [] });
    }
    return Promise.resolve({});
  });
});
afterEach(cleanup);

const legacyCalls = () => api.crmGet.mock.calls.map(([p]) => p)
  .filter((p) => ['/api/admin/overview', '/api/admin/users', '/api/admin/system'].includes(p));

describe('AdminOpsTab', () => {
  it('opens on the operator overview and keeps the noisy panels folded', async () => {
    render(<AdminOpsTab />);
    expect(await screen.findByRole('heading', { name: 'Operator overview' })).toBeTruthy();
    const toggle = screen.getByRole('button', { name: /Platform detail/ });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('Live Feed')).toBeNull();
    // Folded detail does not poll.
    await waitFor(() => expect(api.crmGet).toHaveBeenCalledWith('/api/admin/release'));
    expect(legacyCalls()).toEqual([]);
  });

  it('loads the detail panels only when asked, and says when Stripe did not answer', async () => {
    render(<AdminOpsTab />);
    fireEvent.click(await screen.findByRole('button', { name: /Platform detail/ }));
    expect(screen.getByRole('button', { name: /Hide platform detail/ }).getAttribute('aria-expanded')).toBe('true');
    await waitFor(() => expect(legacyCalls().length).toBe(3));
    expect(await screen.findByText(/Stripe did not answer/)).toBeTruthy();
    expect(screen.getByText('Live Feed')).toBeTruthy();
  });
});
