// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn() }));
vi.mock('../../state/useCrmApi', () => api);
const feedState = vi.hoisted(() => ({ liveFeed: [] }));
vi.mock('../../state', () => ({ useOracleState: () => feedState }));
vi.mock('../HarvestControl', () => ({ HarvestControl: () => null }));
vi.mock('../BillingUsagePanel', () => ({ default: () => null }));
vi.mock('../DataSourceHealthPanel', () => ({ default: () => null }));
vi.mock('../RoleChangePanel', () => ({ default: () => null }));

const { default: AdminOpsTab } = await import('../AdminOpsTab');

beforeEach(() => {
  feedState.liveFeed = [];
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

  it('shows a mirrored call as a metadata card: type, brokerage, time, flags and nothing else', async () => {
    feedState.liveFeed = [{
      id: 'm1', kind: 'firehose_metadata', frameType: 'VOICE_TELEMETRY',
      tenant: '11111111-2222-4333-8444-555555555555', flags: ['consent recorded', 'needs approval'],
      timestamp: Date.now() - 5000,
      // Never in a metadata entry; even if it were, the card does not show it.
      text: 'Pat Owner: 251357 is my number', metric: '$233,911', actorName: 'Pat Owner',
    }];
    render(<AdminOpsTab />);
    fireEvent.click(await screen.findByRole('button', { name: /Platform detail/ }));
    const card = (await screen.findByText('VOICE TELEMETRY')).closest('li');
    expect(card.textContent).toContain('11111111');
    expect(card.textContent).not.toContain('2222-4333');
    expect(card.textContent).toContain('consent recorded');
    expect(card.textContent).toContain('needs approval');
    expect(card.textContent).toContain('content withheld');
    expect(card.textContent).toMatch(/\ds ago/);
    expect(card.textContent).not.toMatch(/Pat|251357|233,911/);
  });

  it('does not claim the firehose mirrors every tenant’s traffic', async () => {
    render(<AdminOpsTab />);
    fireEvent.click(await screen.findByRole('button', { name: /Platform detail/ }));
    const note = await screen.findByText(/Awaiting live frames/);
    expect(note.textContent).not.toMatch(/every tenant/);
    expect(note.textContent).toMatch(/private messages are not mirrored/);
    expect(note.textContent).toMatch(/calls show only their type, brokerage, time and flags/);
  });
});
