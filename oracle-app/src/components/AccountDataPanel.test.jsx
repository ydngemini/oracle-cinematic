// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn(), crmPost: vi.fn(), crmPostDownload: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);

const { AccountDataPanel } = await import('./AccountDataPanel');

const ACTIVE = {
  id: 't1', name: 'Lockwood Realty', lifecycle_state: 'active', grace_days: 30, backup_days: 7,
  legal_hold: false, operations: [],
};
const future = new Date(Date.now() + 3 * 86400e3).toISOString();

beforeEach(() => {
  api.crmGet.mockReset(); api.crmPost.mockReset(); api.crmPostDownload.mockReset();
  api.crmGet.mockResolvedValue(ACTIVE);
  api.crmPost.mockResolvedValue({});
});
afterEach(cleanup);

const typePassword = () => fireEvent.change(
  screen.getByLabelText(/Password \(required/), { target: { value: 'hunter22hunter22' } });

describe('AccountDataPanel', () => {
  it('will not start an export without the password', async () => {
    render(<AccountDataPanel />);
    const button = await screen.findByRole('button', { name: 'Prepare export' });
    expect(button.disabled).toBe(true);
    typePassword();
    expect(button.disabled).toBe(false);
    fireEvent.click(button);
    await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith('/api/privacy/exports', { password: 'hunter22hunter22' }));
  });

  it('offers a download only for an unexpired, finished export', async () => {
    api.crmGet.mockResolvedValue({ ...ACTIVE, operations: [
      { id: 'op1', kind: 'export', state: 'succeeded', artifact_expires_at: future }] });
    render(<AccountDataPanel />);
    expect(await screen.findByText(/Ready — available until/)).toBeTruthy();
    typePassword();
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    await waitFor(() => expect(api.crmPostDownload).toHaveBeenCalledWith(
      '/api/privacy/exports/op1/download', { password: 'hunter22hunter22' }, 'neoh-export-op1.zip'));
  });

  it('says so when the last export has expired, and offers no download', async () => {
    api.crmGet.mockResolvedValue({ ...ACTIVE, operations: [
      { id: 'op1', kind: 'export', state: 'succeeded', artifact_expires_at: '2020-01-01T00:00:00Z' }] });
    render(<AccountDataPanel />);
    expect(await screen.findByText(/has expired/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Download' })).toBeNull();
  });

  it('closes only after the exact brokerage name, a reason and the password', async () => {
    render(<AccountDataPanel />);
    fireEvent.click(await screen.findByRole('button', { name: /Close this brokerage/ }));
    const close = screen.getByRole('button', { name: 'Close brokerage' });
    expect(screen.getByText(/permanently erased/)).toBeTruthy();
    typePassword();
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'retiring' } });
    fireEvent.change(screen.getByLabelText(/Type the brokerage name/), { target: { value: 'Lockwood' } });
    expect(close.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/Type the brokerage name/), { target: { value: 'Lockwood Realty' } });
    expect(close.disabled).toBe(false);
    fireEvent.click(close);
    await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith('/api/privacy/closure',
      { confirm_name: 'Lockwood Realty', reason: 'retiring', password: 'hunter22hunter22' }));
  });

  it('while closing, shows the erasure date and lets the owner keep the brokerage', async () => {
    api.crmGet.mockResolvedValue({ ...ACTIVE, lifecycle_state: 'closing', erase_after: '2026-11-01T12:00:00Z' });
    render(<AccountDataPanel />);
    expect(await screen.findByText(/This brokerage is closing/)).toBeTruthy();
    expect(screen.getByText(/2026/)).toBeTruthy();
    typePassword();
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'changed my mind' } });
    fireEvent.click(screen.getByRole('button', { name: 'Keep the brokerage' }));
    await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith('/api/privacy/closure/withdraw',
      { password: 'hunter22hunter22', reason: 'changed my mind' }));
  });

  it('shows the server refusal and changes nothing', async () => {
    api.crmPost.mockRejectedValue(new Error('Password is incorrect.'));
    render(<AccountDataPanel />);
    const button = await screen.findByRole('button', { name: 'Prepare export' });
    typePassword();
    fireEvent.click(button);
    expect((await screen.findByRole('alert')).textContent).toMatch('Password is incorrect.');
  });

  it('publishes the retention schedule on request', async () => {
    api.crmGet.mockImplementation((path) => Promise.resolve(path.endsWith('/policy')
      ? { backup_days: 7, categories: [{ category: 'call_audio', retention_days: 30 },
        { category: 'communication_content', retention_days: null }] }
      : ACTIVE));
    render(<AccountDataPanel />);
    fireEvent.click(await screen.findByRole('button', { name: /retention schedule/ }));
    expect(await screen.findByText('30 days')).toBeTruthy();
    expect(screen.getByText('Kept until you delete it')).toBeTruthy();
    expect(screen.getByText(/roll over every 7 days/)).toBeTruthy();
  });
});
