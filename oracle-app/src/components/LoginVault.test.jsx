// @vitest-environment jsdom
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => {
  class ApiError extends Error {
    constructor(detail, status) {
      super(typeof detail === 'string' ? detail : detail?.message);
      this.status = status;
      this.code = typeof detail === 'object' ? detail?.code || '' : '';
    }
  }
  return { apiPost: vi.fn(), ApiError };
});
vi.mock('../lib/apiClient', () => api);
vi.mock('../context/useNetwork', () => ({ useNetwork: () => ({ formatError: (e) => e.message }) }));

const { LoginVault } = await import('./LoginVault');

function signIn() {
  fireEvent.change(screen.getByLabelText('Email or Agent ID'), { target: { value: 'ops@neoh.test' } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'correct-passphrase' } });
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
}

describe('LoginVault operator second factor', () => {
  beforeEach(() => api.apiPost.mockReset());
  afterEach(cleanup);

  it('asks for the emailed code and sends it with the next attempt', async () => {
    api.apiPost
      .mockRejectedValueOnce(new api.ApiError({ code: 'OTP_REQUIRED', message: 'We emailed a 6-digit sign-in code to y***@gmail.com.' }, 401))
      .mockResolvedValueOnce({ agent_id: 'ops@neoh.test', role: 'platform_admin' });
    render(<LoginVault onAuthenticated={() => {}} />);
    signIn();
    const codeField = await screen.findByLabelText('Sign-in code');
    expect(screen.getByText(/emailed a 6-digit sign-in code/)).toBeTruthy();
    expect(api.apiPost.mock.calls[0][1]).toEqual({ agent_id: 'ops@neoh.test', passphrase: 'correct-passphrase' });

    fireEvent.change(codeField, { target: { value: '12 34-56' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await waitFor(() => expect(api.apiPost).toHaveBeenCalledTimes(2));
    expect(api.apiPost.mock.calls[1][1]).toEqual({ agent_id: 'ops@neoh.test', passphrase: 'correct-passphrase', otp: '123456' });
  });

  it('keeps the code field and clears it after a rejected code', async () => {
    api.apiPost
      .mockRejectedValueOnce(new api.ApiError({ code: 'OTP_REQUIRED', message: 'code sent' }, 401))
      .mockRejectedValueOnce(new api.ApiError({ code: 'OTP_INVALID', message: 'That code is not valid or has expired. Try again.' }, 401));
    render(<LoginVault onAuthenticated={() => {}} />);
    signIn();
    fireEvent.change(await screen.findByLabelText('Sign-in code'), { target: { value: '000000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText(/not valid or has expired/)).toBeTruthy();
    expect(screen.getByLabelText('Sign-in code').value).toBe('');
    expect(screen.getByRole('button', { name: 'Send a new code' })).toBeTruthy();
  });
});
