// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn(), crmPost: vi.fn() }));
vi.mock('../../state/useCrmApi', () => api);

const { default: AlertDeliveryCard } = await import('./AlertDeliveryCard');
const { alertDeliverySummary } = await import('./operatorModel');

const base = {
  recipient_configured: true, proven: false, confirmed_at: null, stale: false, max_age_days: 14,
  awaiting_code: false, last_attempt: null,
};

function apiError(message, status, detail) {
  return Object.assign(new Error(message), { status, detail });
}

describe('AlertDeliveryCard (audit 30.3)', () => {
  beforeEach(() => { api.crmGet.mockReset(); api.crmPost.mockReset(); });
  afterEach(cleanup);

  it('says alert delivery is not proven until a test code is confirmed', () => {
    expect(alertDeliverySummary({ ...base, recipient_configured: false })).toMatch(/reach only the logs/);
    expect(alertDeliverySummary({ ...base, last_attempt: { outcome: 'SmtpConfigurationError', confirmed: false } }))
      .toMatch(/did not accept the last test \(SmtpConfigurationError\)/);
    expect(alertDeliverySummary({ ...base, stale: true })).toMatch(/older than 14 days/);
    expect(alertDeliverySummary({ ...base, proven: true, confirmed_at: new Date().toISOString() }))
      .toMatch(/accepts it for 14 days/);
  });

  it('sends a test alert, then confirms the code from the email', async () => {
    api.crmGet.mockResolvedValueOnce(base)
      .mockResolvedValueOnce({ ...base, awaiting_code: true, last_attempt: { outcome: 'sent', confirmed: false } })
      .mockResolvedValue({ ...base, proven: true, confirmed_at: new Date().toISOString() });
    api.crmPost.mockResolvedValueOnce({ outcome: 'sent', error: null }).mockResolvedValueOnce({ result: 'confirmed' });
    render(<AlertDeliveryCard />);
    fireEvent.click(await screen.findByRole('button', { name: 'Send test alert' }));
    expect((await screen.findByRole('status')).textContent).toContain('Check the alert inbox for the code');
    expect(api.crmPost).toHaveBeenCalledWith('/api/admin/alerts/test', {});
    fireEvent.change(await screen.findByLabelText('Code from the email'), { target: { value: ' abcd-2345 ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(api.crmPost).toHaveBeenLastCalledWith('/api/admin/alerts/test/confirm', { code: 'abcd-2345' }));
    expect(await screen.findByText('Proven')).toBeTruthy();
    expect(screen.getByRole('status').textContent).toContain('alerts reach the current recipient');
  });

  it('shows a wrong code, a refused send and a missing recipient plainly', async () => {
    api.crmGet.mockResolvedValue({ ...base, awaiting_code: true });
    api.crmPost.mockRejectedValueOnce(apiError('That code does not match the latest test alert.', 400,
      { code: 'wrong_code', attempts_left: 1 }));
    render(<AlertDeliveryCard />);
    fireEvent.change(await screen.findByLabelText('Code from the email'), { target: { value: 'ZZZZ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect((await screen.findByRole('alert')).textContent).toContain('does not match the latest test alert. 1 try left.');

    api.crmPost.mockRejectedValueOnce(apiError('slow down', 429));
    fireEvent.click(screen.getByRole('button', { name: 'Send test alert' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('less than a minute ago'));

    cleanup();
    api.crmGet.mockResolvedValue({ ...base, recipient_configured: false });
    render(<AlertDeliveryCard />);
    expect((await screen.findByRole('button', { name: 'Send test alert' })).disabled).toBe(true);
    expect(screen.queryByLabelText('Code from the email')).toBeNull();
  });

  it('says why when the status cannot be loaded', async () => {
    api.crmGet.mockRejectedValue(apiError('nope', 403));
    render(<AlertDeliveryCard />);
    expect((await screen.findByRole('alert')).textContent).toContain('platform admin only');
  });
});
