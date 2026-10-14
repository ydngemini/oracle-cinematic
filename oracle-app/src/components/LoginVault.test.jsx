// @vitest-environment jsdom
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => {
  class ApiError extends Error {
    constructor(detail, status) {
      super(typeof detail === 'string' ? detail : detail?.message);
      this.status = status;
      this.detail = detail;
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

  it('says the password did not match, not that a session ended, when sign-in is refused', async () => {
    api.apiPost.mockRejectedValueOnce(new api.ApiError('Invalid agent credentials.', 401));
    render(<LoginVault onAuthenticated={() => {}} />);
    signIn();
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/email and password don.t match/);
    expect(alert.textContent).not.toMatch(/session ended/i);
  });

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

describe('LoginVault two-step sign-in for brokerage accounts (L17-19)', () => {
  beforeEach(() => api.apiPost.mockReset());
  afterEach(cleanup);

  const required = () => new api.ApiError({
    code: 'OTP_REQUIRED', factor: 'totp', recovery: true,
    message: 'Enter the 6-digit code from your authenticator app, or one of your recovery codes.',
  }, 401);

  it('asks for the authenticator code, offers a recovery code, and never offers to email one', async () => {
    api.apiPost.mockRejectedValueOnce(required()).mockResolvedValueOnce({ agent_id: 'jo@x.test', role: 'broker_owner' });
    render(<LoginVault onAuthenticated={() => {}} />);
    signIn();
    const codeField = await screen.findByLabelText('Authenticator code');
    expect(screen.queryByRole('button', { name: 'Send a new code' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /use a recovery code/i }));
    const recovery = screen.getByLabelText('Recovery code');
    expect(codeField.value).toBe('');
    expect(screen.queryByLabelText('Authenticator code')).toBeNull();
    fireEvent.change(recovery, { target: { value: 'abcd-efgh-jkmn-pqrs' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await waitFor(() => expect(api.apiPost).toHaveBeenCalledTimes(2));
    expect(api.apiPost.mock.calls[1][1]).toEqual({
      agent_id: 'ops@neoh.test', passphrase: 'correct-passphrase', otp: 'ABCD-EFGH-JKMN-PQRS',
    });
  });

  it('keeps the sign-in button off until a whole code is typed', async () => {
    api.apiPost.mockRejectedValueOnce(required());
    render(<LoginVault onAuthenticated={() => {}} />);
    signIn();
    fireEvent.change(await screen.findByLabelText('Authenticator code'), { target: { value: '12345' } });
    expect(screen.getByRole('button', { name: 'Sign in' }).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Authenticator code'), { target: { value: '123456' } });
    expect(screen.getByRole('button', { name: 'Sign in' }).disabled).toBe(false);
  });

  it('shows the lockout sentence the server sends', async () => {
    api.apiPost
      .mockRejectedValueOnce(required())
      .mockRejectedValueOnce(new api.ApiError({ code: 'MFA_LOCKED', message: 'Too many wrong codes. Wait 15 minutes.' }, 429));
    render(<LoginVault onAuthenticated={() => {}} />);
    signIn();
    fireEvent.change(await screen.findByLabelText('Authenticator code'), { target: { value: '000000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect((await screen.findByRole('alert')).textContent).toMatch(/Too many wrong codes/);
  });

  it('asks for the code during a password reset too, and sends it with the new password', async () => {
    window.history.replaceState({}, '', '/?reset=reset-token-123');
    api.apiPost.mockRejectedValueOnce(required()).mockResolvedValueOnce({ agent_id: 'jo@x.test', role: 'broker_owner' });
    render(<LoginVault onAuthenticated={() => {}} />);
    fireEvent.change(screen.getByLabelText('New password (10+ chars)'), { target: { value: 'a-new-password-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Set password & sign in' }));
    fireEvent.change(await screen.findByLabelText('Authenticator code'), { target: { value: '654321' } });
    fireEvent.click(screen.getByRole('button', { name: 'Set password & sign in' }));
    await waitFor(() => expect(api.apiPost).toHaveBeenCalledTimes(2));
    expect(api.apiPost.mock.calls[1]).toEqual(['/auth/reset',
      { token: 'reset-token-123', new_password: 'a-new-password-1', otp: '654321' }, { retries: 0 }]);
    window.history.replaceState({}, '', '/');
  });
});
