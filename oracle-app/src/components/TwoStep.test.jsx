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
  return { apiGet: vi.fn(), apiPost: vi.fn(), apiPut: vi.fn(), ApiError };
});
vi.mock('../lib/apiClient', () => api);

const { TwoStepSetup, QrCode } = await import('./TwoStepSetup');
const { TwoStepGate, MFA_ENROLMENT_EVENT } = await import('./TwoStepGate');
const { TwoStepSettings } = await import('./TwoStepSettings');

const SETUP = {
  secret: 'JBSW Y3DP EHPK 3PXP',
  otpauth_uri: 'otpauth://totp/Neoh:jo%40x.test?secret=JBSWY3DPEHPK3PXP&issuer=Neoh&digits=6&period=30',
  qr: ['111', '101', '111'],
  account: 'jo@x.test',
};
const CODES = Array.from({ length: 10 }, (_, i) => `AAAA-BBBB-CCCC-000${i}`);

beforeEach(() => {
  api.apiGet.mockReset();
  api.apiPost.mockReset();
  api.apiPut.mockReset();
});
afterEach(cleanup);

describe('QrCode', () => {
  it('draws one square per dark module inside a 4-module quiet zone', () => {
    const { container } = render(<QrCode rows={['10', '01']} label="qr" />);
    const svg = container.querySelector('svg');
    expect(svg.getAttribute('viewBox')).toBe('0 0 10 10');
    expect(container.querySelector('path').getAttribute('d')).toBe('M4 4h1v1h-1zM5 5h1v1h-1z');
  });
});

