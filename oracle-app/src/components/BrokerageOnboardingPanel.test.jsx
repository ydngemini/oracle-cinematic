// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../state/useCrmApi', () => ({
  crmGet: vi.fn(() => Promise.resolve({})), crmPost: vi.fn(), crmPut: vi.fn(),
}));
vi.mock('../state/identity', () => ({ getUserId: () => 'a@b.test' }));
vi.mock('./IntegrationStatus', () => ({ IntegrationStatus: () => null }));

const { BrokerageOnboardingPanel } = await import('./BrokerageOnboardingPanel');

afterEach(() => { cleanup(); window.history.replaceState({}, '', '/'); });

const landOn = (outcome) => window.history.replaceState({}, '', `/settings?google=${outcome}`);

// A refused Google connect says why (commands_api.finish_google_oauth, audit 22.7).
describe('BrokerageOnboardingPanel Google connect outcome', () => {
  it('says a connect was refused because the earlier connection is still being removed', async () => {
    landOn('revocation_pending');
    render(<BrokerageOnboardingPanel />);
    expect((await screen.findByRole('status')).textContent).toMatch('still being removed');
  });

  it('says a connect was refused because the member is no longer active', async () => {
    landOn('member_inactive');
    render(<BrokerageOnboardingPanel />);
    expect((await screen.findByRole('status')).textContent).toMatch('no longer an active member');
  });

  it('keeps the plain messages for connected and any other outcome', async () => {
    landOn('connected');
    render(<BrokerageOnboardingPanel />);
    expect((await screen.findByRole('status')).textContent).toBe('Google Calendar connected.');
    cleanup();
    landOn('error');
    render(<BrokerageOnboardingPanel />);
    expect((await screen.findByRole('status')).textContent).toBe('Google Calendar connection was not completed.');
  });
});
