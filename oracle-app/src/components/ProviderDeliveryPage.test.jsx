// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn(), crmPost: vi.fn(), crmPut: vi.fn(), crmDelete: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);

const { default: ProviderDeliveryPage } = await import('./ProviderDeliveryPage');

const ROUTE = { voice_caller_id_e164: '+13025550100', voice_caller_id_verified: true, active: true };

function serve(messaging) {
  api.crmGet.mockImplementation((path) => {
    if (path === '/api/messaging/business-number') return Promise.resolve(messaging);
    if (path === '/api/sales/providers') return Promise.resolve({ providers: [], channels: {}, route: ROUTE });
    return Promise.resolve({});
  });
}

beforeEach(() => { Object.values(api).forEach((fn) => fn.mockReset()); });
afterEach(cleanup);

describe('ProviderDeliveryPage texting status (audit 25.1)', () => {
  it('does not call a hosted number ready while its texting registration is incomplete', async () => {
    serve({ hosted_order_status: 'active', eligibility_status: 'eligible', ready: false,
            send_blockers: ['campaign_not_approved'],
            send_blocker_messages: ['your texting campaign (10DLC) is not approved yet'] });
    render(<ProviderDeliveryPage />);
    expect(await screen.findByText('your texting campaign (10DLC) is not approved yet')).toBeTruthy();
    expect(screen.queryByText(/can send and receive text messages through Neoh/)).toBeNull();
    expect(screen.queryByRole('button', { name: /Connect text messages/ })).toBeNull();

    api.crmPost.mockResolvedValue({ messaging: { ready: true, send_blockers: [], send_blocker_messages: [] } });
    fireEvent.click(screen.getByRole('button', { name: /Check texting registration/ }));
    expect(await screen.findByText(/can send and receive text messages through Neoh/)).toBeTruthy();
    expect(api.crmPost).toHaveBeenCalledWith('/api/messaging/business/registration/refresh', {});
  });

  it('says ready only when the server says the number may send', async () => {
    serve({ hosted_order_status: 'active', eligibility_status: 'eligible', ready: true,
            send_blockers: [], send_blocker_messages: [] });
    render(<ProviderDeliveryPage />);
    expect(await screen.findByText(/can send and receive text messages through Neoh/)).toBeTruthy();
  });
});
