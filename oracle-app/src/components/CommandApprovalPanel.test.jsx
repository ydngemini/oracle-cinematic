// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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

  describe('the purpose of an approved text (external audit 26.5)', () => {
    const sms = (overrides = {}) => ({
      id: 's1', state: 'awaiting_approval', command_type: 'SMS', risk_class: 'outreach',
      target: { phone: '+13025550142' }, draft: { content: { body: 'Your showing is confirmed for 3 pm.' }, context: {} },
      ...overrides,
    });
    const open = async (commands) => {
      api.crmGet.mockResolvedValue({ commands });
      api.crmPost.mockReset();
      api.crmPost.mockResolvedValue({});
      render(<CommandApprovalPanel />);
      fireEvent.click(screen.getByRole('button', { name: /Waiting for your approval/ }));
      await screen.findByText('Your showing is confirmed for 3 pm.');
    };

    it('preselects marketing, explains both choices, and sends marketing by default', async () => {
      await open([sms()]);
      const marketing = screen.getByRole('radio', { name: /^Marketing/ });
      const informational = screen.getByRole('radio', { name: /^Informational/ });
      expect(marketing.checked).toBe(true);
      expect(informational.checked).toBe(false);
      expect(screen.getByText(/Needs documented written consent\./)).toBeTruthy();
      expect(screen.getByText(/showing or appointment confirmation.*Needs their consent to be texted\./)).toBeTruthy();
      fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
      await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith('/api/commands/s1/approve', {
        reason: 'Reviewed target, content, timing, and compliance context.', purpose: 'marketing',
      }));
    });

    it('sends informational when the approver chooses it', async () => {
      await open([sms()]);
      fireEvent.click(screen.getByRole('radio', { name: /^Informational/ }));
      fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
      await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith('/api/commands/s1/approve',
        expect.objectContaining({ purpose: 'informational' })));
    });

    it('does not offer the choice for a campaign text, an email or a rejection', async () => {
      await open([
        sms({ id: 's2', draft: { content: { body: 'Your showing is confirmed for 3 pm.' }, context: { source: 'speed-to-lead' } } }),
        { id: 'e1', state: 'awaiting_approval', command_type: 'EMAIL', risk_class: 'outreach',
          target: {}, draft: { content: { subject: 'Comps for 12 Elm St' } } },
      ]);
      expect(screen.queryByRole('radio')).toBeNull();
      expect(screen.getByText(/comes from speed-to-lead, so it is treated as marketing/)).toBeTruthy();
      const approves = screen.getAllByRole('button', { name: 'Approve' });
      fireEvent.click(approves[0]);
      await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith('/api/commands/s2/approve',
        { reason: 'Reviewed target, content, timing, and compliance context.' }));
      fireEvent.click(approves[1]);
      await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith('/api/commands/e1/approve',
        { reason: 'Reviewed target, content, timing, and compliance context.' }));
    });

    it('never sends a purpose with a rejection', async () => {
      await open([sms()]);
      fireEvent.click(screen.getByRole('radio', { name: /^Informational/ }));
      fireEvent.click(screen.getByRole('button', { name: 'Reject' }));
      await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith('/api/commands/s1/reject',
        { reason: 'Reviewed target, content, timing, and compliance context.' }));
    });
  });
});