describe('TwoStepSetup', () => {
  async function scan() {
    api.apiPost.mockResolvedValueOnce(SETUP);
    render(<TwoStepSetup onDone={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Set up authenticator app' }));
    return screen.findByLabelText('Code from the app');
  }

  it('shows the QR code, the manual key and a link for this phone', async () => {
    await scan();
    expect(api.apiPost).toHaveBeenCalledWith('/auth/mfa/totp/setup', {}, { retries: 0 });
    expect(screen.getByRole('img', { name: /QR code/ })).toBeTruthy();
    expect(screen.getByTestId('totp-manual-key').textContent).toBe('JBSW Y3DP EHPK 3PXP');
    expect(screen.getByRole('link', { name: /open in your authenticator app/i }).getAttribute('href')).toBe(SETUP.otpauth_uri);
  });

  it('asks for the password only when the server wants fresh proof', async () => {
    api.apiPost
      .mockRejectedValueOnce(new api.ApiError('Confirm your password to continue.', 403))
      .mockResolvedValueOnce(SETUP);
    render(<TwoStepSetup onDone={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Set up authenticator app' }));
    const password = await screen.findByLabelText('Password');
    fireEvent.change(password, { target: { value: 'my password' } });
    fireEvent.click(screen.getByRole('button', { name: 'Set up authenticator app' }));
    await screen.findByLabelText('Code from the app');
    expect(api.apiPost.mock.calls[1]).toEqual(['/auth/mfa/totp/setup', { password: 'my password' }, { retries: 0 }]);
  });

  it('confirms with a 6-digit code, then shows the recovery codes once and waits for "I saved them"', async () => {
    const onDone = vi.fn();
    api.apiPost.mockResolvedValueOnce(SETUP).mockResolvedValueOnce({ enabled: true, recovery_codes: CODES });
    render(<TwoStepSetup onDone={onDone} />);
    fireEvent.click(screen.getByRole('button', { name: 'Set up authenticator app' }));
    const code = await screen.findByLabelText('Code from the app');
    fireEvent.change(code, { target: { value: '12 34 56 7' } });
    expect(code.value).toBe('123456');
    fireEvent.click(screen.getByRole('button', { name: 'Turn on two-step sign-in' }));
    const list = await screen.findByRole('list', { name: 'Recovery codes' });
    expect(list.querySelectorAll('li')).toHaveLength(10);
    expect(api.apiPost.mock.calls[1]).toEqual(['/auth/mfa/totp/confirm', { code: '123456' }, { retries: 0 }]);
    const cont = screen.getByRole('button', { name: 'Continue' });
    expect(cont.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText(/I saved my recovery codes/));
    fireEvent.click(cont);
    expect(onDone).toHaveBeenCalledWith({ enabled: true, recovery_codes: CODES });
  });

  it('a wrong code keeps the person on the scan step with the server\'s sentence', async () => {
    api.apiPost.mockResolvedValueOnce(SETUP)
      .mockRejectedValueOnce(new api.ApiError({ code: 'OTP_INVALID', message: 'That code does not match.' }, 400));
    render(<TwoStepSetup onDone={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Set up authenticator app' }));
    fireEvent.change(await screen.findByLabelText('Code from the app'), { target: { value: '000000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Turn on two-step sign-in' }));
    expect((await screen.findByRole('alert')).textContent).toMatch(/does not match/);
    expect(screen.getByLabelText('Code from the app').value).toBe('');
  });
});

describe('TwoStepGate', () => {
  it('lets a session that owes nothing straight through', async () => {
    const onReady = vi.fn();
    api.apiGet.mockResolvedValueOnce({ available: true, enabled: true, enrolment_required: false });
    render(<TwoStepGate onReady={onReady} onSignOut={vi.fn()} />);
    await waitFor(() => expect(onReady).toHaveBeenCalled());
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('holds a broker owner on setup until it is done, with a way out', async () => {
    const onReady = vi.fn();
    const onSignOut = vi.fn();
    api.apiGet.mockResolvedValueOnce({ available: true, enabled: false, enrolment_required: true, required_reason: 'role' });
    render(<TwoStepGate onReady={onReady} onSignOut={onSignOut} />);
    expect(await screen.findByRole('heading', { name: 'Set up two-step sign-in' })).toBeTruthy();
    expect(screen.getByText(/Brokerage owners must use two-step sign-in/)).toBeTruthy();
    expect(await screen.findByRole('button', { name: 'Set up authenticator app' })).toBeTruthy();
    expect(onReady).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    expect(onSignOut).toHaveBeenCalled();
  });

  it('says when it is the brokerage that requires it', async () => {
    api.apiGet.mockResolvedValueOnce({ available: true, enabled: false, enrolment_required: true, required_reason: 'brokerage' });
    render(<TwoStepGate onReady={vi.fn()} onSignOut={vi.fn()} />);
    expect(await screen.findByText(/Your brokerage requires two-step sign-in/)).toBeTruthy();
  });

  it('reopens when the API answers mfa_enrolment_required mid-session', async () => {
    api.apiGet.mockResolvedValueOnce({ available: true, enabled: true, enrolment_required: false });
    const onReady = vi.fn();
    render(<TwoStepGate onReady={onReady} onSignOut={vi.fn()} />);
    await waitFor(() => expect(onReady).toHaveBeenCalled());
    window.dispatchEvent(new CustomEvent(MFA_ENROLMENT_EVENT));
    expect(await screen.findByRole('heading', { name: 'Set up two-step sign-in' })).toBeTruthy();
  });
});

describe('TwoStepSettings', () => {
  it('offers setup when off, and the brokerage switch to an owner', async () => {
    api.apiGet
      .mockResolvedValueOnce({ available: true, enabled: false, required: false, recovery_codes_remaining: 0 })
      .mockResolvedValueOnce({ require_mfa: false, owners_always_required: true });
    api.apiPut.mockResolvedValueOnce({ require_mfa: true, owners_always_required: true });
    render(<TwoStepSettings isOwner />);
    expect(await screen.findByRole('button', { name: 'Turn on two-step sign-in' })).toBeTruthy();
    const toggle = screen.getByLabelText(/Require two-step sign-in for everyone/);
    fireEvent.click(toggle);
    await waitFor(() => expect(api.apiPut).toHaveBeenCalledWith('/api/brokerage/security', { require_mfa: true }, { retries: 0 }));
    expect(await screen.findByText(/at their next sign-in/)).toBeTruthy();
  });

  it('an agent sees no brokerage switch', async () => {
    api.apiGet.mockResolvedValueOnce({ available: true, enabled: false, required: false });
    render(<TwoStepSettings isOwner={false} />);
    await screen.findByRole('button', { name: 'Turn on two-step sign-in' });
    expect(screen.queryByLabelText(/Require two-step sign-in for everyone/)).toBeNull();
    expect(api.apiGet).toHaveBeenCalledTimes(1);
  });

  it('a required account "moves to a new phone" and goes straight back to setup', async () => {
    api.apiGet
      .mockResolvedValue({ available: true, enabled: true, required: true, required_reason: 'role', recovery_codes_remaining: 7 });
    api.apiPost.mockResolvedValueOnce({ enabled: false, enrolment_required: true });
    const reopened = vi.fn();
    window.addEventListener(MFA_ENROLMENT_EVENT, reopened);
    render(<TwoStepSettings isOwner={false} />);
    expect(await screen.findByText(/7 recovery codes left/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Move to a new phone' }));
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'pw' } });
    fireEvent.change(screen.getByLabelText(/Code from your app/), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'Remove and set up again' }));
    await waitFor(() => expect(reopened).toHaveBeenCalled());
    expect(api.apiPost).toHaveBeenCalledWith('/auth/mfa/disable', { password: 'pw', code: '123456' }, { retries: 0 });
    window.removeEventListener(MFA_ENROLMENT_EVENT, reopened);
  });

  it('shows the server\'s sentence for a wrong code, not a generic "sign in again"', async () => {
    api.apiGet.mockResolvedValue({ available: true, enabled: true, required: false, recovery_codes_remaining: 10 });
    api.apiPost.mockRejectedValueOnce(new api.ApiError({ code: 'OTP_INVALID', message: 'That code is not valid or has already been used.' }, 401));
    render(<TwoStepSettings isOwner={false} />);
    fireEvent.click(await screen.findByRole('button', { name: 'New recovery codes' }));
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'pw' } });
    fireEvent.change(screen.getByLabelText(/Code from your app/), { target: { value: '000000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Get new codes' }));
    expect((await screen.findByRole('alert')).textContent).toMatch(/already been used/);
  });

  it('renders nothing for an account whose sign-in is managed by configuration', async () => {
    api.apiGet.mockResolvedValueOnce({ available: false });
    const { container } = render(<TwoStepSettings isOwner={false} />);
    await waitFor(() => expect(api.apiGet).toHaveBeenCalled());
    await waitFor(() => expect(container.textContent).toBe(''));
  });
});
