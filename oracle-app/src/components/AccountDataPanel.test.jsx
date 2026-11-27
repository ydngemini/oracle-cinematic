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

  it('says when some photos or documents could not be included (29.5)', async () => {
    api.crmGet.mockResolvedValue({ ...ACTIVE, operations: [
      { id: 'op1', kind: 'export', state: 'succeeded', artifact_expires_at: future,
        result: { completeness: 'partial_media' } }] });
    render(<AccountDataPanel />);
    expect(await screen.findByText(/Some photos or documents could not be included/)).toBeTruthy();
  });

  it('does not say "try again" when an export was refused for missing data (29.5)', async () => {
    api.crmGet.mockResolvedValue({ ...ACTIVE, operations: [
      { id: 'op1', kind: 'export', state: 'failed', error: 'export failed: master_key_missing' }] });
    render(<AccountDataPanel />);
    expect(await screen.findByText(/contact Neoh support/)).toBeTruthy();
    expect(screen.queryByText(/Try again/)).toBeNull();
    cleanup();
    api.crmGet.mockResolvedValue({ ...ACTIVE, operations: [
      { id: 'op2', kind: 'export', state: 'failed', error: 'export failed: TimeoutError' }] });
    render(<AccountDataPanel />);
    expect(await screen.findByText(/The last export failed. Try again./)).toBeTruthy();
  });

  it('closes only after the exact brokerage name, a reason and the password', async () => {
    api.crmGet.mockImplementation((path) => Promise.resolve(path.endsWith('/closure/preview')
      ? { highlights: { clients: 120, agent_contacts: 80, sms_messages: 900, inbound_voice_calls: 12,
        ai_chat_messages: 40, users: 4 }, stored_objects: 33 }
      : ACTIVE));
    render(<AccountDataPanel />);
    fireEvent.click(await screen.findByRole('button', { name: /Close this brokerage/ }));
    expect((await screen.findByTestId('closure-preview')).textContent).toMatch('120 clients');
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

  it('says what Stripe confirmed after closing, never an unconditional "will not renew" (22.1)', async () => {
    api.crmGet.mockResolvedValue(ACTIVE);
    api.crmPost.mockResolvedValue({ state: 'closing', billing: 'pending' });
    render(<AccountDataPanel />);
    fireEvent.click(await screen.findByRole('button', { name: /Close this brokerage/ }));
    expect(screen.queryByText('Your subscription will not renew.')).toBeNull();
    expect(screen.getByText(/shows here once Stripe confirms it/)).toBeTruthy();
    typePassword();
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'retiring' } });
    fireEvent.change(screen.getByLabelText(/Type the brokerage name/), { target: { value: 'Lockwood Realty' } });
    fireEvent.click(screen.getByRole('button', { name: 'Close brokerage' }));
    const notice = await screen.findByText(/Closure scheduled/);
    expect(notice.textContent).toMatch('not confirmed by Stripe yet');
    expect(notice.textContent).not.toMatch('will not renew');
  });

  it('says confirmed only when the server says Stripe confirmed it', async () => {
    api.crmPost.mockResolvedValue({ state: 'closing', billing: 'confirmed' });
    render(<AccountDataPanel />);
    fireEvent.click(await screen.findByRole('button', { name: /Close this brokerage/ }));
    typePassword();
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'retiring' } });
    fireEvent.change(screen.getByLabelText(/Type the brokerage name/), { target: { value: 'Lockwood Realty' } });
    fireEvent.click(screen.getByRole('button', { name: 'Close brokerage' }));
    expect((await screen.findByText(/Closure scheduled/)).textContent)
      .toMatch('Stripe has confirmed your subscription will not renew.');
  });

  it('while closing, shows whether stopping the renewal is confirmed, pending or not confirmable', async () => {
    const closing = (state) => ({ ...ACTIVE, lifecycle_state: 'closing', erase_after: '2026-12-01T12:00:00Z',
      billing_renewal: { desired: 'stop_renewal', state } });
    api.crmGet.mockResolvedValue(closing('pending'));
    render(<AccountDataPanel />);
    expect((await screen.findByTestId('renewal-status')).textContent).toMatch('not confirmed by Stripe yet');
    cleanup();
    api.crmGet.mockResolvedValue(closing('divergent'));
    render(<AccountDataPanel />);
    expect((await screen.findByTestId('renewal-status')).textContent).toMatch('support has been alerted');
    cleanup();
    api.crmGet.mockResolvedValue(closing('confirmed'));
    render(<AccountDataPanel />);
    expect((await screen.findByTestId('renewal-status')).textContent).toMatch('will not renew');
  });

  it('still shows a divergence whose direction was not recorded', async () => {
    api.crmGet.mockResolvedValue({ ...ACTIVE, lifecycle_state: 'closing', erase_after: '2026-12-01T12:00:00Z',
      billing_renewal: { desired: null, state: 'divergent' } });
    render(<AccountDataPanel />);
    expect((await screen.findByTestId('renewal-status')).textContent).toMatch('support has been alerted');
  });

  it('after keeping the brokerage, says when restarting the renewal is still pending', async () => {
    api.crmGet.mockResolvedValue({ ...ACTIVE, lifecycle_state: 'closing', erase_after: '2026-12-01T12:00:00Z' });
    api.crmPost.mockResolvedValue({ state: 'active', billing: 'pending' });
    render(<AccountDataPanel />);
    await screen.findByText(/This brokerage is closing/);
    typePassword();
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'changed my mind' } });
    api.crmGet.mockResolvedValue({ ...ACTIVE, billing_renewal: { desired: 'renew', state: 'pending' } });
    fireEvent.click(screen.getByRole('button', { name: 'Keep the brokerage' }));
    const notice = await screen.findByText(/Closure withdrawn/);
    expect(notice.textContent).toMatch('Restarting your subscription\u2019s renewal is not confirmed');
    expect((await screen.findByTestId('renewal-status')).textContent).toMatch('not confirmed by Stripe yet');
  });

  // L17-58: a withdrawal leaves off the members the owner suspended or
  // offboarded after closing, with the numbers they hold, and says so.
  const keepBrokerage = async (answer) => {
    api.crmGet.mockResolvedValue({ ...ACTIVE, lifecycle_state: 'closing', erase_after: '2026-12-01T12:00:00Z' });
    api.crmPost.mockResolvedValue(answer);
    render(<AccountDataPanel />);
    await screen.findByText(/This brokerage is closing/);
    typePassword();
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'changed my mind' } });
    api.crmGet.mockResolvedValue(ACTIVE);
    fireEvent.click(screen.getByRole('button', { name: 'Keep the brokerage' }));
    return screen.findByText(/Closure withdrawn/);
  };

  it('after keeping the brokerage, names who was left off, why, and how to bring them back', async () => {
    const notice = await keepBrokerage({
      state: 'active', billing: 'confirmed', restored_users: 3, restored_routes: 6,
      skipped_users: [
        { id: 'u-2', reason: 'suspended', name: 'Kim Keller' },
        { id: 'u-3', reason: 'offboarded', name: 'nora@lockwood.test' },
      ],
      skipped_routes: ['tr-2', 'mr-2', 'tr-3', 'mr-3'],
    });
    expect(notice.textContent).toMatch('Your other agents can sign in again');
    const left = await screen.findByTestId('withdraw-left-off');
    expect(left.textContent).toMatch('2 members you suspended or offboarded after closing stay signed out');
    expect(left.textContent).toMatch('the numbers they hold stay off');
    const items = [...left.querySelectorAll('li')].map((li) => li.textContent);
    expect(items).toEqual(['Kim Keller \u2014 suspended after you closed',
      'nora@lockwood.test \u2014 offboarded after you closed']);
    expect(left.textContent).toMatch('4 call and text routes');
    expect(left.textContent).toMatch('reinstate them under Team');
    // L17-59: reinstating brings their numbers back too; no support detour
    expect(left.textContent).toMatch('switches back on the numbers left off here that they still hold');
    // review of PR #190: offboarded members' numbers are not promised
    expect(left.textContent).toMatch('parked or handed to a successor when someone was offboarded stays off');
    expect(left.textContent).not.toMatch('not their numbers');
    expect(left.textContent).not.toMatch(/support/);
    // the next action starts clean
    typePassword();
    fireEvent.click(screen.getByRole('button', { name: 'Prepare export' }));
    await waitFor(() => expect(screen.queryByTestId('withdraw-left-off')).toBeNull());
  });

  it('counts members the answer does not name, and never invents a name', async () => {
    await keepBrokerage({
      state: 'active', billing: 'confirmed',
      skipped_users: [{ id: 'u-2', reason: 'suspended' }, { id: 'u-3', reason: 'suspended', name: '  ' }],
      skipped_routes: ['tr-2'],
    });
    const left = await screen.findByTestId('withdraw-left-off');
    expect(left.textContent).toMatch('2 members you suspended or offboarded after closing stay signed out');
    expect(left.querySelectorAll('li')).toHaveLength(0);
    expect(left.textContent).not.toMatch('u-2');
    expect(left.textContent).toMatch('1 call or text route');
    cleanup();
    await keepBrokerage({
      state: 'active', billing: 'confirmed',
      skipped_users: [{ id: 'u-2', reason: 'suspended', name: 'Kim Keller' }, { id: 'u-3', reason: 'offboarded' }],
      skipped_routes: [],
    });
    const items = [...(await screen.findByTestId('withdraw-left-off')).querySelectorAll('li')].map((li) => li.textContent);
    expect(items).toEqual(['Kim Keller \u2014 suspended after you closed', '1 other member']);
    expect(screen.getByTestId('withdraw-left-off').textContent).not.toMatch('routes');
  });

  it('says nothing about anyone left off when everyone came back', async () => {
    const notice = await keepBrokerage({ state: 'active', billing: 'confirmed', restored_users: 2,
      skipped_users: [], skipped_routes: [] });
    expect(notice.textContent).toMatch('Closure withdrawn. Your agents can sign in again');
    expect(screen.queryByTestId('withdraw-left-off')).toBeNull();
    cleanup();
    await keepBrokerage({ state: 'active', billing: 'confirmed' });            // an older server
    expect(screen.queryByTestId('withdraw-left-off')).toBeNull();
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
