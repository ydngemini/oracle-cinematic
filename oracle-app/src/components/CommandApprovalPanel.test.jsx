// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn(), crmPost: vi.fn(), crmPut: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);
vi.mock('../state', () => ({ useOracleState: () => ({}) }));

const { CommandApprovalPanel } = await import('./CommandApprovalPanel');

afterEach(cleanup);

describe('CommandApprovalPanel', () => {
  it('shows what the voice assistant will be told on a call card (34.3)', async () => {
    api.crmGet.mockResolvedValue({ commands: [
      { id: 'c1', state: 'awaiting_approval', command_type: 'CALL', risk_class: 'live_call',
        target: { phone: '+13025550142' },
        draft: { content: { objective: 'Call Ana about 12 Elm St', property_address: '12 Elm St, Dover DE' } } },
      { id: 'c2', state: 'awaiting_approval', command_type: 'CALL', risk_class: 'live_call',
        target: { phone: '+13025550143' }, draft: { content: { reason: 'She asked for a callback' } } },
    ] });
    render(<CommandApprovalPanel />);
    fireEvent.click(screen.getByRole('button', { name: /Waiting for your approval/ }));
    expect(await screen.findByText('+13025550142 · Call Ana about 12 Elm St · 12 Elm St, Dover DE')).toBeTruthy();
    expect(screen.getByText('+13025550143 · She asked for a callback')).toBeTruthy();
  });
});
